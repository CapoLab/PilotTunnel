import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pilottunnel import cli


class CliWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        base = Path(self.temp_dir.name)
        self.config = base / "config.json"
        self.state = base / "state.json"
        self.registry = base / "registry.json"
        self.audit = base / "audit.log"
        self.lock_dir = base / "locks"
        self.work_dir = base / "work"
        self.staging_root = base / "staging"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(
                [
                    "--config",
                    str(self.config),
                    "--state",
                    str(self.state),
                    "--registry",
                    str(self.registry),
                    "--audit-log",
                    str(self.audit),
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

    def test_profile_create_writes_valid_config(self) -> None:
        self.run_cli("init")
        code, output = self.run_cli(
            "profile",
            "create",
            "--name",
            "turkey-6221",
            "--main-port",
            "6221",
            "--target-host",
            "127.0.0.1",
            "--target-port",
            "6221",
            "--role",
            "iran",
            "--control-port",
            "49323",
            "--service-port",
            "2106",
            "--check-port",
            "3106",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["profile"]["role"], "controller")
        config_data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config_data["profiles"][0]["name"], "turkey-6221")

    def test_duplicate_profile_create_is_blocked(self) -> None:
        self.run_cli("init")
        self.run_cli("profile", "create", "--name", "turkey-6221", "--main-port", "6221", "--target-port", "6221")
        code, output = self.run_cli("profile", "create", "--name", "turkey-6221", "--main-port", "6221", "--target-port", "6221")
        self.assertEqual(code, 1)
        self.assertIn("already exists", output)

    def test_adapter_list_includes_backhaul_and_rathole_transports(self) -> None:
        code, output = self.run_cli("adapter", "list")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        backhaul = next(item for item in payload if item["id"] == "backhaul")
        rathole = next(item for item in payload if item["id"] == "rathole")
        self.assertIn("tcpmux", backhaul["usable_in_v0_1"])
        self.assertEqual(rathole["usable_in_v0_1"], ["tcp"])

    def test_adapter_show_blocks_unknown_adapter(self) -> None:
        code, output = self.run_cli("adapter", "show", "--name", "missing")
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def _create_profile(self) -> None:
        self.run_cli("init")
        self.run_cli(
            "profile",
            "create",
            "--name",
            "turkey-6221",
            "--main-port",
            "6221",
            "--target-port",
            "6221",
            "--control-port",
            "49323",
            "--service-port",
            "2106",
            "--check-port",
            "3106",
        )

    def test_cli_dry_run_switch_to_backhaul_tcpmux_works(self) -> None:
        self._create_profile()
        code, output = self.run_cli("switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["target"]["adapter"], "backhaul")
        self.assertEqual(payload["target"]["transport"], "tcpmux")
        self.assertTrue(payload["generated_service"].endswith("backhaul-tcpmux-controller.service"))

    def test_cli_dry_run_switch_to_rathole_tcp_works(self) -> None:
        self._create_profile()
        code, output = self.run_cli("switch", "--profile", "turkey-6221", "--adapter", "rathole", "--transport", "tcp")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["target"]["adapter"], "rathole")
        self.assertEqual(payload["healthcheck"]["result"], True)

    def test_status_output_includes_active_adapter_transport(self) -> None:
        self._create_profile()
        self.run_cli("switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        code, output = self.run_cli("status", "--profile", "turkey-6221")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["active_adapter"], "backhaul")
        self.assertEqual(payload["active_transport"], "tcpmux")

    def test_rollback_without_snapshot_fails_safely(self) -> None:
        self._create_profile()
        code, output = self.run_cli("rollback", "--profile", "turkey-6221")
        self.assertEqual(code, 1)
        self.assertIn("No rollback snapshot available", output)

    def test_logs_command_filters_by_profile(self) -> None:
        self._create_profile()
        self.run_cli("switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.audit.write_text(
            self.audit.read_text(encoding="utf-8")
            + json.dumps({"timestamp": "now", "profile": "other", "action": "switch", "details": {}})
            + "\n",
            encoding="utf-8",
        )
        code, output = self.run_cli("logs", "--profile", "turkey-6221", "--limit", "5")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(all(item["profile"] == "turkey-6221" for item in payload))

    def test_registry_check_reports_no_conflict_on_valid_config(self) -> None:
        self._create_profile()
        code, output = self.run_cli("registry", "check")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])

    def test_registry_check_reports_conflict_on_duplicate_ports(self) -> None:
        self._create_profile()
        config_data = json.loads(self.config.read_text(encoding="utf-8"))
        config_data["profiles"].append(
            {
                "name": "other",
                "main_port": 7443,
                "target_host": "127.0.0.1",
                "target_port": 7443,
                "role": "worker",
                "active_layer": "layer4",
                "active_adapter": "",
                "active_transport": "",
                "candidates": [],
                "ports": {
                    "main_port": 7443,
                    "control_port": 49323,
                    "service_port": 2206,
                    "check_port": 3206,
                },
                "safety": {
                    "cooldown_seconds": 30,
                    "rollback_on_failure": True,
                    "dry_run_default": True,
                },
            }
        )
        self.config.write_text(json.dumps(config_data), encoding="utf-8")
        code, output = self.run_cli("registry", "check")
        self.assertEqual(code, 1)
        self.assertIn("conflict", output)

    def test_plan_command_for_backhaul_tcpmux(self) -> None:
        self._create_profile()
        code, output = self.run_cli("plan", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["adapter"], "backhaul")
        self.assertEqual(payload["transport"], "tcpmux")
        self.assertIn("configs", payload["generated_config_path"])

    def test_plan_command_for_rathole_tcp(self) -> None:
        self._create_profile()
        code, output = self.run_cli("plan", "--profile", "turkey-6221", "--adapter", "rathole", "--transport", "tcp")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["adapter"], "rathole")
        self.assertTrue(payload["supported_in_v0_1"])

    def test_apply_writes_backhaul_staged_files_only_under_staging_root(self) -> None:
        self._create_profile()
        code, output = self.run_cli("--apply", "switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["staged_only"])
        self.assertFalse(payload["real_systemd_touched"])
        self.assertTrue((self.staging_root / "configs" / "turkey-6221" / "backhaul" / "tcpmux" / "controller" / "backhaul-controller.toml").exists())
        self.assertTrue((self.staging_root / "systemd" / "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service").exists())
        self.assertFalse(Path("/etc/systemd/system").joinpath("pilottunnel-turkey-6221-backhaul-tcpmux-controller.service").exists())

    def test_apply_writes_rathole_staged_files_only_under_staging_root(self) -> None:
        self._create_profile()
        code, output = self.run_cli("--apply", "switch", "--profile", "turkey-6221", "--adapter", "rathole", "--transport", "tcp")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["staged_only"])
        self.assertTrue((self.staging_root / "configs" / "turkey-6221" / "rathole" / "tcp" / "controller" / "rathole-controller.toml").exists())
        self.assertTrue((self.staging_root / "systemd" / "pilottunnel-turkey-6221-rathole-tcp-controller.service").exists())

    def test_staged_list_shows_generated_files(self) -> None:
        self._create_profile()
        self.run_cli("--apply", "switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        code, output = self.run_cli("staged", "list")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(any("backhaul-controller.toml" in item for item in payload))

    def test_staged_show_displays_generated_file_content(self) -> None:
        self._create_profile()
        self.run_cli("--apply", "switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        code, output = self.run_cli("staged", "show", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(any("role = controller" in content for content in payload["configs"].values()))

    def test_path_traversal_staging_root_or_profile_name_is_blocked(self) -> None:
        self.run_cli("init")
        code, output = self.run_cli("profile", "create", "--name", "../bad", "--main-port", "6221", "--target-port", "6221")
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)

    def test_audit_records_staged_only_true(self) -> None:
        self._create_profile()
        self.run_cli("--apply", "switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        lines = self.audit.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[-1])
        self.assertTrue(payload["details"]["staged_only"])

    def test_preflight_returns_structured_host_info(self) -> None:
        code, output = self.run_cli("preflight")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("host", payload)
        self.assertIn("commands", payload)
        self.assertIn("safe_to_stage", payload)

    def test_preflight_with_profile_does_not_crash(self) -> None:
        self._create_profile()
        code, output = self.run_cli("preflight", "--profile", "turkey-6221")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("port_availability", payload)

    def test_preflight_marks_safe_to_real_apply_false(self) -> None:
        code, output = self.run_cli("preflight")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["safe_to_real_apply"])

    def test_binary_list_includes_backhaul_and_rathole(self) -> None:
        code, output = self.run_cli("binary", "list")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        adapters = {item["adapter"] for item in payload}
        self.assertIn("backhaul", adapters)
        self.assertIn("rathole", adapters)

    def test_binary_plan_for_backhaul_shows_no_download(self) -> None:
        code, output = self.run_cli("binary", "plan", "--adapter", "backhaul")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["download_performed"])

    def test_binary_plan_for_rathole_shows_no_download(self) -> None:
        code, output = self.run_cli("binary", "plan", "--adapter", "rathole")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["download_performed"])

    def test_unknown_binary_adapter_is_rejected(self) -> None:
        code, output = self.run_cli("binary", "plan", "--adapter", "missing")
        self.assertEqual(code, 1)
        self.assertIn("Unknown binary adapter", output)

    def test_staged_switch_output_includes_preflight_info(self) -> None:
        self._create_profile()
        code, output = self.run_cli("--apply", "switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("preflight", payload)
        self.assertIn("warnings", payload["preflight"])

    def _make_binary_source(self, name: str, content: str = "binary") -> Path:
        path = Path(self.temp_dir.name) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_binary_import_backhaul_from_local_temp_file(self) -> None:
        source = self._make_binary_source("backhaul")
        code, output = self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(Path(payload["imported_path"]).exists())

    def test_binary_import_rathole_from_local_temp_file(self) -> None:
        source = self._make_binary_source("rathole")
        code, output = self.run_cli("binary", "import", "--adapter", "rathole", "--source", str(source), "--version", "manual-v0.0.0")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("sha256", payload)

    def test_binary_import_rejects_unknown_adapter(self) -> None:
        source = self._make_binary_source("unknown")
        code, output = self.run_cli("binary", "import", "--adapter", "missing", "--source", str(source), "--version", "manual-v0.0.0")
        self.assertEqual(code, 1)
        self.assertIn("Unknown binary adapter", output)

    def test_binary_import_rejects_missing_source(self) -> None:
        code, output = self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(Path(self.temp_dir.name) / "missing"), "--version", "manual-v0.0.0")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", output)

    def test_binary_import_rejects_directory_source(self) -> None:
        source_dir = Path(self.temp_dir.name) / "srcdir"
        source_dir.mkdir()
        code, output = self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source_dir), "--version", "manual-v0.0.0")
        self.assertEqual(code, 1)
        self.assertIn("must be a file", output)

    def test_binary_import_rejects_checksum_mismatch(self) -> None:
        source = self._make_binary_source("backhaul")
        code, output = self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0", "--sha256", "deadbeef")
        self.assertEqual(code, 1)
        self.assertIn("sha256", output)

    def test_binary_import_refuses_overwrite_without_force(self) -> None:
        source = self._make_binary_source("backhaul")
        self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0")
        code, output = self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.1")
        self.assertEqual(code, 1)
        self.assertIn("Use --force", output)

    def test_binary_import_with_force_overwrites_safely(self) -> None:
        source = self._make_binary_source("backhaul", "one")
        self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0")
        source.write_text("two", encoding="utf-8")
        code, output = self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.1", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["version"], "manual-v0.0.1")

    def test_binary_status_shows_imported_binary(self) -> None:
        source = self._make_binary_source("backhaul")
        self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0")
        code, output = self.run_cli("binary", "status", "--adapter", "backhaul")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["install_status"], "imported")

    def test_binary_verify_shows_sha256_executable_platform(self) -> None:
        source = self._make_binary_source("backhaul")
        self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0")
        code, output = self.run_cli("binary", "verify", "--adapter", "backhaul")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("sha256", payload)
        self.assertIn("platform", payload)
        self.assertIn("executable", payload)

    @patch("pilottunnel.binaries.subprocess.run")
    def test_binary_verify_run_version_is_mocked_and_timeout_safe(self, mock_run) -> None:
        source = self._make_binary_source("backhaul")
        self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0")
        class Completed:
            returncode = 0
            stdout = "backhaul 1.2.3"
            stderr = ""
        mock_run.return_value = Completed()
        code, output = self.run_cli("binary", "verify", "--adapter", "backhaul", "--run-version")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["run_version_result"]["ran"])

    def test_binary_cache_path_traversal_is_blocked(self) -> None:
        source = Path(self.temp_dir.name) / ".." / "backhaul"
        code, output = self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0")
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)
