from contextlib import contextmanager
import json
import os
import re
import sys
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from pilottunnel.binaries import binary_spec, provider_required_adapters


class InstallerScriptTests(unittest.TestCase):
    SNAPSHOT_IGNORE_PATTERNS = (
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".var",
        ".provider-source",
        ".provider-release",
        ".provider-release-2026-06-22",
        ".provider-debug",
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.script_path = Path("scripts") / "install.sh"
        cls.script_text = cls.script_path.read_text(encoding="utf-8")
        cls.menu_path = Path("scripts") / "pilottunnel-menu"
        cls.menu_text = cls.menu_path.read_text(encoding="utf-8")
        cls.test_runner_path = Path("scripts") / "pilottunnel-test"
        cls.test_runner_text = cls.test_runner_path.read_text(encoding="utf-8")

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
                ignore=shutil.ignore_patterns(*self.SNAPSHOT_IGNORE_PATTERNS),
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

    @contextmanager
    def menu_install_root(self) -> Path:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo_root = root / "repo"
            shutil.copytree(
                Path.cwd(),
                repo_root,
                ignore=shutil.ignore_patterns(*self.SNAPSHOT_IGNORE_PATTERNS),
            )
            for name in ("state", "work", "staging", "install-root"):
                (root / name).mkdir(parents=True, exist_ok=True)
            yield root

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

    def run_menu(self, base_dir: Path, input_text: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        bash_bin = self.find_bash()
        if not bash_bin or not Path(bash_bin).exists():
            self.skipTest("bash is not available")
        env = os.environ.copy()
        env["PILOTTUNNEL_MENU_ALLOW_NON_TTY"] = "1"
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [bash_bin, str(self.menu_path), "--base-dir", self.to_bash_path(base_dir)],
            input=input_text,
            capture_output=True,
            text=True,
            check=False,
            cwd=Path.cwd(),
            env=env,
        )

    def run_test_runner(self, base_dir: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        bash_bin = self.find_bash()
        if not bash_bin or not Path(bash_bin).exists():
            self.skipTest("bash is not available")
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [bash_bin, str(self.test_runner_path), "--base-dir", self.to_bash_path(base_dir), *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=Path.cwd(),
            env=env,
        )

    def run_base_cli(self, base_dir: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pilottunnel.cli",
                "--config",
                str(base_dir / "state" / "config.json"),
                "--state",
                str(base_dir / "state" / "state.json"),
                "--registry",
                str(base_dir / "state" / "registry.json"),
                "--audit-log",
                str(base_dir / "state" / "audit.log"),
                "--lock-dir",
                str(base_dir / "state" / "locks"),
                "--work-dir",
                str(base_dir / "work"),
                "--staging-root",
                str(base_dir / "staging"),
                *args,
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=Path.cwd(),
            env=env,
        )

    def create_controller_pairing_code(self, base_dir: Path) -> str:
        init_result = self.run_base_cli(base_dir, "init", "--role", "controller")
        self.assertEqual(init_result.returncode, 0, msg=init_result.stderr or init_result.stdout)
        create_result = self.run_base_cli(
            base_dir,
            "link",
            "create-controller",
            "--worker-address",
            "worker.example.invalid",
            "--service-port",
            "41181",
            "--transport-port",
            "41182",
            "--user-facing-port",
            "41183",
            "--controller-address-override",
            "controller.example.invalid",
        )
        self.assertEqual(create_result.returncode, 0, msg=create_result.stderr or create_result.stdout)
        export_result = self.run_base_cli(base_dir, "link", "export-pairing-code", "--label", "link-001")
        self.assertEqual(export_result.returncode, 0, msg=export_result.stderr or export_result.stdout)
        return json.loads(export_result.stdout)["pairing_code"]

    def create_source_archive_from_tree(self, tree_root: Path, output_dir: Path, *, archive_name: str = "PilotTunnel-source.tar.gz") -> Path:
        archive_path = output_dir / archive_name
        with tarfile.open(archive_path, "w:gz") as tar_handle:
            for path in tree_root.rglob("*"):
                if any(part in set(self.SNAPSHOT_IGNORE_PATTERNS) for part in path.parts):
                    continue
                arcname = Path("PilotTunnel-main") / path.relative_to(tree_root)
                tar_handle.add(path, arcname=str(arcname), recursive=False)
        return archive_path

    def create_source_archive(self, output_dir: Path) -> Path:
        return self.create_source_archive_from_tree(Path.cwd(), output_dir)

    def create_modified_source_archive(
        self,
        output_dir: Path,
        *,
        archive_name: str,
        replacements: dict[str, str] | None = None,
        removals: tuple[str, ...] = (),
    ) -> Path:
        with tempfile.TemporaryDirectory() as temp_dir:
            source_root = Path(temp_dir) / "archive-source"
            shutil.copytree(
                Path.cwd(),
                source_root,
                ignore=shutil.ignore_patterns(*self.SNAPSHOT_IGNORE_PATTERNS),
            )
            for relative_path, content in (replacements or {}).items():
                target = source_root / relative_path
                target.write_text(content, encoding="utf-8")
            for relative_path in removals:
                target = source_root / relative_path
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            return self.create_source_archive_from_tree(source_root, output_dir, archive_name=archive_name)

    def write_fake_git(self, target_dir: Path, body: str) -> Path:
        bash_bin = self.find_bash()
        if not bash_bin or not Path(bash_bin).exists():
            self.skipTest("bash is not available")
        git_path = target_dir / "git"
        git_path.write_text(f"#!/usr/bin/env bash\nset -eu\n{body}\n", encoding="utf-8")
        git_path.chmod(0o755)
        sleep_match = re.search(r"sleep\s+(\d+)", body)
        exit_match = re.search(r"exit\s+(\d+)", body)
        sleep_seconds = int(sleep_match.group(1)) if sleep_match else 0
        exit_code = int(exit_match.group(1)) if exit_match else 0
        git_cmd = target_dir / "git.cmd"
        if os.name == "nt":
            git_cmd.write_text(
                "@echo off\r\n"
                "setlocal\r\n"
                "python -c \"import sys,time; time.sleep(%d); sys.exit(%d)\"\r\n"
                "exit /b %d\r\n" % (sleep_seconds, exit_code, exit_code),
                encoding="utf-8",
            )
        else:
            git_cmd.write_text(f"#!/usr/bin/env bash\nset -eu\n{body}\n", encoding="utf-8")
            git_cmd.chmod(0o755)
        return git_path

    def write_fake_python(self, target_dir: Path, *, readiness_stdout: str, readiness_exit_code: int = 0) -> None:
        real_python = self.to_bash_path(sys.executable)
        wrapper = (
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "if [ \"$#\" -ge 3 ] && [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pilottunnel.cli\" ]; then\n"
            "  args=\" $* \"\n"
            "  case \"$args\" in\n"
            "    *\" readiness report --json \"*)\n"
            f"      printf '%s\\n' {readiness_stdout!r}\n"
            f"      exit {readiness_exit_code}\n"
            "      ;;\n"
            "  esac\n"
            "fi\n"
            f"exec '{real_python}' \"$@\"\n"
        )
        for executable in ("python3", "python"):
            path = target_dir / executable
            path.write_text(wrapper, encoding="utf-8")
            path.chmod(0o755)

    def write_fake_cli_python(self, target_dir: Path, *, body: str) -> None:
        real_python = self.to_bash_path(sys.executable)
        wrapper = (
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "if [ \"$#\" -ge 2 ] && [ \"$1\" = \"-m\" ] && [ \"$2\" = \"pilottunnel.cli\" ]; then\n"
            "  shift 2\n"
            f"{body}\n"
            "fi\n"
            f"exec '{real_python}' \"$@\"\n"
        )
        for executable in ("python3", "python"):
            path = target_dir / executable
            path.write_text(wrapper, encoding="utf-8")
            path.chmod(0o755)

    def write_fake_systemctl(self, target_dir: Path, *, body: str) -> None:
        path = target_dir / "systemctl"
        path.write_text(f"#!/usr/bin/env bash\nset -eu\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def write_manifest_fixture(self, base_dir: Path) -> Path:
        manifest_path = base_dir / "state" / "provider-manifest.json"
        binaries = []
        for adapter in provider_required_adapters():
            spec = binary_spec(adapter)
            binaries.append(
                {
                    "adapter": adapter,
                    "binary_name": spec.binary_name,
                    "version": "test-version",
                    "platform": "windows-amd64",
                    "filename": spec.binary_name,
                    "url": f"https://example.invalid/{spec.binary_name}",
                    "sha256": "a" * 64,
                    "size_bytes": 1,
                }
            )
        manifest_path.write_text(
            __import__("json").dumps(
                {
                    "schema": "pilottunnel-binary-provider-v1",
                    "provider": "test-provider",
                    "generated_at": "2026-06-21T00:00:00+00:00",
                    "binaries": binaries,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return manifest_path

    def write_config_fixture(
        self,
        base_dir: Path,
        *,
        role: str = "",
        display_name: str = "",
        manifest_path: Path | None = None,
        links: list[dict] | None = None,
        active_link_label: str = "",
    ) -> None:
        payload = {
            "controller_role": "controller",
            "worker_role": "worker",
            "pre_armed_configs": False,
            "partition_mode": False,
            "binary_resolution": {
                "managed_install_dir": "",
                "provider_manifest": str(manifest_path) if manifest_path else "",
                "provider_allow_host": "example.invalid" if manifest_path else "",
                "allow_system_path": False,
                "prefer_managed_install": True,
            },
            "node": {
                "node_id": "node-test-001",
                "node_role": role,
                "initialized_at": "2026-06-21T00:00:00+00:00" if role else "",
                "role_alias_used": role,
                "normalized_role": role,
                "side_label": "Iran side" if role == "controller" else ("Kharej side" if role == "worker" else ""),
                "preferred_layer": "layer4" if role else "",
                "preferred_layer_selected_at": "2026-06-21T00:00:00+00:00" if role else "",
                "display_name": display_name,
                "install_root": str(base_dir),
                "state_directory": str(base_dir / "state"),
                "work_directory": str(base_dir / "work"),
                "endpoint_address": "",
                "notes": "",
                "active_link_label": active_link_label,
                "managed_remote_endpoints": [],
            },
            "links": links or [],
            "profiles": [],
        }
        (base_dir / "state" / "config.json").write_text(__import__("json").dumps(payload, indent=2), encoding="utf-8")
        (base_dir / "state" / "state.json").write_text('{"profiles": {}, "binaries": {}, "manual_active_tunnel": "", "manual_previous_tunnel": "", "last_manual_switch": {}}', encoding="utf-8")
        (base_dir / "state" / "registry.json").write_text('{"owners": {}}', encoding="utf-8")

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
                input_text="8\n",
                extra_env={"PILOTTUNNEL_MENU_ALLOW_NON_TTY": "1"},
            )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("PilotTunnel Installer", result.stdout)
        self.assertIn("[1/5] Checking system packages", result.stdout)
        self.assertIn("[2/5] Installing/updating PilotTunnel source", result.stdout)
        self.assertIn("[3/5] Preparing required binaries", result.stdout)
        self.assertIn("[4/5] Running safe checks", result.stdout)
        self.assertIn("[5/5] Opening PilotTunnel menu", result.stdout)
        self.assertIn("Required binaries: skipped (--without-binaries)", result.stdout)
        self.assertIn("Safety: no services started, no firewall/routes changed", result.stdout)
        self.assertIn("Opening PilotTunnel menu...", result.stdout)
        self.assertIn("PilotTunnel Menu", result.stdout)
        self.assertIn("1. Setup / Configure this server", result.stdout)
        self.assertIn("8. Exit", result.stdout)
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

    def test_pilottunnel_test_help_and_bash_syntax_are_valid(self) -> None:
        bash_bin = self.find_bash()
        if not bash_bin or not Path(bash_bin).exists():
            self.skipTest("bash is not available")
        syntax = subprocess.run(
            [bash_bin, "-n", str(self.test_runner_path)],
            capture_output=True,
            text=True,
            check=False,
            cwd=Path.cwd(),
        )
        self.assertEqual(syntax.returncode, 0, msg=syntax.stderr)
        help_result = subprocess.run(
            [bash_bin, str(self.test_runner_path), "--help"],
            capture_output=True,
            text=True,
            check=False,
            cwd=Path.cwd(),
        )
        self.assertEqual(help_result.returncode, 0, msg=help_result.stderr)
        self.assertIn("pilottunnel-test --link <LINK> --adapter <ADAPTER>", help_result.stdout)
        self.assertIn("--start-only", help_result.stdout)
        self.assertIn("--smoke-only", help_result.stdout)

    def test_install_script_installs_pilottunnel_test_launcher(self) -> None:
        with self.snapshot_repo() as repo_url, tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir)
            result = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                repo_url,
                "--without-binaries",
                "--no-menu",
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((install_dir / "repo" / "scripts" / "pilottunnel-test").exists())
            self.assertTrue((install_dir / "bin" / "pilottunnel-test").exists())
            self.assertTrue(os.access(install_dir / "bin" / "pilottunnel-test", os.X_OK))

    def test_install_script_test_mode_installs_and_invokes_candidate_runner(self) -> None:
        with self.snapshot_repo() as repo_url, tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "install"
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            self.write_fake_cli_python(
                fake_bin,
                body=(
                    f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(command_log)}'\n"
                    "args=\" $* \"\n"
                    "if [[ \"$args\" == *\" node status \"* ]]; then\n"
                    "  printf '%s\\n' '{\"normalized_role\": \"controller\", \"initialized\": true}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate prepare-all \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"candidates\": [{\"adapter\": \"rathole\", \"runnable\": true, \"blockers\": [], \"warnings\": []}]}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate start \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"runtime_config_status\": \"current\", \"message\": \"started\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate result \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"candidates\": [{\"adapter\": \"rathole\", \"runtime_systemd_ok\": true, \"runtime_services\": [{\"service_name\": \"pilottunnel-link-001-rathole.service\", \"active_state\": \"active\", \"sub_state\": \"running\"}]}]}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate smoke-test \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"runtime_config_status\": \"current\", \"result\": {\"probe_status\": \"passed\", \"real_service_status\": \"passed\"}}'\n"
                    "  exit 0\n"
                    "fi\n"
                ),
            )
            result = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                repo_url,
                "--without-binaries",
                "--test",
                "--link",
                "link-001",
                "--adapter",
                "rathole",
                "--attempts",
                "4",
                "--timeout",
                "6",
                "--json",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertTrue((install_dir / "bin" / "pilottunnel-test").exists())
            payload = json.loads(result.stdout[result.stdout.rfind("{"):])
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["role"], "controller")
            commands = command_log.read_text(encoding="utf-8").splitlines()
            runner_calls = "\n".join(commands)
            self.assertIn(f"--config {self.to_bash_path(install_dir / 'state' / 'config.json')}", runner_calls)
            self.assertIn("candidate prepare-all --link link-001 --json", runner_calls)
            self.assertIn("candidate start --adapter rathole --link link-001 --json", runner_calls)
            self.assertIn("candidate smoke-test --adapter rathole --link link-001 --mode probe --attempts 4 --timeout 6 --json", runner_calls)
            self.assertIn("candidate smoke-test --adapter rathole --link link-001 --mode real_service --attempts 4 --timeout 6 --json", runner_calls)

    def test_pilottunnel_test_uses_opt_pilottunnel_layout_paths(self) -> None:
        self.assertIn('DEFAULT_BASE_DIR="/opt/pilottunnel"', self.test_runner_text)
        self.assertIn('REPO_DIR="/opt/pilottunnel/repo"', self.test_runner_text)
        self.assertIn('CONFIG_FILE="/opt/pilottunnel/state/config.json"', self.test_runner_text)
        self.assertIn('STATE_FILE="/opt/pilottunnel/state/state.json"', self.test_runner_text)
        self.assertIn('REGISTRY_FILE="/opt/pilottunnel/state/registry.json"', self.test_runner_text)
        self.assertIn('AUDIT_LOG="/opt/pilottunnel/state/audit.log"', self.test_runner_text)
        self.assertIn('LOCK_DIR="/opt/pilottunnel/state/locks"', self.test_runner_text)
        self.assertIn('WORK_DIR="/opt/pilottunnel/work"', self.test_runner_text)
        self.assertIn('STAGING_ROOT="/opt/pilottunnel/staging"', self.test_runner_text)

    def test_worker_test_runner_retries_after_probe_busy_with_candidate_stop(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            prepare_count = Path(temp_dir) / "prepare.count"
            self.write_fake_cli_python(
                fake_bin,
                body=(
                    f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(command_log)}'\n"
                    f"count_file='{self.to_bash_path(prepare_count)}'\n"
                    "args=\" $* \"\n"
                    "if [[ \"$args\" == *\" node status \"* ]]; then\n"
                    "  printf '%s\\n' '{\"normalized_role\": \"worker\", \"initialized\": true}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate prepare-all \"* ]]; then\n"
                    "  count=0\n"
                    "  [ -f \"$count_file\" ] && count=\"$(cat \"$count_file\")\"\n"
                    "  count=$((count + 1))\n"
                    "  printf '%s' \"$count\" > \"$count_file\"\n"
                    "  if [ \"$count\" = \"1\" ]; then\n"
                    "    printf '%s\\n' '{\"ok\": true, \"message\": \"prepared\", \"candidates\": [{\"adapter\": \"rathole\", \"runnable\": false, \"state\": \"config_only\", \"blockers\": [\"Probe/test port is unavailable\"], \"warnings\": []}]}'\n"
                    "    exit 0\n"
                    "  fi\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"prepared\", \"candidates\": [{\"adapter\": \"rathole\", \"runnable\": true, \"state\": \"prepared\", \"blockers\": [], \"warnings\": []}]}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate result \"* ]]; then\n"
                    "  printf '%s\\n' '{\"candidates\": [{\"adapter\": \"rathole\", \"runtime_systemd_state\": \"active\"}]}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate stop \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"stopped\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate start \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"worker ready\", \"runtime_config_status\": \"current\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                ),
            )
            result = self.run_test_runner(
                base_dir,
                "--link",
                "link-001",
                "--adapter",
                "rathole",
                "--json",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["role"], "worker")
            self.assertEqual(payload["status"], "worker_ready")
            commands = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(prepare_count.read_text(encoding="utf-8"), "2")
            self.assertTrue(any("candidate stop" in line for line in commands))
            self.assertTrue(any("candidate start" in line for line in commands))

    def write_fake_worker_restore_cli(self, fake_bin: Path, command_log: Path, active_file: Path) -> None:
        self.write_fake_cli_python(
            fake_bin,
            body=(
                f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(command_log)}'\n"
                f"active_file='{self.to_bash_path(active_file)}'\n"
                "args=\" $* \"\n"
                "active=\"\"\n"
                "[ -f \"$active_file\" ] && active=\"$(cat \"$active_file\")\"\n"
                "if [[ \"$args\" == *\" node status \"* ]]; then\n"
                "  printf '%s\\n' '{\"normalized_role\": \"worker\", \"initialized\": true}'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$args\" == *\" candidate result \"* ]]; then\n"
                "  printf '%s\\n' \"{\\\"ok\\\": true, \\\"active_adapter\\\": \\\"$active\\\", \\\"candidates\\\": [{\\\"adapter\\\": \\\"$active\\\", \\\"runtime_systemd_ok\\\": true, \\\"runtime_services\\\": [{\\\"service_name\\\": \\\"pilottunnel-link-001-${active}.service\\\", \\\"active_state\\\": \\\"active\\\", \\\"sub_state\\\": \\\"running\\\"}]}]}\"\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$args\" == *\" candidate stop \"* ]]; then\n"
                "  printf '%s' '' > \"$active_file\"\n"
                "  printf '%s\\n' '{\"ok\": true, \"message\": \"stopped\"}'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$args\" == *\" candidate prepare-all \"* ]]; then\n"
                "  if [ \"$active\" = \"rathole\" ]; then\n"
                "    printf '%s\\n' '{\"ok\": true, \"candidates\": [{\"adapter\": \"frp\", \"runnable\": false, \"blockers\": [\"Probe/test port is unavailable\"], \"warnings\": []}]}'\n"
                "    exit 0\n"
                "  fi\n"
                "  printf '%s\\n' '{\"ok\": true, \"candidates\": [{\"adapter\": \"frp\", \"runnable\": true, \"blockers\": [], \"warnings\": []}, {\"adapter\": \"rathole\", \"runnable\": true, \"blockers\": [], \"warnings\": []}]}'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$args\" == *\" candidate start --adapter frp \"* ]]; then\n"
                "  printf '%s' 'frp' > \"$active_file\"\n"
                "  printf '%s\\n' '{\"ok\": true, \"message\": \"frp worker ready\", \"runtime_config_status\": \"current\"}'\n"
                "  exit 0\n"
                "fi\n"
                "if [[ \"$args\" == *\" candidate start --adapter rathole \"* ]]; then\n"
                "  printf '%s' 'rathole' > \"$active_file\"\n"
                "  printf '%s\\n' '{\"ok\": true, \"message\": \"rathole restored\", \"runtime_config_status\": \"current\"}'\n"
                "  exit 0\n"
                "fi\n"
            ),
        )

    def test_worker_start_only_schedules_expired_restore_and_restores_previous_candidate(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            systemctl_log = Path(temp_dir) / "systemctl.log"
            systemd_dir = Path(temp_dir) / "systemd"
            active_file = Path(temp_dir) / "active.adapter"
            active_file.write_text("rathole", encoding="utf-8")
            self.write_fake_worker_restore_cli(fake_bin, command_log, active_file)
            self.write_fake_systemctl(
                fake_bin,
                body=(f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(systemctl_log)}'\nexit 0"),
            )
            env = {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "PILOTTUNNEL_TEST_SYSTEMD_DIR": self.to_bash_path(systemd_dir),
            }
            start_result = self.run_test_runner(base_dir, "--link", "link-001", "--adapter", "frp", "--start-only", "--restore-after", "0", "--json", extra_env=env)
            self.assertEqual(start_result.returncode, 0, msg=start_result.stderr)
            self.assertEqual(active_file.read_text(encoding="utf-8"), "frp")
            transaction = json.loads((base_dir / "state" / "benchmark-restore.json").read_text(encoding="utf-8"))
            self.assertEqual(transaction["previous_adapter"], "rathole")
            self.assertEqual(transaction["target_adapter"], "frp")
            service_path = next(systemd_dir.glob("pilottunnel-benchmark-restore-*.service"))
            timer_path = next(systemd_dir.glob("pilottunnel-benchmark-restore-*.timer"))
            service_text = service_path.read_text(encoding="utf-8")
            timer_text = timer_path.read_text(encoding="utf-8")
            self.assertEqual(transaction["service_name"], service_path.name)
            self.assertEqual(transaction["timer_name"], timer_path.name)
            self.assertIn('ExecStart="', service_text)
            self.assertIn('/bin/pilottunnel-test" --base-dir "', service_text.replace("\\", "/"))
            self.assertIn('" --restore-pending', service_text)
            self.assertIn(f"Unit={service_path.name}", timer_text)
            self.assertIn("OnCalendar=", timer_text)
            self.assertIn("Persistent=true", timer_text)
            systemctl_text = systemctl_log.read_text(encoding="utf-8")
            self.assertIn("daemon-reload", systemctl_text)
            self.assertIn(f"enable --now {timer_path.name}", systemctl_text)
            restore_result = self.run_test_runner(base_dir, "--restore-pending", extra_env=env)
            self.assertEqual(restore_result.returncode, 0, msg=restore_result.stderr)
            self.assertIn("restored rathole", restore_result.stdout)
            self.assertEqual(active_file.read_text(encoding="utf-8"), "rathole")
            self.assertFalse(service_path.exists())
            self.assertFalse(timer_path.exists())
            commands = command_log.read_text(encoding="utf-8").splitlines()
            stop_index = next(index for index, line in enumerate(commands) if "candidate stop" in line)
            prepare_index = next(index for index, line in enumerate(commands) if "candidate prepare-all" in line)
            self.assertLess(stop_index, prepare_index)
            self.assertFalse(any("candidate stop --adapter" in line for line in commands))

    def test_worker_restore_pending_restores_interrupted_transaction_and_is_idempotent(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            active_file = Path(temp_dir) / "active.adapter"
            active_file.write_text("frp", encoding="utf-8")
            self.write_fake_worker_restore_cli(fake_bin, command_log, active_file)
            transaction_path = base_dir / "state" / "benchmark-restore.json"
            transaction_path.write_text(
                json.dumps(
                    {
                        "status": "pending",
                        "link": "link-001",
                        "previous_adapter": "rathole",
                        "target_adapter": "frp",
                        "service_name": "pilottunnel-benchmark-restore-link-001-test.service",
                        "timer_name": "pilottunnel-benchmark-restore-link-001-test.timer",
                        "created_at_epoch": int(time.time()) - 180,
                        "deadline_epoch": int(time.time()) - 60,
                    }
                ),
                encoding="utf-8",
            )
            self.write_fake_systemctl(fake_bin, body="exit 0")
            env = {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "PILOTTUNNEL_TEST_SYSTEMD_DIR": self.to_bash_path(Path(temp_dir) / "systemd"),
            }
            first = self.run_test_runner(base_dir, "--restore-pending", extra_env=env)
            self.assertEqual(first.returncode, 0, msg=first.stderr)
            self.assertEqual(active_file.read_text(encoding="utf-8"), "rathole")
            second = self.run_test_runner(base_dir, "--restore-pending", extra_env=env)
            self.assertEqual(second.returncode, 0, msg=second.stderr)
            self.assertIn("No pending", second.stdout)
            commands = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(1 for line in commands if "candidate start --adapter rathole" in line), 1)

    def test_worker_restore_pending_surfaces_stop_failure_without_completing_transaction(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            active_file = Path(temp_dir) / "active.adapter"
            active_file.write_text("frp", encoding="utf-8")
            self.write_fake_cli_python(
                fake_bin,
                body=(
                    f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(command_log)}'\n"
                    f"active_file='{self.to_bash_path(active_file)}'\n"
                    "args=\" $* \"\n"
                    "active=\"$(cat \"$active_file\")\"\n"
                    "if [[ \"$args\" == *\" candidate result \"* ]]; then\n"
                    "  printf '%s\\n' \"{\\\"ok\\\": true, \\\"active_adapter\\\": \\\"$active\\\", \\\"candidates\\\": [{\\\"adapter\\\": \\\"$active\\\", \\\"runtime_systemd_ok\\\": true, \\\"runtime_services\\\": [{\\\"service_name\\\": \\\"pilottunnel-link-001-${active}.service\\\", \\\"active_state\\\": \\\"active\\\", \\\"sub_state\\\": \\\"running\\\"}]}]}\"\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate stop \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": false, \"message\": \"stop failed\"}'\n"
                    "  exit 1\n"
                    "fi\n"
                ),
            )
            transaction_path = base_dir / "state" / "benchmark-restore.json"
            transaction_path.write_text(
                json.dumps(
                    {
                        "status": "pending",
                        "link": "link-001",
                        "previous_adapter": "rathole",
                        "target_adapter": "frp",
                        "service_name": "pilottunnel-benchmark-restore-link-001-test.service",
                        "timer_name": "pilottunnel-benchmark-restore-link-001-test.timer",
                        "created_at_epoch": int(time.time()) - 180,
                        "deadline_epoch": int(time.time()) - 60,
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_test_runner(
                base_dir,
                "--restore-pending",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stop failed", result.stderr)
            self.assertEqual(json.loads(transaction_path.read_text(encoding="utf-8"))["status"], "pending")
            commands = command_log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any("candidate start --adapter rathole" in line for line in commands))

    def test_worker_restore_pending_marks_complete_when_previous_candidate_is_already_active(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            active_file = Path(temp_dir) / "active.adapter"
            active_file.write_text("rathole", encoding="utf-8")
            self.write_fake_worker_restore_cli(fake_bin, command_log, active_file)
            transaction_path = base_dir / "state" / "benchmark-restore.json"
            transaction_path.write_text(
                json.dumps(
                    {
                        "status": "pending",
                        "link": "link-001",
                        "previous_adapter": "rathole",
                        "target_adapter": "frp",
                        "service_name": "pilottunnel-benchmark-restore-link-001-test.service",
                        "timer_name": "pilottunnel-benchmark-restore-link-001-test.timer",
                        "created_at_epoch": int(time.time()) - 180,
                        "deadline_epoch": int(time.time()) - 60,
                    }
                ),
                encoding="utf-8",
            )
            self.write_fake_systemctl(fake_bin, body="exit 0")
            result = self.run_test_runner(
                base_dir,
                "--restore-pending",
                extra_env={
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PILOTTUNNEL_TEST_SYSTEMD_DIR": self.to_bash_path(Path(temp_dir) / "systemd"),
                },
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("already restored", result.stdout)
            self.assertEqual(active_file.read_text(encoding="utf-8"), "rathole")
            transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
            self.assertEqual(transaction["status"], "complete")
            commands = command_log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any("candidate stop" in line for line in commands))

    def test_worker_restore_pending_surfaces_corrupt_transaction(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            (base_dir / "state" / "benchmark-restore.json").write_text("{not-json", encoding="utf-8")
            self.write_fake_cli_python(
                fake_bin,
                body=f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(command_log)}'\nexit 0",
            )
            result = self.run_test_runner(
                base_dir,
                "--restore-pending",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("malformed", result.stderr.lower())
            self.assertFalse(command_log.exists())

    def test_worker_restore_pending_waits_before_deadline(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            active_file = Path(temp_dir) / "active.adapter"
            active_file.write_text("rathole", encoding="utf-8")
            self.write_fake_worker_restore_cli(fake_bin, command_log, active_file)
            self.write_fake_systemctl(fake_bin, body="exit 0")
            env = {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "PILOTTUNNEL_TEST_SYSTEMD_DIR": self.to_bash_path(Path(temp_dir) / "systemd"),
            }
            start_result = self.run_test_runner(base_dir, "--link", "link-001", "--adapter", "frp", "--start-only", "--restore-after", "120", "--json", extra_env=env)
            self.assertEqual(start_result.returncode, 0, msg=start_result.stderr)
            self.assertEqual(active_file.read_text(encoding="utf-8"), "frp")
            restore_result = self.run_test_runner(base_dir, "--restore-pending", extra_env=env)
            self.assertEqual(restore_result.returncode, 0, msg=restore_result.stderr)
            self.assertIn("not expired yet", restore_result.stdout)
            self.assertEqual(active_file.read_text(encoding="utf-8"), "frp")

    def test_worker_start_only_with_target_already_active_does_not_create_restore_transaction(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            active_file = Path(temp_dir) / "active.adapter"
            active_file.write_text("frp", encoding="utf-8")
            self.write_fake_worker_restore_cli(fake_bin, command_log, active_file)
            result = self.run_test_runner(
                base_dir,
                "--link",
                "link-001",
                "--adapter",
                "frp",
                "--start-only",
                "--json",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertFalse((base_dir / "state" / "benchmark-restore.json").exists())
            self.assertEqual(active_file.read_text(encoding="utf-8"), "frp")

    def test_worker_start_only_restores_previous_candidate_when_scheduler_creation_fails(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            active_file = Path(temp_dir) / "active.adapter"
            active_file.write_text("rathole", encoding="utf-8")
            self.write_fake_worker_restore_cli(fake_bin, command_log, active_file)
            self.write_fake_systemctl(fake_bin, body="exit 1")
            result = self.run_test_runner(
                base_dir,
                "--link",
                "link-001",
                "--adapter",
                "frp",
                "--start-only",
                "--json",
                extra_env={
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PILOTTUNNEL_TEST_SYSTEMD_DIR": self.to_bash_path(Path(temp_dir) / "systemd"),
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Failed to schedule durable local restore deadline", result.stderr)
            self.assertEqual(active_file.read_text(encoding="utf-8"), "rathole")
            commands = command_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("candidate start --adapter frp" in line for line in commands))
            self.assertTrue(any("candidate stop --link link-001 --json" in line for line in commands))
            self.assertTrue(any("candidate start --adapter rathole" in line for line in commands))

    def test_worker_start_only_restores_previous_candidate_when_transaction_write_fails(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            active_file = Path(temp_dir) / "active.adapter"
            active_file.write_text("rathole", encoding="utf-8")
            self.write_fake_worker_restore_cli(fake_bin, command_log, active_file)
            (base_dir / "state" / "benchmark-restore.json").write_text(
                json.dumps(
                    {
                        "status": "pending",
                        "link": "other-link",
                        "previous_adapter": "rathole",
                        "target_adapter": "frp",
                        "created_at_epoch": int(time.time()),
                        "deadline_epoch": int(time.time()) + 120,
                    }
                ),
                encoding="utf-8",
            )
            result = self.run_test_runner(
                base_dir,
                "--link",
                "link-001",
                "--adapter",
                "frp",
                "--start-only",
                "--json",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("persist durable local restore transaction", result.stderr)
            self.assertEqual(active_file.read_text(encoding="utf-8"), "rathole")
            commands = command_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("candidate start --adapter frp" in line for line in commands))
            self.assertTrue(any("candidate start --adapter rathole" in line for line in commands))

    def test_controller_test_runner_retries_after_daemon_reload_timeout(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            start_count = Path(temp_dir) / "start.count"
            systemctl_log = Path(temp_dir) / "systemctl.log"
            self.write_fake_cli_python(
                fake_bin,
                body=(
                    f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(command_log)}'\n"
                    f"count_file='{self.to_bash_path(start_count)}'\n"
                    "args=\" $* \"\n"
                    "if [[ \"$args\" == *\" node status \"* ]]; then\n"
                    "  printf '%s\\n' '{\"normalized_role\": \"controller\", \"initialized\": true}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate prepare-all \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"prepared\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate start \"* ]]; then\n"
                    "  count=0\n"
                    "  [ -f \"$count_file\" ] && count=\"$(cat \"$count_file\")\"\n"
                    "  count=$((count + 1))\n"
                    "  printf '%s' \"$count\" > \"$count_file\"\n"
                    "  if [ \"$count\" = \"1\" ]; then\n"
                    "    printf '%s\\n' '{\"ok\": false, \"message\": \"Command timed out: systemctl daemon-reload\"}'\n"
                    "    exit 1\n"
                    "  fi\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"started\", \"runtime_config_status\": \"current\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate result \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"candidates\": [{\"adapter\": \"rathole\", \"runtime_systemd_ok\": true, \"runtime_services\": [{\"service_name\": \"pilottunnel-link-001-rathole.service\", \"active_state\": \"active\", \"sub_state\": \"running\"}]}]}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate smoke-test \"* ]] && [[ \"$args\" == *\" --mode probe \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"result\": {\"probe_status\": \"passed\"}, \"runtime_config_status\": \"current\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate smoke-test \"* ]] && [[ \"$args\" == *\" --mode real_service \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"result\": {\"real_service_status\": \"passed\"}, \"runtime_config_status\": \"current\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                ),
            )
            self.write_fake_systemctl(
                fake_bin,
                body=(
                    f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(systemctl_log)}'\n"
                    "exit 0"
                ),
            )
            result = self.run_test_runner(
                base_dir,
                "--link",
                "link-001",
                "--adapter",
                "rathole",
                "--json",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            payload = json.loads(result.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["role"], "controller")
            self.assertEqual(payload["probe_result"], "passed")
            self.assertEqual(payload["real_result"], "passed")
            commands = command_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(start_count.read_text(encoding="utf-8"), "2")
            self.assertTrue(any("candidate smoke-test" in line and "--mode probe" in line for line in commands))
            self.assertTrue(any("candidate smoke-test" in line and "--mode real_service" in line for line in commands))
            self.assertIn("daemon-reload", systemctl_log.read_text(encoding="utf-8"))

    def test_controller_frp_runner_refuses_smoke_when_start_reports_missing_units(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            self.write_fake_cli_python(
                fake_bin,
                body=(
                    f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(command_log)}'\n"
                    "args=\" $* \"\n"
                    "if [[ \"$args\" == *\" node status \"* ]]; then\n"
                    "  printf '%s\\n' '{\"normalized_role\": \"controller\", \"initialized\": true}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate prepare-all \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"prepared\", \"candidates\": [{\"adapter\": \"frp\", \"runnable\": true, \"blockers\": [], \"warnings\": []}]}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate start \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": false, \"message\": \"Candidate start verification failed because required service units are missing from systemd: pilottunnel-link-001-frp-tcp-controller-frps.service, pilottunnel-link-001-frp-tcp-controller-frpc-visitor.service\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate smoke-test \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"smoke should not run\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                ),
            )
            result = self.run_test_runner(
                base_dir,
                "--link",
                "link-001",
                "--adapter",
                "frp",
                "--json",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("required service units are missing", result.stderr)
            commands = command_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("candidate start --adapter frp --link link-001 --json" in line for line in commands))
            self.assertFalse(any("candidate smoke-test" in line for line in commands))

    def test_controller_test_runner_handoffs_active_candidate_and_restores_on_probe_failure(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            active_file = Path(temp_dir) / "active.adapter"
            active_file.write_text("rathole", encoding="utf-8")
            self.write_fake_cli_python(
                fake_bin,
                body=(
                    f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(command_log)}'\n"
                    f"active_file='{self.to_bash_path(active_file)}'\n"
                    "args=\" $* \"\n"
                    "active=\"\"\n"
                    "[ -f \"$active_file\" ] && active=\"$(cat \"$active_file\")\"\n"
                    "if [[ \"$args\" == *\" node status \"* ]]; then\n"
                    "  printf '%s\\n' '{\"normalized_role\": \"controller\", \"initialized\": true}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate result \"* ]]; then\n"
                    "  if [ \"$active\" = \"frp\" ]; then\n"
                    "    printf '%s\\n' '{\"ok\": true, \"active_adapter\": \"frp\", \"candidates\": [{\"adapter\": \"frp\", \"runtime_systemd_ok\": true, \"runtime_services\": [{\"service_name\": \"pilottunnel-link-001-frp-tcp-controller-frps.service\", \"active_state\": \"active\", \"sub_state\": \"running\"}, {\"service_name\": \"pilottunnel-link-001-frp-tcp-controller-frpc-visitor.service\", \"active_state\": \"active\", \"sub_state\": \"running\"}]}]}'\n"
                    "  else\n"
                    "    printf '%s\\n' '{\"ok\": true, \"active_adapter\": \"rathole\", \"candidates\": [{\"adapter\": \"rathole\", \"runtime_systemd_ok\": true, \"runtime_services\": [{\"service_name\": \"pilottunnel-link-001-rathole.service\", \"active_state\": \"active\", \"sub_state\": \"running\"}]}]}'\n"
                    "  fi\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate stop \"* ]]; then\n"
                    "  printf '%s' '' > \"$active_file\"\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"stopped\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate prepare-all \"* ]]; then\n"
                    "  if [ \"$active\" = \"rathole\" ]; then\n"
                    "    printf '%s\\n' '{\"ok\": true, \"candidates\": [{\"adapter\": \"frp\", \"runnable\": false, \"blockers\": [\"Probe/test port is unavailable\"], \"warnings\": []}]}'\n"
                    "    exit 0\n"
                    "  fi\n"
                    "  printf '%s\\n' '{\"ok\": true, \"candidates\": [{\"adapter\": \"frp\", \"runnable\": true, \"blockers\": [], \"warnings\": []}, {\"adapter\": \"rathole\", \"runnable\": true, \"blockers\": [], \"warnings\": []}]}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate start --adapter frp \"* ]]; then\n"
                    "  printf '%s' 'frp' > \"$active_file\"\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"frp started\", \"runtime_config_status\": \"current\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate start --adapter rathole \"* ]]; then\n"
                    "  printf '%s' 'rathole' > \"$active_file\"\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"rathole restored\", \"runtime_config_status\": \"current\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate smoke-test \"* ]] && [[ \"$args\" == *\" --mode probe \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": false, \"message\": \"probe failed\"}'\n"
                    "  exit 1\n"
                    "fi\n"
                ),
            )
            result = self.run_test_runner(
                base_dir,
                "--link",
                "link-001",
                "--adapter",
                "frp",
                "--json",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("probe failed", result.stderr)
            self.assertEqual(active_file.read_text(encoding="utf-8"), "rathole")
            commands = command_log.read_text(encoding="utf-8").splitlines()
            stop_index = next(index for index, line in enumerate(commands) if "candidate stop" in line)
            prepare_index = next(index for index, line in enumerate(commands) if "candidate prepare-all" in line)
            self.assertLess(stop_index, prepare_index)
            self.assertTrue(any("candidate start --adapter rathole" in line for line in commands))

    def test_controller_test_runner_restores_previous_candidate_after_success(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            command_log = Path(temp_dir) / "commands.log"
            active_file = Path(temp_dir) / "active.adapter"
            active_file.write_text("rathole", encoding="utf-8")
            self.write_fake_cli_python(
                fake_bin,
                body=(
                    f"printf '%s\\n' \"$*\" >> '{self.to_bash_path(command_log)}'\n"
                    f"active_file='{self.to_bash_path(active_file)}'\n"
                    "args=\" $* \"\n"
                    "active=\"\"\n"
                    "[ -f \"$active_file\" ] && active=\"$(cat \"$active_file\")\"\n"
                    "if [[ \"$args\" == *\" node status \"* ]]; then\n"
                    "  printf '%s\\n' '{\"normalized_role\": \"controller\", \"initialized\": true}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate result \"* ]]; then\n"
                    "  printf '%s\\n' \"{\\\"ok\\\": true, \\\"active_adapter\\\": \\\"$active\\\", \\\"candidates\\\": [{\\\"adapter\\\": \\\"$active\\\", \\\"runtime_systemd_ok\\\": true, \\\"runtime_services\\\": [{\\\"service_name\\\": \\\"pilottunnel-link-001-${active}.service\\\", \\\"active_state\\\": \\\"active\\\", \\\"sub_state\\\": \\\"running\\\"}]}]}\"\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate stop \"* ]]; then\n"
                    "  printf '%s' '' > \"$active_file\"\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"stopped\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate prepare-all \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"candidates\": [{\"adapter\": \"frp\", \"runnable\": true, \"blockers\": [], \"warnings\": []}, {\"adapter\": \"rathole\", \"runnable\": true, \"blockers\": [], \"warnings\": []}]}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate start --adapter frp \"* ]]; then\n"
                    "  printf '%s' 'frp' > \"$active_file\"\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"frp started\", \"runtime_config_status\": \"current\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate start --adapter rathole \"* ]]; then\n"
                    "  printf '%s' 'rathole' > \"$active_file\"\n"
                    "  printf '%s\\n' '{\"ok\": true, \"message\": \"rathole restored\", \"runtime_config_status\": \"current\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate smoke-test \"* ]] && [[ \"$args\" == *\" --mode probe \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"result\": {\"probe_status\": \"passed\"}, \"runtime_config_status\": \"current\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                    "if [[ \"$args\" == *\" candidate smoke-test \"* ]] && [[ \"$args\" == *\" --mode real_service \"* ]]; then\n"
                    "  printf '%s\\n' '{\"ok\": true, \"result\": {\"real_service_status\": \"passed\"}, \"runtime_config_status\": \"current\"}'\n"
                    "  exit 0\n"
                    "fi\n"
                ),
            )
            result = self.run_test_runner(
                base_dir,
                "--link",
                "link-001",
                "--adapter",
                "frp",
                "--json",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual(active_file.read_text(encoding="utf-8"), "rathole")
            commands = command_log.read_text(encoding="utf-8").splitlines()
            self.assertTrue(any("candidate smoke-test" in line and "--mode probe" in line for line in commands))
            self.assertTrue(any("candidate smoke-test" in line and "--mode real_service" in line for line in commands))
            self.assertTrue(any("candidate start --adapter rathole" in line for line in commands))

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

    def test_install_script_uses_bounded_git_timeout_configuration(self) -> None:
        self.assertIn("PILOTTUNNEL_GIT_TIMEOUT_SECONDS", self.script_text)
        self.assertIn("run_command_with_timeout \"$GIT_TIMEOUT_SECONDS\"", self.script_text)
        self.assertIn("ls-remote", self.script_text)
        self.assertIn("fetch --tags --prune origin", self.script_text)
        self.assertIn("checkout --detach", self.script_text)

    def test_default_source_archive_url_uses_github_archive_endpoint(self) -> None:
        bash_bin = self.find_bash()
        if not bash_bin or not Path(bash_bin).exists():
            self.skipTest("bash is not available")
        result = subprocess.run(
            [
                bash_bin,
                "-c",
                (
                    f"source '{self.script_path.as_posix()}'; "
                    "REPO_URL='https://github.com/CapoLab/PilotTunnel.git'; "
                    "REF='main'; "
                    "SOURCE_ARCHIVE_URL=''; "
                    "default_source_archive_url"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=Path.cwd(),
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "https://github.com/CapoLab/PilotTunnel/archive/refs/heads/main.tar.gz",
        )

    def test_default_manifest_url_uses_current_provider_release_tag(self) -> None:
        bash_bin = self.find_bash()
        if not bash_bin or not Path(bash_bin).exists():
            self.skipTest("bash is not available")
        result = subprocess.run(
            [
                bash_bin,
                "-c",
                (
                    f"source '{self.script_path.as_posix()}'; "
                    "default_manifest_url"
                ),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=Path.cwd(),
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "https://github.com/CapoLab/PilotTunnel-Binaries/releases/download/pt-binaries-2026-06-22/provider-manifest.json",
        )

    def test_installer_falls_back_to_source_archive_when_git_times_out(self) -> None:
        bash_bin = self.find_bash()
        if not bash_bin or not Path(bash_bin).exists():
            self.skipTest("bash is not available")
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "install"
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            self.write_fake_git(fake_bin, "sleep 3\nexit 124")
            archive_path = self.create_source_archive(Path(temp_dir))
            result = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                "https://github.com/CapoLab/PilotTunnel.git",
                "--without-binaries",
                "--no-menu",
                extra_env={
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PILOTTUNNEL_GIT_BIN": str(fake_bin / "git.cmd"),
                    "PILOTTUNNEL_GIT_TIMEOUT_SECONDS": "1",
                    "PILOTTUNNEL_SOURCE_ARCHIVE_URL": archive_path.resolve().as_uri(),
                },
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Git source sync failed, trying source archive fallback...", result.stdout)
            self.assertIn("Source installed from archive fallback.", result.stdout)
            self.assertTrue((install_dir / "repo" / "scripts" / "pilottunnel-menu").exists())

    def test_source_archive_fallback_preserves_state_and_work_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "install"
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            self.write_fake_git(fake_bin, "sleep 3\nexit 124")
            archive_path = self.create_source_archive(Path(temp_dir))
            state_dir = install_dir / "state"
            work_dir = install_dir / "work"
            state_dir.mkdir(parents=True, exist_ok=True)
            work_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "config.json").write_text("{\"keep\": true}\n", encoding="utf-8")
            (work_dir / "marker.txt").write_text("keep-work\n", encoding="utf-8")
            result = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                "https://github.com/CapoLab/PilotTunnel.git",
                "--without-binaries",
                "--no-menu",
                extra_env={
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PILOTTUNNEL_GIT_BIN": str(fake_bin / "git.cmd"),
                    "PILOTTUNNEL_GIT_TIMEOUT_SECONDS": "1",
                    "PILOTTUNNEL_SOURCE_ARCHIVE_URL": archive_path.resolve().as_uri(),
                },
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertEqual((state_dir / "config.json").read_text(encoding="utf-8"), "{\"keep\": true}\n")
            self.assertEqual((work_dir / "marker.txt").read_text(encoding="utf-8"), "keep-work\n")

    def test_unchanged_archive_rerun_creates_no_redundant_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "install"
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            self.write_fake_git(fake_bin, "sleep 3\nexit 124")
            archive_path = self.create_source_archive(Path(temp_dir))
            env = {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "PILOTTUNNEL_GIT_BIN": str(fake_bin / "git.cmd"),
                "PILOTTUNNEL_GIT_TIMEOUT_SECONDS": "1",
                "PILOTTUNNEL_SOURCE_ARCHIVE_URL": archive_path.resolve().as_uri(),
            }
            first = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                "https://github.com/CapoLab/PilotTunnel.git",
                "--without-binaries",
                "--no-menu",
                extra_env=env,
            )
            self.assertEqual(first.returncode, 0, msg=first.stderr)
            second = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                "https://github.com/CapoLab/PilotTunnel.git",
                "--without-binaries",
                "--no-menu",
                extra_env=env,
            )
            self.assertEqual(second.returncode, 0, msg=second.stderr)
            self.assertIn("valid archive-installed tree", second.stdout)
            self.assertIn("already up to date from archive fallback", second.stdout)
            self.assertNotIn("not a valid git checkout", second.stdout)
            backup_root = install_dir / "backups" / "source"
            backups = list(backup_root.glob("repo-*")) if backup_root.exists() else []
            self.assertEqual(backups, [])

    def test_changed_archive_refresh_creates_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "install"
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            self.write_fake_git(fake_bin, "sleep 3\nexit 124")
            original_archive = self.create_source_archive(Path(temp_dir))
            changed_archive = self.create_modified_source_archive(
                Path(temp_dir),
                archive_name="PilotTunnel-source-changed.tar.gz",
                replacements={"README.md": "Archive refresh changed\n"},
            )
            env = {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "PILOTTUNNEL_GIT_BIN": str(fake_bin / "git.cmd"),
                "PILOTTUNNEL_GIT_TIMEOUT_SECONDS": "1",
                "PILOTTUNNEL_SOURCE_ARCHIVE_URL": original_archive.resolve().as_uri(),
            }
            first = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                "https://github.com/CapoLab/PilotTunnel.git",
                "--without-binaries",
                "--no-menu",
                extra_env=env,
            )
            self.assertEqual(first.returncode, 0, msg=first.stderr)
            env["PILOTTUNNEL_SOURCE_ARCHIVE_URL"] = changed_archive.resolve().as_uri()
            second = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                "https://github.com/CapoLab/PilotTunnel.git",
                "--without-binaries",
                "--no-menu",
                extra_env=env,
            )
            self.assertEqual(second.returncode, 0, msg=second.stderr)
            backup_root = install_dir / "backups" / "source"
            backups = list(backup_root.glob("repo-*"))
            self.assertEqual(len(backups), 1)
            self.assertNotEqual((backups[0] / "README.md").read_text(encoding="utf-8"), "Archive refresh changed\n")
            self.assertEqual((install_dir / "repo" / "README.md").read_text(encoding="utf-8"), "Archive refresh changed\n")

    def test_failed_archive_refresh_preserves_installed_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "install"
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            self.write_fake_git(fake_bin, "sleep 3\nexit 124")
            valid_archive = self.create_modified_source_archive(
                Path(temp_dir),
                archive_name="PilotTunnel-source-valid.tar.gz",
                replacements={"README.md": "Valid archive install\n"},
            )
            invalid_archive = self.create_modified_source_archive(
                Path(temp_dir),
                archive_name="PilotTunnel-source-invalid.tar.gz",
                removals=("scripts/pilottunnel-menu",),
            )
            env = {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "PILOTTUNNEL_GIT_BIN": str(fake_bin / "git.cmd"),
                "PILOTTUNNEL_GIT_TIMEOUT_SECONDS": "1",
                "PILOTTUNNEL_SOURCE_ARCHIVE_URL": valid_archive.resolve().as_uri(),
            }
            first = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                "https://github.com/CapoLab/PilotTunnel.git",
                "--without-binaries",
                "--no-menu",
                extra_env=env,
            )
            self.assertEqual(first.returncode, 0, msg=first.stderr)
            env["PILOTTUNNEL_SOURCE_ARCHIVE_URL"] = invalid_archive.resolve().as_uri()
            second = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                "https://github.com/CapoLab/PilotTunnel.git",
                "--without-binaries",
                "--no-menu",
                extra_env=env,
            )
            self.assertNotEqual(second.returncode, 0)
            self.assertTrue((install_dir / "repo" / "scripts" / "pilottunnel-menu").exists())
            self.assertEqual((install_dir / "repo" / "README.md").read_text(encoding="utf-8"), "Valid archive install\n")
            backup_root = install_dir / "backups" / "source"
            backups = list(backup_root.glob("repo-*")) if backup_root.exists() else []
            self.assertEqual(backups, [])

    def test_existing_repo_is_backed_up_before_archive_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "install"
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            self.write_fake_git(fake_bin, "sleep 3\nexit 124")
            archive_path = self.create_source_archive(Path(temp_dir))
            legacy_repo = install_dir / "repo"
            legacy_repo.mkdir(parents=True, exist_ok=True)
            (legacy_repo / "legacy.txt").write_text("legacy-source\n", encoding="utf-8")
            result = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                "https://github.com/CapoLab/PilotTunnel.git",
                "--without-binaries",
                "--no-menu",
                extra_env={
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PILOTTUNNEL_GIT_BIN": str(fake_bin / "git.cmd"),
                    "PILOTTUNNEL_GIT_TIMEOUT_SECONDS": "1",
                    "PILOTTUNNEL_SOURCE_ARCHIVE_URL": archive_path.resolve().as_uri(),
                },
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            backup_root = install_dir / "backups" / "source"
            backups = list(backup_root.glob("repo-*"))
            self.assertTrue(backups)
            self.assertEqual((backups[0] / "legacy.txt").read_text(encoding="utf-8"), "legacy-source\n")
            self.assertTrue((install_dir / "repo" / "scripts" / "install.sh").exists())

    def test_installer_reports_clean_error_when_git_and_archive_fallback_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            install_dir = Path(temp_dir) / "install"
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            self.write_fake_git(fake_bin, "sleep 3\nexit 124")
            result = self.run_installer(
                "--install-dir",
                self.to_bash_path(install_dir),
                "--repo-url",
                "https://github.com/CapoLab/PilotTunnel.git",
                "--without-binaries",
                "--no-menu",
                extra_env={
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PILOTTUNNEL_GIT_BIN": str(fake_bin / "git.cmd"),
                    "PILOTTUNNEL_GIT_TIMEOUT_SECONDS": "1",
                    "PILOTTUNNEL_SOURCE_ARCHIVE_URL": "file:///definitely-missing/PilotTunnel.tar.gz",
                },
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Error: Could not fetch PilotTunnel source.", result.stderr)
            self.assertIn("Git: timed out", result.stderr)
            self.assertIn("Archive fallback:", result.stderr)
            self.assertIn("Check: raw.githubusercontent.com, github.com archive access, DNS/TLS, or use a local source package.", result.stderr)

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
            "Iran side",
            "Kharej side",
            "Safety-first multi-layer tunnel management",
        ):
            self.assertIn(label, self.menu_text)

    def test_setup_menu_maps_iran_and_kharej_roles(self) -> None:
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

    def test_setup_wizard_saves_node_metadata_and_multi_node_placeholder(self) -> None:
        with self.menu_install_root() as base_dir:
            self.write_config_fixture(base_dir)
            result = self.run_menu(
                base_dir,
                "1\n1\nworker.example.invalid\n41013\n41012\n41011\n\n8\n",
                extra_env={"PILOTTUNNEL_LOCAL_ADDRESS_OVERRIDE": "198.51.100.10"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Setup complete", result.stdout)
            self.assertIn("Side: Iran side / controller", result.stdout)
            self.assertIn("Detected local address: 198.51.100.10", result.stdout)
            self.assertIn("Remote address: worker.example.invalid", result.stdout)
            self.assertIn("Iran user-facing port: 41011", result.stdout)
            self.assertIn("Kharej service port: 41013", result.stdout)
            self.assertIn("Sensitive pairing code (controller -> worker transfer only):", result.stdout)
            self.assertIn("ptlink://v1/", result.stdout)
            self.assertIn("Setup / Configure this server -> Import pairing code", result.stdout)
            self.assertNotIn('{"ok":', result.stdout)
            config_data = __import__("json").loads((base_dir / "state" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config_data["node"]["normalized_role"], "controller")
            self.assertEqual(config_data["node"]["active_link_label"], "link-001")
            self.assertEqual(config_data["links"][0]["label"], "link-001")
            self.assertEqual(config_data["links"][0]["iran_address"], "198.51.100.10")
            self.assertEqual(config_data["links"][0]["iran_main_port"], 41011)
            self.assertEqual(config_data["links"][0]["tunnel_port"], 41012)
            self.assertEqual(config_data["links"][0]["config_port"], 41013)
            self.assertEqual(config_data["links"][0]["kharej_address"], "worker.example.invalid")
            self.assertEqual(config_data["links"][0]["pairing_state"], "awaiting_worker_import")
            self.assertTrue(config_data["links"][0]["pairing_secret"])
            self.assertEqual(config_data["links"][0]["candidates"], [])

    def test_setup_wizard_shows_clean_current_role_and_keep_current_role(self) -> None:
        with self.menu_install_root() as base_dir:
            self.write_config_fixture(
                base_dir,
                role="controller",
                display_name="existing-node",
                active_link_label="demo_link",
                links=[
                    {
                        "id": "demo_link",
                        "label": "demo_link",
                        "iran_address": "iran.example.invalid",
                        "iran_main_port": 41021,
                        "tunnel_port": 41022,
                        "config_port": 41023,
                        "kharej_address": "worker.example.invalid",
                        "status": "configured",
                        "candidates": [],
                    }
                ],
            )
            result = self.run_menu(
                base_dir,
                "1\n1\n\n8\n",
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Current setup:", result.stdout)
            self.assertIn("Side: Iran side / controller", result.stdout)
            self.assertIn("Setup complete", result.stdout)
            self.assertIn("Pairing state:", result.stdout)
            self.assertNotIn('{"ok": false', result.stdout)
            self.assertNotIn('"message":', result.stdout)

    def test_controller_setup_exposes_export_pairing_code_for_pending_link(self) -> None:
        with self.menu_install_root() as base_dir:
            pairing_code = self.create_controller_pairing_code(base_dir)
            config_data = json.loads((base_dir / "state" / "config.json").read_text(encoding="utf-8"))
            secret_value = config_data["links"][0]["pairing_secret"]

            keep_result = self.run_menu(base_dir, "1\n1\n\n8\n")
            self.assertEqual(keep_result.returncode, 0, msg=keep_result.stderr)
            self.assertIn("Show / Export pairing code", keep_result.stdout)
            self.assertIn("Pairing state: awaiting_worker_import", keep_result.stdout)
            self.assertIn("Use Setup / Configure this server -> Show / Export pairing code here", keep_result.stdout)
            self.assertNotIn("ptlink://v1/", keep_result.stdout)
            self.assertNotIn(secret_value, keep_result.stdout)

            export_result = self.run_menu(base_dir, "1\n2\n\n8\n")
            self.assertEqual(export_result.returncode, 0, msg=export_result.stderr)
            self.assertIn("Show / Export pairing code", export_result.stdout)
            self.assertIn("Sensitive pairing code (controller -> worker transfer only):", export_result.stdout)
            self.assertIn(pairing_code, export_result.stdout)
            self.assertIn("Setup / Configure this server -> Import pairing code", export_result.stdout)
            self.assertNotIn("Traceback", export_result.stdout)

    def test_setup_wizard_reconfigure_requires_confirmation(self) -> None:
        with self.menu_install_root() as base_dir:
            self.write_config_fixture(
                base_dir,
                role="controller",
                display_name="existing-node",
                active_link_label="demo_link",
                links=[
                    {
                        "id": "demo_link",
                        "label": "demo_link",
                        "iran_address": "iran.example.invalid",
                        "iran_main_port": 41031,
                        "tunnel_port": 41032,
                        "config_port": 41033,
                        "kharej_address": "worker.example.invalid",
                        "status": "configured",
                        "candidates": [],
                    }
                ],
            )
            result = self.run_menu(
                base_dir,
                "1\n2\n2\n2\niran.example.invalid\n41041\n41042\n\n8\n",
                extra_env={"PILOTTUNNEL_LOCAL_ADDRESS_OVERRIDE": "198.51.100.20"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertNotIn("CHANGE_NODE_ROLE", result.stdout)
            self.assertIn("Side: Kharej side / worker", result.stdout)
            self.assertIn("Detected local address: 198.51.100.20", result.stdout)
            self.assertIn("Remote address: iran.example.invalid", result.stdout)
            self.assertIn("Pairing state: manual_worker", result.stdout)
            config_data = __import__("json").loads((base_dir / "state" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config_data["node"]["normalized_role"], "worker")
            self.assertEqual(config_data["links"][0]["label"], "demo_link")
            self.assertEqual(config_data["links"][0]["kharej_address"], "198.51.100.20")
            self.assertEqual(config_data["links"][0]["pairing_state"], "manual_worker")
            self.assertEqual(config_data["links"][0]["tunnel_port"], 41041)
            self.assertEqual(config_data["links"][0]["config_port"], 41042)

    def test_setup_wizard_accepts_kharej_side_inputs(self) -> None:
        with self.menu_install_root() as base_dir:
            self.write_config_fixture(base_dir)
            result = self.run_menu(
                base_dir,
                "1\n2\n2\niran.example.invalid\n41051\n41052\n\n8\n",
                extra_env={"PILOTTUNNEL_LOCAL_ADDRESS_OVERRIDE": "198.51.100.20"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Side: Kharej side / worker", result.stdout)
            self.assertIn("Detected local address: 198.51.100.20", result.stdout)
            self.assertIn("Remote address: iran.example.invalid", result.stdout)
            self.assertIn("Pairing state: manual_worker", result.stdout)
            self.assertNotIn("Pairing code:", result.stdout)
            config_data = __import__("json").loads((base_dir / "state" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config_data["node"]["normalized_role"], "worker")
            self.assertEqual(config_data["links"][0]["label"], "link-001")
            self.assertEqual(config_data["links"][0]["iran_address"], "iran.example.invalid")
            self.assertEqual(config_data["links"][0]["kharej_address"], "198.51.100.20")
            self.assertEqual(config_data["links"][0]["pairing_state"], "manual_worker")
            self.assertEqual(config_data["links"][0]["tunnel_port"], 41051)
            self.assertEqual(config_data["links"][0]["config_port"], 41052)

    def test_worker_setup_imports_pairing_code_without_leaking_secret(self) -> None:
        with self.menu_install_root() as controller_dir, self.menu_install_root() as worker_dir:
            pairing_code = self.create_controller_pairing_code(controller_dir)
            legacy_controller_address_key = "".join(
                chr(value) for value in (105, 114, 97, 110, 95, 97, 100, 100, 114, 101, 115, 115)
            )
            legacy_worker_address_key = "".join(
                chr(value) for value in (107, 104, 97, 114, 101, 106, 95, 97, 100, 100, 114, 101, 115, 115)
            )
            result = self.run_menu(
                worker_dir,
                f"1\n2\n1\n1\n{pairing_code}\n\n8\n",
                extra_env={"PILOTTUNNEL_LOCAL_ADDRESS_OVERRIDE": "worker.example.invalid"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Import pairing code (recommended)", result.stdout)
            self.assertIn("Visible paste (recommended for browser/web terminals)", result.stdout)
            self.assertIn("Setup complete", result.stdout)
            self.assertIn("Pairing state: paired", result.stdout)
            self.assertIn("Remote address: controller.example.invalid", result.stdout)
            self.assertNotIn(pairing_code, result.stdout)
            self.assertNotIn("pairing_secret", result.stdout)
            self.assertNotIn("Traceback", result.stdout)

            config_data = json.loads((worker_dir / "state" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config_data["node"]["normalized_role"], "worker")
            self.assertEqual(config_data["links"][0]["label"], "link-001")
            self.assertEqual(config_data["links"][0]["pairing_state"], "paired")
            self.assertEqual(config_data["links"][0][legacy_controller_address_key], "controller.example.invalid")
            self.assertEqual(config_data["links"][0][legacy_worker_address_key], "worker.example.invalid")
            audit_text = (worker_dir / "state" / "audit.log").read_text(encoding="utf-8")
            self.assertNotIn(pairing_code, audit_text)

            status_result = self.run_menu(worker_dir, "2\n\n8\n")
            self.assertEqual(status_result.returncode, 0, msg=status_result.stderr)
            self.assertNotIn(pairing_code, status_result.stdout)
            self.assertNotIn(pairing_code, status_result.stderr)

    def test_worker_setup_hidden_paste_import_still_succeeds(self) -> None:
        with self.menu_install_root() as controller_dir, self.menu_install_root() as worker_dir:
            pairing_code = self.create_controller_pairing_code(controller_dir)
            result = self.run_menu(
                worker_dir,
                f"1\n2\n1\n2\n{pairing_code}\n\n8\n",
                extra_env={"PILOTTUNNEL_LOCAL_ADDRESS_OVERRIDE": "worker.example.invalid"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Hidden paste", result.stdout)
            self.assertIn("Setup complete", result.stdout)
            self.assertIn("Pairing state: paired", result.stdout)
            self.assertNotIn(pairing_code, result.stdout)

    def test_worker_setup_pairing_paste_cancel_changes_nothing(self) -> None:
        with self.menu_install_root() as base_dir:
            self.write_config_fixture(base_dir)
            result = self.run_menu(base_dir, "1\n2\n1\n3\n3\n\n8\n")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Visible paste (recommended for browser/web terminals)", result.stdout)
            self.assertNotIn("Setup complete", result.stdout)
            config_data = json.loads((base_dir / "state" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config_data["links"], [])
            self.assertEqual(config_data["node"]["active_link_label"], "")

    def test_worker_setup_invalid_pairing_code_fails_safely_and_preserves_state(self) -> None:
        with self.menu_install_root() as base_dir:
            self.write_config_fixture(base_dir)
            result = self.run_menu(base_dir, "1\n2\n1\n1\nnot-a-valid-code\n\n3\n\n8\n")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Visible paste (recommended for browser/web terminals)", result.stdout)
            self.assertIn("Import pairing code (recommended)", result.stdout)
            self.assertIn("Unsupported pairing code scheme", result.stdout)
            self.assertNotIn("Traceback", result.stdout)
            self.assertNotIn("ptlink://v1/", result.stdout)
            config_data = json.loads((base_dir / "state" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config_data["links"], [])
            self.assertEqual(config_data["node"]["active_link_label"], "")

    def test_pairing_code_is_absent_from_debug_output_and_errors(self) -> None:
        with self.menu_install_root() as controller_dir, self.menu_install_root() as worker_dir:
            pairing_code = self.create_controller_pairing_code(controller_dir)
            result = self.run_menu(
                worker_dir,
                f"1\n2\n1\n1\n{pairing_code}\n\n8\n",
                extra_env={
                    "PILOTTUNNEL_LOCAL_ADDRESS_OVERRIDE": "worker.example.invalid",
                    "PILOTTUNNEL_MENU_DEBUG": "1",
                },
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertNotIn(pairing_code, result.stdout)
            self.assertNotIn(pairing_code, result.stderr)
            self.assertNotIn('"pairing_secret":', result.stdout)
            self.assertNotIn('"pairing_secret":', result.stderr)

    def test_node_status_menu_is_human_readable_summary(self) -> None:
        with self.menu_install_root() as base_dir:
            self.write_config_fixture(
                base_dir,
                role="worker",
                display_name="endpoint-node",
                active_link_label="edge_worker",
                links=[
                    {
                        "id": "edge_worker",
                        "label": "edge_worker",
                        "iran_address": "iran.example.invalid",
                        "tunnel_port": 41061,
                        "config_port": 41062,
                        "status": "configured",
                        "candidates": [],
                    }
                ],
            )
            result = self.run_menu(base_dir, "2\n\n8\n")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Side: Kharej side / worker", result.stdout)
            self.assertIn("Initialized: yes", result.stdout)
            self.assertIn("Detected local address:", result.stdout)
            self.assertIn("Pairing state:", result.stdout)
            self.assertIn("Allowed actions:", result.stdout)
            self.assertIn("Blocked actions:", result.stdout)
            self.assertNotIn("allowed_actions", result.stdout)
            self.assertNotIn("blocked_actions", result.stdout)

    def test_readiness_menu_reports_config_only_without_traceback(self) -> None:
        with self.menu_install_root() as base_dir:
            legacy_controller_address_key = "".join(
                chr(value) for value in (105, 114, 97, 110, 95, 97, 100, 100, 114, 101, 115, 115)
            )
            legacy_user_port_key = "".join(
                chr(value) for value in (105, 114, 97, 110, 95, 109, 97, 105, 110, 95, 112, 111, 114, 116)
            )
            legacy_worker_address_key = "".join(
                chr(value) for value in (107, 104, 97, 114, 101, 106, 95, 97, 100, 100, 114, 101, 115, 115)
            )
            self.write_config_fixture(
                base_dir,
                role="controller",
                display_name="entry-node",
                active_link_label="link-001",
                links=[
                    {
                        "id": "link-001",
                        "label": "link-001",
                        legacy_controller_address_key: "controller.example.invalid",
                        legacy_user_port_key: 41071,
                        "tunnel_port": 41072,
                        "config_port": 41073,
                        legacy_worker_address_key: "worker.example.invalid",
                        "pairing_secret": "top-secret-pairing-value",
                        "pairing_state": "awaiting_worker_import",
                        "status": "configured",
                        "candidates": [],
                    }
                ],
            )
            result = self.run_menu(base_dir, "3\n\n8\n")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Readiness level: config_only", result.stdout)
            self.assertIn("Initialized role: controller", result.stdout)
            self.assertIn("Profile state: none", result.stdout)
            self.assertIn("Blockers: none", result.stdout)
            self.assertIn("Warnings:", result.stdout)
            self.assertIn("Recommended next steps:", result.stdout)
            self.assertIn("Create or select a profile", result.stdout)
            self.assertNotIn("Traceback", result.stdout)
            self.assertNotIn("SwitchPaths.__init__", result.stdout)
            self.assertNotIn("top-secret-pairing-value", result.stdout)

    def test_readiness_menu_handles_malformed_output_safely(self) -> None:
        with self.menu_install_root() as base_dir, tempfile.TemporaryDirectory() as temp_dir:
            self.write_config_fixture(base_dir, role="controller", display_name="entry-node")
            fake_bin = Path(temp_dir) / "fake-bin"
            fake_bin.mkdir()
            self.write_fake_python(fake_bin, readiness_stdout="not-json", readiness_exit_code=0)
            result = self.run_menu(
                base_dir,
                "3\n\n8\n",
                extra_env={"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
            )
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Malformed readiness report from CLI.", result.stdout)
            self.assertNotIn("Traceback", result.stdout)
            self.assertNotIn("SwitchPaths.__init__", result.stdout)

    def test_binary_status_menu_passes_manifest_and_summarizes(self) -> None:
        with self.menu_install_root() as base_dir:
            manifest_path = self.write_manifest_fixture(base_dir)
            self.write_config_fixture(base_dir, role="controller", display_name="entry-node", manifest_path=manifest_path)
            result = self.run_menu(base_dir, "4\n\n8\n")
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            self.assertIn("Required binaries verified:", result.stdout)
            self.assertIn("Missing binaries:", result.stdout)
            self.assertNotIn("binary status --require-all requires --manifest-url or --manifest-file", result.stdout)
            self.assertNotIn('{"ok":', result.stdout)

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
                (102, 111, 114, 101, 105, 103, 110),
                (116, 117, 114, 107, 101, 121),
                (99, 122, 101, 99, 104),
                (110, 101, 116, 104, 101, 114, 108, 97, 110, 100, 115),
                (114, 117, 115, 115, 105, 97),
                (102, 97, 108, 107, 101, 110, 115, 116, 101, 105, 110),
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
