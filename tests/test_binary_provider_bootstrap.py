import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pilottunnel import cli
from pilottunnel.binaries import all_binary_adapters, binary_spec, current_platform_id, provider_required_adapters
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
    ) -> dict[str, object]:
        spec = binary_spec(adapter)
        return {
            "adapter": adapter,
            "binary_name": binary_name or spec.binary_name,
            "version": "v0.0.1",
            "platform": platform_id or current_platform_id(),
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
            )
        finally:
            for listener in listeners:
                listener.close()
        self.assertEqual(code, 0, msg=output)
        self.assertFalse((root / "config.json").exists())
        self.assertFalse((root / "staging").exists())

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
                "--bundle-input",
                str(bundle_path),
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
                    "--bundle-input",
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

    def test_readme_uses_placeholders_for_provider_workflow(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        for placeholder in (
            "<PROFILE>",
            "<MAIN_PORT>",
            "<TARGET_HOST>",
            "<TARGET_PORT>",
            "<CONTROL_PORT>",
            "<SERVICE_PORT>",
            "<CHECK_PORT>",
            "<MANIFEST_URL>",
            "<PROVIDER_HOST>",
        ):
            self.assertIn(placeholder, readme)
        for disallowed in ("38080", "39080", "39081", "39082", "39083", "turkey-6221"):
            self.assertNotIn(disallowed, readme)
