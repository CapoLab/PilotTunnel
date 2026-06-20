"""Controlled upstream binary source catalog and fetch workflow."""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import io
import json
import os
import posixpath
import socket
import stat
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .audit import write_audit_log
from .binaries import binary_spec, cache_layout, current_platform_id, provider_required_adapters, supported_platforms
from .binary_provider import generate_manifest, verify_manifest_file

GITHUB_API_HOST = "api.github.com"
GITHUB_RELEASE_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "github-releases.githubusercontent.com",
}
SOURCE_SUMMARY_FILENAME = "pilottunnel-source-summary.json"
DOWNLOAD_TIMEOUT_SECONDS = 30
DOWNLOAD_RETRY_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_SECONDS = 0.25

IGNORED_ARCHIVE_SUFFIXES = (
    ".md",
    ".txt",
    ".rst",
    ".html",
    ".htm",
    ".json",
    ".sha256",
    ".sha512",
    ".sig",
    ".asc",
    ".pem",
)
IGNORED_ARCHIVE_BASENAMES = {
    "license",
    "licenses",
    "copying",
    "copying.txt",
    "notice",
    "notices",
    "authors",
    "contributors",
    "readme",
    "changelog",
    "news",
    "checksums",
    "sha256sum",
    "sha256sums",
    "sha512sum",
    "sha512sums",
}


@dataclass(frozen=True)
class ArchiveCandidate:
    name: str
    payload: bytes
    executable_like: bool
    depth: int
    basename_length: int


@dataclass(frozen=True)
class UpstreamSource:
    adapter: str
    category: str
    upstream_type: str
    repo_slug: str
    binary_name: str
    supported_platforms: tuple[str, ...]
    asset_patterns: dict[str, tuple[str, ...]]
    archive_handling: dict[str, str]
    extracted_binary_path_patterns: dict[str, tuple[str, ...]]
    notes: str = ""


