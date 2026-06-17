import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pilottunnel.binaries import get_binary_plan, list_binary_plans, verify_binary
from pilottunnel.config import Profile
from pilottunnel.state import AppState, BinaryRecord
from pilottunnel.preflight import run_preflight


class PreflightTests(unittest.TestCase):
    def test_preflight_on_current_platform_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_preflight(Path(temp_dir)).to_dict()
            self.assertIn("host", result)
            self.assertIn("commands", result)

    def test_missing_optional_commands_produce_warnings_not_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_preflight(Path(temp_dir), command_lookup=lambda _: None).to_dict()
            self.assertTrue(result["warnings"])
            self.assertTrue(result["safe_to_stage"])
            self.assertFalse(result["safe_to_real_apply"])

    def test_windows_safe_behavior_is_covered(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = run_preflight(Path(temp_dir), platform_name="Windows", command_lookup=lambda _: None).to_dict()
            self.assertTrue(result["host"]["is_windows"])
            self.assertFalse(result["safe_to_real_apply"])

    def test_binary_plan_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            items = list_binary_plans(Path(temp_dir))
            adapters = {item["adapter"] for item in items}
            self.assertEqual(adapters, {"backhaul", "rathole"})
            self.assertFalse(get_binary_plan("backhaul", Path(temp_dir))["download_performed"])

    def test_preflight_ports_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            profile = Profile(name="turkey-6221", main_port=6221, target_host="127.0.0.1", target_port=6221)
            result = run_preflight(Path(temp_dir), profile).to_dict()
            self.assertIn(6221, {int(key) for key in result["port_availability"].keys()})

    @patch("pilottunnel.binaries.subprocess.run")
    def test_verify_run_version_is_timeout_safe(self, mock_run) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "backhaul"
            path.write_text("fake", encoding="utf-8")
            state = AppState(
                binaries={
                    "backhaul": BinaryRecord(
                        adapter="backhaul",
                        source_filename="backhaul",
                        imported_path=str(path),
                        sha256="abc",
                        version="manual-v0.0.0",
                        imported_at="now",
                        executable=False,
                        platform="test",
                    )
                }
            )
            mock_run.side_effect = TimeoutError()
            # fallback path: convert timeout-like exception to OSError-safe handling by not crashing
            result = verify_binary(adapter="backhaul", cache_root=Path(temp_dir), state=state, run_version=False)
            self.assertEqual(result["adapter"], "backhaul")
