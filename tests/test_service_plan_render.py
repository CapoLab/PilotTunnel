import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pilottunnel import cli
from pilottunnel.binaries import binary_spec, current_platform_id
from pilottunnel.config import AppConfig, BinaryResolutionSettings, Profile, ProfilePorts, save_config
from pilottunnel.state import AppState, save_state


class ServiceRenderPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.config_path = self.base / "config.json"
        self.state_path = self.base / "state.json"
        self.registry_path = self.base / "registry.json"
        self.audit_path = self.base / "audit.log"
        self.lock_dir = self.base / "locks"
        self.work_dir = self.base / "work"
        self.staging_root = self.base / "staging"
        self.runtime_dir = self.base / "runtime"
        self.service_dir = self.base / "service-staging"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(
                [
                    "--config",
                    str(self.config_path),
                    "--state",
                    str(self.state_path),
                    "--registry",
                    str(self.registry_path),
                    "--audit-log",
                    str(self.audit_path),
                    "--lock-dir",
                    str(self.lock_dir),
                    "--work-dir",
                    str(self.work_dir),
                    "--staging-root",
                    str(self.staging_root),
                    *args,
                ]
            )
        return code, output.getvalue()

    def _profile(self, name: str, *, adapter: str, runtime_role: str) -> Profile:
        return Profile(
            name=name,
            main_port=40101,
            target_host="127.0.0.1",
            target_port=40102,
            role="controller",
            active_adapter=adapter,
            active_transport="tcp",
            runtime_role=runtime_role,
            ports=ProfilePorts(main_port=40101, control_port=40103, service_port=40104, check_port=40105),
        )

    def _managed_install_dir(self, *adapters: str) -> Path:
        install_dir = self.base / "managed-install"
        platform_id = current_platform_id()
        for adapter in adapters:
            filename = binary_spec(adapter).binary_name
            if platform_id.startswith("windows") and not filename.endswith(".exe"):
                filename = f"{filename}.exe"
            binary_path = install_dir / adapter / platform_id / filename
            binary_path.parent.mkdir(parents=True, exist_ok=True)
            binary_path.write_bytes(f"{adapter}-binary".encode("utf-8"))
        return install_dir

    def _write_config(self, profiles: list[Profile], *, managed_install_dir: Path) -> None:
        config = AppConfig(
            binary_resolution=BinaryResolutionSettings(
                managed_install_dir=str(managed_install_dir),
                allow_system_path=False,
                prefer_managed_install=True,
            ),
            profiles=profiles,
        )
        save_config(config, self.config_path)
        save_state(AppState(), self.state_path)
        self.registry_path.write_text("{}", encoding="utf-8")

    def test_service_render_builds_from_runtime_plan(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        with patch("pilottunnel.service_plan.build_runtime_plan") as mock_runtime_plan:
            mock_runtime_plan.return_value = {
                "ok": True,
                "tunnels": [
                    {
                        "tunnel_id": "smoke-l4-001",
                        "adapter": "rathole",
                        "transport": "tcp",
                        "role": "active",
                        "command_argv": ["rathole", "/tmp/config"],
                        "config_file_path": "/tmp/config",
                        "warnings": [],
                        "errors": [],
                    }
                ],
                "warnings": [],
                "errors": [],
            }
            code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(mock_runtime_plan.called)

    def test_active_tunnel_gets_a_staged_service_unit(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["active_services"], ["smoke-l4-001"])
        service = payload["services"][0]
        self.assertTrue(service["service_unit_rendered"])
        self.assertTrue(Path(service["staged_unit_file_path"]).exists())

    def test_hot_standby_tunnels_get_staged_service_units(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        profiles = [
            self._profile("smoke-l4-001", adapter="rathole", runtime_role="active"),
            self._profile("demo-l4-002", adapter="frp", runtime_role="hot_standby"),
        ]
        self._write_config(profiles, managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["hot_standby_services"], ["demo-l4-002"])
        standby = next(item for item in payload["services"] if item["runtime_role"] == "hot_standby")
        self.assertTrue(standby["service_unit_rendered"])

    def test_config_only_tunnels_do_not_get_startable_units(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        profiles = [
            self._profile("smoke-l4-001", adapter="rathole", runtime_role="active"),
            self._profile("demo-l4-002", adapter="frp", runtime_role="config_only"),
        ]
        self._write_config(profiles, managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        config_only = next(item for item in payload["services"] if item["runtime_role"] == "config_only")
        self.assertFalse(config_only["service_unit_rendered"])
        self.assertEqual(config_only["exec_start_argv_summary"], [])

    def test_service_render_inherits_hot_standby_limit_from_runtime_rules(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp", "gost")
        profiles = [
            self._profile("smoke-l4-001", adapter="rathole", runtime_role="active"),
            self._profile("demo-l4-002", adapter="frp", runtime_role="hot_standby"),
            self._profile("demo-l4-003", adapter="gost", runtime_role="hot_standby"),
            self._profile("demo-l4-004", adapter="rathole", runtime_role="hot_standby"),
        ]
        self._write_config(profiles, managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 1)
        self.assertIn("At most two tunnels can be marked hot_standby", output)

    def test_deterministic_safe_service_names(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        service = json.loads(output)["services"][0]
        self.assertEqual(service["service_name"], "pilottunnel-smoke-l4-001-rathole-tcp.service")

    def test_unsafe_tunnel_names_are_rejected(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("demo tunnel", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 1)
        self.assertIn("Unsafe tunnel_id", output)

    def test_service_staging_dir_path_traversal_is_blocked(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.base / "safe" / ".." / "escape"))
        self.assertEqual(code, 1)
        self.assertIn("Path traversal blocked", output)

    def test_service_staging_dir_symlink_escape_is_blocked(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        outside = self.base / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        link = self.base / "service-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("Symlink creation is not available on this host")
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(link))
        self.assertEqual(code, 1)
        self.assertIn("Symlink escape blocked", output)

    def test_service_render_refuses_real_systemd_path(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", "/etc/systemd/system")
        self.assertEqual(code, 1)
        self.assertIn("/etc/systemd/system", output)

    @patch("subprocess.run", side_effect=AssertionError("subprocess.run must not be called"))
    def test_service_render_does_not_execute_systemctl_or_processes(self, _mock_run) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)

    @patch("pilottunnel.adapters.rathole.RatholeAdapter.render_runtime_plan")
    def test_service_render_redacts_secrets_in_output(self, mock_render_runtime_plan) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        config_path = self.runtime_dir / "configs" / "smoke-l4-001" / "rathole" / "tcp" / "controller" / "rathole-controller.toml"
        mock_render_runtime_plan.return_value = {
            "config_path": str(config_path),
            "content": "token = super-secret\npassword: hidden\nmode = tcp",
            "argv": ["rathole", str(config_path)],
            "environment": {"API_KEY": "secret-key"},
            "healthcheck_target_summary": {"kind": "tcp", "host": "127.0.0.1", "port": 40102},
        }
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        self.assertNotIn("super-secret", output)
        self.assertNotIn("hidden", output)
        self.assertNotIn("secret-key", output)

    def test_staged_unit_content_contains_expected_execstart_for_rathole(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        service = json.loads(output)["services"][0]
        content = Path(service["staged_unit_file_path"]).read_text(encoding="utf-8")
        self.assertIn("ExecStart=", content)
        self.assertIn("rathole", content)
        self.assertIn("rathole-controller.toml", content)

    def test_staged_unit_content_contains_expected_execstart_for_frp(self) -> None:
        install_dir = self._managed_install_dir("frp")
        self._write_config([self._profile("smoke-l4-001", adapter="frp", runtime_role="active")], managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        service = json.loads(output)["services"][0]
        content = Path(service["staged_unit_file_path"]).read_text(encoding="utf-8")
        self.assertIn("ExecStart=", content)
        self.assertIn("frpc", content)
        self.assertIn("frp-controller.ini", content)

    def test_staged_unit_content_contains_expected_execstart_for_gost(self) -> None:
        install_dir = self._managed_install_dir("gost")
        self._write_config([self._profile("smoke-l4-001", adapter="gost", runtime_role="active")], managed_install_dir=install_dir)
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        service = json.loads(output)["services"][0]
        content = Path(service["staged_unit_file_path"]).read_text(encoding="utf-8")
        self.assertIn("ExecStart=", content)
        self.assertIn("gost", content)
        self.assertIn("gost-controller.toml", content)