UPSTREAM_SOURCE_CATALOG: dict[str, UpstreamSource] = {
    "backhaul": UpstreamSource(
        adapter="backhaul",
        category="external_binary",
        upstream_type="github_release",
        repo_slug="Musixal/Backhaul",
        binary_name="backhaul",
        supported_platforms=("linux-amd64", "linux-arm64"),
        asset_patterns={
            "linux-amd64": ("backhaul_linux_amd64.tar.gz",),
            "linux-arm64": ("backhaul_linux_arm64.tar.gz",),
        },
        archive_handling={"linux-amd64": "tar.gz", "linux-arm64": "tar.gz"},
        extracted_binary_path_patterns={"linux-amd64": ("*/backhaul",), "linux-arm64": ("*/backhaul",)},
        notes="Official GitHub release tarballs for Layer 4 controller and worker use.",
    ),
    "rathole": UpstreamSource(
        adapter="rathole",
        category="external_binary",
        upstream_type="github_release",
        repo_slug="rathole-org/rathole",
        binary_name="rathole",
        supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
        asset_patterns={
            "linux-amd64": ("rathole-x86_64-unknown-linux-gnu.zip",),
            "linux-arm64": ("rathole-aarch64-unknown-linux-*.zip",),
            "windows-amd64": ("rathole-x86_64-pc-windows-msvc.zip",),
        },
        archive_handling={"linux-amd64": "zip", "linux-arm64": "zip", "windows-amd64": "zip"},
        extracted_binary_path_patterns={
            "linux-amd64": ("*/rathole",),
            "linux-arm64": ("*/rathole",),
            "windows-amd64": ("*/rathole.exe",),
        },
        notes="Official GitHub release zip bundles.",
    ),
    "frp": UpstreamSource(
        adapter="frp",
        category="external_binary",
        upstream_type="github_release",
        repo_slug="fatedier/frp",
        binary_name="frpc",
        supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
        asset_patterns={
            "linux-amd64": ("frp_*_linux_amd64.tar.gz",),
            "linux-arm64": ("frp_*_linux_arm64.tar.gz",),
            "windows-amd64": ("frp_*_windows_amd64.zip",),
        },
        archive_handling={"linux-amd64": "tar.gz", "linux-arm64": "tar.gz", "windows-amd64": "zip"},
        extracted_binary_path_patterns={
            "linux-amd64": ("*/frpc",),
            "linux-arm64": ("*/frpc",),
            "windows-amd64": ("*/frpc.exe",),
        },
        notes="Uses the client binary from the official frp release bundle.",
    ),
    "gost": UpstreamSource(
        adapter="gost",
        category="external_binary",
        upstream_type="github_release",
        repo_slug="go-gost/gost",
        binary_name="gost",
        supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
        asset_patterns={
            "linux-amd64": ("gost_*_linux_amd64.tar.gz",),
            "linux-arm64": ("gost_*_linux_arm64.tar.gz",),
            "windows-amd64": ("gost_*_windows_amd64.zip",),
        },
        archive_handling={"linux-amd64": "tar.gz", "linux-arm64": "tar.gz", "windows-amd64": "zip"},
        extracted_binary_path_patterns={
            "linux-amd64": ("*/gost",),
            "linux-arm64": ("*/gost",),
            "windows-amd64": ("*/gost.exe",),
        },
        notes="Selects the non-v3 amd64 asset on Windows and Linux.",
    ),
    "chisel": UpstreamSource(
        adapter="chisel",
        category="external_binary",
        upstream_type="github_release",
        repo_slug="jpillora/chisel",
        binary_name="chisel",
        supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
        asset_patterns={
            "linux-amd64": ("chisel_*_linux_amd64.gz",),
            "linux-arm64": ("chisel_*_linux_arm64.gz",),
            "windows-amd64": ("chisel_*_windows_amd64.zip",),
        },
        archive_handling={"linux-amd64": "gz", "linux-arm64": "gz", "windows-amd64": "zip"},
        extracted_binary_path_patterns={
            "linux-amd64": ("chisel",),
            "linux-arm64": ("chisel",),
            "windows-amd64": ("*/chisel.exe",),
        },
        notes="Linux releases are gzip-compressed raw binaries.",
    ),
    "realm": UpstreamSource(
        adapter="realm",
        category="external_binary",
        upstream_type="github_release",
        repo_slug="zhboner/realm",
        binary_name="realm",
        supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
        asset_patterns={
            "linux-amd64": ("realm-x86_64-unknown-linux-gnu.tar.gz",),
            "linux-arm64": ("realm-aarch64-unknown-linux-gnu.tar.gz",),
            "windows-amd64": ("realm-x86_64-pc-windows-msvc.tar.gz",),
        },
        archive_handling={"linux-amd64": "tar.gz", "linux-arm64": "tar.gz", "windows-amd64": "tar.gz"},
        extracted_binary_path_patterns={
            "linux-amd64": ("*/realm",),
            "linux-arm64": ("*/realm",),
            "windows-amd64": ("*/realm.exe",),
        },
        notes="Uses the standard release bundle, not the slim variant.",
    ),
    "bore": UpstreamSource(
        adapter="bore",
        category="external_binary",
        upstream_type="github_release",
        repo_slug="ekzhang/bore",
        binary_name="bore",
        supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
        asset_patterns={
            "linux-amd64": ("bore-v*-x86_64-unknown-linux-musl.tar.gz",),
            "linux-arm64": ("bore-v*-aarch64-unknown-linux-musl.tar.gz",),
            "windows-amd64": ("bore-v*-x86_64-pc-windows-msvc.zip",),
        },
        archive_handling={"linux-amd64": "tar.gz", "linux-arm64": "tar.gz", "windows-amd64": "zip"},
        extracted_binary_path_patterns={
            "linux-amd64": ("*/bore",),
            "linux-arm64": ("*/bore",),
            "windows-amd64": ("*/bore.exe",),
        },
        notes="Official GitHub release artifacts for the Layer 4 helper binary.",
    ),
    "ssh_reverse": UpstreamSource(
        adapter="ssh_reverse",
        category="system_dependency",
        upstream_type="system_dependency",
        repo_slug="",
        binary_name="ssh",
        supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
        asset_patterns={},
        archive_handling={},
        extracted_binary_path_patterns={},
        notes="Uses the host SSH client and is intentionally not downloaded.",
    ),
}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(newurl, code, "Redirects are handled explicitly", headers, fp)


