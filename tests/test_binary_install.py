import io
import json
import tempfile
import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pilottunnel import cli
from pilottunnel.binary_install import apply_binary_install, resolve_binary_reference
from pilottunnel.binary_provider import generate_manifest
from pilottunnel.binaries import binary_components, binary_spec, current_platform_id, import_binary, provider_required_adapters
from pilottunnel.config import AppConfig, BinaryResolutionSettings
from pilottunnel.state import AppState
from testsupport import static_http_server


class BinaryInstallWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        root = self.base / "runtime"
        with redirect_stdout(output):
            code = cli.main(
                [
                    "--config",
                    str(root / "config.json"),
                    "--state",
                    str(root / "state.json"),
                    "--registry",
                    str(root / "registry.json"),
                    "--audit-log",
                    str(root / "audit.log"),
                    "--lock-dir",
                    str(root / "locks"),
                    "--work-dir",
                    str(root / "work"),
                    "--staging-root",
                    str(root / "staging"),
                    *args,
                ]
            )
        return code, output.getvalue()

    def _provider_source_tree(self) -> tuple[Path, dict[str, bytes]]:
        source_root = self.base / "provider-source"
        payloads: dict[str, bytes] = {}
        platform_id = current_platform_id()
        for adapter in provider_required_adapters():
            if platform_id not in binary_spec(adapter).supported_platforms:
                continue
            for component in binary_components(adapter):
                binary_dir = source_root / adapter / platform_id
                binary_dir.mkdir(parents=True, exist_ok=True)
                payload = f"{adapter}-{component}-{platform_id}-install".encode("utf-8")
                binary_path = binary_dir / component
                binary_path.write_bytes(payload)
                payloads.setdefault(adapter, payload)
                payloads[f"{adapter}:{component}"] = payload
        return source_root, payloads

    @contextmanager
    def _manifest_fixture(self):
        source_root, payloads = self._provider_source_tree()
        output_path = self.base / "provider-manifest.json"
        with static_http_server(source_root) as base_url:
            generate_manifest(
                provider_name="generic-provider",
                base_url=base_url,
                source_dir=source_root,
                output_path=output_path,
            )
            yield output_path, payloads

    def test_binary_install_plan_output(self) -> None:
        with self._manifest_fixture() as (manifest_file, _payloads):
            code, output = self.run_cli(
                "binary",
                "install",
                "plan",
                "--manifest",
                str(manifest_file),
                "--platform",
                current_platform_id(),
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["action"], "binary-install-plan")
        self.assertEqual(payload["platform"], current_platform_id())
        self.assertTrue(any(item["result"] == "install_dir_required" for item in payload["results"] if item["adapter"] in provider_required_adapters()))

    def test_binary_install_apply_requires_confirmation(self) -> None:
        with self._manifest_fixture() as (manifest_file, _payloads):
            code, output = self.run_cli(
                "binary",
                "install",
                "apply",
                "--manifest",
                str(manifest_file),
                "--platform",
                current_platform_id(),
                "--install-dir",
                str(self.base / "managed-install"),
            )
        self.assertEqual(code, 1)
        self.assertIn("INSTALL_PROVIDER_BINARIES", output)

    def test_binary_install_apply_verifies_checksum(self) -> None:
        with self._manifest_fixture() as (manifest_file, payloads):
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            adapter = next(iter(payloads))
            for item in manifest["binaries"]:
                if item["adapter"] == adapter and item["platform"] == current_platform_id():
                    item["sha256"] = "0" * 64
                    break
            manifest_file.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            code, output = self.run_cli(
                "binary",
                "install",
                "apply",
                "--manifest",
                str(manifest_file),
                "--platform",
                current_platform_id(),
                "--install-dir",
                str(self.base / "managed-install"),
                "--confirm",
                "INSTALL_PROVIDER_BINARIES",
            )
        self.assertEqual(code, 1)
        self.assertIn("Checksum verification failed", output)

    def test_binary_install_apply_blocks_path_traversal(self) -> None:
        with self._manifest_fixture() as (manifest_file, _payloads):
            install_dir = self.base / "safe-root" / ".." / "escape-root"
            code, output = self.run_cli(
                "binary",
                "install",
                "apply",
                "--manifest",
                str(manifest_file),
                "--platform",
                current_platform_id(),
                "--install-dir",
                str(install_dir),
                "--confirm",
                "INSTALL_PROVIDER_BINARIES",
            )
        self.assertEqual(code, 1)
        self.assertIn("Path traversal blocked", output)

    def test_binary_install_apply_blocks_symlink_escape(self) -> None:
        with self._manifest_fixture() as (manifest_file, payloads):
            if not payloads:
                self.skipTest("Current platform has no provider fixture adapters")
            install_dir = self.base / "managed-install"
            install_dir.mkdir(parents=True, exist_ok=True)
            adapter = next(iter(payloads))
            outside = self.base / "outside"
            outside.mkdir(parents=True, exist_ok=True)
            link = install_dir / adapter
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (NotImplementedError, OSError):
                self.skipTest("Symlink creation is not available on this host")
            code, output = self.run_cli(
                "binary",
                "install",
                "apply",
                "--manifest",
                str(manifest_file),
                "--platform",
                current_platform_id(),
                "--install-dir",
                str(install_dir),
                "--confirm",
                "INSTALL_PROVIDER_BINARIES",
            )
        self.assertEqual(code, 1)
        self.assertIn("Symlink escape blocked", output)

    def test_binary_install_plan_rejects_unknown_platform(self) -> None:
        with self._manifest_fixture() as (manifest_file, _payloads):
            code, output = self.run_cli(
                "binary",
                "install",
                "plan",
                "--manifest",
                str(manifest_file),
                "--platform",
                "unknown-platform",
            )
        self.assertEqual(code, 1)
        self.assertIn("Unsupported platform", output)

    def test_binary_install_apply_is_idempotent_when_checksum_matches(self) -> None:
        with self._manifest_fixture() as (manifest_file, payloads):
            if not payloads:
                self.skipTest("Current platform has no provider fixture adapters")
            install_dir = self.base / "managed-install"
            first_code, first_output = self.run_cli(
                "binary",
                "install",
                "apply",
                "--manifest",
                str(manifest_file),
                "--platform",
                current_platform_id(),
                "--install-dir",
                str(install_dir),
                "--confirm",
                "INSTALL_PROVIDER_BINARIES",
            )
            self.assertEqual(first_code, 0, msg=first_output)
            second_code, second_output = self.run_cli(
                "binary",
                "install",
                "apply",
                "--manifest",
                str(manifest_file),
                "--platform",
                current_platform_id(),
                "--install-dir",
                str(install_dir),
                "--confirm",
                "INSTALL_PROVIDER_BINARIES",
            )
        self.assertEqual(second_code, 0, msg=second_output)
        payload = json.loads(second_output)
        self.assertTrue(any(item["result"] == "already_installed" for item in payload["results"] if item["adapter"] in payloads))

    def test_binary_install_list_reports_installed_entries(self) -> None:
        with self._manifest_fixture() as (manifest_file, payloads):
            if not payloads:
                self.skipTest("Current platform has no provider fixture adapters")
            install_dir = self.base / "managed-install"
            code, output = self.run_cli(
                "binary",
                "install",
                "apply",
                "--manifest",
                str(manifest_file),
                "--platform",
                current_platform_id(),
                "--install-dir",
                str(install_dir),
                "--confirm",
                "INSTALL_PROVIDER_BINARIES",
            )
        self.assertEqual(code, 0, msg=output)
        list_code, list_output = self.run_cli(
            "binary",
            "install",
            "list",
            "--install-dir",
            str(install_dir),
        )
        self.assertEqual(list_code, 0, msg=list_output)
        payload = json.loads(list_output)
        self.assertTrue(payload["entries"])

    def test_binary_resolver_prefers_managed_install_dir_over_path_unless_configured_otherwise(self) -> None:
        platform_id = current_platform_id()
        managed_dir = self.base / "managed"
        managed_dir.mkdir(parents=True, exist_ok=True)
        managed_binary = managed_dir / "rathole" / platform_id / ("rathole.exe" if platform_id.startswith("windows") else "rathole")
        managed_binary.parent.mkdir(parents=True, exist_ok=True)
        managed_binary.write_bytes(b"managed")
        path_binary = self.base / "path-rathole"
        path_binary.write_bytes(b"path")
        config = AppConfig(
            binary_resolution=BinaryResolutionSettings(
                managed_install_dir=str(managed_dir),
                allow_system_path=True,
                prefer_managed_install=True,
            )
        )
        resolved = resolve_binary_reference(
            adapter="rathole",
            config=config,
            state=AppState(),
            requested_platform=platform_id,
            path_lookup=lambda name: str(path_binary),
        )
        self.assertEqual(resolved["source"], "managed_install")
        config.binary_resolution.prefer_managed_install = False
        resolved_path_first = resolve_binary_reference(
            adapter="rathole",
            config=config,
            state=AppState(),
            requested_platform=platform_id,
            path_lookup=lambda name: str(path_binary),
        )
        self.assertEqual(resolved_path_first["source"], "system_path")

    def test_binary_resolver_rejects_unknown_binary(self) -> None:
        with self.assertRaises(KeyError):
            resolve_binary_reference(
                adapter="unknown",
                config=AppConfig(),
                state=AppState(),
                requested_platform=current_platform_id(),
            )

    @patch("pilottunnel.binary_install._download_provider_binary")
    def test_binary_install_apply_uses_local_cache_when_checksum_matches(self, mock_download) -> None:
        with self._manifest_fixture() as (manifest_file, payloads):
            if "rathole" not in payloads:
                self.skipTest("Current platform does not support rathole fixture")
            runtime_root = self.base / "direct-runtime"
            runtime_root.mkdir(parents=True, exist_ok=True)
            source_path = self.base / "rathole-cache"
            source_path.write_bytes(payloads["rathole"])
            state = AppState()
            import_binary(
                adapter="rathole",
                source=source_path,
                version="manual-v0.0.0",
                cache_root=runtime_root,
                state=state,
                force=True,
            )
            config = AppConfig()
            mock_download.side_effect = lambda entry: {
                "bytes": payloads[entry.adapter],
                "sha256": entry.sha256,
                "size_bytes": len(payloads[entry.adapter]),
            }
            payload = apply_binary_install(
                manifest_file=manifest_file,
                requested_platform=current_platform_id(),
                install_dir=self.base / "managed-install-cache",
                config=config,
                state=state,
                confirm="INSTALL_PROVIDER_BINARIES",
                audit_path=self.base / "audit.log",
            )
        self.assertTrue(payload["ok"])
        selected = next(item for item in payload["results"] if item["adapter"] == "rathole")
        self.assertEqual(selected["source"], "local_cache")
