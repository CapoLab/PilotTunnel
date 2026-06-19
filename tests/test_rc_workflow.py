import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pilottunnel import cli
from pilottunnel.binaries import binary_spec, current_platform_id
from pilottunnel.config import AppConfig, BinaryResolutionSettings, Profile, ProfilePorts, build_node_settings, save_config
from pilottunnel.state import AppState, save_state
from testsupport import allocate_tcp_ports


class RcWorkflowTests(unittest.TestCase):
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
        self.target_dir = self.base / "target-systemd"
        ports, listeners = allocate_tcp_ports(10)
        self.ports = ports
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

    def _profile(self, name: str, *, adapter: str, runtime_role: str, offset: int) -> Profile:
        main_port, target_port, control_port, service_port, check_port = self.ports[offset : offset + 5]
        return Profile(
            name=name,
            main_port=main_port,
            target_host="127.0.0.1",
            target_port=target_port,
            role="controller",
            active_adapter=adapter,
            active_transport="tcp",
            runtime_role=runtime_role,
            ports=ProfilePorts(
                main_port=main_port,
                control_port=control_port,
                service_port=service_port,
                check_port=check_port,
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

    def _write_config(self, profiles: list[Profile], *, managed_install_dir: Path | None) -> None:
        config = AppConfig(
            node=build_node_settings("controller"),
            binary_resolution=BinaryResolutionSettings(
                managed_install_dir=str(managed_install_dir) if managed_install_dir else "",
                allow_system_path=False,
                prefer_managed_install=True,
            ),
            profiles=profiles,
        )
        save_config(config, self.config_path)
        save_state(AppState(), self.state_path)
        self.registry_path.write_text("{}", encoding="utf-8")

    def _profiles(self) -> list[Profile]:
        return [
            self._profile("smoke-l4-001", adapter="rathole", runtime_role="active", offset=0),
            self._profile("demo-l4-002", adapter="frp", runtime_role="hot_standby", offset=5),
        ]

    def test_rc_check_is_read_only(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        before_state = self.state_path.read_text(encoding="utf-8")
        code, output = self.run_cli(
            "rc",
            "check",
            "--target",
            "demo-l4-002",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["read_only"])
        self.assertFalse(self.runtime_dir.exists())
        self.assertFalse(self.service_dir.exists())
        self.assertEqual(before_state, self.state_path.read_text(encoding="utf-8"))

    def test_rc_smoke_is_safe_by_default(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        before_state = self.state_path.read_text(encoding="utf-8")
        code, output = self.run_cli(
            "rc",
            "smoke",
            "--target",
            "demo-l4-002",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertFalse(payload["read_only"])
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["systemctl_executed"])
        self.assertTrue(self.runtime_dir.exists())
        self.assertTrue(self.service_dir.exists())
        self.assertEqual(before_state, self.state_path.read_text(encoding="utf-8"))

    def test_rc_check_reports_config_load_status(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        code, output = self.run_cli(
            "rc",
            "check",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["sections"]["config"]["ok"])
        self.assertEqual(payload["sections"]["config"]["profile_count"], 2)

    def test_rc_check_reports_binary_resolver_status(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        code, output = self.run_cli(
            "rc",
            "check",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)
        binary_section = json.loads(output)["sections"]["binaries"]
        self.assertTrue(binary_section["ok"])
        self.assertEqual(binary_section["adapter_count"], 2)

    def test_rc_check_reports_runtime_service_and_install_statuses(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        code, output = self.run_cli(
            "rc",
            "check",
            "--target",
            "demo-l4-002",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["sections"]["runtime_plan"]["ok"])
        self.assertTrue(payload["sections"]["service_render"]["ok"])
        self.assertTrue(payload["sections"]["service_install_plan"]["ok"])

    def test_rc_check_reports_manual_switch_plan_when_target_supplied(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        code, output = self.run_cli(
            "rc",
            "check",
            "--target",
            "demo-l4-002",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)
        switch_section = json.loads(output)["sections"]["manual_switch_plan"]
        self.assertTrue(switch_section["ok"])
        self.assertEqual(switch_section["summary"]["action"], "switch-plan")

    def test_rc_check_warns_clearly_when_no_manual_switch_target_is_supplied(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        code, output = self.run_cli(
            "rc",
            "check",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)
        switch_section = json.loads(output)["sections"]["manual_switch_plan"]
        self.assertTrue(switch_section["ok"])
        self.assertIn("No manual switch target was supplied", switch_section["warnings"][0])

    def test_rc_check_does_not_call_systemd_lifecycle_or_reload_execution(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        with patch("pilottunnel.systemd_control._default_command_runner", side_effect=AssertionError("should not run systemctl")):
            code, output = self.run_cli(
                "rc",
                "check",
                "--runtime-dir",
                str(self.runtime_dir),
                "--service-dir",
                str(self.service_dir),
                "--target-dir",
                str(self.target_dir),
            )
        self.assertEqual(code, 0, msg=output)

    @patch("subprocess.run", side_effect=AssertionError("adapter binaries must not execute"))
    def test_rc_check_does_not_execute_adapter_binaries(self, _mock_run) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        code, output = self.run_cli(
            "rc",
            "check",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)

    def test_rc_check_does_not_mutate_state_file(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        before = self.state_path.read_text(encoding="utf-8")
        code, output = self.run_cli(
            "rc",
            "check",
            "--target",
            "demo-l4-002",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(before, self.state_path.read_text(encoding="utf-8"))

    @patch("pilottunnel.adapters.rathole.RatholeAdapter.render_runtime_plan")
    def test_rc_check_redacts_secrets(self, mock_render_runtime_plan) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(self._profiles(), managed_install_dir=install_dir)
        config_path = self.base / "scratch" / "rathole-controller.toml"
        mock_render_runtime_plan.return_value = {
            "config_path": str(config_path),
            "content": "token = super-secret\npassword: hidden\nmode = tcp",
            "argv": ["rathole", str(config_path)],
            "environment": {"API_KEY": "secret-key"},
            "healthcheck_target_summary": {"kind": "tcp", "host": "127.0.0.1", "port": self.ports[1]},
        }
        code, output = self.run_cli(
            "rc",
            "check",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)
        self.assertNotIn("super-secret", output)
        self.assertNotIn("hidden", output)
        self.assertNotIn("secret-key", output)

    def test_rc_check_reports_missing_binary_as_blocker(self) -> None:
        self._write_config(self._profiles(), managed_install_dir=None)
        code, output = self.run_cli(
            "rc",
            "check",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertTrue(payload["blockers"])
        self.assertIn("Binary resolver is not ready", "\n".join(payload["blockers"]))

    def test_rc_check_reports_invalid_config_blocker_cleanly(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        profiles = [
            self._profile("smoke-l4-001", adapter="rathole", runtime_role="active", offset=0),
            self._profile("demo-l4-002", adapter="frp", runtime_role="active", offset=5),
        ]
        self._write_config(profiles, managed_install_dir=install_dir)
        code, output = self.run_cli(
            "rc",
            "check",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertIn("exactly one active tunnel", "\n".join(payload["blockers"]))
