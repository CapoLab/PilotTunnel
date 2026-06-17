import json
import tempfile
import unittest
from pathlib import Path

from pilottunnel.audit import write_audit_log
from pilottunnel.config import AppConfig, Profile, SUPPORTED_LAYERS
from pilottunnel.registry import PortRegistry
from pilottunnel.state import AppState
from pilottunnel.switch_engine import SwitchEngine, SwitchPaths


class SafetyTests(unittest.TestCase):
    def test_secrets_are_redacted_from_audit_logs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "audit.log"
            write_audit_log(
                "switch",
                "turkey-6221",
                {"token": "123", "nested": {"password": "abc"}},
                log_path,
            )
            record = json.loads(log_path.read_text(encoding="utf-8").strip())
            self.assertEqual(record["details"]["token"], "***REDACTED***")
            self.assertEqual(record["details"]["nested"]["password"], "***REDACTED***")

    def test_unsupported_layers_are_listed_but_blocked(self) -> None:
        self.assertIn("layer7", SUPPORTED_LAYERS)
        config = AppConfig(profiles=[Profile(name="turkey-6221", main_port=6221, target_host="127.0.0.1", target_port=6221)])
        engine = SwitchEngine(
            config=config,
            state=AppState(),
            registry=PortRegistry(),
            paths=SwitchPaths(lock_dir=Path(tempfile.gettempdir()) / "locks", work_dir=Path(tempfile.gettempdir()) / "work", audit_path=Path(tempfile.gettempdir()) / "audit.log"),
        )
        with self.assertRaises(ValueError):
            engine.switch("turkey-6221", "wstunnel", "ws", apply_changes=False)
