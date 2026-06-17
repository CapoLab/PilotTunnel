import io
import json
import socket
import tempfile
import threading
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
        self.run_cli("init", "--role", "controller")
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

    def test_init_with_role_controller_stores_normalized_controller_role(self) -> None:
        code, output = self.run_cli("init", "--role", "controller")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["normalized_role"], "controller")
        config_data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config_data["node"]["normalized_role"], "controller")

    def test_init_with_role_iran_stores_normalized_controller_role(self) -> None:
        code, output = self.run_cli("init", "--role", "iran")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["normalized_role"], "controller")

    def test_init_with_role_worker_stores_normalized_worker_role(self) -> None:
        code, output = self.run_cli("init", "--role", "worker")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["normalized_role"], "worker")

    def test_init_with_role_kharej_stores_normalized_worker_role(self) -> None:
        code, output = self.run_cli("init", "--role", "kharej")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["normalized_role"], "worker")

    def test_init_without_role_in_non_interactive_mode_fails_safely(self) -> None:
        code, output = self.run_cli("init")
        self.assertEqual(code, 1)
        self.assertIn("Role is required in non-interactive mode", output)

    @patch("pilottunnel.cli.input", side_effect=AssertionError("input should not be called"))
    def test_init_without_role_does_not_call_input_in_non_interactive_mode(self, _mock_input) -> None:
        code, output = self.run_cli("init")
        self.assertEqual(code, 1)
        self.assertIn("Role is required in non-interactive mode", output)

    def test_init_refuses_to_overwrite_role_without_force(self) -> None:
        self.run_cli("init", "--role", "controller")
        code, output = self.run_cli("init", "--role", "worker")
        self.assertEqual(code, 1)
        self.assertIn("Use --force", output)

    def test_init_force_changes_role_and_audits_it(self) -> None:
        self.run_cli("init", "--role", "controller")
        code, output = self.run_cli("init", "--force", "--role", "worker")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["normalized_role"], "worker")
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        init_role_events = [item for item in lines if item["action"] == "init_role"]
        self.assertEqual(init_role_events[-1]["details"]["old_role"], "controller")
        self.assertEqual(init_role_events[-1]["details"]["new_role"], "worker")
        self.assertTrue(init_role_events[-1]["details"]["force"])

    def test_node_status_shows_initialized_role(self) -> None:
        self.run_cli("init", "--role", "iran")
        code, output = self.run_cli("node", "status")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["initialized"])
        self.assertEqual(payload["normalized_role"], "controller")
        self.assertIn("switch", payload["allowed_actions"])

    def test_worker_role_blocks_controller_only_switch_action(self) -> None:
        self.run_cli("init", "--role", "controller")
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
        self.run_cli("init", "--force", "--role", "worker")
        code, output = self.run_cli("switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 1)
        self.assertIn("blocked for node role 'worker'", output)

    def test_controller_role_allows_switch_action(self) -> None:
        self.run_cli("init", "--role", "controller")
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
        code, output = self.run_cli("switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["ok"])

    def test_safe_inspect_commands_are_allowed_for_both_roles(self) -> None:
        self.run_cli("init", "--role", "controller")
        controller_code, _ = self.run_cli("adapter", "list")
        self.assertEqual(controller_code, 0)
        self.run_cli("init", "--force", "--role", "worker")
        worker_code, worker_output = self.run_cli("adapter", "list")
        self.assertEqual(worker_code, 0)
        self.assertTrue(json.loads(worker_output))

    def test_invalid_role_is_rejected(self) -> None:
        code, output = self.run_cli("init", "--role", "boss")
        self.assertEqual(code, 1)
        self.assertIn("Unsupported role", output)

    def test_duplicate_profile_create_is_blocked(self) -> None:
        self.run_cli("init", "--role", "controller")
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
        self.run_cli("init", "--role", "controller")
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

    def _stage_switch(self, adapter: str, transport: str) -> None:
        self.run_cli("--apply", "switch", "--profile", "turkey-6221", "--adapter", adapter, "--transport", transport)

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

    def test_tcp_healthcheck_succeeds_against_local_test_socket(self) -> None:
        server, port, _ = self._start_tcp_server()
        self.addCleanup(server.close)
        code, output = self.run_cli("healthcheck", "--host", "127.0.0.1", "--port", str(port))
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["port"], port)
        self.assertIsNotNone(payload["latency_ms"])

    def test_tcp_healthcheck_fails_safely_when_port_is_closed(self) -> None:
        code, output = self.run_cli("healthcheck", "--host", "127.0.0.1", "--port", "65534")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["error"])

    def test_healthcheck_json_output_is_valid(self) -> None:
        server, port, _ = self._start_tcp_server()
        self.addCleanup(server.close)
        code, output = self.run_cli("healthcheck", "--host", "127.0.0.1", "--port", str(port), "--json")
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["ok"])

    def test_healthcheck_direct_host_port_works(self) -> None:
        server, port, _ = self._start_tcp_server()
        self.addCleanup(server.close)
        code, output = self.run_cli("healthcheck", "--host", "127.0.0.1", "--port", str(port), "--timeout", "1")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["host"], "127.0.0.1")

    def test_profile_healthcheck_checks_expected_ports(self) -> None:
        self._create_profile()
        code, output = self.run_cli("healthcheck", "--profile", "turkey-6221", "--all")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        labels = {item["label"] for item in payload["results"]}
        self.assertIn("target", labels)
        self.assertTrue(any("port" in label for label in labels))

    def test_controller_role_healthcheck_includes_controller_side_checks(self) -> None:
        self.run_cli("init", "--role", "controller")
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
        code, output = self.run_cli("healthcheck", "--profile", "turkey-6221", "--role-aware")
        self.assertEqual(code, 1)
        labels = {item["label"] for item in json.loads(output)["results"]}
        self.assertIn("target", labels)
        self.assertIn("main_port", labels)

    def test_worker_role_healthcheck_includes_worker_side_checks(self) -> None:
        self.run_cli("init", "--role", "controller")
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
        self.run_cli("init", "--force", "--role", "worker")
        code, output = self.run_cli("healthcheck", "--profile", "turkey-6221", "--role-aware")
        self.assertEqual(code, 1)
        labels = {item["label"] for item in json.loads(output)["results"]}
        self.assertIn("worker_target_port", labels)
        self.assertIn("controller_endpoint", labels)

    def test_invalid_healthcheck_port_is_rejected(self) -> None:
        code, output = self.run_cli("healthcheck", "--host", "127.0.0.1", "--port", "70000")
        self.assertEqual(code, 1)
        self.assertIn("port must be between", output)

    @patch("pilottunnel.healthcheck.socket.create_connection", side_effect=TimeoutError("timed out"))
    def test_healthcheck_timeout_is_handled_safely(self, _mock_create_connection) -> None:
        code, output = self.run_cli("healthcheck", "--host", "127.0.0.1", "--port", "6553", "--timeout", "0.1")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])

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
        self.run_cli("init", "--role", "controller")
        code, output = self.run_cli("profile", "create", "--name", "../bad", "--main-port", "6221", "--target-port", "6221")
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)

    def test_audit_records_staged_only_true(self) -> None:
        self._create_profile()
        self.run_cli("--apply", "switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        lines = self.audit.read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[-1])
        self.assertTrue(payload["details"]["staged_only"])

    def test_service_plan_for_backhaul_start_is_read_only(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--action",
            "start",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["action"], "start")
        self.assertFalse(payload["real_systemd_touched"])
        self.assertIn("systemctl start pilottunnel-turkey-6221-backhaul-tcpmux-controller.service", payload["future_command"])
        self.assertIn("systemctl start pilottunnel-turkey-6221-backhaul-tcpmux-controller.service", payload["plan_steps"][0])

    def test_service_plan_for_rathole_stop_is_read_only(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "rathole",
            "--transport",
            "tcp",
            "--action",
            "stop",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["action"], "stop")
        self.assertIn("systemctl stop pilottunnel-turkey-6221-rathole-tcp-controller.service", payload["future_command"])

    def test_service_plan_enable_and_disable_are_supported(self) -> None:
        self._create_profile()
        enable_code, enable_output = self.run_cli(
            "service",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--action",
            "enable",
        )
        disable_code, disable_output = self.run_cli(
            "service",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--action",
            "disable",
        )
        self.assertEqual(enable_code, 0)
        self.assertEqual(disable_code, 0)
        self.assertIn("systemctl enable", json.loads(enable_output)["future_command"])
        self.assertIn("systemctl disable", json.loads(disable_output)["future_command"])

    def test_service_plan_audits_attempts(self) -> None:
        self._create_profile()
        code, _ = self.run_cli(
            "service",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--action",
            "restart",
        )
        self.assertEqual(code, 0)
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        service_events = [item for item in lines if item["action"] == "service-plan"]
        self.assertTrue(service_events)
        self.assertEqual(service_events[-1]["details"]["action"], "restart")

    def test_service_plan_rejects_unknown_adapter(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "missing",
            "--transport",
            "tcp",
            "--action",
            "start",
        )
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_service_plan_rejects_unsupported_backhaul_transport(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcptun",
            "--action",
            "start",
        )
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    def test_service_plan_blocks_path_traversal(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--action",
            "start",
            "--install-root",
            str(Path(self.temp_dir.name) / ".." / "escape"),
        )
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)

    def test_service_plan_respects_controller_and_worker_roles(self) -> None:
        self.run_cli("init", "--role", "controller")
        self.run_cli(
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
        controller_code, controller_output = self.run_cli(
            "service",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--action",
            "start",
        )
        self.assertEqual(controller_code, 0)
        self.assertEqual(json.loads(controller_output)["role"], "controller")
        self.run_cli("init", "--force", "--role", "worker")
        worker_code, worker_output = self.run_cli(
            "service",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--action",
            "start",
        )
        self.assertEqual(worker_code, 0)
        self.assertEqual(json.loads(worker_output)["role"], "worker")

    @patch("pilottunnel.service_lifecycle.platform.system", return_value="Windows")
    @patch("pilottunnel.service_lifecycle.subprocess.run")
    def test_service_status_is_read_only_and_windows_safe(self, mock_run, _mock_platform) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "status",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertIn("Windows host detected", payload["warning"])
        mock_run.assert_not_called()

    @patch("pilottunnel.service_lifecycle.platform.system", return_value="Windows")
    @patch("pilottunnel.service_lifecycle.subprocess.run")
    def test_service_logs_is_read_only_and_windows_safe(self, mock_run, _mock_platform) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "logs",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--limit",
            "10",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["limit"], 10)
        self.assertIn("Windows host detected", payload["warning"])
        mock_run.assert_not_called()

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

    def _import_binary(self, adapter: str, content: str = "binary") -> Path:
        source = self._make_binary_source(adapter, content)
        self.run_cli("binary", "import", "--adapter", adapter, "--source", str(source), "--version", "manual-v0.0.0")
        return source

    def _start_tcp_server(self) -> tuple[socket.socket, int, threading.Thread]:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]

        def run() -> None:
            try:
                while True:
                    conn, _ = server.accept()
                    conn.close()
            except OSError:
                return

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return server, port, thread

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

    def test_install_plan_for_backhaul_tcpmux_includes_expected_destinations(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        source = self._make_binary_source("backhaul")
        self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0")
        code, output = self.run_cli("install", "plan", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        destinations = {item["kind"]: item["path"] for item in payload["planned_destination_files"]}
        self.assertIn("/etc/pilottunnel/profiles/turkey-6221/backhaul/tcpmux/controller/backhaul-controller.toml", destinations["config"])
        self.assertIn("/etc/systemd/system/pilottunnel-turkey-6221-backhaul-tcpmux-controller.service", destinations["systemd_unit"])
        self.assertIn("/usr/local/bin/backhaul", destinations["binary"])

    def test_install_plan_for_rathole_tcp_includes_expected_destinations(self) -> None:
        self._create_profile()
        self._stage_switch("rathole", "tcp")
        source = self._make_binary_source("rathole")
        self.run_cli("binary", "import", "--adapter", "rathole", "--source", str(source), "--version", "manual-v0.0.0")
        code, output = self.run_cli("install", "plan", "--profile", "turkey-6221", "--adapter", "rathole", "--transport", "tcp")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        destinations = {item["kind"]: item["path"] for item in payload["planned_destination_files"]}
        self.assertIn("/etc/pilottunnel/profiles/turkey-6221/rathole/tcp/controller/rathole-controller.toml", destinations["config"])
        self.assertIn("/etc/systemd/system/pilottunnel-turkey-6221-rathole-tcp-controller.service", destinations["systemd_unit"])
        self.assertIn("/usr/local/bin/rathole", destinations["binary"])

    def test_install_plan_does_not_touch_real_system_paths(self) -> None:
        self._create_profile()
        code, output = self.run_cli("install", "plan", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["service_started"])
        self.assertFalse(Path("/etc/systemd/system").joinpath("pilottunnel-turkey-6221-backhaul-tcpmux-controller.service").exists())

    def test_install_plan_with_test_install_root_stays_inside_temp_root(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        install_root = Path(self.temp_dir.name) / "install-root"
        code, output = self.run_cli(
            "install",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        for item in payload["planned_destination_files"]:
            self.assertTrue(Path(item["path"]).resolve().is_relative_to(install_root.resolve()))

    def test_install_plan_reports_missing_staged_files_with_warnings(self) -> None:
        self._create_profile()
        code, output = self.run_cli("install", "plan", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(any("run staged apply first" in item.lower() for item in payload["warnings"]))

    def test_install_plan_reports_imported_binary_status(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        source = self._make_binary_source("backhaul")
        self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0")
        code, output = self.run_cli("install", "plan", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["binary"]["imported_binary_exists"])

    def test_uninstall_plan_includes_service_and_file_cleanup_steps(self) -> None:
        self._create_profile()
        code, output = self.run_cli("uninstall", "plan", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("pilottunnel-turkey-6221-backhaul-tcpmux-controller.service", payload["services_that_would_be_stopped_disabled"][0])
        self.assertTrue(any(path.endswith("backhaul-controller.toml") for path in payload["files_that_would_be_removed"]))

    def test_uninstall_plan_does_not_stop_real_services(self) -> None:
        self._create_profile()
        code, output = self.run_cli("uninstall", "plan", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["service_stopped"])

    def test_install_plan_path_traversal_is_blocked(self) -> None:
        self._create_profile()
        code, output = self.run_cli("install", "plan", "--profile", "../bad", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)
        code, output = self.run_cli(
            "install",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(Path(self.temp_dir.name) / ".." / "escape"),
        )
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)

    def test_install_plan_blocks_unsupported_backhaul_experimental_transport(self) -> None:
        self._create_profile()
        code, output = self.run_cli("install", "plan", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcptun")
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    def test_install_plan_rejects_unknown_adapter(self) -> None:
        self._create_profile()
        code, output = self.run_cli("install", "plan", "--profile", "turkey-6221", "--adapter", "missing", "--transport", "tcp")
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_install_plan_json_output_is_valid(self) -> None:
        self._create_profile()
        code, output = self.run_cli("install", "plan", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["action"], "install-plan")

    def test_install_apply_refuses_without_confirm_apply(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(Path(self.temp_dir.name) / "install-root"),
        )
        self.assertEqual(code, 1)
        self.assertIn("--confirm APPLY", output)

    def test_require_healthcheck_blocks_install_apply_when_check_fails(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
            "--require-healthcheck",
        )
        self.assertEqual(code, 1)
        self.assertIn("Healthcheck requirement failed", output)
        self.assertFalse((install_root / "var" / "lib" / "pilottunnel" / "apply-manifests").exists())

    @patch("pilottunnel.install_plan.run_profile_healthchecks")
    def test_require_healthcheck_allows_install_apply_when_mocked_check_passes(self, mock_run_profile_healthchecks) -> None:
        mock_run_profile_healthchecks.return_value = [
            {
                "ok": True,
                "host": "127.0.0.1",
                "port": 6221,
                "timeout": 2.0,
                "latency_ms": 1.0,
                "error": "",
                "checked_at": "now",
                "role": "controller",
                "profile": "turkey-6221",
                "label": "target",
            }
        ]
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
            "--require-healthcheck",
        )
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output)["healthcheck"]["ok"])

    def test_install_apply_refuses_without_install_root(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--confirm",
            "APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("--install-root", output)

    def test_install_apply_refuses_dangerous_install_root(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        dangerous_root = Path(self.temp_dir.name).resolve().anchor
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            dangerous_root,
            "--confirm",
            "APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("dangerous install-root", output)

    def test_install_apply_copies_backhaul_staged_files_into_temp_install_root(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue((install_root / "etc" / "pilottunnel" / "profiles" / "turkey-6221" / "backhaul" / "tcpmux" / "controller" / "backhaul-controller.toml").exists())
        self.assertTrue((install_root / "etc" / "systemd" / "system" / "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service").exists())
        self.assertTrue((install_root / "usr" / "local" / "bin" / "backhaul.exe").exists() or (install_root / "usr" / "local" / "bin" / "backhaul").exists())
        self.assertFalse(payload["real_systemd_touched"])

    def test_install_apply_copies_rathole_staged_files_into_temp_install_root(self) -> None:
        self._create_profile()
        self._stage_switch("rathole", "tcp")
        self._import_binary("rathole")
        install_root = Path(self.temp_dir.name) / "install-root"
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "rathole",
            "--transport",
            "tcp",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        self.assertEqual(code, 0)
        self.assertTrue((install_root / "etc" / "pilottunnel" / "profiles" / "turkey-6221" / "rathole" / "tcp" / "controller" / "rathole-controller.toml").exists())

    def test_install_apply_creates_backup_before_overwrite(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        target = install_root / "etc" / "pilottunnel" / "profiles" / "turkey-6221" / "backhaul" / "tcpmux" / "controller" / "backhaul-controller.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old-config", encoding="utf-8")
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(any(item["target"].endswith("backhaul-controller.toml") for item in payload["backups_created"]))
        self.assertEqual(target.with_name(target.name + ".bak.planned").read_text(encoding="utf-8"), "old-config")

    def test_install_apply_writes_manifest(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        manifest_path = Path(payload["manifest_path"])
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["profile"], "turkey-6221")
        self.assertFalse(manifest["service_started"])

    def test_install_apply_does_not_call_systemctl(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["service_started"])
        self.assertFalse(payload["firewall_touched"])
        self.assertFalse(payload["routes_touched"])

    def test_install_apply_does_not_touch_real_system_paths(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        self.assertEqual(code, 0)
        self.assertFalse(Path("/etc/systemd/system").joinpath("pilottunnel-turkey-6221-backhaul-tcpmux-controller.service").exists())

    def test_install_rollback_refuses_without_confirm_rollback(self) -> None:
        self._create_profile()
        code, output = self.run_cli("install", "rollback", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux", "--install-root", str(Path(self.temp_dir.name) / "install-root"))
        self.assertEqual(code, 1)
        self.assertIn("--confirm ROLLBACK", output)

    def test_install_rollback_restores_backup_and_removes_new_files(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        config_target = install_root / "etc" / "pilottunnel" / "profiles" / "turkey-6221" / "backhaul" / "tcpmux" / "controller" / "backhaul-controller.toml"
        config_target.parent.mkdir(parents=True, exist_ok=True)
        config_target.write_text("old-config", encoding="utf-8")
        self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        unit_target = install_root / "etc" / "systemd" / "system" / "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"
        self.assertTrue(unit_target.exists())
        code, output = self.run_cli(
            "install",
            "rollback",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "ROLLBACK",
        )
        self.assertEqual(code, 0)
        self.assertEqual(config_target.read_text(encoding="utf-8"), "old-config")
        self.assertFalse(unit_target.exists())

    def test_uninstall_apply_refuses_without_confirm_uninstall(self) -> None:
        self._create_profile()
        code, output = self.run_cli("uninstall", "apply", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux", "--install-root", str(Path(self.temp_dir.name) / "install-root"))
        self.assertEqual(code, 1)
        self.assertIn("--confirm UNINSTALL", output)

    def test_uninstall_apply_backs_up_and_removes_only_pilottunnel_owned_files(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        extra = install_root / "etc" / "pilottunnel" / "profiles" / "unowned.txt"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("keep", encoding="utf-8")
        code, output = self.run_cli(
            "uninstall",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "UNINSTALL",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["removed_files"])
        self.assertTrue(extra.exists())
        self.assertTrue(any(item["backup"].endswith(".bak.planned") for item in payload["backups_created"]))

    def test_apply_fails_safely_when_staged_files_missing(self) -> None:
        self._create_profile()
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("staged files are missing", output.lower())

    def test_install_apply_path_traversal_is_blocked(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "install",
            "apply",
            "--profile",
            "../bad",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(Path(self.temp_dir.name) / "install-root"),
            "--confirm",
            "APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)

    def test_audit_records_apply_rollback_uninstall_attempts(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        self.run_cli(
            "install",
            "rollback",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "ROLLBACK",
        )
        self.run_cli(
            "install",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "APPLY",
        )
        self.run_cli(
            "uninstall",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--install-root",
            str(install_root),
            "--confirm",
            "UNINSTALL",
        )
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        actions = [item["action"] for item in lines]
        self.assertIn("install-apply", actions)
        self.assertIn("install-rollback", actions)
        self.assertIn("uninstall-apply", actions)

    def test_audit_records_healthcheck_failure(self) -> None:
        code, output = self.run_cli("healthcheck", "--host", "127.0.0.1", "--port", "65534")
        self.assertEqual(code, 1)
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines[-1]["action"], "healthcheck")
        self.assertEqual(lines[-1]["details"]["result"], "failed")
