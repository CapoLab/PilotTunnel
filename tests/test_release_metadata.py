import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from pilottunnel import cli


class ReleaseMetadataTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            with redirect_stdout(output):
                code = cli.main(
                    [
                        "--config",
                        str(base / "config.json"),
                        "--state",
                        str(base / "state.json"),
                        "--registry",
                        str(base / "registry.json"),
                        "--audit-log",
                        str(base / "audit.log"),
                        "--lock-dir",
                        str(base / "locks"),
                        "--work-dir",
                        str(base / "work"),
                        "--staging-root",
                        str(base / "staging"),
                        *args,
                    ]
                )
        return code, output.getvalue()

    def test_version_command_works(self) -> None:
        code, output = self.run_cli("version")
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["project"], "PilotTunnel")
        self.assertEqual(payload["release_phase"], "v0.1-final")
        combined_notes = "\n".join(payload["safety_notes"]).lower()
        self.assertIn("auto-switch", combined_notes)
        self.assertIn("background", combined_notes)

    def test_version_command_includes_0_1_0(self) -> None:
        code, output = self.run_cli("version")
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(json.loads(output)["version"], "0.1.0")

    def test_release_docs_note_no_auto_switch_or_background_monitoring(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8").lower()
        release_notes = Path("RELEASE_NOTES.md").read_text(encoding="utf-8").lower()
        combined = f"{readme}\n{release_notes}"
        self.assertIn("no auto-switch", combined)
        self.assertIn("no background", combined)

    def test_release_docs_stay_generic(self) -> None:
        docs = "\n".join(
            [
                Path("RELEASE_NOTES.md").read_text(encoding="utf-8"),
                Path("CHANGELOG.md").read_text(encoding="utf-8"),
            ]
        )
        lower_docs = docs.lower()
        forbidden_terms = [
            "".join(chr(value) for value in codes)
            for codes in (
                (105, 114, 97, 110),
                (102, 111, 114, 101, 105, 103, 110),
                (107, 104, 97, 114, 101, 106),
                (116, 117, 114, 107, 101, 121),
            )
        ]
        for term in forbidden_terms:
            self.assertNotIn(term, lower_docs)
        self.assertNotIn("127.0.0.1", docs)
        self.assertNotRegex(docs, r"--(?:main|target|control|service|check)-port\s+\d+")
