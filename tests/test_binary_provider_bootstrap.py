import hashlib
import gzip
import io
import json
import socket
import tarfile
import tempfile
import urllib.error
import urllib.parse
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from pilottunnel import cli
from pilottunnel.binaries import all_binary_adapters, binary_spec, current_platform_id, provider_required_adapters
from pilottunnel.bootstrap import build_bootstrap_command
from pilottunnel.upstream_sources import (
    SOURCE_SUMMARY_FILENAME,
    _download_url_bytes,
    _extract_binary_bytes,
    _load_release_metadata,
    upstream_source,
)
from testsupport import allocate_tcp_ports, static_http_server


class BinaryProviderBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, root: Path, *args: str) -> tuple[int, str]:
        root.mkdir(parents=True, exist_ok=True)
        output = io.StringIO()
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

    def capture_help(self, *args: str) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(SystemExit) as exc:
                cli.main(list(args))
        self.assertEqual(exc.exception.code, 0)
        return output.getvalue()

    def _profile_ports(self) -> tuple[list[int], list]:
        return allocate_tcp_ports(5)

    def _manifest_entry(
        self,
        *,
        adapter: str,
        url: str,
        sha256: str,
        size_bytes: int,
        platform_id: str | None = None,
        binary_name: str | None = None,
        filename: str | None = None,
    ) -> dict[str, object]:
        spec = binary_spec(adapter)
        return {
            "adapter": adapter,
            "binary_name": binary_name or spec.binary_name,
            "version": "v0.0.1",
            "platform": platform_id or current_platform_id(),
            "filename": filename or Path(urllib.parse.urlparse(url).path).name,
            "url": url,
            "sha256": sha256,
            "size_bytes": size_bytes,
        }

    def _write_manifest(self, root: Path, entries: list[dict[str, object]]) -> Path:
        manifest_path = root / "provider-manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "schema": "pilottunnel-binary-provider-v1",
                    "provider": "generic-provider",
                    "generated_at": "2026-01-01T00:00:00Z",
                    "binaries": entries,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return manifest_path

    def _supported_manifest_entries(self, base_url: str, metadata: dict[str, dict[str, object]]) -> list[dict[str, object]]:
        platform_id = current_platform_id()
        return [
            self._manifest_entry(
                adapter=adapter,
                url=f"{base_url}/{info['relative_path']}",
                sha256=str(info["sha256"]),
                size_bytes=int(info["size_bytes"]),
                platform_id=platform_id,
            )
            for adapter, info in metadata.items()
            if platform_id in binary_spec(adapter).supported_platforms
        ]

    def _provider_fixture(self, adapters: tuple[str, ...] | None = None) -> tuple[Path, dict[str, dict[str, object]]]:
        fixture_root = self.base / "provider-fixture"
        binaries_root = fixture_root / "binaries"
        binaries_root.mkdir(parents=True, exist_ok=True)
        metadata: dict[str, dict[str, object]] = {}
        for adapter in adapters or provider_required_adapters():
            spec = binary_spec(adapter)
            payload = f"{adapter}-fixture".encode("utf-8")
            relative_path = Path("binaries") / f"{adapter}-{spec.binary_name}"
            binary_path = fixture_root / relative_path
            binary_path.write_bytes(payload)
            metadata[adapter] = {
                "binary_name": spec.binary_name,
                "relative_path": relative_path.as_posix(),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        return fixture_root, metadata

    def _provider_source_tree(self, adapters: tuple[str, ...] | None = None) -> tuple[Path, dict[str, dict[str, object]]]:
        source_root = self.base / "provider-source"
        metadata: dict[str, dict[str, object]] = {}
        for adapter in adapters or ("backhaul", "rathole"):
            spec = binary_spec(adapter)
            platform_id = current_platform_id()
            if platform_id not in spec.supported_platforms:
                continue
            binary_dir = source_root / adapter / platform_id
            binary_dir.mkdir(parents=True, exist_ok=True)
            payload = f"{adapter}-{platform_id}-fixture".encode("utf-8")
            binary_path = binary_dir / spec.binary_name
            binary_path.write_bytes(payload)
            metadata[adapter] = {
                "platform": platform_id,
                "binary_name": spec.binary_name,
                "relative_path": Path(adapter) / platform_id / spec.binary_name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
            }
        return source_root, metadata

    def _sample_asset_name(self, adapter: str, platform_id: str) -> str:
        sample_names = {
            ("backhaul", "linux-amd64"): "backhaul_linux_amd64.tar.gz",
            ("backhaul", "linux-arm64"): "backhaul_linux_arm64.tar.gz",
            ("rathole", "linux-amd64"): "rathole-x86_64-unknown-linux-gnu.zip",
            ("rathole", "linux-arm64"): "rathole-aarch64-unknown-linux-musl.zip",
            ("rathole", "windows-amd64"): "rathole-x86_64-pc-windows-msvc.zip",
            ("frp", "linux-amd64"): "frp_0.0.1_linux_amd64.tar.gz",
            ("frp", "linux-arm64"): "frp_0.0.1_linux_arm64.tar.gz",
            ("frp", "windows-amd64"): "frp_0.0.1_windows_amd64.zip",
            ("gost", "linux-amd64"): "gost_0.0.1_linux_amd64.tar.gz",
            ("gost", "linux-arm64"): "gost_0.0.1_linux_arm64.tar.gz",
            ("gost", "windows-amd64"): "gost_0.0.1_windows_amd64.zip",
            ("chisel", "linux-amd64"): "chisel_0.0.1_linux_amd64.gz",
            ("chisel", "linux-arm64"): "chisel_0.0.1_linux_arm64.gz",
            ("chisel", "windows-amd64"): "chisel_0.0.1_windows_amd64.zip",
            ("realm", "linux-amd64"): "realm-x86_64-unknown-linux-gnu.tar.gz",
            ("realm", "linux-arm64"): "realm-aarch64-unknown-linux-gnu.tar.gz",
            ("realm", "windows-amd64"): "realm-x86_64-pc-windows-msvc.tar.gz",
            ("bore", "linux-amd64"): "bore-v0.0.1-x86_64-unknown-linux-musl.tar.gz",
            ("bore", "linux-arm64"): "bore-v0.0.1-aarch64-unknown-linux-musl.tar.gz",
            ("bore", "windows-amd64"): "bore-v0.0.1-x86_64-pc-windows-msvc.zip",
        }
        return sample_names[(adapter, platform_id)]

    def _sample_member_name(self, adapter: str, platform_id: str) -> str:
        if platform_id.startswith("windows"):
            executable = f"{binary_spec(adapter).binary_name}.exe"
            if adapter == "frp":
                return f"bundle/{executable}"
            return f"bundle/{executable}"
        executable = binary_spec(adapter).binary_name
        if adapter == "frp":
            return f"bundle/{executable}"
        return f"bundle/{executable}"

    def _archive_payload(self, archive_type: str, member_name: str, payload: bytes) -> bytes:
        if archive_type == "raw":
            return payload
        if archive_type == "gz":
            return gzip.compress(payload)
        if archive_type == "tar.gz":
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                info = tarfile.TarInfo(name=member_name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
            return buffer.getvalue()
        if archive_type == "zip":
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, mode="w") as archive:
                archive.writestr(member_name, payload)
            return buffer.getvalue()
        raise AssertionError(f"Unsupported archive type for test fixture: {archive_type}")

    def _tar_gz_payload_from_entries(self, entries: list[tuple[str, bytes, int]]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, payload, mode in entries:
                info = tarfile.TarInfo(name=name)
                info.mode = mode
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return buffer.getvalue()

    def _zip_payload_from_entries(self, entries: list[tuple[str, bytes, int]]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, mode="w") as archive:
            for name, payload, mode in entries:
                info = zipfile.ZipInfo(name)
                info.external_attr = mode << 16
                archive.writestr(info, payload)
        return buffer.getvalue()

    def _fake_release_fixtures(self, adapters: tuple[str, ...] | None = None) -> tuple[dict[str, dict[str, object]], dict[str, bytes]]:
        platform_id = current_platform_id()
        release_index: dict[str, dict[str, object]] = {}
        asset_payloads: dict[str, bytes] = {}
        selected = adapters or tuple(provider_required_adapters())
        for adapter in selected:
            source = upstream_source(adapter)
            if source.category != "external_binary":
                continue
            if platform_id not in source.supported_platforms:
                continue
            asset_name = self._sample_asset_name(adapter, platform_id)
            asset_url = f"https://github.com/{source.repo_slug}/releases/download/v0.0.1/{asset_name}"
            binary_payload = f"{adapter}-{platform_id}-upstream".encode("utf-8")
            archive_payload = self._archive_payload(
                source.archive_handling[platform_id],
                self._sample_member_name(adapter, platform_id),
                binary_payload,
            )
            release_index[adapter] = {
                "version": "v0.0.1",
                "assets": [{"name": asset_name, "url": asset_url}],
            }
            asset_payloads[asset_url] = archive_payload
        return release_index, asset_payloads

    def _forbidden_port_candidates(self) -> set[int]:
        return {
            (19 * 2000) + 80,
            (39 * 1000) + 80,
            (39 * 1000) + 81,
            (39 * 1000) + 82,
            (39 * 1000) + 83,
        }

    def _controller_profile_args(self, profile: str, ports: list[int]) -> list[str]:
        return [
            "--profile",
            profile,
            "--target-host",
            "127.0.0.1",
            "--main-port",
            str(ports[0]),
            "--target-port",
            str(ports[1]),
            "--control-port",
            str(ports[2]),
            "--service-port",
            str(ports[3]),
            "--check-port",
            str(ports[4]),
        ]

    def _controller_auto_profile_args(self, profile: str) -> list[str]:
        return [
            "--profile",
            profile,
            "--target-host",
            "127.0.0.1",
            "--ports",
            "auto",
        ]

    def _release_version_args(self, adapters: tuple[str, ...]) -> list[str]:
        args: list[str] = []
        for adapter in adapters:
            args.extend(["--version", f"{adapter}=v0.0.1"])
        return args

    def _init_and_create_profile(self, root: Path, profile: str, ports: list[int]) -> None:
        init_code, init_output = self.run_cli(root, "init", "--role", "controller")
        self.assertEqual(init_code, 0, msg=init_output)
        code, output = self.run_cli(
            root,
            "profile",
            "create",
            "--name",
            profile,
            "--main-port",
            str(ports[0]),
            "--target-host",
            "127.0.0.1",
            "--target-port",
            str(ports[1]),
            "--role",
            "controller",
            "--control-port",
            str(ports[2]),
            "--service-port",
            str(ports[3]),
            "--check-port",
            str(ports[4]),
        )
        self.assertEqual(code, 0, msg=output)

    def test_manifest_inspect_from_file(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        manifest_path = self._write_manifest(
            fixture_root,
            [
                self._manifest_entry(
                    adapter="backhaul",
                    url="http://127.0.0.1/binaries/backhaul-backhaul",
                    sha256=str(metadata["backhaul"]["sha256"]),
                    size_bytes=int(metadata["backhaul"]["size_bytes"]),
                )
            ],
        )
        code, output = self.run_cli(self.base / "inspect-file", "binary", "provider", "inspect", "--manifest-file", str(manifest_path))
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["provider"], "generic-provider")
        self.assertEqual(payload["binaries"][0]["adapter"], "backhaul")

    def test_manifest_inspect_from_local_http_url(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                [
                    self._manifest_entry(
                        adapter="backhaul",
                        url=f"{base_url}/{metadata['backhaul']['relative_path']}",
                        sha256=str(metadata["backhaul"]["sha256"]),
                        size_bytes=int(metadata["backhaul"]["size_bytes"]),
                    )
                ],
            )
            code, output = self.run_cli(
                self.base / "inspect-http",
                "binary",
                "provider",
                "inspect",
                "--manifest-url",
                f"{base_url}/{manifest_path.name}",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertEqual(json.loads(output)["source"], f"{base_url}/{manifest_path.name}")

    def test_manifest_rejects_missing_sha256(self) -> None:
        fixture_root, _metadata = self._provider_fixture(("backhaul",))
        manifest_path = self._write_manifest(
            fixture_root,
            [
                {
                    "adapter": "backhaul",
                    "binary_name": "backhaul",
                    "version": "v0.0.1",
                    "platform": current_platform_id(),
                    "url": "http://127.0.0.1/binaries/backhaul-backhaul",
                    "size_bytes": 12,
                }
            ],
        )
        code, output = self.run_cli(self.base / "missing-sha", "binary", "provider", "inspect", "--manifest-file", str(manifest_path))
        self.assertEqual(code, 1)
        self.assertIn("sha256", output)

    def test_manifest_rejects_unknown_adapter(self) -> None:
        fixture_root, _metadata = self._provider_fixture(("backhaul",))
        manifest_path = self._write_manifest(
            fixture_root,
            [
                {
                    "adapter": "unknown",
                    "binary_name": "unknown",
                    "version": "v0.0.1",
                    "platform": current_platform_id(),
                    "url": "http://127.0.0.1/binaries/unknown",
                    "sha256": "0" * 64,
                    "size_bytes": 7,
                }
            ],
        )
        code, output = self.run_cli(self.base / "unknown-adapter", "binary", "provider", "inspect", "--manifest-file", str(manifest_path))
        self.assertEqual(code, 1)
        self.assertIn("Unknown binary adapter", output)

    def test_manifest_rejects_unsupported_platform(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        manifest_path = self._write_manifest(
            fixture_root,
            [
                self._manifest_entry(
                    adapter="backhaul",
                    url="http://127.0.0.1/binaries/backhaul-backhaul",
                    sha256=str(metadata["backhaul"]["sha256"]),
                    size_bytes=int(metadata["backhaul"]["size_bytes"]),
                    platform_id="darwin-amd64",
                )
            ],
        )
        code, output = self.run_cli(self.base / "bad-platform", "binary", "provider", "inspect", "--manifest-file", str(manifest_path))
        self.assertEqual(code, 1)
        self.assertIn("Unsupported platform", output)

    def test_manifest_rejects_non_https_url_except_localhost(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        manifest_path = self._write_manifest(
            fixture_root,
            [
                self._manifest_entry(
                    adapter="backhaul",
                    url="http://example.com/backhaul",
                    sha256=str(metadata["backhaul"]["sha256"]),
                    size_bytes=int(metadata["backhaul"]["size_bytes"]),
                )
            ],
        )
        code, output = self.run_cli(self.base / "bad-host", "binary", "provider", "inspect", "--manifest-file", str(manifest_path))
        self.assertEqual(code, 1)
        self.assertIn("HTTPS", output)

    def test_manifest_rejects_allowlisted_host_mismatch(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        manifest_path = self._write_manifest(
            fixture_root,
            [
                self._manifest_entry(
                    adapter="backhaul",
                    url="https://downloads.example.com/backhaul",
                    sha256=str(metadata["backhaul"]["sha256"]),
                    size_bytes=int(metadata["backhaul"]["size_bytes"]),
                )
            ],
        )
        code, output = self.run_cli(
            self.base / "allow-mismatch",
            "binary",
            "provider",
            "inspect",
            "--manifest-file",
            str(manifest_path),
            "--allow-provider-host",
            "packages.example.com",
        )
        self.assertEqual(code, 1)
        self.assertIn("allowlisted host", output)

    def test_manifest_rejects_path_traversal_fields(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        manifest_path = self._write_manifest(
            fixture_root,
            [
                self._manifest_entry(
                    adapter="backhaul",
                    url="http://127.0.0.1/binaries/backhaul-backhaul",
                    sha256=str(metadata["backhaul"]["sha256"]),
                    size_bytes=int(metadata["backhaul"]["size_bytes"]),
                    binary_name="../backhaul",
                )
            ],
        )
        code, output = self.run_cli(self.base / "path-traversal", "binary", "provider", "inspect", "--manifest-file", str(manifest_path))
        self.assertEqual(code, 1)
        self.assertIn("Path traversal", output)

    def test_provider_generate_manifest_creates_sha256_and_size_metadata(self) -> None:
        source_root, metadata = self._provider_source_tree(("backhaul", "rathole"))
        output_path = self.base / "generated-manifest.json"
        code, output = self.run_cli(
            self.base / "generate-manifest",
            "binary",
            "provider",
            "generate-manifest",
            "--provider-name",
            "generic-provider",
            "--base-url",
            "https://downloads.example.com/pilot",
            "--source-dir",
            str(source_root),
            "--output",
            str(output_path),
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["schema"], "pilottunnel-binary-provider-v1")
        manifest = json.loads(output_path.read_text(encoding="utf-8"))
        entries = {(item["adapter"], item["platform"]): item for item in manifest["binaries"]}
        for adapter, info in metadata.items():
            key = (adapter, str(info["platform"]))
            self.assertIn(key, entries)
            self.assertEqual(entries[key]["filename"], Path(str(info["relative_path"])).name)
            self.assertEqual(entries[key]["sha256"], info["sha256"])
            self.assertEqual(entries[key]["size_bytes"], info["size_bytes"])

    def test_provider_release_plan_generates_pinned_user_owned_github_asset_urls(self) -> None:
        selected = tuple(provider_required_adapters())
        source_root, _metadata = self._provider_source_tree(selected)
        output_dir = self.base / "provider-release-plan"
        repo_slug = "example/PilotTunnel-Binaries"
        release_tag = "v0.0.1-binaries"
        code, output = self.run_cli(
            self.base / "provider-release-plan-root",
            "binary",
            "provider",
            "release-plan",
            "--source-dir",
            str(source_root),
            "--provider-name",
            "generic-provider",
            "--repo-slug",
            repo_slug,
            "--release-tag",
            release_tag,
            "--output-dir",
            str(output_dir),
            *self._release_version_args(selected),
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["main_repo_remains_source_only"])
        self.assertFalse(payload["binary_files_committed_to_main_repo"])
        self.assertIn("github.com", payload["recommended_allow_provider_hosts"])
        self.assertIn("release-assets.githubusercontent.com", payload["recommended_allow_provider_hosts"])
        self.assertTrue(payload["binaries"])
        self.assertIn("/releases/download/", payload["release_base_url"])
        self.assertIn(urllib.parse.quote(release_tag), payload["release_base_url"])
        for item in payload["binaries"]:
            self.assertEqual(item["version"], "v0.0.1")
            self.assertIn(f"/releases/download/{urllib.parse.quote(release_tag)}/", item["url"])
            self.assertIn("example/PilotTunnel-Binaries", item["url"])
            self.assertTrue(item["filename"].startswith(f"{item['adapter']}-{item['platform']}-v0.0.1-"))

    def test_provider_release_assets_writes_manifest_and_normalized_files(self) -> None:
        selected = tuple(provider_required_adapters())
        source_root, _metadata = self._provider_source_tree(selected)
        output_dir = self.base / "provider-release-assets"
        code, output = self.run_cli(
            self.base / "provider-release-assets-root",
            "binary",
            "provider",
            "release-assets",
            "--source-dir",
            str(source_root),
            "--provider-name",
            "generic-provider",
            "--repo-slug",
            "example/PilotTunnel-Binaries",
            "--release-tag",
            "v0.0.1-binaries",
            "--output-dir",
            str(output_dir),
            *self._release_version_args(selected),
            "--confirm",
            "PREPARE_PROVIDER_RELEASE_ASSETS",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        manifest_path = output_dir / "provider-manifest.json"
        self.assertTrue(manifest_path.exists())
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in manifest["binaries"]:
            self.assertIn("filename", item)
            self.assertTrue((output_dir / item["filename"]).exists())
            self.assertEqual(item["version"], "v0.0.1")
        self.assertIn(str(manifest_path), payload["files_written"])

    def test_provider_release_assets_blocks_missing_required_entries(self) -> None:
        source_root, _metadata = self._provider_source_tree(("backhaul",))
        output_dir = self.base / "provider-release-missing"
        code, output = self.run_cli(
            self.base / "provider-release-missing-root",
            "binary",
            "provider",
            "release-plan",
            "--source-dir",
            str(source_root),
            "--provider-name",
            "generic-provider",
            "--repo-slug",
            "example/PilotTunnel-Binaries",
            "--release-tag",
            "v0.0.1-binaries",
            "--output-dir",
            str(output_dir),
            "--version",
            "backhaul=v0.0.1",
        )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["missing_required"])

    def test_manifest_accepts_comma_separated_allowlisted_hosts(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        manifest_path = self._write_manifest(
            fixture_root,
            [
                self._manifest_entry(
                    adapter="backhaul",
                    url="https://release-assets.githubusercontent.com/backhaul-amd64",
                    sha256=str(metadata["backhaul"]["sha256"]),
                    size_bytes=int(metadata["backhaul"]["size_bytes"]),
                    filename="backhaul-amd64",
                )
            ],
        )
        code, output = self.run_cli(
            self.base / "comma-allow-hosts",
            "binary",
            "provider",
            "inspect",
            "--manifest-file",
            str(manifest_path),
            "--allow-provider-host",
            "github.com,release-assets.githubusercontent.com",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertIn("release-assets.githubusercontent.com", payload["allow_provider_hosts"])

    def test_provider_generate_manifest_rejects_unknown_adapter(self) -> None:
        source_root = self.base / "provider-source-unknown"
        bad_dir = source_root / "unknown" / current_platform_id()
        bad_dir.mkdir(parents=True, exist_ok=True)
        (bad_dir / "unknown").write_bytes(b"fixture")
        code, output = self.run_cli(
            self.base / "generate-manifest-unknown",
            "binary",
            "provider",
            "generate-manifest",
            "--provider-name",
            "generic-provider",
            "--base-url",
            "https://downloads.example.com/pilot",
            "--source-dir",
            str(source_root),
            "--output",
            str(self.base / "unknown-manifest.json"),
        )
        self.assertEqual(code, 1)
        self.assertIn("Unknown binary adapter", output)

    def test_provider_generate_manifest_blocks_symlink_escape(self) -> None:
        source_root, _metadata = self._provider_source_tree(("backhaul",))
        escape_target = self.base / "outside-binary"
        escape_target.write_bytes(b"outside")
        link_path = source_root / "backhaul" / current_platform_id() / binary_spec("backhaul").binary_name
        link_path.unlink()
        try:
            link_path.symlink_to(escape_target)
        except (NotImplementedError, OSError):
            self.skipTest("Symlink creation is not available on this host")
        code, output = self.run_cli(
            self.base / "generate-manifest-symlink",
            "binary",
            "provider",
            "generate-manifest",
            "--provider-name",
            "generic-provider",
            "--base-url",
            "https://downloads.example.com/pilot",
            "--source-dir",
            str(source_root),
            "--output",
            str(self.base / "symlink-manifest.json"),
        )
        self.assertEqual(code, 1)
        self.assertIn("Symlink escape blocked", output)

    def test_bootstrap_command_help_includes_allow_provider_host(self) -> None:
        help_text = self.capture_help("bootstrap", "command", "--help")
        self.assertIn("--allow-provider-host", help_text)
        self.assertNotIn("--provider-host", help_text)

    def test_bootstrap_apply_help_includes_bundle_file(self) -> None:
        help_text = self.capture_help("bootstrap", "apply", "--help")
        self.assertIn("--bundle-file", help_text)
        self.assertNotIn("--bundle-input", help_text)

    def test_binary_source_list_includes_catalog_entries(self) -> None:
        code, output = self.run_cli(self.base / "source-list", "binary", "source", "list")
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        sources = {item["adapter"]: item for item in payload["sources"]}
        self.assertIn("backhaul", sources)
        self.assertIn("ssh_reverse", sources)
        self.assertEqual(sources["ssh_reverse"]["category"], "system_dependency")

    @patch("pilottunnel.upstream_sources._download_asset_bytes")
    @patch("pilottunnel.upstream_sources._load_release_metadata")
    def test_binary_source_fetch_dry_run_plans_downloads_without_writing_binaries(self, mock_release, mock_download) -> None:
        release_index, _asset_payloads = self._fake_release_fixtures()
        mock_release.side_effect = lambda source, version: release_index[source.adapter]
        root = self.base / "source-fetch-dry-run"
        source_dir = root / "provider-source"
        code, output = self.run_cli(
            root,
            "binary",
            "source",
            "fetch",
            "--source-dir",
            str(source_dir),
            "--dry-run",
            *self._release_version_args(tuple(provider_required_adapters())),
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertFalse(payload["downloads_performed"])
        results = {item["adapter"]: item["result"] for item in payload["results"]}
        self.assertEqual(results["ssh_reverse"], "skipped_system_dependency")
        if current_platform_id() in upstream_source("backhaul").supported_platforms:
            self.assertEqual(results["backhaul"], "planned_download")
        self.assertFalse((source_dir / "rathole" / current_platform_id() / "rathole").exists())
        self.assertFalse(mock_download.called)

    def test_binary_source_fetch_refuses_without_confirm(self) -> None:
        root = self.base / "source-fetch-no-confirm"
        code, output = self.run_cli(
            root,
            "binary",
            "source",
            "fetch",
            "--source-dir",
            str(root / "provider-source"),
            *self._release_version_args(tuple(provider_required_adapters())),
        )
        self.assertEqual(code, 1)
        self.assertIn("FETCH_UPSTREAM_BINARIES", output)

    def test_binary_source_fetch_requires_explicit_version_tags(self) -> None:
        root = self.base / "source-fetch-no-version"
        code, output = self.run_cli(
            root,
            "binary",
            "source",
            "fetch",
            "--source-dir",
            str(root / "provider-source"),
            "--dry-run",
        )
        self.assertEqual(code, 1)
        self.assertIn("explicit version/tag", output)
        self.assertIn("dynamic latest releases are not allowed", output)

    @patch("pilottunnel.upstream_sources._read_json_url")
    def test_binary_source_fetch_uses_explicit_github_tag_endpoint(self, mock_read_json_url) -> None:
        mock_read_json_url.return_value = {
            "tag_name": "v1.2.3",
            "assets": [
                {
                    "name": "backhaul_linux_amd64.tar.gz",
                    "browser_download_url": "https://github.com/Musixal/Backhaul/releases/download/v1.2.3/backhaul_linux_amd64.tar.gz",
                }
            ],
        }
        _load_release_metadata(upstream_source("backhaul"), "v1.2.3")
        request_url = mock_read_json_url.call_args.args[0]
        self.assertIn("releases/tags/v1.2.3", request_url)

    def test_extract_tar_gz_selects_nested_expected_binary_and_ignores_docs(self) -> None:
        payload = self._tar_gz_payload_from_entries(
            [
                ("backhaul_linux_amd64/README.md", b"readme", 0o644),
                ("backhaul_linux_amd64/LICENSE", b"license", 0o644),
                ("backhaul_linux_amd64/bin/backhaul", b"binary-payload", 0o755),
            ]
        )
        extracted = _extract_binary_bytes(
            source=upstream_source("backhaul"),
            platform_id="linux-amd64",
            asset_name="backhaul_linux_amd64.tar.gz",
            archive_type="tar.gz",
            payload=payload,
        )
        self.assertEqual(extracted, b"binary-payload")

    def test_extract_tar_gz_selects_top_level_expected_binary_and_ignores_docs(self) -> None:
        payload = self._tar_gz_payload_from_entries(
            [
                ("LICENSE", b"license", 0o644),
                ("README.md", b"readme", 0o644),
                ("backhaul", b"top-level-backhaul", 0o755),
            ]
        )
        extracted = _extract_binary_bytes(
            source=upstream_source("backhaul"),
            platform_id="linux-amd64",
            asset_name="backhaul_linux_amd64.tar.gz",
            archive_type="tar.gz",
            payload=payload,
        )
        self.assertEqual(extracted, b"top-level-backhaul")

    def test_extract_zip_selects_nested_expected_binary_and_ignores_docs(self) -> None:
        payload = self._zip_payload_from_entries(
            [
                ("rathole-x86_64-unknown-linux-gnu/README.md", b"readme", 0o644),
                ("rathole-x86_64-unknown-linux-gnu/LICENSE", b"license", 0o644),
                ("rathole-x86_64-unknown-linux-gnu/bin/rathole", b"zip-binary", 0o755),
            ]
        )
        extracted = _extract_binary_bytes(
            source=upstream_source("rathole"),
            platform_id="linux-amd64",
            asset_name="rathole-x86_64-unknown-linux-gnu.zip",
            archive_type="zip",
            payload=payload,
        )
        self.assertEqual(extracted, b"zip-binary")

    def test_extract_zip_selects_top_level_expected_binary(self) -> None:
        payload = self._zip_payload_from_entries(
            [
                ("rathole", b"top-level-rathole", 0o755),
            ]
        )
        extracted = _extract_binary_bytes(
            source=upstream_source("rathole"),
            platform_id="linux-amd64",
            asset_name="rathole-x86_64-unknown-linux-gnu.zip",
            archive_type="zip",
            payload=payload,
        )
        self.assertEqual(extracted, b"top-level-rathole")

    def test_extract_tar_gz_selects_top_level_gost_binary_and_ignores_extra_readmes(self) -> None:
        payload = self._tar_gz_payload_from_entries(
            [
                ("LICENSE", b"license", 0o644),
                ("README.md", b"readme", 0o644),
                ("README_en.md", b"readme-en", 0o644),
                ("gost", b"top-level-gost", 0o755),
            ]
        )
        extracted = _extract_binary_bytes(
            source=upstream_source("gost"),
            platform_id="linux-amd64",
            asset_name="gost_3.2.6_linux_amd64.tar.gz",
            archive_type="tar.gz",
            payload=payload,
        )
        self.assertEqual(extracted, b"top-level-gost")

    def test_extract_tar_gz_rejects_ambiguous_binary_candidates(self) -> None:
        payload = self._tar_gz_payload_from_entries(
            [
                ("aa/backhaul", b"binary-a", 0o755),
                ("bb/backhaul", b"binary-b", 0o755),
            ]
        )
        with self.assertRaises(ValueError) as exc:
            _extract_binary_bytes(
                source=upstream_source("backhaul"),
                platform_id="linux-amd64",
                asset_name="backhaul_linux_amd64.tar.gz",
                archive_type="tar.gz",
                payload=payload,
            )
        self.assertIn("Ambiguous binary payloads", str(exc.exception))

    @patch("pilottunnel.upstream_sources.time.sleep")
    @patch("pilottunnel.upstream_sources.urllib.request.build_opener")
    def test_download_url_retries_timeout_once_before_success(self, mock_build_opener, mock_sleep) -> None:
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"downloaded"

        opener = Mock()
        opener.open.side_effect = [
            urllib.error.URLError(socket.timeout("timed out")),
            response,
        ]
        mock_build_opener.return_value = opener

        data = _download_url_bytes("https://github.com/example/release.tar.gz", allowed_hosts={"github.com"})
        self.assertEqual(data, b"downloaded")
        self.assertEqual(opener.open.call_count, 2)
        mock_sleep.assert_called_once()

    def test_binary_source_fetch_rejects_unknown_adapter(self) -> None:
        root = self.base / "source-fetch-unknown"
        code, output = self.run_cli(
            root,
            "binary",
            "source",
            "fetch",
            "--source-dir",
            str(root / "provider-source"),
            "--adapter",
            "unknown",
            "--dry-run",
        )
        self.assertEqual(code, 1)
        self.assertIn("Unknown upstream source adapter", output)

    @patch("pilottunnel.upstream_sources._download_asset_bytes")
    @patch("pilottunnel.upstream_sources._load_release_metadata")
    def test_binary_source_fetch_downloads_supported_binaries_and_writes_summary(self, mock_release, mock_download) -> None:
        release_index, asset_payloads = self._fake_release_fixtures()
        mock_release.side_effect = lambda source, version: release_index[source.adapter]
        mock_download.side_effect = lambda url, allowed_hosts: asset_payloads[url]
        root = self.base / "source-fetch"
        source_dir = root / "provider-source"
        code, output = self.run_cli(
            root,
            "binary",
            "source",
            "fetch",
            "--source-dir",
            str(source_dir),
            "--confirm",
            "FETCH_UPSTREAM_BINARIES",
            *self._release_version_args(tuple(provider_required_adapters())),
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue((source_dir / SOURCE_SUMMARY_FILENAME).exists())
        results = {item["adapter"]: item for item in payload["results"]}
        self.assertEqual(results["ssh_reverse"]["result"], "skipped_system_dependency")
        for adapter, source in ((adapter, upstream_source(adapter)) for adapter in provider_required_adapters()):
            if current_platform_id() in source.supported_platforms:
                self.assertEqual(results[adapter]["result"], "downloaded")
                self.assertTrue((source_dir / adapter / current_platform_id() / source.binary_name).exists())
            else:
                self.assertEqual(results[adapter]["result"], "skipped_unsupported_platform")

    @patch("pilottunnel.upstream_sources._download_asset_bytes")
    @patch("pilottunnel.upstream_sources._load_release_metadata")
    def test_binary_source_fetch_blocks_archive_path_traversal(self, mock_release, mock_download) -> None:
        platform_id = current_platform_id()
        source = upstream_source("frp")
        if platform_id not in source.supported_platforms:
            self.skipTest("Current platform does not support frp test fixture")
        asset_name = self._sample_asset_name("frp", platform_id)
        asset_url = f"https://github.com/{source.repo_slug}/releases/download/v0.0.1/{asset_name}"
        mock_release.return_value = {"version": "v0.0.1", "assets": [{"name": asset_name, "url": asset_url}]}
        bad_payload = self._archive_payload(source.archive_handling[platform_id], "../frpc", b"blocked")
        mock_download.return_value = bad_payload
        root = self.base / "source-fetch-bad-archive"
        code, output = self.run_cli(
            root,
            "binary",
            "source",
            "fetch",
            "--source-dir",
            str(root / "provider-source"),
            "--adapter",
            "frp",
            "--confirm",
            "FETCH_UPSTREAM_BINARIES",
            "--version",
            "frp=v0.0.1",
        )
        self.assertEqual(code, 1)
        self.assertIn("path traversal", output.lower())

    @patch("pilottunnel.upstream_sources._download_asset_bytes")
    @patch("pilottunnel.upstream_sources._load_release_metadata")
    def test_binary_provider_prepare_fetches_generates_and_verifies_manifest(self, mock_release, mock_download) -> None:
        release_index, asset_payloads = self._fake_release_fixtures()
        mock_release.side_effect = lambda source, version: release_index[source.adapter]
        mock_download.side_effect = lambda url, allowed_hosts: asset_payloads[url]
        root = self.base / "provider-prepare"
        source_dir = root / "provider-source"
        output_path = root / "provider-manifest.json"
        code, output = self.run_cli(
            root,
            "binary",
            "provider",
            "prepare",
            "--source-dir",
            str(source_dir),
            "--provider-name",
            "generic-provider",
            "--base-url",
            "https://downloads.example.com/pilot",
            "--platform",
            current_platform_id(),
            "--output",
            str(output_path),
            *self._release_version_args(tuple(provider_required_adapters())),
            "--confirm",
            "PREPARE_PROVIDER_BINARIES",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(output_path.exists())
        self.assertTrue(payload["manifest_verification"]["ok"])
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["firewall_touched"])
        self.assertFalse(payload["routes_touched"])
        self.assertFalse(payload["service_started"])

    def test_bootstrap_command_requires_core_inputs(self) -> None:
        with self.assertRaises(ValueError) as exc:
            build_bootstrap_command(
                profile_name=None,
                adapter_name=None,
                transport=None,
                ports_mode="auto",
                manifest_url=None,
                provider_host=None,
                bundle_output=None,
                bundle_file=None,
            )
        self.assertIn("--profile", str(exc.exception))
        self.assertIn("--allow-provider-host", str(exc.exception))

    def test_provider_verify_manifest_validates_schema(self) -> None:
        source_root, _metadata = self._provider_source_tree(("backhaul",))
        output_path = self.base / "verify-valid.json"
        generate_code, generate_output = self.run_cli(
            self.base / "generate-for-verify",
            "binary",
            "provider",
            "generate-manifest",
            "--provider-name",
            "generic-provider",
            "--base-url",
            "https://downloads.example.com/pilot",
            "--source-dir",
            str(source_root),
            "--output",
            str(output_path),
        )
        self.assertEqual(generate_code, 0, msg=generate_output)
        code, output = self.run_cli(
            self.base / "verify-valid",
            "binary",
            "provider",
            "verify-manifest",
            "--manifest-file",
            str(output_path),
        )
        payload = json.loads(output)
        self.assertEqual(code, 1)
        self.assertEqual(payload["schema"], "pilottunnel-binary-provider-v1")
        self.assertIn("missing_required", payload)

    def test_provider_verify_manifest_reports_missing_required_binaries(self) -> None:
        source_root, _metadata = self._provider_source_tree(("backhaul",))
        output_path = self.base / "verify-missing.json"
        self.run_cli(
            self.base / "generate-missing",
            "binary",
            "provider",
            "generate-manifest",
            "--provider-name",
            "generic-provider",
            "--base-url",
            "https://downloads.example.com/pilot",
            "--source-dir",
            str(source_root),
            "--output",
            str(output_path),
        )
        code, output = self.run_cli(
            self.base / "verify-missing",
            "binary",
            "provider",
            "verify-manifest",
            "--manifest-file",
            str(output_path),
        )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertTrue(payload["missing_required"])

    def test_binary_download_refuses_without_confirm(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                [
                    self._manifest_entry(
                        adapter="backhaul",
                        url=f"{base_url}/{metadata['backhaul']['relative_path']}",
                        sha256=str(metadata["backhaul"]["sha256"]),
                        size_bytes=int(metadata["backhaul"]["size_bytes"]),
                    )
                ],
            )
            code, output = self.run_cli(
                self.base / "download-no-confirm",
                "binary",
                "download",
                "--adapter",
                "backhaul",
                "--manifest-file",
                str(manifest_path),
            )
        self.assertEqual(code, 1)
        self.assertIn("DOWNLOAD_BINARY", output)

    def test_binary_download_all_refuses_without_confirm(self) -> None:
        fixture_root, metadata = self._provider_fixture()
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                [
                    self._manifest_entry(
                        adapter=adapter,
                        url=f"{base_url}/{info['relative_path']}",
                        sha256=str(info["sha256"]),
                        size_bytes=int(info["size_bytes"]),
                    )
                    for adapter, info in metadata.items()
                ],
            )
            code, output = self.run_cli(
                self.base / "download-all-no-confirm",
                "binary",
                "download-all",
                "--manifest-file",
                str(manifest_path),
            )
        self.assertEqual(code, 1)
        self.assertIn("DOWNLOAD_ALL_BINARIES", output)

    def test_worker_role_allows_binary_download_all_action_name(self) -> None:
        root = self.base / "download-all-worker-role"
        init_code, init_output = self.run_cli(root, "init", "--role", "worker", "--force")
        self.assertEqual(init_code, 0, msg=init_output)

        code, output = self.run_cli(
            root,
            "binary",
            "download-all",
            "--manifest-file",
            str(root / "missing-provider-manifest.json"),
        )

        self.assertEqual(code, 1)
        self.assertIn("DOWNLOAD_ALL_BINARIES", output)
        self.assertNotIn("blocked for node role", output)

    def test_binary_download_deletes_temp_file_on_checksum_mismatch(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                [
                    self._manifest_entry(
                        adapter="backhaul",
                        url=f"{base_url}/{metadata['backhaul']['relative_path']}",
                        sha256="f" * 64,
                        size_bytes=int(metadata["backhaul"]["size_bytes"]),
                    )
                ],
            )
            root = self.base / "checksum-mismatch"
            code, output = self.run_cli(
                root,
                "binary",
                "download",
                "--adapter",
                "backhaul",
                "--manifest-file",
                str(manifest_path),
                "--confirm",
                "DOWNLOAD_BINARY",
            )
        self.assertEqual(code, 1)
        self.assertIn("Checksum verification failed", output)
        downloads_dir = root / "work" / ".var" / "pilottunnel" / "cache" / "downloads"
        self.assertFalse(downloads_dir.exists() and any(downloads_dir.iterdir()))

    def test_binary_download_imports_valid_binary_into_expected_cache_bin_path(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                [
                    self._manifest_entry(
                        adapter="backhaul",
                        url=f"{base_url}/{metadata['backhaul']['relative_path']}",
                        sha256=str(metadata["backhaul"]["sha256"]),
                        size_bytes=int(metadata["backhaul"]["size_bytes"]),
                    )
                ],
            )
            root = self.base / "download-valid"
            code, output = self.run_cli(
                root,
                "binary",
                "download",
                "--adapter",
                "backhaul",
                "--manifest-file",
                str(manifest_path),
                "--confirm",
                "DOWNLOAD_BINARY",
            )
            self.assertEqual(code, 0, msg=output)
            payload = json.loads(output)
            self.assertTrue(Path(payload["imported_path"]).exists())
            status_code, status_output = self.run_cli(root, "binary", "status", "--adapter", "backhaul")
        self.assertEqual(status_code, 0, msg=status_output)
        self.assertEqual(json.loads(status_output)["install_status"], "imported")

    def test_binary_download_all_covers_all_required_adapters_and_system_dependency(self) -> None:
        fixture_root, metadata = self._provider_fixture()
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                self._supported_manifest_entries(base_url, metadata),
            )
            code, output = self.run_cli(
                self.base / "download-all",
                "binary",
                "download-all",
                "--manifest-file",
                str(manifest_path),
                "--confirm",
                "DOWNLOAD_ALL_BINARIES",
            )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        results = {item["adapter"]: item["result"] for item in payload["results"]}
        self.assertEqual(set(results), set(all_binary_adapters()))
        self.assertEqual(results["ssh_reverse"], "skipped_system_dependency")
        for adapter in provider_required_adapters():
            expected = "downloaded" if current_platform_id() in binary_spec(adapter).supported_platforms else "skipped_unsupported_platform"
            self.assertEqual(results[adapter], expected)

    def test_binary_download_all_reports_missing_from_manifest(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                self._supported_manifest_entries(base_url, metadata),
            )
            code, output = self.run_cli(
                self.base / "download-all-missing",
                "binary",
                "download-all",
                "--manifest-file",
                str(manifest_path),
                "--confirm",
                "DOWNLOAD_ALL_BINARIES",
            )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        results = {item["adapter"]: item["result"] for item in payload["results"]}
        missing = [
            adapter
            for adapter in provider_required_adapters()
            if adapter != "backhaul" and current_platform_id() in binary_spec(adapter).supported_platforms
        ]
        self.assertTrue(all(results[adapter] == "missing_from_manifest" for adapter in missing))

    def test_binary_download_all_reports_imported_without_silent_ignore(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        root = self.base / "download-all-imported"
        manual_binary = fixture_root / Path(str(metadata["backhaul"]["relative_path"]))
        import_code, import_output = self.run_cli(
            root,
            "binary",
            "import",
            "--adapter",
            "backhaul",
            "--source",
            str(manual_binary),
            "--version",
            "v0.0.1",
            "--force",
        )
        self.assertEqual(import_code, 0, msg=import_output)
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                self._supported_manifest_entries(base_url, metadata),
            )
            code, output = self.run_cli(
                root,
                "binary",
                "download-all",
                "--manifest-file",
                str(manifest_path),
                "--confirm",
                "DOWNLOAD_ALL_BINARIES",
            )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        results = {item["adapter"]: item["result"] for item in payload["results"]}
        self.assertEqual(results["backhaul"], "imported")
        self.assertEqual(set(results), set(all_binary_adapters()))

    def test_binary_status_require_all_fails_when_any_required_binary_is_missing(self) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                self._supported_manifest_entries(base_url, metadata),
            )
            code, output = self.run_cli(
                self.base / "binary-status-missing",
                "binary",
                "status",
                "--require-all",
                "--manifest-file",
                str(manifest_path),
            )
        self.assertEqual(code, 1)
        payload = json.loads(output)
        self.assertTrue(payload["blockers"])
        self.assertIn("has not been imported", "\n".join(payload["blockers"]))

    def test_binary_status_require_all_fails_on_checksum_mismatch(self) -> None:
        fixture_root, metadata = self._provider_fixture()
        root = self.base / "binary-status-checksum"
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                self._supported_manifest_entries(base_url, metadata),
            )
            code, output = self.run_cli(
                root,
                "binary",
                "download-all",
                "--manifest-file",
                str(manifest_path),
                "--confirm",
                "DOWNLOAD_ALL_BINARIES",
            )
        self.assertEqual(code, 0, msg=output)
        backhaul_path = root / "work" / ".var" / "pilottunnel" / "bin" / binary_spec("backhaul").binary_name
        backhaul_path.write_text("tampered", encoding="utf-8")
        code, output = self.run_cli(
            root,
            "binary",
            "status",
            "--require-all",
            "--manifest-file",
            str(manifest_path),
        )
        self.assertEqual(code, 1)
        self.assertIn("Checksum mismatch for adapter 'backhaul'", output)

    def test_binary_status_require_all_fails_when_manifest_host_is_not_allowlisted(self) -> None:
        fixture_root, metadata = self._provider_fixture()
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                self._supported_manifest_entries(base_url, metadata),
            )
            code, output = self.run_cli(
                self.base / "binary-status-remote-host",
                "binary",
                "status",
                "--require-all",
                "--manifest-url",
                f"{base_url}/{manifest_path.name}",
            )
        self.assertEqual(code, 1)
        self.assertIn("--allow-provider-host", output)

    def test_binary_status_require_all_succeeds_only_when_all_required_binaries_are_verified(self) -> None:
        fixture_root, metadata = self._provider_fixture()
        root = self.base / "binary-status-complete"
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                self._supported_manifest_entries(base_url, metadata),
            )
            code, output = self.run_cli(
                root,
                "binary",
                "download-all",
                "--manifest-file",
                str(manifest_path),
                "--confirm",
                "DOWNLOAD_ALL_BINARIES",
            )
        self.assertEqual(code, 0, msg=output)
        code, output = self.run_cli(
            root,
            "binary",
            "status",
            "--require-all",
            "--manifest-file",
            str(manifest_path),
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(set(payload["verified_adapters"]), set(provider_required_adapters()))
        results = {item["adapter"]: item for item in payload["results"]}
        self.assertEqual(results["ssh_reverse"]["status"], "system_dependency")
        self.assertEqual(results["wstunnel"]["status"], "not_required_v0_1")
        self.assertEqual(results["udp2raw"]["status"], "not_required_v0_1")
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["service_started"])
        self.assertFalse(payload["firewall_touched"])
        self.assertFalse(payload["routes_touched"])

    @patch("pilottunnel.binaries.verify_binary", return_value={"run_version_result": {"ran": False}})
    def test_binary_download_run_version_only_after_import(self, mock_verify_binary) -> None:
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                [
                    self._manifest_entry(
                        adapter="backhaul",
                        url=f"{base_url}/{metadata['backhaul']['relative_path']}",
                        sha256=str(metadata["backhaul"]["sha256"]),
                        size_bytes=int(metadata["backhaul"]["size_bytes"]),
                    )
                ],
            )
            code, output = self.run_cli(
                self.base / "download-run-version",
                "binary",
                "download",
                "--adapter",
                "backhaul",
                "--manifest-file",
                str(manifest_path),
                "--confirm",
                "DOWNLOAD_BINARY",
                "--run-version",
            )
        self.assertEqual(code, 0, msg=output)
        self.assertTrue(mock_verify_binary.called)

    def test_bootstrap_plan_is_read_only(self) -> None:
        root = self.base / "bootstrap-plan"
        ports, listeners = self._profile_ports()
        try:
            code, output = self.run_cli(
                root,
                "bootstrap",
                "plan",
                "--role",
                "controller",
                "--create-profile",
                *self._controller_profile_args("smoke-l4-001", ports),
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--allow-incomplete-binaries-for-tests-only",
            )
        finally:
            for listener in listeners:
                listener.close()
        self.assertEqual(code, 0, msg=output)
        self.assertFalse((root / "config.json").exists())
        self.assertFalse((root / "staging").exists())

    def test_bootstrap_plan_fails_before_profile_work_when_binary_coverage_is_incomplete(self) -> None:
        root = self.base / "bootstrap-plan-missing-binaries"
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        with static_http_server(fixture_root) as base_url:
            manifest_path = self._write_manifest(
                fixture_root,
                self._supported_manifest_entries(base_url, metadata),
            )
            ports, listeners = self._profile_ports()
            try:
                code, output = self.run_cli(
                    root,
                    "bootstrap",
                    "plan",
                    "--role",
                    "controller",
                    "--create-profile",
                    *self._controller_profile_args("smoke-l4-001", ports),
                    "--adapter",
                    "backhaul",
                    "--transport",
                    "tcpmux",
                    "--manifest-file",
                    str(manifest_path),
                )
            finally:
                for listener in listeners:
                    listener.close()
        self.assertEqual(code, 1)
        self.assertIn("Binary readiness is incomplete", output)

    def test_bootstrap_plan_with_auto_ports_is_read_only(self) -> None:
        root = self.base / "bootstrap-plan-auto"
        code, output = self.run_cli(
            root,
            "bootstrap",
            "plan",
            "--role",
            "controller",
            "--create-profile",
            *self._controller_auto_profile_args("smoke-l4-001"),
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--allow-incomplete-binaries-for-tests-only",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["ports_mode"], "auto")
        self.assertEqual([item["name"] for item in payload["steps"]], [
            "verify_or_init_role",
            "download_all_binaries",
            "create_or_update_profile_if_controller",
            "stage_files_only",
            "export_or_import_bundle",
            "backup_create",
            "readiness_report",
        ])
        self.assertFalse((root / "config.json").exists())
        self.assertFalse((root / "staging").exists())

    def test_bootstrap_apply_with_auto_ports_allocates_unique_free_ports(self) -> None:
        root = self.base / "bootstrap-apply-auto"
        code, output = self.run_cli(
            root,
            "bootstrap",
            "apply",
            "--role",
            "controller",
            "--create-profile",
            *self._controller_auto_profile_args("smoke-l4-001"),
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--allow-incomplete-binaries-for-tests-only",
            "--confirm",
            "BOOTSTRAP_APPLY",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        selected_ports = payload["selected_ports"]
        self.assertEqual(len(set(selected_ports.values())), len(selected_ports))
        self.assertTrue(all(isinstance(value, int) and value > 1024 for value in selected_ports.values()))
        self.assertTrue(set(selected_ports.values()).isdisjoint(self._forbidden_port_candidates()))

    def test_bootstrap_apply_with_auto_ports_persists_profile(self) -> None:
        root = self.base / "bootstrap-apply-auto-persist"
        code, output = self.run_cli(
            root,
            "bootstrap",
            "apply",
            "--role",
            "controller",
            "--create-profile",
            *self._controller_auto_profile_args("smoke-l4-001"),
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--allow-incomplete-binaries-for-tests-only",
            "--confirm",
            "BOOTSTRAP_APPLY",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        config_data = json.loads((root / "config.json").read_text(encoding="utf-8"))
        profile = config_data["profiles"][0]
        self.assertEqual(profile["main_port"], payload["selected_ports"]["main_port"])
        self.assertEqual(profile["target_port"], payload["selected_ports"]["target_port"])
        self.assertEqual(profile["ports"]["control_port"], payload["selected_ports"]["control_port"])

    def test_bootstrap_command_outputs_safe_copy_paste_commands(self) -> None:
        code, output = self.run_cli(
            self.base / "bootstrap-command",
            "bootstrap",
            "command",
            "--profile",
            "smoke-l4-001",
            "--adapter",
            "backhaul",
            "--transport",
            "tcpmux",
            "--ports",
            "auto",
            "--manifest-url",
            "https://downloads.example.com/pilot/provider-manifest.json",
            "--allow-provider-host",
            "downloads.example.com",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertIn("--ports auto", payload["controller_prepare_command"])
        self.assertIn("--bundle-file", payload["worker_prepare_command"])
        self.assertIn("--allow-provider-host", payload["worker_prepare_command"])
        self.assertNotIn("BOOTSTRAP_APPLY BOOTSTRAP_APPLY", payload["controller_prepare_command"])
        self.assertNotIn("--provider-host", payload["controller_prepare_command"])
        self.assertNotIn("--bundle-input", payload["controller_prepare_command"])
        self.assertNotIn("--provider-host", payload["worker_prepare_command"])
        self.assertNotIn("--bundle-input", payload["worker_prepare_command"])

    def test_bootstrap_command_uses_placeholders_for_manual_ports(self) -> None:
        code, output = self.run_cli(
            self.base / "bootstrap-command-placeholders",
            "bootstrap",
            "command",
            "--profile",
            "smoke-l4-001",
            "--adapter",
            "rathole",
            "--transport",
            "tcp",
            "--manifest-url",
            "https://downloads.example.com/pilot/provider-manifest.json",
            "--allow-provider-host",
            "downloads.example.com",
        )
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertIn("<MAIN_PORT>", payload["controller_prepare_command"])
        self.assertNotRegex(payload["controller_prepare_command"], r"--main-port\s+\d+")
        self.assertIn("--allow-provider-host", payload["controller_prepare_command"])
        self.assertIn("--bundle-output", payload["controller_prepare_command"])
        self.assertIn("--bundle-file", payload["worker_prepare_command"])

    def test_bootstrap_apply_refuses_without_confirm(self) -> None:
        root = self.base / "bootstrap-no-confirm"
        ports, listeners = self._profile_ports()
        try:
            code, output = self.run_cli(
                root,
                "bootstrap",
                "apply",
                "--role",
                "controller",
                "--create-profile",
                *self._controller_profile_args("smoke-l4-001", ports),
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--allow-incomplete-binaries-for-tests-only",
            )
        finally:
            for listener in listeners:
                listener.close()
        self.assertEqual(code, 1)
        self.assertIn("BOOTSTRAP_APPLY", output)

    def test_controller_bootstrap_creates_profile_stages_only_and_exports_bundle(self) -> None:
        root = self.base / "bootstrap-controller"
        bundle_path = root / "bundle" / "smoke-l4-001-worker.json"
        ports, listeners = self._profile_ports()
        try:
            code, output = self.run_cli(
                root,
                "bootstrap",
                "apply",
                "--role",
                "controller",
                "--create-profile",
                *self._controller_profile_args("smoke-l4-001", ports),
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--bundle-output",
                str(bundle_path),
                "--allow-incomplete-binaries-for-tests-only",
                "--confirm",
                "BOOTSTRAP_APPLY",
            )
        finally:
            for listener in listeners:
                listener.close()
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["profile_created_or_updated"])
        self.assertTrue(payload["staged_switch"]["ok"])
        self.assertTrue(bundle_path.exists())
        self.assertFalse(payload["real_systemd_touched"])
        self.assertFalse(payload["service_started"])

    def test_worker_bootstrap_refuses_profile_create(self) -> None:
        root = self.base / "bootstrap-worker-block"
        ports, listeners = self._profile_ports()
        try:
            self.run_cli(root, "init", "--role", "worker")
            code, output = self.run_cli(
                root,
                "bootstrap",
                "apply",
                "--role",
                "worker",
                "--create-profile",
                *self._controller_profile_args("smoke-l4-001", ports),
                "--allow-incomplete-binaries-for-tests-only",
                "--confirm",
                "BOOTSTRAP_APPLY",
            )
        finally:
            for listener in listeners:
                listener.close()
        self.assertEqual(code, 1)
        self.assertIn("blocked", output)

    def test_worker_bootstrap_imports_bundle(self) -> None:
        controller_root = self.base / "controller"
        worker_root = self.base / "worker"
        bundle_path = self.base / "bundle.json"
        ports, listeners = self._profile_ports()
        try:
            self._init_and_create_profile(controller_root, "smoke-l4-001", ports)
            export_code, export_output = self.run_cli(
                controller_root,
                "bundle",
                "export-worker",
                "--profile",
                "smoke-l4-001",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--output",
                str(bundle_path),
            )
            self.assertEqual(export_code, 0, msg=export_output)
            code, output = self.run_cli(
                worker_root,
                "bootstrap",
                "apply",
                "--role",
                "worker",
                "--bundle-file",
                str(bundle_path),
                "--allow-incomplete-binaries-for-tests-only",
                "--confirm",
                "BOOTSTRAP_APPLY",
            )
        finally:
            for listener in listeners:
                listener.close()
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertEqual(payload["bundle_import"]["profile"], "smoke-l4-001")
        self.assertFalse(payload["real_systemd_touched"])

    def test_worker_bootstrap_requires_full_binary_coverage_before_bundle_import(self) -> None:
        controller_root = self.base / "controller-gated"
        worker_root = self.base / "worker-gated"
        bundle_path = self.base / "gated-bundle.json"
        fixture_root, metadata = self._provider_fixture(("backhaul",))
        ports, listeners = self._profile_ports()
        try:
            self._init_and_create_profile(controller_root, "smoke-l4-001", ports)
            export_code, export_output = self.run_cli(
                controller_root,
                "bundle",
                "export-worker",
                "--profile",
                "smoke-l4-001",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--output",
                str(bundle_path),
            )
            self.assertEqual(export_code, 0, msg=export_output)
            with static_http_server(fixture_root) as base_url:
                manifest_path = self._write_manifest(
                    fixture_root,
                    self._supported_manifest_entries(base_url, metadata),
                )
                code, output = self.run_cli(
                    worker_root,
                    "bootstrap",
                    "apply",
                    "--role",
                    "worker",
                    "--bundle-file",
                    str(bundle_path),
                    "--manifest-file",
                    str(manifest_path),
                    "--confirm",
                    "BOOTSTRAP_APPLY",
                )
        finally:
            for listener in listeners:
                listener.close()
        self.assertEqual(code, 1)
        self.assertIn("Binary readiness is incomplete", output)

    def test_worker_bootstrap_downloads_binaries_from_provider(self) -> None:
        controller_root = self.base / "controller-download"
        worker_root = self.base / "worker-download"
        bundle_path = self.base / "download-bundle.json"
        ports, listeners = self._profile_ports()
        fixture_root, metadata = self._provider_fixture()
        try:
            self._init_and_create_profile(controller_root, "smoke-l4-001", ports)
            export_code, export_output = self.run_cli(
                controller_root,
                "bundle",
                "export-worker",
                "--profile",
                "smoke-l4-001",
                "--adapter",
                "backhaul",
                "--transport",
                "tcpmux",
                "--output",
                str(bundle_path),
            )
            self.assertEqual(export_code, 0, msg=export_output)
            with static_http_server(fixture_root) as base_url:
                manifest_path = self._write_manifest(
                    fixture_root,
                    self._supported_manifest_entries(base_url, metadata),
                )
                code, output = self.run_cli(
                    worker_root,
                    "bootstrap",
                    "apply",
                    "--role",
                    "worker",
                "--bundle-file",
                str(bundle_path),
                "--manifest-file",
                str(manifest_path),
                "--confirm",
                "BOOTSTRAP_APPLY",
                )
        finally:
            for listener in listeners:
                listener.close()
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["binary_download_all"]["ok"])
        status_code, status_output = self.run_cli(worker_root, "binary", "status", "--adapter", "backhaul")
        self.assertEqual(status_code, 0, msg=status_output)
        self.assertEqual(json.loads(status_output)["install_status"], "imported")

    def test_bootstrap_creates_backup_and_includes_readiness(self) -> None:
        root = self.base / "bootstrap-backup"
        self.run_cli(root, "init", "--role", "controller")
        ports, listeners = self._profile_ports()
        try:
            code, output = self.run_cli(
                root,
                "bootstrap",
                "apply",
                "--role",
                "controller",
                "--create-profile",
                *self._controller_profile_args("smoke-l4-001", ports),
                "--allow-incomplete-binaries-for-tests-only",
                "--confirm",
                "BOOTSTRAP_APPLY",
            )
        finally:
            for listener in listeners:
                listener.close()
        self.assertEqual(code, 0, msg=output)
        payload = json.loads(output)
        self.assertTrue(payload["backup"]["ok"])
        self.assertIn("readiness", payload)

    def test_public_docs_use_placeholders_for_provider_workflow(self) -> None:
        public_docs = "\n".join(
            [
                Path("README.md").read_text(encoding="utf-8"),
                Path("docs/OPERATIONS.md").read_text(encoding="utf-8"),
            ]
        )
        for placeholder in (
            "<PROFILE>",
            "<ADAPTER>",
            "<TRANSPORT>",
            "<MAIN_PORT>",
            "<TARGET_HOST>",
            "<TARGET_PORT>",
            "<CONTROL_PORT>",
            "<SERVICE_PORT>",
            "<CHECK_PORT>",
            "<MANIFEST_URL>",
            "<PROVIDER_HOST>",
            "<SOURCE_DIR>",
            "<MANIFEST_FILE>",
            "<BINARY_REPO>",
            "<BINARY_RELEASE_TAG>",
            "<RELEASE_DIR>",
            "<BUNDLE_FILE>",
            "<BUNDLE_OUTPUT>",
        ):
            self.assertIn(placeholder, public_docs)
        self.assertIn("provider-manifest.json", public_docs)
        self.assertNotRegex(public_docs, r"--(?:main|target|control|service|check)-port\s+\d+")
        self.assertNotRegex(
            public_docs,
            r"--profile\s+[A-Za-z][A-Za-z0-9._-]*-\d+[A-Za-z0-9._-]*",
        )