def list_upstream_sources() -> dict[str, Any]:
    return {
        "ok": True,
        "sources": [_source_to_dict(source) for source in UPSTREAM_SOURCE_CATALOG.values()],
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def fetch_upstream_sources(
    *,
    source_dir: Path,
    platform_id: str | None,
    cache_root: Path,
    confirm: str | None,
    force: bool,
    dry_run: bool,
    adapter_filters: list[str] | None,
    version_filters: list[str] | None,
    audit_path: Path,
    expected_confirm: str = "FETCH_UPSTREAM_BINARIES",
) -> dict[str, Any]:
    resolved_source_dir = _validated_source_dir(source_dir, must_exist=False)
    resolved_source_dir.mkdir(parents=True, exist_ok=True)
    resolved_platform = _resolve_platform_id(platform_id)
    selected_adapters = _resolve_adapters(adapter_filters)
    requested_versions = _resolve_versions(version_filters or [], selected_adapters)
    _require_explicit_versions(selected_adapters, requested_versions)
    attempt = {
        "action": "binary-source-fetch",
        "source_dir": str(resolved_source_dir),
        "platform": resolved_platform,
        "adapters": selected_adapters,
        "force": force,
        "dry_run": dry_run,
        "confirm": confirm or "",
        "requested_versions": requested_versions,
    }
    if not dry_run and confirm != expected_confirm:
        payload = {
            "ok": False,
            "message": f"Refusing upstream binary fetch without --confirm {expected_confirm}",
            "results": [],
            "failed_adapters": [],
            "summary_file": str(resolved_source_dir / SOURCE_SUMMARY_FILENAME),
            "downloads_performed": False,
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            **attempt,
        }
        _audit("binary-source-fetch", "source-catalog", payload, audit_path)
        return payload

    results: list[dict[str, Any]] = []
    failed: list[str] = []
    warnings: list[str] = []
    downloads_performed = False
    for adapter in selected_adapters:
        source = upstream_source(adapter)
        if resolved_platform not in source.supported_platforms:
            results.append(
                {
                    "adapter": adapter,
                    "result": "skipped_unsupported_platform",
                    "platform": resolved_platform,
                    "binary_name": source.binary_name,
                    "category": source.category,
                }
            )
            continue
        if source.category == "system_dependency":
            results.append(
                {
                    "adapter": adapter,
                    "result": "skipped_system_dependency",
                    "platform": resolved_platform,
                    "binary_name": source.binary_name,
                    "category": source.category,
                    "message": source.notes,
                }
            )
            continue
        try:
            release = _load_release_metadata(source, requested_versions.get(adapter))
            asset = _select_asset(source, resolved_platform, release)
            destination = _source_destination(resolved_source_dir, adapter, resolved_platform, source.binary_name)
            if dry_run:
                result = {
                    "adapter": adapter,
                    "result": "planned_download",
                    "platform": resolved_platform,
                    "binary_name": source.binary_name,
                    "category": source.category,
                    "version": release["version"],
                    "asset_name": asset["name"],
                    "asset_url": asset["url"],
                    "archive_handling": source.archive_handling[resolved_platform],
                    "destination": str(destination),
                    "downloads_performed": False,
                }
            elif destination.exists() and not force:
                result = {
                    "adapter": adapter,
                    "result": "already_present",
                    "platform": resolved_platform,
                    "binary_name": source.binary_name,
                    "category": source.category,
                    "version": release["version"],
                    "asset_name": asset["name"],
                    "asset_url": asset["url"],
                    "archive_handling": source.archive_handling[resolved_platform],
                    "destination": str(destination),
                    "sha256": _sha256_file(destination),
                    "size_bytes": destination.stat().st_size,
                    "downloads_performed": False,
                }
            else:
                result = _download_and_store_source(
                    source=source,
                    platform_id=resolved_platform,
                    release=release,
                    asset=asset,
                    destination=destination,
                    cache_root=cache_root,
                )
                downloads_performed = True
            results.append(result)
        except Exception as exc:
            failed.append(adapter)
            results.append(
                {
                    "adapter": adapter,
                    "result": "failed",
                    "platform": resolved_platform,
                    "binary_name": source.binary_name,
                    "category": source.category,
                    "message": str(exc),
                }
            )

    summary_path = resolved_source_dir / SOURCE_SUMMARY_FILENAME
    payload = {
        "ok": not failed,
        "action": "binary-source-fetch",
        "source_dir": str(resolved_source_dir),
        "platform": resolved_platform,
        "adapters": selected_adapters,
        "requested_versions": requested_versions,
        "results": results,
        "failed_adapters": failed,
        "warnings": warnings,
        "summary_file": str(summary_path),
        "downloads_performed": downloads_performed,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        "dry_run": dry_run,
    }
    _write_summary(summary_path, payload)
    _audit("binary-source-fetch", "source-catalog", payload, audit_path)
    return payload


def prepare_provider_binaries(
    *,
    source_dir: Path,
    provider_name: str,
    base_url: str,
    platform_id: str | None,
    output_path: Path,
    cache_root: Path,
    confirm: str | None,
    version_filters: list[str] | None,
    audit_path: Path,
) -> dict[str, Any]:
    resolved_platform = _resolve_platform_id(platform_id)
    required_manifest_adapters = tuple(
        adapter
        for adapter in provider_required_adapters()
        if adapter in UPSTREAM_SOURCE_CATALOG
        and UPSTREAM_SOURCE_CATALOG[adapter].category == "external_binary"
        and resolved_platform in UPSTREAM_SOURCE_CATALOG[adapter].supported_platforms
    )
    fetch_payload = fetch_upstream_sources(
        source_dir=source_dir,
        platform_id=resolved_platform,
        cache_root=cache_root,
        confirm=confirm,
        force=False,
        dry_run=False,
        adapter_filters=list(provider_required_adapters()) + ["ssh_reverse"],
        version_filters=version_filters,
        audit_path=audit_path,
        expected_confirm="PREPARE_PROVIDER_BINARIES",
    )
    if not fetch_payload["ok"]:
        return {
            "ok": False,
            "action": "binary-provider-prepare",
            "message": fetch_payload.get("message", "Upstream fetch failed"),
            "fetch": fetch_payload,
            "downloads_performed": fetch_payload["downloads_performed"],
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
    generate_payload = generate_manifest(
        provider_name=provider_name,
        base_url=base_url,
        source_dir=source_dir,
        output_path=output_path,
    )
    verify_payload = verify_manifest_file(
        manifest_file=output_path,
        required_platforms=(resolved_platform,),
        required_adapters=required_manifest_adapters,
    )
    payload = {
        "ok": bool(fetch_payload["ok"] and generate_payload["ok"] and verify_payload["ok"]),
        "action": "binary-provider-prepare",
        "platform": resolved_platform,
        "source_dir": str(_validated_source_dir(source_dir, must_exist=True)),
        "manifest_output": str(output_path.resolve()),
        "fetch": fetch_payload,
        "manifest_generation": generate_payload,
        "manifest_verification": verify_payload,
        "downloads_performed": fetch_payload["downloads_performed"],
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }
    _audit("binary-provider-prepare", "source-catalog", payload, audit_path)
    return payload


def upstream_source(adapter: str) -> UpstreamSource:
    try:
        return UPSTREAM_SOURCE_CATALOG[adapter]
    except KeyError as exc:
        raise KeyError(f"Unknown upstream source adapter '{adapter}'") from exc


def _source_to_dict(source: UpstreamSource) -> dict[str, Any]:
    return asdict(source)


def _resolve_platform_id(platform_id: str | None) -> str:
    if platform_id in {None, "", "auto", "default"}:
        return current_platform_id()
    if platform_id not in supported_platforms():
        raise ValueError(f"Unsupported platform '{platform_id}'")
    return platform_id


def _resolve_adapters(values: list[str] | None) -> list[str]:
    if not values:
        return list(UPSTREAM_SOURCE_CATALOG)
    adapters: list[str] = []
    for value in values:
        for item in value.split(","):
            candidate = item.strip()
            if not candidate:
                continue
            upstream_source(candidate)
            if candidate not in adapters:
                adapters.append(candidate)
    if not adapters:
        raise ValueError("At least one adapter must be selected when using --adapter")
    return adapters


def _resolve_versions(values: list[str], adapters: list[str]) -> dict[str, str]:
    versions: dict[str, str] = {}
    tokens = [token.strip() for value in values for token in value.split(",") if token.strip()]
    if not tokens:
        return versions
    multi_target = [adapter for adapter in adapters if upstream_source(adapter).category == "external_binary"]
    for token in tokens:
        if "=" in token:
            adapter, version = token.split("=", 1)
            adapter_name = adapter.strip()
            version_name = version.strip()
            if not version_name:
                raise ValueError("Version override cannot be empty")
            if adapter_name not in adapters:
                raise ValueError(f"Version override references unselected adapter '{adapter_name}'")
            if upstream_source(adapter_name).category != "external_binary":
                raise ValueError(f"Version override is not supported for system dependency adapter '{adapter_name}'")
            versions[adapter_name] = version_name
            continue
        if len(multi_target) != 1:
            raise ValueError("Use --version adapter=tag when fetching multiple external adapters")
        versions[multi_target[0]] = token
    return versions


def _require_explicit_versions(adapters: list[str], versions: dict[str, str]) -> None:
    missing = [
        adapter
        for adapter in adapters
        if upstream_source(adapter).category == "external_binary" and not versions.get(adapter)
    ]
    if missing:
        raise ValueError(
            "Upstream source fetch requires an explicit version/tag; dynamic latest releases are not allowed"
        )


def _load_release_metadata(source: UpstreamSource, version: str | None) -> dict[str, Any]:
    if source.upstream_type != "github_release":
        raise ValueError(f"Adapter '{source.adapter}' does not support upstream release downloads")
    if not version or not version.strip():
        raise ValueError("Upstream source fetch requires an explicit version/tag; dynamic latest releases are not allowed")
    endpoint = f"releases/tags/{urllib.parse.quote(version.strip())}"
    url = f"https://{GITHUB_API_HOST}/repos/{source.repo_slug}/{endpoint}"
    payload = _read_json_url(url, allowed_hosts={GITHUB_API_HOST})
    if not isinstance(payload, dict):
        raise ValueError(f"Release metadata for adapter '{source.adapter}' must be a JSON object")
    tag_name = payload.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name.strip():
        raise ValueError(f"Release metadata for adapter '{source.adapter}' is missing tag_name")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ValueError(f"Release metadata for adapter '{source.adapter}' is missing assets")
    normalized_assets: list[dict[str, str]] = []
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        url_value = asset.get("browser_download_url")
        if not isinstance(name, str) or not name.strip() or not isinstance(url_value, str) or not url_value.strip():
            continue
        _validate_release_asset_url(source, url_value)
        normalized_assets.append({"name": name.strip(), "url": url_value.strip()})
    return {"version": tag_name.strip(), "assets": normalized_assets}


def _select_asset(source: UpstreamSource, platform_id: str, release: dict[str, Any]) -> dict[str, str]:
    patterns = source.asset_patterns.get(platform_id)
    if not patterns:
        raise ValueError(f"Adapter '{source.adapter}' does not define an asset pattern for platform '{platform_id}'")
    assets = release["assets"]
    for pattern in patterns:
        matches = [asset for asset in assets if fnmatch.fnmatch(asset["name"], pattern)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError(f"Asset pattern '{pattern}' is ambiguous for adapter '{source.adapter}' on platform '{platform_id}'")
    raise ValueError(f"No upstream release asset matched adapter '{source.adapter}' on platform '{platform_id}'")


def _download_and_store_source(
    *,
    source: UpstreamSource,
    platform_id: str,
    release: dict[str, Any],
    asset: dict[str, str],
    destination: Path,
    cache_root: Path,
) -> dict[str, Any]:
    cache_path = _download_temp_path(cache_root, source.adapter, platform_id, asset["name"])
    raw_bytes = _download_asset_bytes(asset["url"], allowed_hosts=GITHUB_RELEASE_HOSTS)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(raw_bytes)
    try:
        binary_bytes = _extract_binary_bytes(
            source=source,
            platform_id=platform_id,
            asset_name=asset["name"],
            archive_type=source.archive_handling[platform_id],
            payload=raw_bytes,
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(binary_bytes)
        if not platform_id.startswith("windows"):
            destination.chmod(destination.stat().st_mode | 0o755)
    finally:
        cache_path.unlink(missing_ok=True)
    return {
        "adapter": source.adapter,
        "result": "downloaded",
        "platform": platform_id,
        "binary_name": source.binary_name,
        "category": source.category,
        "version": release["version"],
        "asset_name": asset["name"],
        "asset_url": asset["url"],
        "archive_handling": source.archive_handling[platform_id],
        "destination": str(destination),
        "sha256": _sha256_bytes(binary_bytes),
        "size_bytes": len(binary_bytes),
        "downloads_performed": True,
    }


def _extract_binary_bytes(
    *,
    source: UpstreamSource,
    platform_id: str,
    asset_name: str,
    archive_type: str,
    payload: bytes,
) -> bytes:
    patterns = source.extracted_binary_path_patterns.get(platform_id, ())
    expected_names = _expected_binary_names(source.binary_name, platform_id)
    if archive_type == "raw":
        return payload
    if archive_type == "gz":
        return gzip.decompress(payload)
    if archive_type == "tar.gz":
        return _extract_from_tar_gz(payload, patterns, source.adapter, asset_name, expected_names)
    if archive_type == "zip":
        return _extract_from_zip(payload, patterns, source.adapter, asset_name, expected_names)
    raise ValueError(f"Unsupported archive handling '{archive_type}' for adapter '{source.adapter}'")


def _extract_from_tar_gz(
    payload: bytes,
    patterns: tuple[str, ...],
    adapter: str,
    asset_name: str,
    expected_names: tuple[str, ...],
) -> bytes:
    candidates: list[ArchiveCandidate] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            normalized = _safe_archive_name(member.name)
            if member.issym() or member.islnk():
                raise ValueError(f"Archive symlink entries are not allowed for adapter '{adapter}'")
            if not member.isfile():
                continue
            if _should_ignore_archive_member(normalized):
                continue
            if not _matches_member(normalized, patterns, expected_names):
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                continue
            candidates.append(_archive_candidate(normalized, extracted.read(), _tar_member_is_executable(member)))
    return _select_archive_candidate(candidates, adapter, asset_name)


def _extract_from_zip(
    payload: bytes,
    patterns: tuple[str, ...],
    adapter: str,
    asset_name: str,
    expected_names: tuple[str, ...],
) -> bytes:
    candidates: list[ArchiveCandidate] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.infolist():
            normalized = _safe_archive_name(member.filename)
            if _zip_member_is_symlink(member):
                raise ValueError(f"Archive symlink entries are not allowed for adapter '{adapter}'")
            if member.is_dir():
                continue
            if _should_ignore_archive_member(normalized):
                continue
            if not _matches_member(normalized, patterns, expected_names):
                continue
            candidates.append(_archive_candidate(normalized, archive.read(member), _zip_member_is_executable(member)))
    return _select_archive_candidate(candidates, adapter, asset_name)


def _matches_member(name: str, patterns: tuple[str, ...], expected_names: tuple[str, ...]) -> bool:
    basename = PurePosixPath(name).name.lower()
    if basename not in expected_names:
        return False
    for pattern in patterns:
        pattern_name = PurePosixPath(pattern).name
        if (
            fnmatch.fnmatch(name, pattern)
            or fnmatch.fnmatch(basename, pattern.lower())
            or fnmatch.fnmatch(basename, pattern_name.lower())
        ):
            return True
    return False


def _expected_binary_names(binary_name: str, platform_id: str) -> tuple[str, ...]:
    names = {binary_name.lower()}
    if platform_id.startswith("windows") and not binary_name.lower().endswith(".exe"):
        names.add(f"{binary_name.lower()}.exe")
    return tuple(sorted(names))


def _should_ignore_archive_member(name: str) -> bool:
    basename = PurePosixPath(name).name.lower()
    if not basename:
        return True
    stem = PurePosixPath(basename).stem.lower()
    if basename in IGNORED_ARCHIVE_BASENAMES or stem in IGNORED_ARCHIVE_BASENAMES:
        return True
    return basename.endswith(IGNORED_ARCHIVE_SUFFIXES)


def _archive_candidate(name: str, payload: bytes, executable_like: bool) -> ArchiveCandidate:
    path = PurePosixPath(name)
    return ArchiveCandidate(
        name=name,
        payload=payload,
        executable_like=executable_like,
        depth=len(path.parts),
        basename_length=len(path.name),
    )


def _select_archive_candidate(candidates: list[ArchiveCandidate], adapter: str, asset_name: str) -> bytes:
    if not candidates:
        raise ValueError(f"Expected exactly one binary payload in '{asset_name}' for adapter '{adapter}'")
    ordered = sorted(
        candidates,
        key=lambda item: (
            0 if item.executable_like else 1,
            item.depth,
            len(item.name),
            item.name,
        ),
    )
    best = ordered[0]
    tied = [
        item
        for item in ordered
        if (
            item.executable_like == best.executable_like
            and item.depth == best.depth
            and len(item.name) == len(best.name)
        )
    ]
    if len(tied) > 1:
        names = ", ".join(item.name for item in tied[:3])
        raise ValueError(
            f"Ambiguous binary payloads in '{asset_name}' for adapter '{adapter}': {names}"
        )
    return best.payload


def _safe_archive_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized.startswith("/") or normalized.startswith("\\"):
        raise ValueError("Archive path traversal blocked")
    parts = [part for part in normalized.split("/") if part]
    if any(part == ".." for part in parts):
        raise ValueError("Archive path traversal blocked")
    if parts and ":" in parts[0]:
        raise ValueError("Archive path traversal blocked")
    return posixpath.join(*parts)


def _zip_member_is_symlink(member: zipfile.ZipInfo) -> bool:
    mode = member.external_attr >> 16
    return stat.S_ISLNK(mode)


def _tar_member_is_executable(member: tarfile.TarInfo) -> bool:
    return bool(member.mode & 0o111)


def _zip_member_is_executable(member: zipfile.ZipInfo) -> bool:
    mode = member.external_attr >> 16
    return bool(mode & 0o111)


def _validate_release_asset_url(source: UpstreamSource, url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Upstream asset URL for adapter '{source.adapter}' must use HTTPS")
    if parsed.hostname != "github.com":
        raise ValueError(f"Upstream asset URL for adapter '{source.adapter}' must originate from github.com")
    expected_prefix = f"/{source.repo_slug}/releases/download/"
    if not parsed.path.startswith(expected_prefix):
        raise ValueError(f"Upstream asset URL for adapter '{source.adapter}' does not match repo '{source.repo_slug}'")
    if ".." in parsed.path.split("/"):
        raise ValueError("Path traversal blocked in upstream asset URL")


def _download_temp_path(cache_root: Path, adapter: str, platform_id: str, asset_name: str) -> Path:
    layout = cache_layout(cache_root)
    download_dir = (layout["downloads_dir"] / "upstream").resolve()
    temp_path = (download_dir / f"{adapter}-{platform_id}-{asset_name}.tmp").resolve()
    if layout["root"] not in temp_path.parents:
        raise ValueError(f"Refusing to write outside cache root: {temp_path}")
    return temp_path


def _source_destination(source_dir: Path, adapter: str, platform_id: str, binary_name: str) -> Path:
    destination = (source_dir / adapter / platform_id / binary_name).resolve()
    if source_dir not in destination.parents:
        raise ValueError(f"Refusing to write outside source dir: {destination}")
    return destination


def _validated_source_dir(source_dir: Path, *, must_exist: bool) -> Path:
    if ".." in source_dir.parts:
        raise ValueError(f"Path traversal blocked for source dir: {source_dir!r}")
    resolved = source_dir.resolve()
    if must_exist and not resolved.exists():
        raise ValueError(f"Source directory does not exist: {source_dir}")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"Source directory must be a directory: {source_dir}")
    return resolved


def _read_json_url(url: str, *, allowed_hosts: set[str]) -> Any:
    return json.loads(_download_url_bytes(url, allowed_hosts=allowed_hosts).decode("utf-8"))


def _download_asset_bytes(url: str, *, allowed_hosts: set[str]) -> bytes:
    return _download_url_bytes(url, allowed_hosts=allowed_hosts)


def _download_url_bytes(url: str, *, allowed_hosts: set[str], max_redirects: int = 3) -> bytes:
    current_url = url
    request_headers = {
        "Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.8",
        "User-Agent": "PilotTunnel-UpstreamSource/1.0",
    }
    opener = urllib.request.build_opener(_NoRedirectHandler)
    for _ in range(max_redirects + 1):
        _validate_download_url(current_url, allowed_hosts)
        request = urllib.request.Request(current_url, headers=request_headers, method="GET")
        for attempt in range(DOWNLOAD_RETRY_ATTEMPTS):
            try:
                with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                if exc.code not in {301, 302, 303, 307, 308}:
                    raise
                location = exc.headers.get("Location", "").strip()
                if not location:
                    raise ValueError("Redirect response did not include a Location header") from exc
                next_url = urllib.parse.urljoin(current_url, location)
                _validate_download_url(next_url, allowed_hosts)
                current_url = next_url
                break
            except Exception as exc:
                if attempt >= DOWNLOAD_RETRY_ATTEMPTS - 1 or not _is_retryable_download_error(exc):
                    raise
                time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
        else:
            continue
        continue
    raise ValueError(f"Too many redirects while downloading '{url}'")


def _is_retryable_download_error(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return True
        if isinstance(reason, str):
            return "timed out" in reason.lower() or "timeout" in reason.lower()
        return "timed out" in str(reason).lower() or "timeout" in str(reason).lower()
    return "timed out" in str(exc).lower() or "timeout" in str(exc).lower()


def _validate_download_url(url: str, allowed_hosts: set[str]) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"Download URL must use HTTPS: {url}")
    host = (parsed.hostname or "").lower()
    if host not in {item.lower() for item in allowed_hosts}:
        raise ValueError(f"Download URL host '{host}' is not in the allowed host set")
    if not parsed.hostname:
        raise ValueError("Download URL host is required")
    if ".." in parsed.path.split("/"):
        raise ValueError("Path traversal blocked in download URL")


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _audit(action: str, profile: str, details: dict[str, Any], path: Path) -> None:
    write_audit_log(action, profile, details, path)


def _sha256_bytes(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
