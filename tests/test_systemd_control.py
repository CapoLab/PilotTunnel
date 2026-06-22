import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pilottunnel import cli
from pilottunnel.binaries import binary_components, binary_filename_for_component, current_platform_id
from pilottunnel.config import AppConfig, BinaryResolutionSettings, Profile, ProfilePorts, save_config
from pilottunnel.state import AppState, save_state


class SystemdControlTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        code = 0
        with redirect_stdout(output):
            try:
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
            except SystemExit as exc:
                code = int(exc.code)
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
            for component in binary_components(adapter):
                filename = binary_filename_for_component(adapter, component, platform_id=platform_id)
                binary_path = install_dir / adapter / platform_id / filename
                binary_path.parent.mkdir(parents=True, exist_ok=True)
                binary_path.write_bytes(f"{adapter}-{component}-binary".encode("utf-8"))
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

    def _render_services(self) -> dict:
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        return json.loads(output)

    def _install_services(self) -> dict:
        code, output = self.run_cli(
            "service",
            "install",
            "apply",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
            "--confirm",
            "INSTALL_PILOTTUNNEL_SERVICES",
        )
        self.assertEqual(code, 0, msg=output)
        return json.loads(output)

    def _set_runtime_role_in_staged_unit(self, service_name: str, runtime_role: str) -> None:
        path = self.service_dir / service_name
        content = path.read_text(encoding="utf-8")
        updated_lines = []
        for line in content.splitlines():
            if line.startswith("Description=PilotTunnel "):
                parts = line[len("Description=PilotTunnel ") :].split(" ")
                if len(parts) >= 3:
                    parts[2] = runtime_role
                    line = "Description=PilotTunnel " + " ".join(parts)
            updated_lines.append(line)
        path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")

    def test_reload_plan_is_read_only(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        self._install_services()
        before = (self.target_dir / "pilottunnel-service-install-summary.json").read_text(encoding="utf-8")
        code, output = self.run_cli("systemd", "reload", "plan", "--target-dir", str(self.target_dir))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["action"], "would_reload")
        after = (self.target_dir / "pilottunnel-service-install-summary.json").read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_reload_apply_requires_exact_confirm_token(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        self._install_services()
        code, output = self.run_cli("systemd", "reload", "apply", "--target-dir", str(self.target_dir))
        self.assertEqual(code, 1)
        self.assertIn("SYSTEMD_DAEMON_RELOAD", output)

    def test_reload_apply_calls_only_systemctl_daemon_reload(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        self._install_services()
        calls: list[list[str]] = []

        def fake_runner(command: list[str], *, timeout_seconds: float) -> dict:
            calls.append(command)
            return {"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False}

        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch("pilottunnel.systemd_control._default_command_runner", side_effect=fake_runner):
            code, output = self.run_cli(
                "systemd",
                "reload",
                "apply",
                "--target-dir",
                str(self.target_dir),
                "--confirm",
                "SYSTEMD_DAEMON_RELOAD",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(calls, [["systemctl", "daemon-reload"]])

    def test_reload_apply_can_be_tested_through_fake_command_runner(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        self._install_services()
        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch(
            "pilottunnel.systemd_control._default_command_runner",
            return_value={"returncode": 0, "stdout": "reload ok", "stderr": "", "timed_out": False},
        ):
            code, output = self.run_cli(
                "systemd",
                "reload",
                "apply",
                "--target-dir",
                str(self.target_dir),
                "--confirm",
                "SYSTEMD_DAEMON_RELOAD",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["action"], "reloaded")

    def test_start_plan_is_read_only(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        before = (self.service_dir / next(self.service_dir.glob("*.service")).name).read_text(encoding="utf-8")
        code, output = self.run_cli("systemd", "start", "plan", "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["services"][0]["action"], "would_start")
        after = (self.service_dir / next(self.service_dir.glob("*.service")).name).read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_start_apply_requires_exact_confirm_token(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        code, output = self.run_cli("systemd", "start", "apply", "--service-dir", str(self.service_dir))
        self.assertEqual(code, 1)
        self.assertIn("START_PILOTTUNNEL_SERVICES", output)

    def test_start_apply_calls_only_systemctl_start_for_managed_services(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(
            [
                self._profile("smoke-l4-001", adapter="rathole", runtime_role="active"),
                self._profile("demo-l4-002", adapter="frp", runtime_role="hot_standby"),
            ],
            managed_install_dir=install_dir,
        )
        render_payload = self._render_services()
        expected = {item["service_name"] for item in render_payload["services"] if item["service_unit_rendered"]}
        calls: list[list[str]] = []

        def fake_runner(command: list[str], *, timeout_seconds: float) -> dict:
            calls.append(command)
            return {"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False}

        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch("pilottunnel.systemd_control._default_command_runner", side_effect=fake_runner):
            code, output = self.run_cli(
                "systemd",
                "start",
                "apply",
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "START_PILOTTUNNEL_SERVICES",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual({tuple(call) for call in calls}, {("systemctl", "start", name) for name in expected})

    def test_start_apply_does_not_start_config_only_services(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        render_payload = self._render_services()
        service_name = render_payload["services"][0]["service_name"]
        self._set_runtime_role_in_staged_unit(service_name, "config_only")
        calls: list[list[str]] = []

        def fake_runner(command: list[str], *, timeout_seconds: float) -> dict:
            calls.append(command)
            return {"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False}

        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch("pilottunnel.systemd_control._default_command_runner", side_effect=fake_runner):
            code, output = self.run_cli(
                "systemd",
                "start",
                "apply",
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "START_PILOTTUNNEL_SERVICES",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["services"][0]["action"], "skipped")
        self.assertEqual(calls, [])

    def test_stop_plan_is_read_only(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        before = (self.service_dir / next(self.service_dir.glob("*.service")).name).read_text(encoding="utf-8")
        code, output = self.run_cli("systemd", "stop", "plan", "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["services"][0]["action"], "would_stop")
        after = (self.service_dir / next(self.service_dir.glob("*.service")).name).read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_stop_apply_requires_exact_confirm_token(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        code, output = self.run_cli("systemd", "stop", "apply", "--service-dir", str(self.service_dir))
        self.assertEqual(code, 1)
        self.assertIn("STOP_PILOTTUNNEL_SERVICES", output)

    def test_stop_apply_calls_only_systemctl_stop_for_managed_services(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        self._write_config(
            [
                self._profile("smoke-l4-001", adapter="rathole", runtime_role="active"),
                self._profile("demo-l4-002", adapter="frp", runtime_role="hot_standby"),
            ],
            managed_install_dir=install_dir,
        )
        render_payload = self._render_services()
        expected = {item["service_name"] for item in render_payload["services"] if item["service_unit_rendered"]}
        calls: list[list[str]] = []

        def fake_runner(command: list[str], *, timeout_seconds: float) -> dict:
            calls.append(command)
            return {"returncode": 0, "stdout": "ok", "stderr": "", "timed_out": False}

        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch("pilottunnel.systemd_control._default_command_runner", side_effect=fake_runner):
            code, output = self.run_cli(
                "systemd",
                "stop",
                "apply",
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "STOP_PILOTTUNNEL_SERVICES",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual({tuple(call) for call in calls}, {("systemctl", "stop", name) for name in expected})

    def test_status_calls_only_read_only_systemctl_commands(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        render_payload = self._render_services()
        service_name = render_payload["services"][0]["service_name"]
        calls: list[list[str]] = []

        def fake_runner(command: list[str], *, timeout_seconds: float) -> dict:
            calls.append(command)
            return {
                "returncode": 0,
                "stdout": "LoadState=loaded\nActiveState=inactive\nSubState=dead\nFragmentPath=/etc/systemd/system/test.service\n",
                "stderr": "",
                "timed_out": False,
            }

        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch("pilottunnel.systemd_control._default_command_runner", side_effect=fake_runner):
            code, output = self.run_cli("systemd", "status", "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(calls, [["systemctl", "show", service_name, "--property", "LoadState,ActiveState,SubState,FragmentPath", "--no-pager"]])

    def test_status_rejects_arbitrary_non_pilottunnel_service_names(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        code, output = self.run_cli("systemd", "status", "--service-dir", str(self.service_dir), "--service-name", "other.service")
        self.assertEqual(code, 1)
        self.assertIn("non-PilotTunnel managed service name", output)

    def test_start_rejects_arbitrary_non_pilottunnel_service_names(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        code, output = self.run_cli("systemd", "start", "plan", "--service-dir", str(self.service_dir), "--service-name", "other.service")
        self.assertEqual(code, 1)
        self.assertIn("non-PilotTunnel managed service name", output)

    def test_status_accepts_deterministic_pilottunnel_managed_service_names(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        render_payload = self._render_services()
        service_name = render_payload["services"][0]["service_name"]
        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch(
            "pilottunnel.systemd_control._default_command_runner",
            return_value={
                "returncode": 0,
                "stdout": "LoadState=loaded\nActiveState=active\nSubState=running\nFragmentPath=/etc/systemd/system/test.service\n",
                "stderr": "",
                "timed_out": False,
            },
        ):
            code, output = self.run_cli("systemd", "status", "--service-dir", str(self.service_dir), "--service-name", service_name)
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["services"][0]["service_name"], service_name)

    def test_systemd_restart_enable_disable_mask_unmask_are_not_callable(self) -> None:
        for subcommand in ("restart", "enable", "disable", "mask", "unmask"):
            code, _output = self.run_cli("systemd", subcommand)
            self.assertNotEqual(code, 0)

    def test_clean_error_when_systemctl_is_missing(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=False,
        ):
            code, output = self.run_cli("systemd", "status", "--service-dir", str(self.service_dir))
        self.assertEqual(code, 1)
        self.assertIn("systemctl is unavailable", output)

    def test_clean_error_when_systemctl_returns_non_zero(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch(
            "pilottunnel.systemd_control._default_command_runner",
            return_value={"returncode": 1, "stdout": "", "stderr": "service failed", "timed_out": False},
        ):
            code, output = self.run_cli("systemd", "status", "--service-dir", str(self.service_dir))
        self.assertEqual(code, 1)
        self.assertIn("service failed", output)

    def test_secrets_are_redacted_from_cli_output(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch(
            "pilottunnel.systemd_control._default_command_runner",
            return_value={"returncode": 1, "stdout": "", "stderr": "token=super-secret", "timed_out": False},
        ):
            code, output = self.run_cli("systemd", "status", "--service-dir", str(self.service_dir))
        self.assertEqual(code, 1)
        self.assertNotIn("super-secret", output)

    @patch("subprocess.run", side_effect=AssertionError("unexpected subprocess.run call"))
    def test_no_service_binary_execution_occurs(self, _mock_run) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch(
            "pilottunnel.systemd_control._default_command_runner",
            return_value={
                "returncode": 0,
                "stdout": "LoadState=loaded\nActiveState=inactive\nSubState=dead\nFragmentPath=/etc/systemd/system/test.service\n",
                "stderr": "",
                "timed_out": False,
            },
        ):
            code, output = self.run_cli("systemd", "status", "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)

    def test_start_apply_clean_error_when_systemctl_returns_non_zero(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        with patch("pilottunnel.systemd_control._is_linux", return_value=True), patch(
            "pilottunnel.systemd_control._systemd_available",
            return_value=True,
        ), patch(
            "pilottunnel.systemd_control._default_command_runner",
            return_value={"returncode": 1, "stdout": "", "stderr": "token=blocked", "timed_out": False},
        ):
            code, output = self.run_cli(
                "systemd",
                "start",
                "apply",
                "--service-dir",
                str(self.service_dir),
                "--confirm",
                "START_PILOTTUNNEL_SERVICES",
            )
        self.assertEqual(code, 1)
        self.assertNotIn("blocked", output)
