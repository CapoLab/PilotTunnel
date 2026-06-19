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

    def test_systemd_start_stop_restart_enable_disable_are_not_callable(self) -> None:
        for subcommand in ("start", "stop", "restart", "enable", "disable"):
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
