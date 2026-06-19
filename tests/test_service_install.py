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
from pilottunnel.service_install import INSTALL_SUMMARY_FILENAME
from pilottunnel.state import AppState, save_state


class ServiceInstallWorkflowTests(unittest.TestCase):
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

    def _render_services(self) -> dict:
        code, output = self.run_cli("service", "render", "--runtime-dir", str(self.runtime_dir), "--service-dir", str(self.service_dir))
        self.assertEqual(code, 0, msg=output)
        return json.loads(output)

    def test_service_install_plan_is_read_only(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        code, output = self.run_cli(
            "service",
            "install",
            "plan",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 0, msg=output)
        self.assertFalse(self.target_dir.exists())

    def test_service_install_apply_requires_confirmation(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
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
        )
        self.assertEqual(code, 1)
        self.assertIn("INSTALL_PILOTTUNNEL_SERVICES", output)

    def test_active_unit_can_be_installed_to_temp_target_dir(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        render_payload = self._render_services()
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
        payload = json.loads(output)
        target_path = Path(payload["services"][0]["target_unit_path"])
        self.assertTrue(target_path.exists())
        self.assertTrue((self.target_dir / INSTALL_SUMMARY_FILENAME).exists())
        self.assertIn(render_payload["services"][0]["service_name"], target_path.name)

    def test_hot_standby_unit_can_be_installed_to_temp_target_dir(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        profiles = [
            self._profile("smoke-l4-001", adapter="rathole", runtime_role="active"),
            self._profile("demo-l4-002", adapter="frp", runtime_role="hot_standby"),
        ]
        self._write_config(profiles, managed_install_dir=install_dir)
        self._render_services()
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
        payload = json.loads(output)
        standby = next(item for item in payload["services"] if item["runtime_role"] == "hot_standby")
        self.assertTrue(Path(standby["target_unit_path"]).exists())

    def test_config_only_unit_is_not_installed(self) -> None:
        install_dir = self._managed_install_dir("rathole", "frp")
        profiles = [
            self._profile("smoke-l4-001", adapter="rathole", runtime_role="active"),
            self._profile("demo-l4-002", adapter="frp", runtime_role="config_only"),
        ]
        self._write_config(profiles, managed_install_dir=install_dir)
        self._render_services()
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
        payload = json.loads(output)
        config_only = next(item for item in payload["services"] if item["runtime_role"] == "config_only")
        self.assertEqual(config_only["action"], "skipped_config_only")
        self.assertEqual(len(list(self.target_dir.glob("*.service"))), 1)

    def test_staged_units_must_match_current_service_plan(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        render_payload = self._render_services()
        staged_path = Path(render_payload["services"][0]["staged_unit_file_path"])
        staged_path.write_text("tampered", encoding="utf-8")
        code, output = self.run_cli(
            "service",
            "install",
            "plan",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 1)
        self.assertIn("Staged unit does not match the current service plan", output)

    def test_arbitrary_extra_staged_unit_is_ignored(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        extra = self.service_dir / "external-extra.service"
        extra.write_text("not owned", encoding="utf-8")
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
        self.assertFalse((self.target_dir / "external-extra.service").exists())

    def test_reinstall_is_idempotent_when_target_matches(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        first_code, first_output = self.run_cli(
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
        self.assertEqual(first_code, 0, msg=first_output)
        second_code, second_output = self.run_cli(
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
        self.assertEqual(second_code, 0, msg=second_output)
        payload = json.loads(second_output)
        self.assertEqual(payload["services"][0]["action"], "unchanged")

    def test_differing_target_unit_fails_without_replace_existing(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        render_payload = self._render_services()
        target_path = self.target_dir / Path(render_payload["services"][0]["service_name"])
        self.target_dir.mkdir(parents=True, exist_ok=True)
        target_path.write_text("old unit", encoding="utf-8")
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
        self.assertEqual(code, 1)
        self.assertIn("--replace-existing", output)

    def test_replace_existing_creates_backup(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        render_payload = self._render_services()
        target_path = self.target_dir / Path(render_payload["services"][0]["service_name"])
        self.target_dir.mkdir(parents=True, exist_ok=True)
        target_path.write_text("secret=old-value", encoding="utf-8")
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
            "--replace-existing",
            "--confirm",
            "INSTALL_PILOTTUNNEL_SERVICES",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        service = payload["services"][0]
        self.assertTrue(service["backup_path"])
        self.assertTrue(Path(service["backup_path"]).exists())
        summary = (self.target_dir / INSTALL_SUMMARY_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("old-value", summary)

    def test_path_traversal_is_blocked_for_service_dir(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        code, output = self.run_cli(
            "service",
            "install",
            "plan",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.base / "safe" / ".." / "escape"),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 1)
        self.assertIn("Path traversal blocked", output)

    def test_symlink_escape_is_blocked_for_service_dir(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        outside = self.base / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        link = self.base / "service-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("Symlink creation is not available on this host")
        code, output = self.run_cli(
            "service",
            "install",
            "plan",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(link),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 1)
        self.assertIn("Symlink escape blocked", output)

    def test_path_traversal_is_blocked_for_target_dir(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        code, output = self.run_cli(
            "service",
            "install",
            "plan",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.base / "safe" / ".." / "escape"),
        )
        self.assertEqual(code, 1)
        self.assertIn("Path traversal blocked", output)

    def test_symlink_escape_is_blocked_for_target_dir(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        outside = self.base / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        link = self.base / "target-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (NotImplementedError, OSError):
            self.skipTest("Symlink creation is not available on this host")
        code, output = self.run_cli(
            "service",
            "install",
            "plan",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(link),
        )
        self.assertEqual(code, 1)
        self.assertIn("Symlink escape blocked", output)

    def test_real_systemd_dir_requires_allow_system_dir(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        code, output = self.run_cli(
            "service",
            "install",
            "plan",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            "/etc/systemd/system",
        )
        self.assertEqual(code, 1)
        self.assertIn("--allow-system-dir", output)

    def test_real_systemd_dir_can_be_planned_with_allow_system_dir(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
        code, output = self.run_cli(
            "service",
            "install",
            "plan",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            "/etc/systemd/system",
            "--allow-system-dir",
        )
        self.assertEqual(code, 0, msg=output)

    @patch("subprocess.run", side_effect=AssertionError("subprocess.run must not be called"))
    def test_service_install_does_not_execute_systemctl_or_processes(self, _mock_run) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        self._render_services()
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

    def test_service_install_redacts_secret_like_mismatch_content_from_output(self) -> None:
        install_dir = self._managed_install_dir("rathole")
        self._write_config([self._profile("smoke-l4-001", adapter="rathole", runtime_role="active")], managed_install_dir=install_dir)
        render_payload = self._render_services()
        staged_path = Path(render_payload["services"][0]["staged_unit_file_path"])
        staged_path.write_text("token=secret-value", encoding="utf-8")
        code, output = self.run_cli(
            "service",
            "install",
            "plan",
            "--runtime-dir",
            str(self.runtime_dir),
            "--service-dir",
            str(self.service_dir),
            "--target-dir",
            str(self.target_dir),
        )
        self.assertEqual(code, 1)
        self.assertNotIn("secret-value", output)
