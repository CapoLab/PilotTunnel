from contextlib import contextmanager
import os
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

    @staticmethod
    def find_bash() -> str | None:
        return shutil.which("bash") or str(Path("C:/Program Files/Git/bin/bash.exe"))

    @staticmethod
    def find_git() -> str | None:
        candidate = shutil.which("git") or str(Path("C:/Program Files/Git/cmd/git.exe"))
        return candidate if Path(candidate).exists() else None

    @staticmethod
    def to_bash_path(path: Path | str) -> str:
        resolved = Path(path).resolve()
        text = str(resolved)
        if len(text) >= 3 and text[1] == ":":
            return f"/{text[0].lower()}{text[2:].replace('\\', '/')}"
        return text.replace("\\", "/")

    @contextmanager
    def snapshot_repo(self) -> str:
        git_bin = self.find_git()
        if not git_bin:
            self.skipTest("git is not available")
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir) / "source-repo"
            shutil.copytree(
                Path.cwd(),
                source_root,
                ignore=shutil.ignore_patterns(
                    ".git",
                    "__pycache__",
                    ".pytest_cache",
                    ".mypy_cache",
                    ".ruff_cache",
                    ".var",
                ),
            )
            for command in (
                [git_bin, "init", "--initial-branch", "main"],
                [git_bin, "config", "user.name", "Installer Test"],
                [git_bin, "config", "user.email", "installer-tests@example.invalid"],
                [git_bin, "add", "."],
                [git_bin, "commit", "-m", "snapshot"],
            ):
                subprocess.run(command, cwd=source_root, capture_output=True, text=True, check=True)
            yield self.to_bash_path(source_root)

    def run_installer(self, *args: str, input_text: str | None = None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        bash_bin = self.find_bash()
        if not bash_bin or not Path(bash_bin).exists():
            self.skipTest("bash is not available")
        env = os.environ.copy()
        env["PILOTTUNNEL_SKIP_DEPENDENCY_CHECKS"] = "1"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [bash_bin, str(self.script_path), *args],
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            cwd=Path.cwd(),
            env=env,
        )

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
            "--debug",
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

        bash_bin = self.find_bash()
        if bash_bin and Path(bash_bin).exists():
            result = subprocess.run(
                [bash_bin, str(self.script_path), "--help"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("--dry-run", result.stdout)
            self.assertIn("--no-menu", result.stdout)
            self.assertIn("--debug", result.stdout)
            self.assertIn("--repo-url", result.stdout)
            self.assertIn("bash install.sh", result.stdout)

    def test_public_no_arg_flow_is_compact_and_opens_menu(self) -> None:
        with self.snapshot_repo() as repo_url, tempfile.TemporaryDirectory() as temp_dir:
            result = self.run_installer(
                "--install-dir",
                self.to_bash_path(temp_dir),
                "--repo-url",
                repo_url,
                "--without-binaries",
                input_text="7\n",
                extra_env={"PILOTTUNNEL_MENU_ALLOW_NON_TTY": "1"},
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("PilotTunnel Installer", result.stdout)
        self.assertIn("Checking system...", result.stdout)
        self.assertIn("Installing/updating PilotTunnel...", result.stdout)
        self.assertIn("Preparing required binaries...", result.stdout)
        self.assertIn("Required binaries: skipped (--without-binaries)", result.stdout)
        self.assertIn("Safety: no services started, no firewall/routes changed", result.stdout)
        self.assertIn("Opening PilotTunnel menu...", result.stdout)
        self.assertIn("PilotTunnel Menu", result.stdout)
        self.assertIn("1. Setup / Configure this server", result.stdout)
        self.assertIn("7. Exit", result.stdout)
        self.assertNotIn("PilotTunnel installer plan", result.stdout)
        self.assertNotIn('"action": "binary-download-all"', result.stdout)
        self.assertNotIn('"results": [', result.stdout)
        self.assertNotIn('"ok":', result.stdout)

    def test_install_menu_launcher_handles_same_resolved_file(self) -> None:
        bash_bin = self.find_bash()
        if not bash_bin or not Path(bash_bin).exists():
            self.skipTest("bash is not available")
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo_scripts = root / "repo" / "scripts"
            bin_dir = root / "bin"
            repo_scripts.mkdir(parents=True)
            bin_dir.mkdir(parents=True)
            menu_source = repo_scripts / "pilottunnel-menu"
            menu_source.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            menu_source.chmod(0o755)
            menu_target = bin_dir / "pilottunnel-menu"
            try:
                os.link(menu_source, menu_target)
            except OSError as exc:
                self.skipTest(f"hard link creation is not available on this host: {exc}")
            result = subprocess.run(
                [
                    bash_bin,
                    "-c",
                    (
                        f"source '{self.script_path.as_posix()}'; "
                        f"REPO_DIR='{(root / 'repo').as_posix()}'; "
                        f"BIN_DIR='{bin_dir.as_posix()}'; "
                        "install_menu_launcher"
                    ),
                ],
                capture_output=True,
                text=True,
                check=False,
                cwd=Path.cwd(),
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue(os.path.samefile(menu_source, menu_target))
            execute_check = subprocess.run(
                [bash_bin, "-c", f"test -x '{menu_target.as_posix()}'"],
                capture_output=True,
                text=True,
                check=False,
                cwd=Path.cwd(),
            )
            self.assertEqual(execute_check.returncode, 0, msg=execute_check.stderr)

    def test_public_install_does_not_require_role_or_basic_confirmation(self) -> None:
        bash_bin = self.find_bash()
        if bash_bin and Path(bash_bin).exists():
            with tempfile.TemporaryDirectory() as temp_dir:
                result = subprocess.run(
                    [
                        bash_bin,
                        str(self.script_path),
                        "--install-dir",
                        self.to_bash_path(temp_dir),
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

    def test_debug_mode_can_show_detailed_output(self) -> None:
        with self.snapshot_repo() as repo_url, tempfile.TemporaryDirectory() as temp_dir:
            result = self.run_installer(
                "--install-dir",
                self.to_bash_path(temp_dir),
                "--repo-url",
                repo_url,
                "--without-binaries",
                "--no-menu",
                "--debug",
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("PilotTunnel installer plan", result.stdout)
        self.assertIn("with_binaries: false", result.stdout)

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
            "Safety-first multi-layer tunnel management",
        ):
            self.assertIn(label, self.menu_text)
        self.assertNotIn("prompt_role", self.script_text)

    def test_setup_menu_maps_main_and_remote_roles(self) -> None:
        bash_bin = self.find_bash()
        if not bash_bin or not Path(bash_bin).exists():
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

    def test_installer_launches_menu_via_terminal_handoff_or_fallback(self) -> None:
        self.assertIn("Opening PilotTunnel menu...", self.script_text)
        self.assertIn("/dev/tty", self.script_text)
        self.assertIn("Menu could not be opened automatically", self.script_text)

    def test_menu_uses_banner_and_color_helpers(self) -> None:
        self.assertIn(" ____  _ _", self.menu_text)
        self.assertIn("init_theme()", self.menu_text)
        self.assertIn("tput cols", self.menu_text)
        self.assertIn("FRAME=", self.menu_text)
        self.assertIn("\\033", self.menu_text)

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
        self.assertIn("opens the menu", lower_readme)
        self.assertIn("choose the server role later", lower_readme)
        self.assertIn(
            "https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh",
            readme,
        )
        self.assertIn(
            "bash <(curl -fsSL https://raw.githubusercontent.com/CapoLab/PilotTunnel/main/scripts/install.sh)",
            readme,
        )
        self.assertRegex(
            readme,
            r"## One-line install\s+```bash\s+bash <\(curl -fsSL https://raw\.githubusercontent\.com/CapoLab/PilotTunnel/main/scripts/install\.sh\)\s+```",
        )
        self.assertNotIn("INSTALL_" + "PILOTTUNNEL", readme)
