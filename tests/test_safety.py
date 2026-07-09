import json
import tempfile
import unittest
from pathlib import Path

from pilottunnel.adapters import ADAPTERS
from pilottunnel.audit import redact_secrets, write_audit_log
from pilottunnel.config import AppConfig, Profile, ProfilePorts, SUPPORTED_LAYERS
from pilottunnel.registry import PortRegistry, RegistryEntry
from pilottunnel.state import AppState
from pilottunnel.switch_engine import SwitchEngine, SwitchPaths
from testsupport import allocate_tcp_ports


class SafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        ports, listeners = allocate_tcp_ports(5)
        self.main_port, self.target_port, self.control_port, self.service_port, self.check_port = ports
        for listener in listeners:
            listener.close()

    def test_secrets_are_redacted_from_audit_logs_and_dry_run_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "audit.log"
            write_audit_log(
                "switch",
                "smoke-l4-001",
                {"token": "123", "dry_run": True, "nested": {"password": "abc"}},
                log_path,
            )
            record = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(record["details"]["token"], "***REDACTED***")
            self.assertEqual(record["details"]["nested"]["password"], "***REDACTED***")
            self.assertTrue(record["details"]["dry_run"])

    def test_secret_redaction_keeps_health_fields_visible(self) -> None:
        payload = {
            "real_pass": True,
            "pass": "passed",
            "pass_rate": 0.75,
            "passed": True,
            "password": "hidden",
            "token": "token-value",
            "auth": "user:pass",
            "private_key": "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
            "nested": {
                "BORE_SECRET": "secret-token",
                "AUTH": "operator:secret",
                "ok": True,
                "runtime_systemd_ok": False,
                "failure_count": 1,
            },
        }
        redacted = redact_secrets(payload)
        self.assertIs(redacted["real_pass"], True)
        self.assertEqual(redacted["pass"], "passed")
        self.assertEqual(redacted["pass_rate"], 0.75)
        self.assertIs(redacted["passed"], True)
        self.assertEqual(redacted["password"], "***REDACTED***")
        self.assertEqual(redacted["token"], "***REDACTED***")
        self.assertEqual(redacted["auth"], "***REDACTED***")
        self.assertEqual(redacted["private_key"], "***REDACTED***")
        self.assertEqual(redacted["nested"]["BORE_SECRET"], "***REDACTED***")
        self.assertEqual(redacted["nested"]["AUTH"], "***REDACTED***")
        self.assertIs(redacted["nested"]["ok"], True)
        self.assertIs(redacted["nested"]["runtime_systemd_ok"], False)
        self.assertEqual(redacted["nested"]["failure_count"], 1)

    def test_unsupported_layers_are_listed_but_blocked(self) -> None:
        self.assertIn("layer7", SUPPORTED_LAYERS)
        config = AppConfig(profiles=[Profile(name="smoke-l4-001", main_port=self.main_port, target_host="127.0.0.1", target_port=self.target_port)])
        engine = SwitchEngine(
            config=config,
            state=AppState(),
            registry=PortRegistry(),
            paths=SwitchPaths(
                lock_dir=Path(tempfile.gettempdir()) / "locks",
                work_dir=Path(tempfile.gettempdir()) / "work",
                audit_path=Path(tempfile.gettempdir()) / "audit.log",
                staging_root=Path(tempfile.gettempdir()) / "staging",
            ),
        )
        with self.assertRaises(ValueError):
            engine.switch("smoke-l4-001", "wstunnel", "ws", apply_changes=False)

    def test_unsupported_backhaul_experimental_tun_transports_blocked(self) -> None:
        adapter = ADAPTERS["backhaul"]()
        profile = Profile(name="smoke-l4-001", main_port=self.main_port, target_host="127.0.0.1", target_port=self.target_port)
        ok, reason = adapter.precheck(
            __import__("pilottunnel.adapters.base", fromlist=["AdapterContext"]).AdapterContext(
                profile=profile,
                transport="tcptun",
                work_dir=Path(tempfile.gettempdir()),
                staging_root=Path(tempfile.gettempdir()) / "staging",
                role="controller",
            )
        )
        self.assertFalse(ok)
        self.assertIn("blocked in v0.1", reason)

    def test_registry_catches_service_control_and_check_port_conflicts(self) -> None:
        registry = PortRegistry(
            owners={
                "a": RegistryEntry(
                    profile="a",
                    main_port=self.main_port,
                    adapter="backhaul",
                    transport="tcp",
                    role="controller",
                    owned_ports=[self.main_port, self.control_port, self.service_port, self.check_port],
                    owned_services=["svc-a"],
                ),
                "b": RegistryEntry(
                    profile="b",
                    main_port=self.target_port,
                    adapter="rathole",
                    transport="tcp",
                    role="worker",
                    owned_ports=[self.target_port, self.check_port],
                    owned_services=["svc-b"],
                ),
            }
        )
        self.assertTrue(any("conflict on owned ports" in item for item in registry.check_conflicts()))
