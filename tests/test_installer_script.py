from contextlib import contextmanager
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
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
        git_cmd.write_text(
            "@echo off\r\n"
            "setlocal\r\n"
            "python -c \"import sys,time; time.sleep(%d); sys.exit(%d)\"\r\n"
            "exit /b %d\r\n" % (sleep_seconds, exit_code, exit_code),
            encoding="utf-8",
        )
        return git_path

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
            self.assertIn("Pairing code:", result.stdout)
            self.assertIn("ptlink://v1/", result.stdout)
            self.assertIn("Copy this code to the Kharej server", result.stdout)
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
