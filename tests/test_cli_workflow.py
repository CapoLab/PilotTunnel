import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

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
