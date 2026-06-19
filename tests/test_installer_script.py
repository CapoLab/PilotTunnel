import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class InstallerScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.script_path = Path("scripts") / "install.sh"
        cls.script_text = cls.script_path.read_text(encoding="utf-8")

    def test_install_script_exists_and_is_text_safe(self) -> None:
        self.assertTrue(self.script_path.exists())
        content = self.script_path.read_bytes()
        self.assertTrue(content.startswith(b"#!/usr/bin/env bash"))
        self.assertNotIn(b"\x00", content)

    def test_install_script_help_mentions_expected_options(self) -> None:
        for token in (
            "--dry-run",
            "--role",
            "--repo-url",
            "--ref",
            "--install-dir",
            "--confirm",
            "INSTALL_PILOTTUNNEL",
        ):
            self.assertIn(token, self.script_text)

        bash_bin = shutil.which("bash")
        if bash_bin:
            result = subprocess.run(
                [bash_bin, str(self.script_path), "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("--dry-run", result.stdout)
            self.assertIn("--repo-url", result.stdout)

    def test_install_script_refuses_apply_without_exact_confirmation(self) -> None:
        bash_bin = shutil.which("bash")
        if bash_bin:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = subprocess.run(
                    [
                        bash_bin,
                        str(self.script_path),
                        "--role",
                        "controller",
                        "--repo-url",
                        ".",
                        "--ref",
                        "main",
                        "--install-dir",
                        temp_dir,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Apply mode requires --confirm INSTALL_PILOTTUNNEL", result.stderr)
        else:
            self.assertIn("Apply mode requires --confirm INSTALL_PILOTTUNNEL", self.script_text)

    def test_install_script_has_no_forbidden_private_or_country_strings(self) -> None:
        lower_text = self.script_text.lower()
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
            self.assertNotIn(term, lower_text)

    def test_install_script_has_no_hardcoded_private_ports(self) -> None:
        self.assertNotRegex(self.script_text, r"\b\d{4,5}\b")

    def test_install_script_does_not_call_firewall_tools(self) -> None:
        self.assertNotRegex(self.script_text, r"\b(?:iptables|nft|ufw|firewall-cmd)\b")

    def test_install_script_does_not_call_route_or_interface_mutation_commands(self) -> None:
        self.assertNotRegex(self.script_text, r"\b(?:ifconfig|nmcli)\b")
        self.assertNotRegex(self.script_text, r"\bip\s+(?:route|link|addr)\b")

    def test_install_script_does_not_call_live_systemctl_actions(self) -> None:
        self.assertNotRegex(self.script_text, r"\bsystemctl\s+(?:start|stop|restart|enable|disable)\b")
        self.assertNotRegex(self.script_text, r"\bdaemon-reload\b")

    def test_install_script_does_not_run_adapter_binaries(self) -> None:
        self.assertNotRegex(self.script_text, r"\b(?:backhaul|rathole|frpc|gost|chisel|realm|bore)\b.*(?:--version|\s)")

    def test_readme_documents_safe_one_command_setup(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        self.assertIn("One-Command Non-Production Server Setup", readme)
        self.assertIn("scripts/install.sh", readme)
        self.assertIn("--confirm INSTALL_PILOTTUNNEL", readme)
