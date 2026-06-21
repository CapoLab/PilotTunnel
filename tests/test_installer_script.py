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
        cls.menu_path = Path("scripts") / "pilottunnel-menu"
        cls.menu_text = cls.menu_path.read_text(encoding="utf-8")

    def test_install_script_exists_and_is_text_safe(self) -> None:
        self.assertTrue(self.script_path.exists())
        content = self.script_path.read_bytes()
        self.assertTrue(content.startswith(b"#!/usr/bin/env bash"))
        self.assertNotIn(b"\x00", content)

    def test_install_script_help_mentions_expected_options(self) -> None:
        for token in (
            "--dry-run",
            "--no-menu",
            "--role",
            "--repo-url",
            "--ref",
            "--install-dir",
            "--with-binaries",
            "--no-binaries",
            "--manifest-url",
            "--manifest-file",
            "--allow-provider-host",
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
            self.assertIn("--no-menu", result.stdout)
            self.assertIn("--repo-url", result.stdout)
            self.assertIn("bash install.sh", result.stdout)

    def test_install_script_launches_menu_after_base_install(self) -> None:
        self.assertIn("install_menu_launcher", self.script_text)
        self.assertIn("launch_menu_if_requested", self.script_text)
        self.assertIn("pilottunnel-menu", self.script_text)
        self.assertIn("multi-layer", self.script_text)

    def test_public_install_does_not_require_role_or_basic_confirmation(self) -> None:
        bash_bin = shutil.which("bash")
        if bash_bin:
            with tempfile.TemporaryDirectory() as temp_dir:
                result = subprocess.run(
                    [
                        bash_bin,
                        str(self.script_path),
                        "--install-dir",
                        temp_dir,
                        "--no-menu",
                        "--no-binaries",
                        "--dry-run",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("deferred until Setup / Configure this server", result.stdout)
        else:
            self.assertIn("role: ${ROLE:-deferred until Setup / Configure this server}", self.script_text)
        self.assertNotIn("INSTALL_" + "PILOTTUNNEL", self.script_text)

    def test_menu_exposes_required_choices_and_defers_role_selection(self) -> None:
        self.assertTrue(self.menu_path.exists())
        for label in (
            "PilotTunnel Menu",
            "Setup / Configure this server",
            "Status",
            "Readiness report",
            "Binary status",
            "Service management",
            "Backup / Restore",
            "Main entry server / controller",
            "Remote endpoint server / worker",
        ):
            self.assertIn(label, self.menu_text)
        self.assertNotIn("prompt_role", self.script_text)

    def test_menu_role_choices_map_to_controller_and_worker(self) -> None:
        bash_bin = shutil.which("bash")
        if not bash_bin:
            self.skipTest("bash is not available")
        menu_path = self.menu_path.as_posix()
        for selection, expected in (("1", "controller"), ("2", "worker")):
            result = subprocess.run(
                [bash_bin, "-c", f"source '{menu_path}'; map_role_selection {selection}"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(result.stdout.strip(), expected)

    def test_installer_binary_status_uses_supported_arguments(self) -> None:
        status_lines = [line for line in self.script_text.splitlines() if "binary status" in line]
        self.assertEqual(len(status_lines), 1)
        self.assertNotIn("--json", status_lines[0])

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

    def test_install_script_uses_placeholder_based_bootstrap_inputs(self) -> None:
        self.assertNotIn("--main-port", self.script_text)
        self.assertNotIn("--target-port", self.script_text)
        self.assertNotIn("--control-port", self.script_text)
        self.assertNotIn("--service-port", self.script_text)
        self.assertNotIn("--check-port", self.script_text)

    def test_install_script_does_not_call_firewall_tools(self) -> None:
        self.assertNotRegex(self.script_text, r"\b(?:iptables|nft|ufw|firewall-cmd)\b")

    def test_install_script_does_not_call_route_or_interface_mutation_commands(self) -> None:
        self.assertNotRegex(self.script_text, r"\b(?:ifconfig|nmcli)\b")
        self.assertNotRegex(self.script_text, r"\bip\s+(?:route|link|addr)\b")

    def test_install_script_does_not_call_live_systemctl_actions(self) -> None:
        self.assertNotRegex(self.script_text, r"\bsystemctl\s+(?:start|stop|restart|enable|disable)\b")
        self.assertNotRegex(self.script_text, r"\bdaemon-reload\b")
        self.assertNotIn("pt_cli service start", self.script_text)
        self.assertNotIn("pt_cli service stop", self.script_text)

    def test_install_script_does_not_run_adapter_binaries(self) -> None:
        self.assertNotRegex(self.script_text, r"\b(?:backhaul|rathole|frpc|gost|chisel|realm|bore)\b.*(?:--version|\s)")

    def test_install_script_uses_manifest_only_binary_workflow(self) -> None:
        self.assertIn("binary download-all", self.script_text)
        self.assertIn("binary status --require-all", self.script_text)
        self.assertNotIn("binary source fetch", self.script_text)
        self.assertNotIn("binary provider prepare", self.script_text)

    def test_install_script_does_not_reference_dynamic_upstream_release_endpoints(self) -> None:
        self.assertNotIn("api.github.com", self.script_text)
        dynamic_latest = "/".join(("releases", "latest"))
        self.assertNotIn(dynamic_latest, self.script_text)

    def test_readme_documents_safe_one_command_setup(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        lower_readme = readme.lower()
        self.assertIn("One-line install", readme)
        self.assertIn("scripts/install.sh", readme)
        self.assertIn("Run the same installer on every server.", readme)
        self.assertIn("choose its role later from the menu", lower_readme)
        self.assertIn(
            "https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh",
            readme,
        )
        self.assertIn("multi-layer", lower_readme)
        self.assertIn(
            "bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh)",
            readme,
        )
        self.assertRegex(
            readme,
            r"## One-line install\s+```bash\s+bash <\(curl -fsSL https://raw\.githubusercontent\.com/CapoLab/PilotTunnel/main/scripts/install\.sh\)\s+```",
        )
        self.assertNotIn("INSTALL_" + "PILOTTUNNEL", readme)
