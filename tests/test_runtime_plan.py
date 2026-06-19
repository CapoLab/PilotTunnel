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
from pilottunnel.state import AppState, RuntimeRecord, save_state
from testsupport import allocate_tcp_ports


class RuntimePlanTests(unittest.TestCase):
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
        ports, listeners = allocate_tcp_ports(5)
        self.main_port, self.target_port, self.control_port, self.service_port, self.check_port = ports
        for listener in listeners:
            listener.close()

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

    def _profile(self, name: str, *, adapter: str = "rathole", transport: str = "tcp", runtime_role: str = "") -> Profile:
        return Profile(
            name=name,
            main_port=self.main_port,
            target_host="127.0.0.1",
            target_port=self.target_port,
            role="controller",
            active_adapter=adapter,
            active_transport=transport,
            runtime_role=runtime_role,
            ports=ProfilePorts(
                main_port=self.main_port,
                control_port=self.control_port,
                service_port=self.service_port,
                check_port=self.check_port,
            ),
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

    def _write_runtime_config(self, profiles: list[Profile], *, managed_install_dir: Path | None = None, state: AppState | None = None) -> None:
        config = AppConfig(
            binary_resolution=BinaryResolutionSettings(
                managed_install_dir=str(managed_install_dir) if managed_install_dir else "",
                allow_system_path=False,
                prefer_managed_install=True,
            ),
            profiles=profiles,
        )
        save_config(config, self.config_path)
        save_state(state or AppState(), self.state_path)
        self.registry_path.write_text("{}", encoding="utf-8")

    def test_runtime_plan_is_dry_run_only(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_runtime_config([self._profile("smoke-l4-001")], managed_install_dir=install_dir)
        runtime_dir = self.base / "runtime"
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(runtime_dir))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["service_started"])
        self.assertFalse(payload["real_systemd_touched"])
        self.assertTrue((runtime_dir / "configs").exists())
        self.assertFalse((runtime_dir / "systemd").exists())

    @patch("pilottunnel.runtime_plan.resolve_binary_reference")
    def test_runtime_plan_uses_binary_resolver(self, mock_resolve) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_runtime_config([self._profile("smoke-l4-001")], managed_install_dir=install_dir)
        mock_resolve.return_value = {
            "ok": True,
            "resolved": True,
            "adapter": "rathole",
            "platform": current_platform_id(),
            "source": "managed_install",
            "path": str(install_dir / "rathole" / current_platform_id() / ("rathole.exe" if current_platform_id().startswith("windows") else "rathole")),
        }
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "runtime"))
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(mock_resolve.called)

    def test_runtime_plan_missing_binary_produces_clean_error(self) -> None:
        self._write_runtime_config([self._profile("smoke-l4-001")], managed_install_dir=None)
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "runtime"))
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertIn("No binary source is available", "\n".join(payload["errors"]))

    def test_rathole_runtime_config_and_argv_rendering(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_runtime_config([self._profile("smoke-l4-001", adapter="rathole")], managed_install_dir=install_dir)
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "runtime"))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        tunnel = payload["tunnels"][0]
        self.assertEqual(tunnel["adapter"], "rathole")
        self.assertTrue(tunnel["config_file_path"].endswith("rathole-controller.toml"))
        self.assertEqual(len(tunnel["command_argv"]), 2)

    def test_frp_runtime_config_and_argv_rendering(self) -> None:
        install_dir = self._managed_install_dir("frp")
        self._write_runtime_config([self._profile("smoke-l4-001", adapter="frp")], managed_install_dir=install_dir)
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "runtime"))
        self.assertEqual(code, 0, msg=output)
        tunnel = json.loads(output)["tunnels"][0]
        self.assertEqual(tunnel["adapter"], "frp")
        self.assertTrue(tunnel["config_file_path"].endswith("frp-controller.ini"))
        self.assertEqual(tunnel["command_argv"][1], "-c")

    def test_gost_tcp_runtime_config_and_argv_rendering(self) -> None:
        install_dir = self._managed_install_dir("gost")
        self._write_runtime_config([self._profile("smoke-l4-001", adapter="gost")], managed_install_dir=install_dir)
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "runtime"))
        self.assertEqual(code, 0, msg=output)
        tunnel = json.loads(output)["tunnels"][0]
        self.assertEqual(tunnel["adapter"], "gost")
        self.assertTrue(tunnel["config_file_path"].endswith("gost-controller.toml"))
        self.assertEqual(tunnel["command_argv"][1], "-C")

    def test_only_one_active_tunnel_allowed(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        profiles = [
            self._profile("smoke-l4-001", adapter="rathole"),
            self._profile("demo-l4-002", adapter="frp"),
        ]
        self._write_runtime_config(profiles, managed_install_dir=install_dir)
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "runtime"))
        self.assertEqual(code, 1)
        self.assertIn("exactly one active tunnel", output)

    def test_hot_standby_limit_enforced(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp", "gost")
        profiles = [
            self._profile("smoke-l4-001", adapter="rathole", runtime_role="active"),
            self._profile("demo-l4-002", adapter="frp", runtime_role="hot_standby"),
            self._profile("demo-l4-003", adapter="gost", runtime_role="hot_standby"),
            self._profile("demo-l4-004", adapter="rathole", runtime_role="hot_standby"),
        ]
        self._write_runtime_config(profiles, managed_install_dir=install_dir)
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "runtime"))
        self.assertEqual(code, 1)
        self.assertIn("At most two tunnels can be marked hot_standby", output)

    def test_config_only_tunnels_are_not_prepared_as_runnable_commands(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        profiles = [
            self._profile("smoke-l4-001", adapter="rathole", runtime_role="active"),
            self._profile("demo-l4-002", adapter="frp", runtime_role="config_only"),
        ]
        self._write_runtime_config(profiles, managed_install_dir=install_dir)
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "runtime"))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        config_only = next(item for item in payload["tunnels"] if item["role"] == "config_only")
        self.assertEqual(config_only["command_argv"], [])

    def test_runtime_dir_path_traversal_is_blocked(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_runtime_config([self._profile("smoke-l4-001")], managed_install_dir=install_dir)
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "safe" / ".." / "escape"))
        self.assertEqual(code, 1)
        self.assertIn("Path traversal blocked", output)

    def test_runtime_dir_symlink_escape_is_blocked(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_runtime_config([self._profile("smoke-l4-001")], managed_install_dir=install_dir)
        outside = self.base / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        runtime_link = self.base / "runtime-link"
        try:
            runtime_link.symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("Symlink creation is not available on this host")
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(runtime_link))
        self.assertEqual(code, 1)
        self.assertIn("Symlink escape blocked", output)

    @patch("pilottunnel.adapters.rathole.RatholeAdapter.render_runtime_plan")
    def test_runtime_plan_redacts_secrets_from_output(self, mock_render_runtime_plan) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_runtime_config([self._profile("smoke-l4-001")], managed_install_dir=install_dir)
        config_path = self.base / "runtime" / "configs" / "smoke-l4-001" / "rathole" / "tcp" / "controller" / "rathole-controller.toml"
        mock_render_runtime_plan.return_value = {
            "config_path": str(config_path),
            "content": "token = super-secret\npassword: another-secret\nmode = tcp",
            "argv": [str(config_path), "arg"],
            "environment": {"API_KEY": "secret-key"},
            "healthcheck_target_summary": {"kind": "tcp", "host": "127.0.0.1", "port": self.target_port},
        }
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "runtime"))
        self.assertEqual(code, 0, msg=output)
        self.assertNotIn("super-secret", output)
        self.assertNotIn("another-secret", output)
        self.assertNotIn("secret-key", output)
        payload = json.loads(output)
        tunnel = payload["tunnels"][0]
        self.assertIn("***REDACTED***", tunnel["redacted_config_summary"])
        self.assertEqual(tunnel["redacted_environment_summary"]["API_KEY"], "***REDACTED***")

    def test_runtime_plan_respects_state_selected_adapter(self) -> None:
        install_dir = self._managed_install_dir("gost")
        profile = self._profile("smoke-l4-001", adapter="rathole")
        state = AppState(
            profiles={
                "smoke-l4-001": RuntimeRecord(
                    profile="smoke-l4-001",
                    active_adapter="gost",
                    active_transport="tcp",
                    role="controller",
                )
            }
        )
        self._write_runtime_config([profile], managed_install_dir=install_dir, state=state)
        code, output = self.run_cli("runtime", "plan", "--runtime-dir", str(self.base / "runtime"))
        self.assertEqual(code, 0, msg=output)
        tunnel = json.loads(output)["tunnels"][0]
        self.assertEqual(tunnel["adapter"], "gost")
