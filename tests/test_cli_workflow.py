import io
import json
import socket
import subprocess
import tempfile
import threading
import unittest
from contextlib import ExitStack, redirect_stdout
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

    def _create_profile_with_ports(
        self,
        *,
        name: str,
        main_port: int,
        target_port: int,
        control_port: int,
        service_port: int,
        check_port: int,
        role: str = "controller",
        target_host: str = "127.0.0.1",
    ) -> None:
        self.run_cli("init", "--role", "controller")
        self.run_cli(
            "profile",
            "create",
            "--name",
            name,
            "--main-port",
            str(main_port),
            "--target-host",
            target_host,
            "--target-port",
            str(target_port),
            "--role",
            role,
            "--control-port",
            str(control_port),
            "--service-port",
            str(service_port),
            "--check-port",
            str(check_port),
        )

    def _export_worker_bundle(self, adapter: str, transport: str, *, force: bool = False, include_staged_paths: bool = False) -> Path:
        bundle_path = Path(self.temp_dir.name) / f"{adapter}-{transport}-worker.json"
        args = [
            "bundle",
            "export-worker",
            "--profile",
            "turkey-6221",
            "--adapter",
            adapter,
            "--transport",
            transport,
            "--output",
            str(bundle_path),
        ]
        if include_staged_paths:
            args.append("--include-staged-paths")
        if force:
            args.append("--force")
        code, output = self.run_cli(*args)
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(bundle_path.exists())
        return bundle_path

    def _simulate_e2e(self, adapter: str, transport: str, *, base_root: Path | None = None, keep_files: bool = False, profile: str = "turkey-6221") -> tuple[int, str, dict]:
        args = [
            "simulate",
            "e2e",
            "--profile",
            profile,
            "--adapter",
            adapter,
            "--transport",
            transport,
        ]
        if base_root is not None:
            args.extend(["--base-root", str(base_root)])
        if keep_files:
            args.append("--keep-files")
        code, output = self.run_cli(*args)
        payload = json.loads(output)
        return code, output, payload

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
            "--real-systemd",
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
            "--real-systemd",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["limit"], 10)
        self.assertIn("Windows host detected", payload["warning"])
        mock_run.assert_not_called()

    def test_service_daemon_reload_refuses_without_real_systemd(self) -> None:
        code, output = self.run_cli("service", "daemon-reload", "--confirm", "DAEMON_RELOAD")
        self.assertEqual(code, 1)
        self.assertIn("--real-systemd", output)

    def test_service_daemon_reload_refuses_without_confirm_daemon_reload(self) -> None:
        with self._mock_real_systemd():
            code, output = self.run_cli("service", "daemon-reload", "--real-systemd")
        self.assertEqual(code, 1)
        self.assertIn("DAEMON_RELOAD", output)

    def test_service_daemon_reload_refuses_on_windows_mock(self) -> None:
        with self._mock_real_systemd(is_linux=False):
            code, output = self.run_cli("service", "daemon-reload", "--real-systemd", "--confirm", "DAEMON_RELOAD")
        self.assertEqual(code, 1)
        self.assertIn("Linux-only", output)

    def test_service_daemon_reload_refuses_when_systemd_unavailable(self) -> None:
        with self._mock_real_systemd(systemctl_available=False):
            code, output = self.run_cli("service", "daemon-reload", "--real-systemd", "--confirm", "DAEMON_RELOAD")
        self.assertEqual(code, 1)
        self.assertIn("unavailable", output)

    def test_service_daemon_reload_executes_only_mocked_daemon_reload_when_gates_pass(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli("service", "daemon-reload", "--real-systemd", "--confirm", "DAEMON_RELOAD")
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(calls, [["systemctl", "daemon-reload"]])
        self.assertTrue(payload["daemon_reload_executed"])
        self.assertTrue(payload["real_systemd_touched"])

    def test_service_daemon_reload_does_not_start_stop_enable_disable_services(self) -> None:
        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli("service", "daemon-reload", "--real-systemd", "--confirm", "DAEMON_RELOAD")
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertFalse(payload["service_started"])
        self.assertFalse(payload["service_stopped"])
        self.assertFalse(payload["service_enabled"])
        self.assertFalse(payload["service_disabled"])

    def test_service_status_real_systemd_is_read_only_and_timeout_safe(self) -> None:
        self._create_profile()

        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 2.0), output="partial", stderr="timed out")

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "status",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["timed_out"])
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["service_started"])

    def test_service_logs_real_systemd_is_read_only_and_timeout_safe(self) -> None:
        self._create_profile()

        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 2.0), output="partial", stderr="timed out")

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "logs",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["timed_out"])
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["service_enabled"])

    def test_service_status_and_logs_sanitize_output(self) -> None:
        self._create_profile()
        responses = [
            type("Completed", (), {"returncode": 0, "stdout": "status\x00line\r\n", "stderr": "warn\r\n"})(),
            type("Completed", (), {"returncode": 0, "stdout": "active\r\n", "stderr": ""})(),
            type("Completed", (), {"returncode": 0, "stdout": "enabled\r\n", "stderr": ""})(),
            type("Completed", (), {"returncode": 0, "stdout": "log\x00entry\r\nnext", "stderr": "err\r\n"})(),
        ]

        def fake_run(command, **kwargs):
            return responses.pop(0)

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            status_code, status_output = self.run_cli(
                "service",
                "status",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
            logs_code, logs_output = self.run_cli(
                "service",
                "logs",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(status_code, 0)
        self.assertEqual(logs_code, 0)
        status_payload = json.loads(status_output)
        logs_payload = json.loads(logs_output)
        self.assertEqual(status_payload["stdout"], "statusline")
        self.assertEqual(status_payload["stderr"], "warn")
        self.assertEqual(status_payload["is_active"], "active")
        self.assertEqual(status_payload["is_enabled"], "enabled")
        self.assertEqual(logs_payload["entries"], ["logentry", "next"])
        self.assertEqual(logs_payload["stderr"], "err")

    def test_service_status_and_logs_return_warnings_when_systemd_unavailable(self) -> None:
        self._create_profile()
        with self._mock_real_systemd(systemctl_available=False, journalctl_available=False):
            status_code, status_output = self.run_cli(
                "service",
                "status",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
            logs_code, logs_output = self.run_cli(
                "service",
                "logs",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(status_code, 0)
        self.assertEqual(logs_code, 0)
        self.assertIn("unavailable", json.loads(status_output)["warning"])
        self.assertIn("unavailable", json.loads(logs_output)["warning"])

    def test_service_status_unknown_adapter_rejected(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "status",
            "--profile",
            "turkey-6221",
            "--adapter",
            "missing",
            "--transport",
            "tcp",
            "--real-systemd",
        )
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_service_status_unsupported_backhaul_experimental_transport_rejected(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "status",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcptun",
            "--real-systemd",
        )
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    def test_real_systemd_status_role_aware_service_names_still_correct_for_controller_worker(self) -> None:
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

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            controller_code, controller_output = self.run_cli(
                "service",
                "status",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(controller_code, 0)
        self.assertTrue(json.loads(controller_output)["service_name"].endswith("controller.service"))

        self.run_cli("init", "--force", "--role", "worker")
        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            worker_code, worker_output = self.run_cli(
                "service",
                "status",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(worker_code, 0)
        self.assertTrue(json.loads(worker_output)["service_name"].endswith("worker.service"))

    def test_audit_records_daemon_reload_status_and_logs_attempts(self) -> None:
        self._create_profile()

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            self.run_cli(
                "service",
                "status",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
            self.run_cli(
                "service",
                "logs",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
            self.run_cli("service", "daemon-reload", "--real-systemd", "--confirm", "DAEMON_RELOAD")
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        actions = [item["action"] for item in lines]
        self.assertIn("service-status", actions)
        self.assertIn("service-logs", actions)
        self.assertIn("service-daemon-reload", actions)

    def test_service_start_refuses_without_real_systemd(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "start",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--confirm",
            "START_SERVICE",
        )
        self.assertEqual(code, 1)
        self.assertIn("--real-systemd", output)

    def test_service_start_refuses_without_confirm_start_service(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(code, 1)
        self.assertIn("START_SERVICE", output)

    def test_service_start_refuses_on_windows_mock_non_linux(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(is_linux=False):
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("Linux-only", output)

    def test_service_start_refuses_when_systemd_unavailable(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(systemctl_available=False):
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("unavailable", output)

    def test_service_start_refuses_without_root_mock(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(is_root=False):
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("root/admin", output)

    def test_service_start_refuses_if_unit_file_missing(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("unit is missing", output)

    def test_service_start_refuses_if_unit_is_not_pilottunnel_owned(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit(owned=False)
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("not marked as PilotTunnel-owned", output)

    def test_service_start_executes_only_mocked_systemctl_start_when_gates_pass(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(calls[0], ["systemctl", "start", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"])
        self.assertTrue(payload["service_started"])
        self.assertTrue(payload["real_systemd_touched"])

    def test_service_start_does_not_call_enable_disable_restart_stop(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(all(command[1] not in {"enable", "disable", "restart", "stop"} for command in calls))

    def test_service_start_runs_read_only_status_and_is_active_after_successful_start(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertIn(["systemctl", "is-active", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"], calls)
        self.assertIn(["systemctl", "status", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service", "--no-pager"], calls)
        self.assertEqual(payload["status"]["is_active"], "active")
        self.assertTrue(payload["status"]["read_only"])

    @patch("pilottunnel.service_lifecycle.run_profile_healthchecks")
    def test_service_start_with_require_healthcheck_reports_failed_healthcheck_without_stopping_service(self, mock_run_profile_healthchecks) -> None:
        mock_run_profile_healthchecks.return_value = [
            {
                "ok": False,
                "host": "127.0.0.1",
                "port": 6221,
                "timeout": 2.0,
                "latency_ms": None,
                "error": "failed",
                "checked_at": "now",
                "role": "controller",
                "profile": "turkey-6221",
                "label": "target",
            }
        ]
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
                "--require-healthcheck",
            )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["healthcheck_ok"])
        self.assertTrue(payload["service_started"])
        self.assertTrue(all(command[1] != "stop" for command in calls))

    @patch("pilottunnel.service_lifecycle.run_profile_healthchecks")
    def test_service_start_with_passing_mocked_healthcheck_reports_healthcheck_ok_true(self, mock_run_profile_healthchecks) -> None:
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
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
                "--require-healthcheck",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(json.loads(output)["healthcheck_ok"])

    def test_service_restart_refuses_without_real_systemd(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "restart",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--confirm",
            "RESTART_SERVICE",
        )
        self.assertEqual(code, 1)
        self.assertIn("--real-systemd", output)

    def test_service_restart_requires_initialized_node_role(self) -> None:
        self.run_cli(
            "profile",
            "create",
            "--name",
            "turkey-6221",
            "--main-port",
            "6221",
            "--target-port",
            "6221",
        )
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("initialized node role", output)

    def test_service_restart_refuses_without_confirm_restart_service(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(code, 1)
        self.assertIn("RESTART_SERVICE", output)

    def test_service_restart_refuses_on_windows_mock_non_linux(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(is_linux=False):
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("Linux-only", output)

    def test_service_restart_refuses_when_systemd_unavailable(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(systemctl_available=False):
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("unavailable", output)

    def test_service_restart_refuses_without_root_mock(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(is_root=False):
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("root/admin", output)

    def test_service_restart_refuses_if_unit_file_missing(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("unit is missing", output)

    def test_service_restart_refuses_if_unit_is_not_pilottunnel_owned(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit(owned=False)
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("not marked as PilotTunnel-owned", output)

    def test_service_restart_executes_only_mocked_systemctl_restart_when_gates_pass(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(calls[0], ["systemctl", "restart", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"])
        self.assertTrue(payload["service_restarted"])
        self.assertTrue(payload["real_systemd_touched"])

    def test_service_restart_does_not_call_enable_disable_start_stop_separately(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(calls[0][1], "restart")
        self.assertTrue(all(command[1] not in {"enable", "disable", "start", "stop"} for command in calls[1:]))

    def test_service_restart_runs_read_only_is_active_is_enabled_status_after_success(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                if command[1] == "is-enabled":
                    stdout = "enabled"
                elif command[1] == "is-active":
                    stdout = "active"
                else:
                    stdout = "unit loaded"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertIn(["systemctl", "is-enabled", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"], calls)
        self.assertIn(["systemctl", "is-active", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"], calls)
        self.assertIn(["systemctl", "status", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service", "--no-pager"], calls)
        self.assertEqual(payload["status"]["is_enabled"], "enabled")
        self.assertEqual(payload["status"]["is_active"], "active")
        self.assertTrue(payload["status"]["read_only"])

    @patch("pilottunnel.service_lifecycle.run_profile_healthchecks")
    def test_service_restart_with_require_healthcheck_reports_failed_healthcheck_without_another_restart(self, mock_run_profile_healthchecks) -> None:
        mock_run_profile_healthchecks.return_value = [
            {
                "ok": False,
                "host": "127.0.0.1",
                "port": 6221,
                "timeout": 2.0,
                "latency_ms": None,
                "error": "failed",
                "checked_at": "now",
                "role": "controller",
                "profile": "turkey-6221",
                "label": "target",
            }
        ]
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
                "--require-healthcheck",
            )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["healthcheck_ok"])
        self.assertTrue(payload["service_restarted"])
        self.assertEqual(sum(1 for command in calls if command[1] == "restart"), 1)

    @patch("pilottunnel.service_lifecycle.run_profile_healthchecks")
    def test_service_restart_with_passing_mocked_healthcheck_reports_healthcheck_ok_true(self, mock_run_profile_healthchecks) -> None:
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
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
                "--require-healthcheck",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(json.loads(output)["healthcheck_ok"])

    def test_service_restart_unknown_adapter_rejected(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "missing",
                "--transport",
                "tcp",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_service_restart_unsupported_backhaul_experimental_transport_rejected(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcptun",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    def test_service_restart_role_aware_service_names_correct_for_controller_worker(self) -> None:
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
        self._prepare_real_systemd_unit(role="controller")

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "enabled" if command[1] == "is-enabled" else "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            controller_code, controller_output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(controller_code, 0)
        self.assertTrue(json.loads(controller_output)["service_name"].endswith("controller.service"))

        self.run_cli("init", "--force", "--role", "worker")
        self._prepare_real_systemd_unit(role="worker")
        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            worker_code, worker_output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        self.assertEqual(worker_code, 0)
        self.assertTrue(json.loads(worker_output)["service_name"].endswith("worker.service"))

    def test_audit_records_service_restart_attempts_success_and_failure(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()

        def fake_run_success(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "enabled" if command[1] == "is-enabled" else "active"
                stderr = ""

            return Completed()

        def fake_run_fail(command, **kwargs):
            class Completed:
                returncode = 1
                stdout = ""
                stderr = "failed"

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run_success):
            self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        with self._mock_real_systemd(subprocess_side_effect=fake_run_fail):
            self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
            )
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        actions = [item["action"] for item in lines]
        self.assertIn("service-restart", actions)

    def test_service_restart_json_output_is_valid(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "enabled" if command[1] == "is-enabled" else "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "restart",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "RESTART_SERVICE",
                "--json",
            )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["service_restarted"])

    @patch("pilottunnel.deploy.build_readiness_report")
    def test_deploy_plan_is_read_only(self, mock_readiness) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        mock_readiness.return_value = self._readiness_ok()
        code, output = self.run_cli(
            "deploy",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["plan_only"])
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["service_started"])
        self.assertFalse(payload["service_enabled"])
        self.assertFalse(payload["firewall_touched"])
        self.assertFalse(payload["routes_touched"])
        self.assertFalse(payload["downloads_performed"])

    @patch("pilottunnel.deploy.build_readiness_report")
    def test_deploy_plan_includes_readiness_install_daemon_reload_start_healthcheck_optional_enable_steps(self, mock_readiness) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        mock_readiness.return_value = self._readiness_ok()
        code, output = self.run_cli(
            "deploy",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--enable-after-start",
            "--require-healthcheck",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        step_names = [item["name"] for item in payload["steps"]]
        self.assertEqual(
            step_names,
            [
                "readiness_report",
                "staged_file_check",
                "binary_imported_check",
                "install_apply",
                "daemon_reload",
                "service_start",
                "healthcheck",
                "service_enable",
            ],
        )
        self.assertTrue(any("deploy plan" not in command and "install apply" in command for command in payload["exact_commands"]))

    def test_deploy_apply_refuses_without_real_host(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "deploy",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--confirm",
            "DEPLOY_APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("--real-host", output)

    def test_deploy_apply_refuses_without_confirm_deploy_apply(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "deploy",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-host",
        )
        self.assertEqual(code, 1)
        self.assertIn("DEPLOY_APPLY", output)

    @patch("pilottunnel.deploy._is_linux_host", return_value=False)
    def test_deploy_apply_refuses_on_windows_mock_non_linux(self, _mock_linux) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "deploy",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-host",
            "--confirm",
            "DEPLOY_APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("Linux-only", output)

    @patch("pilottunnel.deploy._is_admin_or_root", return_value=False)
    @patch("pilottunnel.deploy._is_linux_host", return_value=True)
    def test_deploy_apply_refuses_without_root_mock(self, _mock_linux, _mock_root) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "deploy",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-host",
            "--confirm",
            "DEPLOY_APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("root/admin", output)

    @patch("pilottunnel.deploy.build_readiness_report")
    @patch("pilottunnel.deploy._is_admin_or_root", return_value=True)
    @patch("pilottunnel.deploy._is_linux_host", return_value=True)
    def test_deploy_apply_refuses_when_readiness_blocked(self, _mock_linux, _mock_root, mock_readiness) -> None:
        self._create_profile()
        mock_readiness.return_value = {"readiness_level": "blocked", "blockers": ["blocked"], "staged_files_exist": False, "binary_imported": False}
        code, output = self.run_cli(
            "deploy",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-host",
            "--confirm",
            "DEPLOY_APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("readiness", output.lower())

    @patch("pilottunnel.deploy.verify_service_ownership")
    @patch("pilottunnel.deploy.apply_install")
    @patch("pilottunnel.deploy.build_readiness_report")
    @patch("pilottunnel.deploy._is_admin_or_root", return_value=True)
    @patch("pilottunnel.deploy._is_linux_host", return_value=True)
    def test_deploy_apply_stops_if_install_apply_fails(self, _mock_linux, _mock_root, mock_readiness, mock_install, mock_ownership) -> None:
        self._create_profile()
        mock_readiness.return_value = self._readiness_ok()
        mock_install.return_value = {"ok": False, "message": "install failed"}
        mock_ownership.return_value = {"ok": True}
        with patch("pilottunnel.deploy.run_daemon_reload") as mock_reload, patch("pilottunnel.deploy.start_service") as mock_start:
            code, output = self.run_cli(
                "deploy",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host",
                "--confirm",
                "DEPLOY_APPLY",
            )
        self.assertEqual(code, 1)
        self.assertIn("install failed", output)
        mock_reload.assert_not_called()
        mock_start.assert_not_called()

    @patch("pilottunnel.deploy.verify_service_ownership")
    @patch("pilottunnel.deploy.run_daemon_reload")
    @patch("pilottunnel.deploy.apply_install")
    @patch("pilottunnel.deploy.build_readiness_report")
    @patch("pilottunnel.deploy._is_admin_or_root", return_value=True)
    @patch("pilottunnel.deploy._is_linux_host", return_value=True)
    def test_deploy_apply_stops_if_daemon_reload_fails(self, _mock_linux, _mock_root, mock_readiness, mock_install, mock_reload, mock_ownership) -> None:
        self._create_profile()
        mock_readiness.return_value = self._readiness_ok()
        mock_install.return_value = {"ok": True, "manifest_path": "x"}
        mock_ownership.return_value = {"ok": True, "message": "owned"}
        mock_reload.return_value = {"ok": False, "message": "reload failed"}
        with patch("pilottunnel.deploy.start_service") as mock_start:
            code, output = self.run_cli(
                "deploy",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host",
                "--confirm",
                "DEPLOY_APPLY",
            )
        self.assertEqual(code, 1)
        self.assertIn("reload failed", output)
        mock_start.assert_not_called()

    @patch("pilottunnel.deploy.verify_service_ownership")
    @patch("pilottunnel.deploy.start_service")
    @patch("pilottunnel.deploy.run_daemon_reload")
    @patch("pilottunnel.deploy.apply_install")
    @patch("pilottunnel.deploy.build_readiness_report")
    @patch("pilottunnel.deploy._is_admin_or_root", return_value=True)
    @patch("pilottunnel.deploy._is_linux_host", return_value=True)
    def test_deploy_apply_stops_if_service_start_fails(self, _mock_linux, _mock_root, mock_readiness, mock_install, mock_reload, mock_start, mock_ownership) -> None:
        self._create_profile()
        mock_readiness.return_value = self._readiness_ok()
        mock_install.return_value = {"ok": True, "manifest_path": "x"}
        mock_ownership.return_value = {"ok": True, "message": "owned"}
        mock_reload.return_value = {"ok": True}
        mock_start.return_value = {"ok": False, "message": "start failed", "service_started": False}
        with patch("pilottunnel.deploy.enable_service") as mock_enable:
            code, output = self.run_cli(
                "deploy",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host",
                "--confirm",
                "DEPLOY_APPLY",
            )
        self.assertEqual(code, 1)
        self.assertIn("start failed", output)
        mock_enable.assert_not_called()

    @patch("pilottunnel.deploy.verify_service_ownership")
    @patch("pilottunnel.deploy.start_service")
    @patch("pilottunnel.deploy.run_daemon_reload")
    @patch("pilottunnel.deploy.apply_install")
    @patch("pilottunnel.deploy.build_readiness_report")
    @patch("pilottunnel.deploy._is_admin_or_root", return_value=True)
    @patch("pilottunnel.deploy._is_linux_host", return_value=True)
    def test_deploy_apply_does_not_enable_when_healthcheck_fails(self, _mock_linux, _mock_root, mock_readiness, mock_install, mock_reload, mock_start, mock_ownership) -> None:
        self._create_profile()
        mock_readiness.return_value = self._readiness_ok()
        mock_install.return_value = {"ok": True}
        mock_ownership.return_value = {"ok": True, "message": "owned"}
        mock_reload.return_value = {"ok": True}
        mock_start.return_value = {
            "ok": False,
            "message": "Service started but healthcheck failed; review service status and logs manually",
            "service_started": True,
            "healthcheck_ok": False,
            "status": {"is_active": "active"},
        }
        with patch("pilottunnel.deploy.enable_service") as mock_enable:
            code, output = self.run_cli(
                "deploy",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host",
                "--confirm",
                "DEPLOY_APPLY",
                "--require-healthcheck",
                "--enable-after-start",
            )
        self.assertEqual(code, 1)
        self.assertIn("healthcheck failed", output.lower())
        mock_enable.assert_not_called()

    @patch("pilottunnel.deploy.verify_service_ownership")
    @patch("pilottunnel.deploy.enable_service")
    @patch("pilottunnel.deploy.start_service")
    @patch("pilottunnel.deploy.run_daemon_reload")
    @patch("pilottunnel.deploy.apply_install")
    @patch("pilottunnel.deploy.build_readiness_report")
    @patch("pilottunnel.deploy._is_admin_or_root", return_value=True)
    @patch("pilottunnel.deploy._is_linux_host", return_value=True)
    def test_deploy_apply_enables_only_when_enable_after_start_is_set_and_earlier_steps_pass(self, _mock_linux, _mock_root, mock_readiness, mock_install, mock_reload, mock_start, mock_enable, mock_ownership) -> None:
        self._create_profile()
        mock_readiness.return_value = self._readiness_ok()
        mock_install.return_value = {"ok": True}
        mock_ownership.return_value = {"ok": True, "message": "owned"}
        mock_reload.return_value = {"ok": True}
        mock_start.return_value = {"ok": True, "service_started": True, "healthcheck_ok": True, "status": {"is_active": "active"}}
        mock_enable.return_value = {"ok": True, "service_enabled": True, "status": {"is_enabled": "enabled"}}

        code_without, _ = self.run_cli(
            "deploy",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-host",
            "--confirm",
            "DEPLOY_APPLY",
        )
        self.assertEqual(code_without, 0)
        mock_enable.assert_not_called()

        code_with, output_with = self.run_cli(
            "deploy",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-host",
            "--confirm",
            "DEPLOY_APPLY",
            "--enable-after-start",
        )
        self.assertEqual(code_with, 0, msg=output_with)
        self.assertTrue(json.loads(output_with)["service_enabled"])
        mock_enable.assert_called_once()

    @patch("pilottunnel.service_lifecycle.restart_service")
    @patch("pilottunnel.deploy.verify_service_ownership")
    @patch("pilottunnel.deploy.start_service")
    @patch("pilottunnel.deploy.run_daemon_reload")
    @patch("pilottunnel.deploy.apply_install")
    @patch("pilottunnel.deploy.build_readiness_report")
    @patch("pilottunnel.deploy._is_admin_or_root", return_value=True)
    @patch("pilottunnel.deploy._is_linux_host", return_value=True)
    def test_deploy_apply_does_not_call_firewall_routes_interfaces_downloads_or_restart(self, _mock_linux, _mock_root, mock_readiness, mock_install, mock_reload, mock_start, mock_ownership, mock_restart) -> None:
        self._create_profile()
        mock_readiness.return_value = self._readiness_ok()
        mock_install.return_value = {"ok": True}
        mock_ownership.return_value = {"ok": True, "message": "owned"}
        mock_reload.return_value = {"ok": True}
        mock_start.return_value = {"ok": True, "service_started": True, "healthcheck_ok": "skipped", "status": {"is_active": "active"}}
        code, output = self.run_cli(
            "deploy",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-host",
            "--confirm",
            "DEPLOY_APPLY",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertFalse(payload["firewall_touched"])
        self.assertFalse(payload["routes_touched"])
        self.assertFalse(payload["downloads_performed"])
        mock_restart.assert_not_called()

    @patch("pilottunnel.deploy.run_profile_healthchecks")
    @patch("pilottunnel.deploy.inspect_service_status")
    @patch("pilottunnel.deploy.build_readiness_report")
    def test_deploy_status_is_read_only(self, mock_readiness, mock_status, mock_healthchecks) -> None:
        self._create_profile()
        mock_readiness.return_value = self._readiness_ok()
        mock_status.return_value = {"ok": True, "read_only": True, "is_active": "active", "is_enabled": "enabled", "unit_path": "/etc/systemd/system/pilot.service"}
        mock_healthchecks.return_value = [
            {"ok": True, "host": "127.0.0.1", "port": 6221, "timeout": 2.0, "latency_ms": 1.0, "error": "", "checked_at": "now", "role": "controller", "profile": "turkey-6221", "label": "target"}
        ]
        code, output = self.run_cli(
            "deploy",
            "status",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-systemd",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["read_only"])
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["firewall_touched"])
        self.assertFalse(payload["routes_touched"])

    @patch("pilottunnel.deploy.run_profile_healthchecks")
    @patch("pilottunnel.deploy.inspect_service_status")
    @patch("pilottunnel.deploy.build_readiness_report")
    def test_deploy_status_json_output_is_valid(self, mock_readiness, mock_status, mock_healthchecks) -> None:
        self._create_profile()
        mock_readiness.return_value = self._readiness_ok()
        mock_status.return_value = {"ok": True, "read_only": True, "is_active": "active", "is_enabled": "enabled", "unit_path": "/etc/systemd/system/pilot.service"}
        mock_healthchecks.return_value = [
            {"ok": True, "host": "127.0.0.1", "port": 6221, "timeout": 2.0, "latency_ms": 1.0, "error": "", "checked_at": "now", "role": "controller", "profile": "turkey-6221", "label": "target"}
        ]
        code, output = self.run_cli(
            "deploy",
            "status",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-systemd",
            "--json",
        )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertIn("service_status", payload)
        self.assertIn("healthcheck", payload)

    def test_deploy_unknown_adapter_rejected(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "deploy",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "missing",
            "--transport",
            "tcp",
        )
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_deploy_unsupported_backhaul_experimental_transport_rejected(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "deploy",
            "plan",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcptun",
        )
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    @patch("pilottunnel.deploy.run_profile_healthchecks")
    @patch("pilottunnel.deploy.inspect_service_status")
    @patch("pilottunnel.deploy.verify_service_ownership")
    @patch("pilottunnel.deploy.start_service")
    @patch("pilottunnel.deploy.run_daemon_reload")
    @patch("pilottunnel.deploy.apply_install")
    @patch("pilottunnel.deploy.build_readiness_report")
    @patch("pilottunnel.deploy._is_admin_or_root", return_value=True)
    @patch("pilottunnel.deploy._is_linux_host", return_value=True)
    def test_audit_records_deploy_plan_apply_status_attempts(self, _mock_linux, _mock_root, mock_readiness, mock_install, mock_reload, mock_start, mock_ownership, mock_status, mock_healthchecks) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        mock_readiness.return_value = self._readiness_ok()
        mock_install.return_value = {"ok": True}
        mock_reload.return_value = {"ok": True}
        mock_start.return_value = {"ok": True, "service_started": True, "healthcheck_ok": "skipped", "status": {"is_active": "active"}}
        mock_ownership.return_value = {"ok": True, "message": "owned"}
        mock_status.return_value = {"ok": True, "read_only": True, "is_active": "active", "is_enabled": "enabled", "unit_path": "/etc/systemd/system/pilot.service"}
        mock_healthchecks.return_value = [
            {"ok": True, "host": "127.0.0.1", "port": 6221, "timeout": 2.0, "latency_ms": 1.0, "error": "", "checked_at": "now", "role": "controller", "profile": "turkey-6221", "label": "target"}
        ]
        self.run_cli("deploy", "plan", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.run_cli(
            "deploy",
            "apply",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-host",
            "--confirm",
            "DEPLOY_APPLY",
        )
        self.run_cli(
            "deploy",
            "status",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--real-systemd",
        )
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        actions = [item["action"] for item in lines]
        self.assertIn("deploy-plan", actions)
        self.assertIn("deploy-apply", actions)
        self.assertIn("deploy-status", actions)

    def test_backup_plan_is_read_only(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        code, output = self.run_cli("backup", "plan", "--install-root", str(install_root), "--backup-root", str(backup_root))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["plan_only"])
        self.assertFalse(payload["files_restored"])
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(backup_root.exists())

    def test_backup_create_refuses_without_confirm_backup_create(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        code, output = self.run_cli("backup", "create", "--install-root", str(install_root), "--backup-root", str(backup_root))
        self.assertEqual(code, 1)
        self.assertIn("BACKUP_CREATE", output)

    def test_backup_create_copies_only_pilottunnel_owned_files(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        extra = install_root / "opt" / "unowned.txt"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_text("keep-out", encoding="utf-8")
        code, output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        manifest = json.loads(Path(payload["manifest_path"]).read_text(encoding="utf-8"))
        source_paths = manifest["source_paths"]
        self.assertFalse(any("opt\\unowned.txt" in path or "opt/unowned.txt" in path for path in source_paths))
        self.assertTrue(any("etc/pilottunnel" in path.replace("\\", "/") for path in source_paths))

    def test_backup_create_writes_manifest_and_checksums(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        code, output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        manifest_path = Path(payload["manifest_path"])
        checksums_path = Path(payload["checksums_path"])
        self.assertTrue(manifest_path.exists())
        self.assertTrue(checksums_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertFalse(manifest["real_systemd_touched"])
        self.assertFalse(manifest["service_started"])
        self.assertFalse(manifest["firewall_touched"])

    def test_backup_create_records_skipped_missing_files_as_warnings(self) -> None:
        self._create_profile()
        backup_root = Path(self.temp_dir.name) / "backup-root"
        install_root = Path(self.temp_dir.name) / "missing-install-root"
        code, output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["warnings"])
        self.assertTrue(payload["skipped"])

    def test_backup_list_shows_created_backups(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        code, output = self.run_cli("backup", "list", "--backup-root", str(backup_root))
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["backups"])

    def test_backup_inspect_reads_manifest(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        backup_id = json.loads(create_output)["backup_id"]
        code, output = self.run_cli("backup", "inspect", "--backup-root", str(backup_root), "--backup-id", backup_id)
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["manifest"]["backup_id"], backup_id)

    def test_backup_verify_passes_on_valid_backup(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        backup_id = json.loads(create_output)["backup_id"]
        code, output = self.run_cli("backup", "verify", "--backup-root", str(backup_root), "--backup-id", backup_id)
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(json.loads(output)["ok"])

    def test_backup_verify_fails_on_checksum_mismatch(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        payload = json.loads(create_output)
        manifest = json.loads(Path(payload["manifest_path"]).read_text(encoding="utf-8"))
        stored_rel = manifest["stored_files"][0]["stored_path"]
        (Path(payload["backup_path"]) / stored_rel).write_text("corrupt", encoding="utf-8")
        code, output = self.run_cli("backup", "verify", "--backup-root", str(backup_root), "--backup-id", payload["backup_id"])
        self.assertEqual(code, 1)
        self.assertTrue(json.loads(output)["checksum_mismatches"])

    def test_restore_plan_is_read_only(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        backup_id = json.loads(create_output)["backup_id"]
        code, output = self.run_cli("restore", "plan", "--backup-root", str(backup_root), "--install-root", str(install_root), "--backup-id", backup_id)
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["plan_only"])
        self.assertFalse(payload["files_restored"])
        self.assertFalse(payload["real_systemd_touched"])

    def test_restore_apply_refuses_without_confirm_restore_apply(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        backup_id = json.loads(create_output)["backup_id"]
        code, output = self.run_cli("restore", "apply", "--backup-root", str(backup_root), "--install-root", str(install_root), "--backup-id", backup_id)
        self.assertEqual(code, 1)
        self.assertIn("RESTORE_APPLY", output)

    def test_restore_apply_restores_files_from_backup(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        backup_id = json.loads(create_output)["backup_id"]
        target = install_root / "etc" / "pilottunnel" / "profiles" / "turkey-6221" / "backhaul" / "tcpmux" / "controller" / "backhaul-controller.toml"
        original = target.read_text(encoding="utf-8")
        target.write_text("modified", encoding="utf-8")
        code, output = self.run_cli(
            "restore",
            "apply",
            "--backup-root",
            str(backup_root),
            "--install-root",
            str(install_root),
            "--backup-id",
            backup_id,
            "--confirm",
            "RESTORE_APPLY",
        )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_restore_apply_creates_pre_restore_safety_backup_before_overwrite(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        backup_id = json.loads(create_output)["backup_id"]
        target = install_root / "etc" / "pilottunnel" / "profiles" / "turkey-6221" / "backhaul" / "tcpmux" / "controller" / "backhaul-controller.toml"
        target.write_text("modified", encoding="utf-8")
        code, output = self.run_cli(
            "restore",
            "apply",
            "--backup-root",
            str(backup_root),
            "--install-root",
            str(install_root),
            "--backup-id",
            backup_id,
            "--confirm",
            "RESTORE_APPLY",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        pre_restore_path = Path(payload["pre_restore_backup_path"])
        self.assertTrue(pre_restore_path.exists())
        self.assertTrue((pre_restore_path / "backup-manifest.json").exists())

    def test_restore_apply_refuses_path_traversal_backup_id(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        code, output = self.run_cli(
            "restore",
            "apply",
            "--backup-root",
            str(backup_root),
            "--install-root",
            str(install_root),
            "--backup-id",
            "../bad",
            "--confirm",
            "RESTORE_APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)

    def test_restore_apply_refuses_manifest_with_path_outside_allowed_roots(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        payload = json.loads(create_output)
        manifest_path = Path(payload["manifest_path"])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["stored_files"][0]["target_path"] = str((Path(self.temp_dir.name) / "escape" / "etc" / "passwd").resolve())
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        code, output = self.run_cli(
            "restore",
            "apply",
            "--backup-root",
            str(backup_root),
            "--install-root",
            str(install_root),
            "--backup-id",
            payload["backup_id"],
            "--confirm",
            "RESTORE_APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("outside allowed", output)

    def test_restore_apply_refuses_corrupt_checksum(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        payload = json.loads(create_output)
        checksums_path = Path(payload["checksums_path"])
        checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
        first_key = next(iter(checksums))
        checksums[first_key] = "deadbeef"
        checksums_path.write_text(json.dumps(checksums, indent=2, sort_keys=True), encoding="utf-8")
        code, output = self.run_cli(
            "restore",
            "apply",
            "--backup-root",
            str(backup_root),
            "--install-root",
            str(install_root),
            "--backup-id",
            payload["backup_id"],
            "--confirm",
            "RESTORE_APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("verification failed", output.lower())

    @patch("pilottunnel.service_lifecycle.subprocess.run")
    def test_restore_apply_does_not_call_systemctl_or_touch_firewall_routes_interfaces(self, mock_run) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        backup_id = json.loads(create_output)["backup_id"]
        code, output = self.run_cli(
            "restore",
            "apply",
            "--backup-root",
            str(backup_root),
            "--install-root",
            str(install_root),
            "--backup-id",
            backup_id,
            "--confirm",
            "RESTORE_APPLY",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["firewall_touched"])
        self.assertFalse(payload["routes_touched"])
        mock_run.assert_not_called()

    def test_backup_restore_reject_unknown_adapter(self) -> None:
        code, output = self.run_cli("backup", "plan", "--adapter", "missing", "--transport", "tcp")
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_backup_restore_unsupported_backhaul_experimental_transport_rejected(self) -> None:
        code, output = self.run_cli("backup", "plan", "--adapter", "backhaul", "--transport", "tcptun")
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    @patch("pilottunnel.backup.platform.system", return_value="Windows")
    def test_backup_restore_windows_safe_behavior_is_covered(self, _mock_platform) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        code, output = self.run_cli("backup", "plan", "--install-root", str(install_root), "--backup-root", str(backup_root))
        self.assertEqual(code, 0, msg=output)
        self.assertIn("files", json.loads(output))

    def test_audit_records_backup_and_restore_attempts(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        self.run_cli("backup", "plan", "--install-root", str(install_root), "--backup-root", str(backup_root))
        _, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
        )
        backup_id = json.loads(create_output)["backup_id"]
        self.run_cli("restore", "plan", "--backup-root", str(backup_root), "--install-root", str(install_root), "--backup-id", backup_id)
        self.run_cli(
            "restore",
            "apply",
            "--backup-root",
            str(backup_root),
            "--install-root",
            str(install_root),
            "--backup-id",
            backup_id,
            "--confirm",
            "RESTORE_APPLY",
        )
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        actions = [item["action"] for item in lines]
        self.assertIn("backup-plan", actions)
        self.assertIn("backup-create", actions)
        self.assertIn("restore-plan", actions)
        self.assertIn("restore-apply", actions)

    def test_backup_restore_json_output_is_valid(self) -> None:
        install_root, backup_root = self._prepare_backup_fixture()
        create_code, create_output = self.run_cli(
            "backup",
            "create",
            "--install-root",
            str(install_root),
            "--backup-root",
            str(backup_root),
            "--confirm",
            "BACKUP_CREATE",
            "--json",
        )
        self.assertEqual(create_code, 0, msg=create_output)
        backup_id = json.loads(create_output)["backup_id"]
        inspect_code, inspect_output = self.run_cli("backup", "inspect", "--backup-root", str(backup_root), "--backup-id", backup_id, "--json")
        self.assertEqual(inspect_code, 0)
        self.assertIn("manifest", json.loads(inspect_output))

    def test_service_start_unknown_adapter_rejected(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "missing",
                "--transport",
                "tcp",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_service_start_unsupported_backhaul_experimental_transport_rejected(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcptun",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    def test_service_start_role_aware_service_names_correct_for_controller_worker(self) -> None:
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
        self._prepare_real_systemd_unit(role="controller")

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            controller_code, controller_output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(controller_code, 0)
        self.assertTrue(json.loads(controller_output)["service_name"].endswith("controller.service"))

        self.run_cli("init", "--force", "--role", "worker")
        self._prepare_real_systemd_unit(role="worker")
        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            worker_code, worker_output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        self.assertEqual(worker_code, 0)
        self.assertTrue(json.loads(worker_output)["service_name"].endswith("worker.service"))

    def test_audit_records_service_start_attempts_success_and_failure(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()

        def fake_run_success(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        def fake_run_fail(command, **kwargs):
            class Completed:
                returncode = 1
                stdout = ""
                stderr = "failed"

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run_success):
            self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        with self._mock_real_systemd(subprocess_side_effect=fake_run_fail):
            self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
            )
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        actions = [item["action"] for item in lines]
        self.assertIn("service-start", actions)

    def test_service_start_json_output_is_valid(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "active"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "start",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "START_SERVICE",
                "--json",
            )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["service_started"])

    def test_service_stop_refuses_without_real_systemd(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "stop",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--confirm",
            "STOP_SERVICE",
        )
        self.assertEqual(code, 1)
        self.assertIn("--real-systemd", output)

    def test_service_stop_requires_initialized_node_role(self) -> None:
        self.run_cli(
            "profile",
            "create",
            "--name",
            "turkey-6221",
            "--main-port",
            "6221",
            "--target-port",
            "6221",
        )
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("initialized node role", output)

    def test_service_stop_refuses_without_confirm_stop_service(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(code, 1)
        self.assertIn("STOP_SERVICE", output)

    def test_service_stop_refuses_on_windows_mock_non_linux(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(is_linux=False):
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("Linux-only", output)

    def test_service_stop_refuses_when_systemd_unavailable(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(systemctl_available=False):
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("unavailable", output)

    def test_service_stop_refuses_without_root_mock(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(is_root=False):
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("root/admin", output)

    def test_service_stop_refuses_if_unit_file_missing(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("unit is missing", output)

    def test_service_stop_refuses_if_unit_is_not_pilottunnel_owned(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit(owned=False)
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("not marked as PilotTunnel-owned", output)

    def test_service_stop_executes_only_mocked_systemctl_stop_when_gates_pass(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "inactive"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(calls[0], ["systemctl", "stop", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"])
        self.assertTrue(payload["service_stopped"])
        self.assertTrue(payload["real_systemd_touched"])

    def test_service_stop_does_not_call_enable_disable_restart_start(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "inactive"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(calls[0][1], "stop")
        self.assertTrue(all(command[1] not in {"enable", "disable", "restart", "start"} for command in calls[1:]))

    def test_service_stop_runs_read_only_status_and_is_active_after_successful_stop(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "inactive"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertIn(["systemctl", "is-active", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"], calls)
        self.assertIn(["systemctl", "status", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service", "--no-pager"], calls)
        self.assertEqual(payload["status"]["is_active"], "inactive")
        self.assertTrue(payload["status"]["read_only"])

    def test_service_enable_refuses_without_real_systemd(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "enable",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--confirm",
            "ENABLE_SERVICE",
        )
        self.assertEqual(code, 1)
        self.assertIn("--real-systemd", output)

    def test_service_enable_refuses_without_confirm_enable_service(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(code, 1)
        self.assertIn("ENABLE_SERVICE", output)

    def test_service_enable_refuses_on_windows_mock_non_linux(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(is_linux=False):
            code, output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("Linux-only", output)

    def test_service_enable_refuses_when_systemd_unavailable(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(systemctl_available=False):
            code, output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("unavailable", output)

    def test_service_enable_refuses_without_root_mock(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd(is_root=False):
            code, output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("root/admin", output)

    def test_service_enable_refuses_if_unit_file_missing(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("unit is missing", output)

    def test_service_enable_refuses_if_unit_is_not_pilottunnel_owned(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit(owned=False)
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("not marked as PilotTunnel-owned", output)

    def test_service_enable_executes_only_mocked_systemctl_enable_when_gates_pass(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "enabled"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(calls[0], ["systemctl", "enable", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"])
        self.assertTrue(payload["service_enabled"])
        self.assertTrue(payload["real_systemd_touched"])

    def test_service_enable_does_not_call_start_stop_restart_disable(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "enabled"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(calls[0][1], "enable")
        self.assertTrue(all(command[1] not in {"start", "stop", "restart", "disable"} for command in calls[1:]))

    def test_service_enable_runs_read_only_is_enabled_is_active_status_after_success(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                if command[1] == "is-enabled":
                    stdout = "enabled"
                elif command[1] == "is-active":
                    stdout = "inactive"
                else:
                    stdout = "unit loaded"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertIn(["systemctl", "is-enabled", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"], calls)
        self.assertIn(["systemctl", "is-active", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"], calls)
        self.assertIn(["systemctl", "status", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service", "--no-pager"], calls)
        self.assertEqual(payload["status"]["is_enabled"], "enabled")
        self.assertEqual(payload["status"]["is_active"], "inactive")
        self.assertTrue(payload["status"]["read_only"])

    def test_service_disable_refuses_without_real_systemd(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "service",
            "disable",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--confirm",
            "DISABLE_SERVICE",
        )
        self.assertEqual(code, 1)
        self.assertIn("--real-systemd", output)

    def test_service_disable_refuses_without_confirm_disable_service(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "disable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
            )
        self.assertEqual(code, 1)
        self.assertIn("DISABLE_SERVICE", output)

    def test_service_disable_executes_only_mocked_systemctl_disable_when_gates_pass(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "disabled"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "disable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "DISABLE_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(calls[0], ["systemctl", "disable", "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"])
        self.assertTrue(payload["service_disabled"])
        self.assertTrue(payload["real_systemd_touched"])

    def test_service_disable_does_not_call_start_stop_restart_enable(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)

            class Completed:
                returncode = 0
                stdout = "disabled"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "disable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "DISABLE_SERVICE",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(calls[0][1], "disable")
        self.assertTrue(all(command[1] not in {"start", "stop", "restart", "enable"} for command in calls[1:]))

    def test_service_enable_disable_role_aware_service_names_correct_for_controller_worker(self) -> None:
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
        self._prepare_real_systemd_unit(role="controller")

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "enabled" if command[1] == "is-enabled" else "inactive"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            controller_code, controller_output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        self.assertEqual(controller_code, 0)
        self.assertTrue(json.loads(controller_output)["service_name"].endswith("controller.service"))

        self.run_cli("init", "--force", "--role", "worker")
        self._prepare_real_systemd_unit(role="worker")
        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            worker_code, worker_output = self.run_cli(
                "service",
                "disable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "DISABLE_SERVICE",
            )
        self.assertEqual(worker_code, 0)
        self.assertTrue(json.loads(worker_output)["service_name"].endswith("worker.service"))

    def test_service_enable_unknown_adapter_rejected(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "missing",
                "--transport",
                "tcp",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_service_disable_unsupported_backhaul_experimental_transport_rejected(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "disable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcptun",
                "--real-systemd",
                "--confirm",
                "DISABLE_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    def test_audit_records_service_enable_disable_attempts_success_and_failure(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()

        def fake_run_success(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "enabled" if command[1] == "is-enabled" else "inactive"
                stderr = ""

            return Completed()

        def fake_run_fail(command, **kwargs):
            class Completed:
                returncode = 1
                stdout = ""
                stderr = "failed"

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run_success):
            self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
            )
        with self._mock_real_systemd(subprocess_side_effect=fake_run_fail):
            self.run_cli(
                "service",
                "disable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "DISABLE_SERVICE",
            )
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        actions = [item["action"] for item in lines]
        self.assertIn("service-enable", actions)
        self.assertIn("service-disable", actions)

    def test_service_enable_disable_json_output_is_valid(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "enabled" if command[1] == "is-enabled" else "inactive"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            enable_code, enable_output = self.run_cli(
                "service",
                "enable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "ENABLE_SERVICE",
                "--json",
            )
            disable_code, disable_output = self.run_cli(
                "service",
                "disable",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "DISABLE_SERVICE",
                "--json",
            )
        self.assertEqual(enable_code, 0)
        self.assertEqual(disable_code, 0)
        self.assertTrue(json.loads(enable_output)["service_enabled"])
        self.assertTrue(json.loads(disable_output)["service_disabled"])

    def test_service_stop_unknown_adapter_rejected(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "missing",
                "--transport",
                "tcp",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_service_stop_unsupported_backhaul_experimental_transport_rejected(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        with self._mock_real_systemd():
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcptun",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    def test_service_stop_role_aware_service_names_correct_for_controller_worker(self) -> None:
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
        self._prepare_real_systemd_unit(role="controller")

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "inactive"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            controller_code, controller_output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(controller_code, 0)
        self.assertTrue(json.loads(controller_output)["service_name"].endswith("controller.service"))

        self.run_cli("init", "--force", "--role", "worker")
        self._prepare_real_systemd_unit(role="worker")
        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            worker_code, worker_output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        self.assertEqual(worker_code, 0)
        self.assertTrue(json.loads(worker_output)["service_name"].endswith("worker.service"))

    def test_audit_records_service_stop_attempts_success_and_failure(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()

        def fake_run_success(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "inactive"
                stderr = ""

            return Completed()

        def fake_run_fail(command, **kwargs):
            class Completed:
                returncode = 1
                stdout = ""
                stderr = "failed"

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run_success):
            self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        with self._mock_real_systemd(subprocess_side_effect=fake_run_fail):
            self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
            )
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        actions = [item["action"] for item in lines]
        self.assertIn("service-stop", actions)

    def test_service_stop_json_output_is_valid(self) -> None:
        self.run_cli("init", "--role", "controller")
        self._create_profile()
        self._prepare_real_systemd_unit()

        def fake_run(command, **kwargs):
            class Completed:
                returncode = 0
                stdout = "inactive"
                stderr = ""

            return Completed()

        with self._mock_real_systemd(subprocess_side_effect=fake_run):
            code, output = self.run_cli(
                "service",
                "stop",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-systemd",
                "--confirm",
                "STOP_SERVICE",
                "--json",
            )
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["service_stopped"])

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

    def _allocate_tcp_ports(self, count: int = 4) -> tuple[list[int], list[socket.socket]]:
        listeners: list[socket.socket] = []
        ports: list[int] = []
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            listeners.append(sock)
            ports.append(sock.getsockname()[1])
        return ports, listeners

    def test_readiness_report_without_init_returns_not_initialized(self) -> None:
        code, output = self.run_cli("readiness", "report")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["readiness_level"], "not_initialized")
        self.assertFalse(payload["role_initialized"])

    def test_readiness_report_after_controller_init_shows_controller_role(self) -> None:
        self.run_cli("init", "--role", "controller")
        code, output = self.run_cli("readiness", "report")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["node_role"], "controller")
        self.assertTrue(payload["role_initialized"])

    def test_readiness_report_after_worker_init_shows_worker_role(self) -> None:
        self.run_cli("init", "--role", "worker")
        code, output = self.run_cli("readiness", "report")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertEqual(payload["node_role"], "worker")
        self.assertTrue(payload["role_initialized"])

    def test_readiness_report_with_missing_binary_shows_warning_and_blocker(self) -> None:
        self._create_profile()
        code, output = self.run_cli("readiness", "report", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["binary_imported"])
        self.assertTrue(payload["blockers"])
        self.assertIn("Imported binary missing", " ".join(payload["blockers"]))

    def test_readiness_report_with_existing_profile_shows_profile_exists(self) -> None:
        self._create_profile()
        code, output = self.run_cli("readiness", "report", "--profile", "turkey-6221")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertTrue(payload["profile_exists"])
        self.assertEqual(payload["profile"], "turkey-6221")

    def test_readiness_report_with_staged_files_shows_staged_ready_or_higher(self) -> None:
        ports, listeners = self._allocate_tcp_ports(5)
        try:
            self._create_profile_with_ports(
                name="turkey-6221",
                main_port=ports[0],
                target_port=ports[1],
                control_port=ports[2],
                service_port=ports[3],
                check_port=ports[4],
            )
            source = self._make_binary_source("backhaul")
            self.run_cli("binary", "import", "--adapter", "backhaul", "--source", str(source), "--version", "manual-v0.0.0")
            self.run_cli("--apply", "switch", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
            code, output = self.run_cli("readiness", "report", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcpmux")
            self.assertEqual(code, 0, msg=output)
            payload = json.loads(output)
            self.assertIn(payload["readiness_level"], {"staged_ready", "install_plan_ready", "service_plan_ready"})
            self.assertTrue(payload["staged_files_exist"])
        finally:
            for listener in listeners:
                listener.close()

    def test_readiness_report_json_output_is_valid(self) -> None:
        code, output = self.run_cli("readiness", "report", "--json")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertIn("ok", payload)

    def test_readiness_report_rejects_path_traversal(self) -> None:
        code, output = self.run_cli("readiness", "report", "--profile", "../turkey-6221")
        self.assertEqual(code, 1)
        self.assertIn("Path traversal blocked", output)

    def test_readiness_report_unknown_adapter_rejected(self) -> None:
        self._create_profile()
        code, output = self.run_cli("readiness", "report", "--profile", "turkey-6221", "--adapter", "missing", "--transport", "tcp")
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_readiness_report_unsupported_transport_rejected(self) -> None:
        self._create_profile()
        code, output = self.run_cli("readiness", "report", "--profile", "turkey-6221", "--adapter", "backhaul", "--transport", "tcptun")
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    def test_readiness_output_confirms_no_systemd_firewall_routes_services_downloads_touched(self) -> None:
        code, output = self.run_cli("readiness", "report")
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["firewall_touched"])
        self.assertFalse(payload["routes_touched"])
        self.assertFalse(payload["services_started"])
        self.assertFalse(payload["downloads_performed"])

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

    def test_controller_exports_valid_backhaul_tcpmux_worker_bundle(self) -> None:
        self._create_profile()
        bundle_path = self._export_worker_bundle("backhaul", "tcpmux")
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["bundle_type"], "worker_prepare")
        self.assertEqual(payload["profile"]["role"], "worker")
        self.assertFalse(Path(payload["expected_paths"]["config"]).is_absolute())
        self.assertTrue(payload["no_system_changes"])

    def test_controller_exports_valid_rathole_tcp_worker_bundle(self) -> None:
        self._create_profile()
        bundle_path = self._export_worker_bundle("rathole", "tcp")
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["adapter"], "rathole")
        self.assertEqual(payload["transport"], "tcp")
        self.assertEqual(payload["config_filenames"]["worker"], "rathole-worker.toml")

    def test_worker_role_is_blocked_from_export_worker(self) -> None:
        self._create_profile()
        self.run_cli("init", "--force", "--role", "worker")
        code, output = self.run_cli(
            "bundle",
            "export-worker",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--output",
            str(Path(self.temp_dir.name) / "blocked.json"),
        )
        self.assertEqual(code, 1)
        self.assertIn("blocked for node role 'worker'", output)

    def test_bundle_export_rejects_unknown_adapter(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "bundle",
            "export-worker",
            "--profile",
            "turkey-6221",
            "--adapter",
            "missing",
            "--transport",
            "tcp",
            "--output",
            str(Path(self.temp_dir.name) / "missing.json"),
        )
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_bundle_export_rejects_unsupported_backhaul_experimental_transport(self) -> None:
        self._create_profile()
        code, output = self.run_cli(
            "bundle",
            "export-worker",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcptun",
            "--output",
            str(Path(self.temp_dir.name) / "experimental.json"),
        )
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    def test_bundle_export_refuses_overwrite_without_force(self) -> None:
        self._create_profile()
        bundle_path = self._export_worker_bundle("backhaul", "tcpmux")
        code, output = self.run_cli(
            "bundle",
            "export-worker",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--output",
            str(bundle_path),
        )
        self.assertEqual(code, 1)
        self.assertIn("already exists", output)

    def test_bundle_inspect_reads_valid_bundle_without_writing_files(self) -> None:
        self._create_profile()
        bundle_path = self._export_worker_bundle("backhaul", "tcpmux")
        before = sorted(str(path) for path in self.staging_root.rglob("*")) if self.staging_root.exists() else []
        code, output = self.run_cli("bundle", "inspect", "--input", str(bundle_path))
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertTrue(payload["no_changes_made"])
        self.assertFalse(payload["node_role_matches_worker"])
        after = sorted(str(path) for path in self.staging_root.rglob("*")) if self.staging_root.exists() else []
        self.assertEqual(before, after)

    def test_bundle_inspect_warns_when_node_role_does_not_match_expected_worker(self) -> None:
        self._create_profile()
        bundle_path = self._export_worker_bundle("backhaul", "tcpmux")
        code, output = self.run_cli("bundle", "inspect", "--input", str(bundle_path))
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["node_role_matches_worker"])
        self.assertTrue(payload["warnings"])

    def test_bundle_import_refuses_without_confirm_import(self) -> None:
        self._create_profile()
        bundle_path = self._export_worker_bundle("backhaul", "tcpmux")
        self.run_cli("init", "--force", "--role", "worker")
        before = self.config.read_text(encoding="utf-8")
        code, output = self.run_cli("bundle", "import", "--input", str(bundle_path))
        self.assertEqual(code, 1)
        self.assertIn("confirm IMPORT", output)
        self.assertEqual(before, self.config.read_text(encoding="utf-8"))

    @patch("subprocess.run")
    def test_worker_bundle_import_creates_worker_side_profile_and_staged_files_under_staging_root(self, mock_run) -> None:
        self._create_profile()
        bundle_path = self._export_worker_bundle("backhaul", "tcpmux")
        self.run_cli("init", "--force", "--role", "worker")
        code, output = self.run_cli(
            "bundle",
            "import",
            "--input",
            str(bundle_path),
            "--staging-root",
            str(self.staging_root),
            "--confirm",
            "IMPORT",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["config_written"])
        config_data = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config_data["profiles"][0]["role"], "worker")
        self.assertTrue((self.staging_root / "configs" / "turkey-6221" / "backhaul" / "tcpmux" / "worker" / "backhaul-worker.toml").exists())
        self.assertTrue((self.staging_root / "systemd" / "pilottunnel-turkey-6221-backhaul-tcpmux-worker.service").exists())
        mock_run.assert_not_called()

    def test_controller_bundle_import_refuses_unless_force(self) -> None:
        self._create_profile()
        bundle_path = self._export_worker_bundle("backhaul", "tcpmux")
        code, output = self.run_cli("bundle", "import", "--input", str(bundle_path), "--confirm", "IMPORT")
        self.assertEqual(code, 1)
        self.assertIn("blocked for controller nodes", output)

    def test_invalid_json_bundle_rejected(self) -> None:
        bundle_path = Path(self.temp_dir.name) / "invalid.json"
        bundle_path.write_text("{not json", encoding="utf-8")
        code, output = self.run_cli("bundle", "inspect", "--input", str(bundle_path))
        self.assertEqual(code, 1)
        self.assertIn("Invalid JSON bundle", output)

    def test_missing_required_fields_rejected(self) -> None:
        bundle_path = Path(self.temp_dir.name) / "missing.json"
        bundle_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "bundle_type": "worker_prepare",
                    "created_at": "2024-01-01T00:00:00Z",
                    "profile": {
                        "name": "turkey-6221",
                        "main_port": 6221,
                        "target_host": "127.0.0.1",
                        "target_port": 6221,
                        "role": "worker",
                    },
                    "adapter": "backhaul",
                    "transport": "tcpmux",
                    "controller_role": "controller",
                    "worker_role": "worker",
                    "service_names": {"worker": "pilottunnel-turkey-6221-backhaul-tcpmux-worker.service"},
                    "config_filenames": {"worker": "backhaul-worker.toml"},
                    "healthcheck_expectations": [],
                    "warnings": [],
                    "no_system_changes": True,
                }
            ),
            encoding="utf-8",
        )
        code, output = self.run_cli("bundle", "inspect", "--input", str(bundle_path))
        self.assertEqual(code, 1)
        self.assertIn("Missing required bundle field", output)

    def test_path_traversal_profile_rejected(self) -> None:
        self._create_profile()
        bundle_path = self._export_worker_bundle("backhaul", "tcpmux")
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        payload["profile"]["name"] = "../bad"
        bundle_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        code, output = self.run_cli("bundle", "inspect", "--input", str(bundle_path))
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)

    def test_audit_records_bundle_export_import_attempts(self) -> None:
        self._create_profile()
        bundle_path = self._export_worker_bundle("backhaul", "tcpmux")
        self.run_cli("init", "--force", "--role", "worker")
        code, output = self.run_cli("bundle", "import", "--input", str(bundle_path), "--confirm", "IMPORT")
        self.assertEqual(code, 0, msg=output)
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        actions = [item["action"] for item in lines]
        self.assertIn("bundle-export-worker", actions)
        self.assertIn("bundle-import", actions)

    def test_simulate_e2e_backhaul_tcpmux_succeeds(self) -> None:
        code, output, payload = self._simulate_e2e("backhaul", "tcpmux")
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["adapter"], "backhaul")
        self.assertEqual(payload["transport"], "tcpmux")
        self.assertTrue(payload["healthcheck_summary"]["ok"])

    def test_simulate_e2e_rathole_tcp_succeeds(self) -> None:
        code, output, payload = self._simulate_e2e("rathole", "tcp")
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["adapter"], "rathole")
        self.assertEqual(payload["transport"], "tcp")

    def test_simulate_e2e_writes_only_under_base_root(self) -> None:
        base_root = Path(self.temp_dir.name) / "simulation-base"
        code, output, payload = self._simulate_e2e("backhaul", "tcpmux", base_root=base_root)
        self.assertEqual(code, 0, msg=output)
        run_root = Path(payload["controller_root"]).parent
        self.assertFalse(run_root.exists())
        self.assertFalse(Path(payload["bundle_path"]).exists())

    def test_simulate_e2e_json_output_is_valid(self) -> None:
        code, output, payload = self._simulate_e2e("backhaul", "tcpmux")
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(json.loads(output)["ok"])
        self.assertTrue(payload["ok"])

    def test_simulate_e2e_blocks_path_traversal_base_root(self) -> None:
        code, output = self.run_cli(
            "simulate",
            "e2e",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--base-root",
            str(Path(self.temp_dir.name) / ".." / "escape"),
        )
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)

    def test_simulate_e2e_blocks_path_traversal_profile(self) -> None:
        code, output = self.run_cli(
            "simulate",
            "e2e",
            "--profile",
            "../bad",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
        )
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)

    def test_simulate_e2e_rejects_unknown_adapter(self) -> None:
        code, output = self.run_cli(
            "simulate",
            "e2e",
            "--profile",
            "turkey-6221",
            "--adapter",
            "missing",
            "--transport",
            "tcp",
        )
        self.assertEqual(code, 1)
        self.assertIn("Unknown adapter", output)

    def test_simulate_e2e_rejects_unsupported_backhaul_experimental_transport(self) -> None:
        code, output = self.run_cli(
            "simulate",
            "e2e",
            "--profile",
            "turkey-6221",
            "--adapter",
            "backhaul",
            "--transport",
            "tcptun",
        )
        self.assertEqual(code, 1)
        self.assertIn("blocked in v0.1", output)

    def test_simulate_proves_worker_cannot_perform_controller_only_switch_decision(self) -> None:
        code, output, payload = self._simulate_e2e("backhaul", "tcpmux")
        self.assertEqual(code, 0, msg=output)
        attempt = payload["worker_controller_switch_attempt"]
        self.assertFalse(attempt["ok"])
        self.assertIn("blocked for node role 'worker'", attempt["stdout"])

    def test_simulate_output_includes_controller_and_worker_roots(self) -> None:
        code, output, payload = self._simulate_e2e("backhaul", "tcpmux")
        self.assertEqual(code, 0, msg=output)
        self.assertIn("controller_root", payload)
        self.assertIn("worker_root", payload)
        self.assertTrue(payload["controller_root"])
        self.assertTrue(payload["worker_root"])

    def test_simulate_output_includes_bundle_path(self) -> None:
        code, output, payload = self._simulate_e2e("backhaul", "tcpmux")
        self.assertEqual(code, 0, msg=output)
        self.assertIn("bundle_path", payload)
        self.assertTrue(payload["bundle_path"].endswith("turkey-6221-worker.json"))

    def test_simulate_output_confirms_no_system_changes(self) -> None:
        code, output, payload = self._simulate_e2e("backhaul", "tcpmux")
        self.assertEqual(code, 0, msg=output)
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["real_firewall_touched"])
        self.assertFalse(payload["routes_touched"])
        self.assertFalse(payload["services_started"])
        self.assertFalse(payload["downloads_performed"])

    def test_simulate_keep_files_preserves_simulation_files(self) -> None:
        base_root = Path(self.temp_dir.name) / "keep-files"
        code, output, payload = self._simulate_e2e("backhaul", "tcpmux", base_root=base_root, keep_files=True)
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(Path(payload["bundle_path"]).exists())
        self.assertTrue(Path(payload["controller_root"]).exists())
        self.assertTrue(Path(payload["worker_root"]).exists())

    def test_simulate_default_cleanup_removes_temp_files(self) -> None:
        code, output, payload = self._simulate_e2e("backhaul", "tcpmux")
        self.assertEqual(code, 0, msg=output)
        self.assertFalse(Path(payload["bundle_path"]).exists())
        self.assertFalse(Path(payload["controller_root"]).exists())
        self.assertFalse(Path(payload["worker_root"]).exists())

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

    def _prepare_backup_fixture(self) -> tuple[Path, Path]:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        install_root = Path(self.temp_dir.name) / "install-root"
        backup_root = Path(self.temp_dir.name) / "backup-root"
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
        self.assertEqual(code, 0, msg=output)
        return install_root, backup_root

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

    def _readiness_ok(self) -> dict:
        return {
            "ok": True,
            "readiness_level": "service_plan_ready",
            "role_initialized": True,
            "node_role": "controller",
            "blockers": [],
            "warnings": [],
            "staged_files_exist": True,
            "binary_imported": True,
            "install_plan_available": True,
            "service_plan_available": True,
        }

    def _mock_real_host(
        self,
        *,
        readiness_report: dict | None = None,
        is_linux: bool = True,
        is_root: bool = True,
    ) -> ExitStack:
        real_root = Path(self.temp_dir.name) / "real-host"
        real_root.mkdir(parents=True, exist_ok=True)
        stack = ExitStack()
        stack.enter_context(patch("pilottunnel.install_plan.REAL_HOST_ROOT", real_root))
        stack.enter_context(patch("pilottunnel.install_plan._is_linux_host", return_value=is_linux))
        stack.enter_context(patch("pilottunnel.install_plan._is_admin_or_root", return_value=is_root))
        stack.enter_context(patch("pilottunnel.cli.build_readiness_report", return_value=readiness_report or self._readiness_ok()))
        return stack

    def _mock_real_systemd(
        self,
        *,
        is_linux: bool = True,
        systemctl_available: bool = True,
        journalctl_available: bool = True,
        is_root: bool = True,
        subprocess_side_effect=None,
    ) -> ExitStack:
        stack = ExitStack()
        platform_name = "Linux" if is_linux else "Windows"
        real_root = Path(self.temp_dir.name) / "real-systemd"
        real_root.mkdir(parents=True, exist_ok=True)
        stack.enter_context(patch("pilottunnel.service_lifecycle.REAL_SYSTEM_ROOT", real_root))
        stack.enter_context(patch("pilottunnel.service_lifecycle.platform.system", return_value=platform_name))
        stack.enter_context(patch("pilottunnel.service_lifecycle._is_linux", return_value=is_linux))
        stack.enter_context(patch("pilottunnel.service_lifecycle._is_root", return_value=is_root))

        def which_side_effect(command: str) -> str | None:
            if command == "systemctl":
                return "/usr/bin/systemctl" if systemctl_available else None
            if command == "journalctl":
                return "/usr/bin/journalctl" if journalctl_available else None
            return f"/usr/bin/{command}"

        stack.enter_context(patch("pilottunnel.service_lifecycle.shutil.which", side_effect=which_side_effect))
        if subprocess_side_effect is not None:
            stack.enter_context(patch("pilottunnel.service_lifecycle.subprocess.run", side_effect=subprocess_side_effect))
        else:
            stack.enter_context(patch("pilottunnel.service_lifecycle.subprocess.run"))
        return stack

    def _prepare_real_systemd_unit(
        self,
        *,
        profile: str = "turkey-6221",
        adapter: str = "backhaul",
        transport: str = "tcpmux",
        role: str = "controller",
        owned: bool = True,
        manifest_owned: bool = True,
    ) -> tuple[Path, Path]:
        real_root = Path(self.temp_dir.name) / "real-systemd"
        service_name = f"pilottunnel-{profile}-{adapter}-{transport}-{role}.service"
        unit_path = real_root / "etc" / "systemd" / "system" / service_name
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        marker = "# Managed-by: PilotTunnel\n" if owned else ""
        description = (
            f"Description=PilotTunnel {profile} {adapter} {transport} {role}\n"
            if owned
            else "Description=ThirdParty tunnel service\n"
        )
        unit_path.write_text(
            marker
            + "[Unit]\n"
            + description
            + "\n"
            + "[Service]\nType=simple\nExecStart=/usr/bin/env echo pilot\n",
            encoding="utf-8",
        )
        manifest_path = real_root / "var" / "lib" / "pilottunnel" / "apply-manifests" / f"{profile}-{adapter}-{transport}.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        destinations = [str(unit_path)] if manifest_owned else [str(real_root / "etc" / "systemd" / "system" / "other.service")]
        manifest_path.write_text(
            json.dumps(
                {
                    "profile": profile,
                    "adapter": adapter,
                    "transport": transport,
                    "copied_files": [{"destination": destination} for destination in destinations],
                }
            ),
            encoding="utf-8",
        )
        return real_root, unit_path

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

    def test_real_host_apply_refuses_without_real_host_files(self) -> None:
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
            "REAL_FILES_APPLY",
        )
        self.assertEqual(code, 1)
        self.assertIn("--real-host-files", output)

    def test_real_host_apply_refuses_without_confirm_real_files_apply(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        with self._mock_real_host():
            code, output = self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
            )
        self.assertEqual(code, 1)
        self.assertIn("REAL_FILES_APPLY", output)

    def test_real_host_apply_refuses_on_windows_mock(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        with self._mock_real_host(is_linux=False):
            code, output = self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
            )
        self.assertEqual(code, 1)
        self.assertIn("Linux-only", output)

    def test_real_host_apply_refuses_without_root_mock(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        with self._mock_real_host(is_root=False):
            code, output = self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
            )
        self.assertEqual(code, 1)
        self.assertIn("root/admin", output)

    def test_real_host_apply_refuses_when_readiness_is_blocked(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        readiness = {"readiness_level": "blocked", "blockers": ["blocked"], "role_initialized": True}
        with self._mock_real_host(readiness_report=readiness):
            code, output = self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
            )
        self.assertEqual(code, 1)
        self.assertIn("readiness report", output.lower())

    def test_real_host_apply_writes_only_allowed_pilottunnel_paths_when_using_mocked_real_root(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        real_root = Path(self.temp_dir.name) / "real-host"
        with self._mock_real_host():
            code, output = self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        copied = [Path(item["destination"]) for item in payload["copied_files"]]
        self.assertTrue(all(path.resolve().is_relative_to(real_root.resolve()) for path in copied))
        self.assertTrue(any(str(path).endswith(".service") for path in copied))
        self.assertTrue(payload["real_host_files"])

    def test_real_host_apply_creates_backups_before_overwrite(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        real_root = Path(self.temp_dir.name) / "real-host"
        target = real_root / "etc" / "pilottunnel" / "profiles" / "turkey-6221" / "backhaul" / "tcpmux" / "controller" / "backhaul-controller.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old-config", encoding="utf-8")
        with self._mock_real_host():
            code, output = self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        backup_path = Path(next(item["backup"] for item in payload["backups_created"] if item["target"].endswith("backhaul-controller.toml")))
        self.assertEqual(backup_path.read_text(encoding="utf-8"), "old-config")
        self.assertIn(str(real_root / "var" / "backups" / "pilottunnel"), str(backup_path))

    def test_real_host_apply_writes_manifest(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        with self._mock_real_host():
            code, output = self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        manifest_path = Path(payload["manifest_path"])
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertTrue(manifest["real_host_files"])
        self.assertFalse(manifest["systemctl_executed"])

    def test_real_host_apply_rollback_restores_backups_and_removes_new_files(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        real_root = Path(self.temp_dir.name) / "real-host"
        config_target = real_root / "etc" / "pilottunnel" / "profiles" / "turkey-6221" / "backhaul" / "tcpmux" / "controller" / "backhaul-controller.toml"
        config_target.parent.mkdir(parents=True, exist_ok=True)
        config_target.write_text("old-config", encoding="utf-8")
        with self._mock_real_host():
            self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
            )
            code, output = self.run_cli(
                "install",
                "rollback",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_ROLLBACK",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(config_target.read_text(encoding="utf-8"), "old-config")
        unit_target = real_root / "etc" / "systemd" / "system" / "pilottunnel-turkey-6221-backhaul-tcpmux-controller.service"
        self.assertFalse(unit_target.exists())

    def test_real_host_uninstall_refuses_without_confirm_real_files_uninstall(self) -> None:
        self._create_profile()
        with self._mock_real_host():
            code, output = self.run_cli(
                "uninstall",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
            )
        self.assertEqual(code, 1)
        self.assertIn("REAL_FILES_UNINSTALL", output)

    def test_real_host_uninstall_removes_only_pilottunnel_owned_files(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        real_root = Path(self.temp_dir.name) / "real-host"
        with self._mock_real_host():
            self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
            )
            extra = real_root / "etc" / "pilottunnel" / "profiles" / "unowned.txt"
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
                "--real-host-files",
                "--confirm",
                "REAL_FILES_UNINSTALL",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["removed_files"])
        self.assertTrue(extra.exists())

    def test_real_host_dry_run_writes_nothing(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        real_root = Path(self.temp_dir.name) / "real-host"
        with self._mock_real_host(is_root=False):
            code, output = self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
                "--dry-run",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertFalse((real_root / "etc" / "pilottunnel").exists())
        self.assertFalse((real_root / "var" / "lib" / "pilottunnel" / "apply-manifests").exists())
        payload = json.loads(output)
        self.assertTrue(payload["dry_run"])

    def test_real_host_apply_does_not_call_systemctl_or_touch_firewall_routes(self) -> None:
        self._create_profile()
        self._stage_switch("backhaul", "tcpmux")
        self._import_binary("backhaul")
        with self._mock_real_host():
            code, output = self.run_cli(
                "install",
                "apply",
                "--profile",
                "turkey-6221",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertFalse(payload["service_started"])
        self.assertFalse(payload["service_enabled"])
        self.assertFalse(payload["systemctl_executed"])
        self.assertFalse(payload["firewall_touched"])
        self.assertFalse(payload["routes_touched"])

    def test_real_host_apply_path_traversal_remains_blocked(self) -> None:
        with self._mock_real_host():
            code, output = self.run_cli(
                "install",
                "apply",
                "--profile",
                "../bad",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--real-host-files",
                "--confirm",
                "REAL_FILES_APPLY",
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
