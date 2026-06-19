"""Managed binary install and resolution workflow."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .audit import write_audit_log
from .binaries import binary_spec, provider_required_adapters
from .binary_provider import LOCAL_HOSTS, ProviderBinary, load_manifest, resolve_platform_id, select_manifest_binary
from .config import AppConfig
from .state import AppState

INSTALL_SUMMARY_FILENAME = "pilottunnel-binary-install-summary.json"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(newurl, code, "Redirects are handled explicitly", headers, fp)


def build_binary_install_plan(
    *,
    manifest_file: Path,
    requested_platform: str | None,
    install_dir: Path | None,
    config: AppConfig,
    state: AppState,
) -> dict[str, Any]:
    manifest = load_manifest(
        manifest_file=manifest_file,
        allow_provider_host=None,
        require_allowlisted_remote_host=False,
    )
    platform_id = resolve_platform_id(requested_platform)
    managed_install_dir = _configured_install_dir(config, install_dir)
    results: list[dict[str, Any]] = []
    blockers: list[str] = []
    for adapter in provider_required_adapters():
        spec = binary_spec(adapter)
        if platform_id not in spec.supported_platforms:
            results.append(
                {
                    "adapter": adapter,
                    "platform": platform_id,
                    "result": "skipped_unsupported_platform",
                    "binary_name": _managed_binary_name(adapter, platform_id),
                }
            )
            continue
        try:
            entry = select_manifest_binary(manifest, adapter=adapter, platform_id=platform_id)
        except ValueError as exc:
            blockers.append(str(exc))
            results.append(
                {
                    "adapter": adapter,
                    "platform": platform_id,
                    "result": "missing_from_manifest",
                    "message": str(exc),
                }
            )
            continue
        results.append(
            _plan_entry(
                adapter=adapter,
                platform_id=platform_id,
                entry=entry,
                install_dir=managed_install_dir,
                config=config,
                state=state,
            )
        )
    return {
        "ok": not blockers,
        "action": "binary-install-plan",
        "platform": platform_id,
        "manifest_file": str(_validated_manifest_path(manifest_file)),
        "manifest_provider": manifest.provider,
        "configured_install_dir": str(managed_install_dir) if managed_install_dir else "",
        "install_dir_configured": managed_install_dir is not None,
        "allow_system_path": config.binary_resolution.allow_system_path,
        "prefer_managed_install": config.binary_resolution.prefer_managed_install,
        "results": results,
        "blockers": blockers,
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        "plan_only": True,
    }


def apply_binary_install(
    *,
    manifest_file: Path,
    requested_platform: str | None,
    install_dir: Path,
    config: AppConfig,
    state: AppState,
    confirm: str | None,
    audit_path: Path,
) -> dict[str, Any]:
    resolved_install_dir = _validated_install_dir(install_dir)
    platform_id = resolve_platform_id(requested_platform)
    attempt = {
        "manifest_file": str(_validated_manifest_path(manifest_file)),
        "platform": platform_id,
        "install_dir": str(resolved_install_dir),
        "confirm": confirm or "",
    }
    if confirm != "INSTALL_PROVIDER_BINARIES":
        payload = {
            "ok": False,
            "action": "binary-install-apply",
            "message": "Refusing managed binary install without --confirm INSTALL_PROVIDER_BINARIES",
            "results": [],
            "summary_file": str(resolved_install_dir / INSTALL_SUMMARY_FILENAME),
            "downloads_performed": False,
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            **attempt,
        }
        _audit("binary-install-apply", "binary-install", payload, audit_path)
        return payload

    manifest = load_manifest(
        manifest_file=manifest_file,
        allow_provider_host=None,
        require_allowlisted_remote_host=False,
    )
    resolved_install_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    downloads_performed = False
    for adapter in provider_required_adapters():
        spec = binary_spec(adapter)
        if platform_id not in spec.supported_platforms:
            results.append(
                {
                    "adapter": adapter,
                    "platform": platform_id,
                    "result": "skipped_unsupported_platform",
                    "binary_name": _managed_binary_name(adapter, platform_id),
                }
            )
            continue
        try:
            entry = select_manifest_binary(manifest, adapter=adapter, platform_id=platform_id)
            result = _install_entry(
                adapter=adapter,
                platform_id=platform_id,
                entry=entry,
                install_dir=resolved_install_dir,
                config=config,
                state=state,
            )
            downloads_performed = downloads_performed or result.get("source") == "provider_download"
            results.append(result)
        except Exception as exc:
            failures.append(adapter)
            results.append(
                {
                    "adapter": adapter,
                    "platform": platform_id,
                    "result": "failed",
                    "message": str(exc),
                }
            )

    summary_path = resolved_install_dir / INSTALL_SUMMARY_FILENAME
    payload = {
        "ok": not failures,
        "action": "binary-install-apply",
        "platform": platform_id,
        "manifest_file": str(_validated_manifest_path(manifest_file)),
        "manifest_provider": manifest.provider,
        "install_dir": str(resolved_install_dir),
        "results": results,
        "failed_adapters": failures,
        "summary_file": str(summary_path),
        "downloads_performed": downloads_performed,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }
    _write_summary(summary_path, payload)
    if payload["ok"]:
        config.binary_resolution.managed_install_dir = str(resolved_install_dir)
        config.binary_resolution.provider_manifest = str(_validated_manifest_path(manifest_file))
    _audit("binary-install-apply", "binary-install", payload, audit_path)
    return payload


def list_binary_installations(*, install_dir: Path) -> dict[str, Any]:
    resolved_install_dir = _validated_install_dir(install_dir, must_exist=True)
    summary_path = resolved_install_dir / INSTALL_SUMMARY_FILENAME
    summary_payload = _read_summary(summary_path)
    summary_index = {
        (item.get("adapter"), item.get("platform")): item
        for item in summary_payload.get("results", [])
        if isinstance(item, dict)
    }
    entries: list[dict[str, Any]] = []
    for adapter_dir in sorted(resolved_install_dir.iterdir()):
        if not adapter_dir.is_dir():
            continue
        adapter = adapter_dir.name
        if adapter not in provider_required_adapters():
            continue
        for platform_dir in sorted(adapter_dir.iterdir()):
            if not platform_dir.is_dir():
                continue
            platform_id = platform_dir.name
            path = _binary_destination(resolved_install_dir, adapter, platform_id)
            if not path.exists():
                continue
            summary_item = summary_index.get((adapter, platform_id), {})
            entries.append(
                {
                    "adapter": adapter,
                    "platform": platform_id,
                    "path": str(path),
                    "size_bytes": path.stat().st_size,
                    "sha256": _sha256_file(path),
                    "source": summary_item.get("source", ""),
                    "version": summary_item.get("version", ""),
                    "exists": True,
                }
            )
    return {
        "ok": True,
        "action": "binary-install-list",
        "install_dir": str(resolved_install_dir),
        "summary_file": str(summary_path),
        "entries": entries,
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def resolve_binary_reference(
    *,
    adapter: str,
    config: AppConfig,
    state: AppState,
    requested_platform: str | None = None,
    path_lookup: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    spec = binary_spec(adapter)
    platform_id = resolve_platform_id(requested_platform)
    if platform_id not in spec.supported_platforms:
        raise ValueError(f"Unsupported platform '{platform_id}' for adapter '{adapter}'")
    lookup = path_lookup or shutil.which
    managed_install_dir = _configured_install_dir(config, None)
    candidates: list[dict[str, Any]] = []
    managed_candidate = _managed_candidate(adapter, platform_id, managed_install_dir)
    if managed_candidate:
        candidates.append(managed_candidate)
    system_candidate = _system_path_candidate(adapter, platform_id, lookup) if config.binary_resolution.allow_system_path else None
    if system_candidate:
        if config.binary_resolution.prefer_managed_install:
            candidates.append(system_candidate)
        else:
            candidates.insert(0, system_candidate)
    cache_candidate = _local_cache_candidate(adapter, platform_id, state)
    if cache_candidate:
        candidates.append(cache_candidate)
    for candidate in candidates:
        if candidate["exists"]:
            return {
                "ok": True,
                "adapter": adapter,
                "platform": platform_id,
                "resolved": True,
                "source": candidate["source"],
                "path": candidate["path"],
                "provider_manifest": config.binary_resolution.provider_manifest,
                "managed_install_dir": config.binary_resolution.managed_install_dir,
            }
    provider_available = False
    provider_message = ""
    if config.binary_resolution.provider_manifest:
        provider_available = _manifest_has_entry(config.binary_resolution.provider_manifest, adapter, platform_id)
        if provider_available:
            provider_message = "Provider manifest contains a managed binary entry"
    return {
        "ok": False,
        "adapter": adapter,
        "platform": platform_id,
        "resolved": False,
        "source": "",
        "path": "",
        "provider_manifest": config.binary_resolution.provider_manifest,
        "managed_install_dir": config.binary_resolution.managed_install_dir,
        "provider_available": provider_available,
        "message": provider_message or f"No binary source is available for adapter '{adapter}' on platform '{platform_id}'",
    }


def _plan_entry(
    *,
    adapter: str,
    platform_id: str,
    entry: ProviderBinary,
    install_dir: Path | None,
    config: AppConfig,
    state: AppState,
) -> dict[str, Any]:
    destination = _binary_destination(install_dir, adapter, platform_id) if install_dir else None
    destination_status = _installed_status(destination, entry.sha256) if destination else {"exists": False, "checksum_match": False}
    sources = _source_candidates(adapter=adapter, platform_id=platform_id, entry=entry, config=config, state=state)
    selected_source = _select_install_source(sources)
    result = "already_installed" if destination_status["checksum_match"] else "install_dir_required"
    if install_dir and selected_source:
        result = "replace" if destination_status["exists"] else "install"
    if install_dir and destination_status["checksum_match"]:
        result = "already_installed"
    return {
        "adapter": adapter,
        "platform": platform_id,
        "version": entry.version,
        "binary_name": _managed_binary_name(adapter, platform_id),
        "manifest_url": entry.url,
        "manifest_sha256": entry.sha256,
        "manifest_size_bytes": entry.size_bytes,
        "destination": str(destination) if destination else "",
        "destination_exists": destination_status["exists"],
        "destination_checksum_match": destination_status["checksum_match"],
        "selected_source": selected_source["source"] if selected_source else "",
        "selected_source_path": selected_source.get("path", "") if selected_source else "",
        "selected_source_url": selected_source.get("url", "") if selected_source else "",
        "available_sources": sources,
        "result": result,
    }


def _install_entry(
    *,
    adapter: str,
    platform_id: str,
    entry: ProviderBinary,
    install_dir: Path,
    config: AppConfig,
    state: AppState,
) -> dict[str, Any]:
    destination = _binary_destination(install_dir, adapter, platform_id)
    install_dir.mkdir(parents=True, exist_ok=True)
    _validate_destination_path(destination, install_dir)
    destination_status = _installed_status(destination, entry.sha256)
    if destination_status["checksum_match"]:
        return {
            "adapter": adapter,
            "platform": platform_id,
            "result": "already_installed",
            "source": "managed_install",
            "destination": str(destination),
            "sha256": entry.sha256,
            "version": entry.version,
            "size_bytes": destination.stat().st_size,
        }

    sources = _source_candidates(adapter=adapter, platform_id=platform_id, entry=entry, config=config, state=state)
    selected_source = _select_install_source(sources)
    if selected_source is None:
        raise ValueError(f"No verified binary source is available for adapter '{adapter}' on platform '{platform_id}'")
    if selected_source["source"] == "provider_download":
        payload = _download_provider_binary(entry)
        binary_bytes = payload["bytes"]
        size_bytes = payload["size_bytes"]
        sha256_value = payload["sha256"]
    else:
        source_path = Path(selected_source["path"])
        _validate_existing_source(source_path)
        binary_bytes = source_path.read_bytes()
        sha256_value = _sha256_bytes(binary_bytes)
        if sha256_value.lower() != entry.sha256.lower():
            raise ValueError(f"Checksum verification failed for adapter '{adapter}'")
        size_bytes = len(binary_bytes)
    _atomic_write(destination, binary_bytes, executable=not platform_id.startswith("windows"))
    return {
        "adapter": adapter,
        "platform": platform_id,
        "result": "installed",
        "source": selected_source["source"],
        "destination": str(destination),
        "sha256": sha256_value,
        "version": entry.version,
        "size_bytes": size_bytes,
        "replaced_existing": destination_status["exists"],
    }


def _source_candidates(
    *,
    adapter: str,
    platform_id: str,
    entry: ProviderBinary,
    config: AppConfig,
    state: AppState,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if config.binary_resolution.allow_system_path:
        system_candidate = _system_path_candidate(adapter, platform_id, shutil.which)
        if system_candidate:
            system_candidate["checksum_match"] = _candidate_checksum_match(system_candidate["path"], entry.sha256)
            candidates.append(system_candidate)
    cache_candidate = _local_cache_candidate(adapter, platform_id, state)
    if cache_candidate:
        cache_candidate["checksum_match"] = _candidate_checksum_match(cache_candidate["path"], entry.sha256)
        candidates.append(cache_candidate)
    candidates.append(
        {
            "source": "provider_download",
            "path": "",
            "url": entry.url,
            "exists": True,
            "checksum_match": True,
        }
    )
    return candidates


def _select_install_source(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    for source_name in ("local_cache", "system_path", "provider_download"):
        for candidate in candidates:
            if candidate["source"] != source_name:
                continue
            if source_name == "provider_download":
                return candidate
            if candidate["exists"] and candidate["checksum_match"]:
                return candidate
    return None


def _managed_candidate(adapter: str, platform_id: str, install_dir: Path | None) -> dict[str, Any] | None:
    if install_dir is None:
        return None
    path = _binary_destination(install_dir, adapter, platform_id)
    return {"source": "managed_install", "path": str(path), "exists": path.exists()}


def _local_cache_candidate(adapter: str, platform_id: str, state: AppState) -> dict[str, Any] | None:
    record = state.binaries.get(adapter)
    if record is None:
        return None
    if record.platform != platform_id:
        return None
    return {"source": "local_cache", "path": record.imported_path, "exists": Path(record.imported_path).exists()}


def _system_path_candidate(
    adapter: str,
    platform_id: str,
    path_lookup: Callable[[str], str | None],
) -> dict[str, Any] | None:
    for name in _path_lookup_names(adapter, platform_id):
        resolved = path_lookup(name)
        if resolved:
            return {"source": "system_path", "path": resolved, "exists": True}
    return None


def _path_lookup_names(adapter: str, platform_id: str) -> tuple[str, ...]:
    spec = binary_spec(adapter)
    names = [spec.binary_name]
    managed_name = _managed_binary_name(adapter, platform_id)
    if managed_name not in names:
        names.append(managed_name)
    return tuple(names)


def _candidate_checksum_match(path_value: str, expected_sha256: str) -> bool:
    path = Path(path_value)
    if not path.exists():
        return False
    _validate_existing_source(path)
    return _sha256_file(path).lower() == expected_sha256.lower()


def _download_provider_binary(entry: ProviderBinary) -> dict[str, Any]:
    payload = _download_url_bytes(entry.url, expected_host=(urllib.parse.urlparse(entry.url).hostname or "").lower())
    actual_sha = _sha256_bytes(payload)
    if actual_sha.lower() != entry.sha256.lower():
        raise ValueError(f"Checksum verification failed for adapter '{entry.adapter}'")
    if entry.size_bytes and len(payload) != entry.size_bytes:
        raise ValueError(f"Size verification failed for adapter '{entry.adapter}'")
    return {"bytes": payload, "sha256": actual_sha, "size_bytes": len(payload)}


def _download_url_bytes(url: str, *, expected_host: str, max_redirects: int = 3) -> bytes:
    current_url = url
    opener = urllib.request.build_opener(_NoRedirectHandler)
    for _ in range(max_redirects + 1):
        _validate_provider_url(current_url, expected_host=expected_host)
        request = urllib.request.Request(current_url, headers={"User-Agent": "PilotTunnel-BinaryInstall/1.0"}, method="GET")
        try:
            with opener.open(request, timeout=20) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise
            location = exc.headers.get("Location", "").strip()
            if not location:
                raise ValueError("Redirect response did not include a Location header") from exc
            current_url = urllib.parse.urljoin(current_url, location)
            continue
    raise ValueError(f"Too many redirects while downloading '{url}'")


def _validate_provider_url(url: str, *, expected_host: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        if parsed.scheme == "http" and (parsed.hostname or "") in LOCAL_HOSTS:
            pass
        else:
            raise ValueError("Provider binary URL must use HTTPS unless it targets localhost test fixtures")
    if not parsed.hostname:
        raise ValueError("Provider binary URL host is required")
    if expected_host and parsed.hostname.lower() != expected_host:
        raise ValueError(f"Redirect host '{parsed.hostname}' does not match expected provider host '{expected_host}'")
    if ".." in parsed.path.split("/"):
        raise ValueError("Path traversal blocked in provider binary URL")


def _binary_destination(install_dir: Path, adapter: str, platform_id: str) -> Path:
    return (install_dir / adapter / platform_id / _managed_binary_name(adapter, platform_id)).resolve()


def _managed_binary_name(adapter: str, platform_id: str) -> str:
    filename = binary_spec(adapter).binary_name
    if platform_id.startswith("windows") and not filename.endswith(".exe"):
        return f"{filename}.exe"
    return filename


def _installed_status(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "checksum_match": False}
    _validate_existing_source(path)
    return {"exists": True, "checksum_match": _sha256_file(path).lower() == expected_sha256.lower()}


def _atomic_write(path: Path, data: bytes, *, executable: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_parent_chain(path.parent)
    temp_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    temp_path.write_bytes(data)
    if executable and os.name != "nt":
        temp_path.chmod(temp_path.stat().st_mode | 0o755)
    temp_path.replace(path)


def _configured_install_dir(config: AppConfig, install_dir: Path | None) -> Path | None:
    if install_dir is not None:
        return _validated_install_dir(install_dir)
    if config.binary_resolution.managed_install_dir:
        return _validated_install_dir(Path(config.binary_resolution.managed_install_dir), must_exist=False)
    return None


def _validated_install_dir(path: Path, *, must_exist: bool = False) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for install dir: {path!r}")
    resolved = path.resolve()
    if must_exist and not resolved.exists():
        raise ValueError(f"Install directory does not exist: {path}")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"Install directory must be a directory: {path}")
    _validate_parent_chain(resolved)
    return resolved


def _validated_manifest_path(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for manifest file: {path!r}")
    resolved = path.resolve()
    if not resolved.exists():
        raise ValueError(f"Manifest file does not exist: {path}")
    return resolved


def _validate_destination_path(path: Path, install_dir: Path) -> None:
    _validate_parent_chain(path.parent)
    if install_dir not in path.parents:
        raise ValueError(f"Refusing to write outside install dir: {path}")
    if path.exists() and path.is_symlink():
        raise ValueError(f"Symlink escape blocked for destination path: {path}")


def _validate_parent_chain(path: Path) -> None:
    current = path
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError(f"Symlink escape blocked for install path: {current}")
        if current.parent == current:
            return
        current = current.parent


def _validate_existing_source(path: Path) -> None:
    if not path.exists():
        raise ValueError(f"Source file does not exist: {path}")
    if path.is_symlink():
        raise ValueError(f"Symlink escape blocked for source file: {path}")
    if path.is_dir():
        raise ValueError(f"Source path must be a file, not a directory: {path}")


def _read_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _write_summary(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _manifest_has_entry(manifest_path: str, adapter: str, platform_id: str) -> bool:
    try:
        manifest = load_manifest(
            manifest_file=Path(manifest_path),
            allow_provider_host=None,
            require_allowlisted_remote_host=False,
        )
        select_manifest_binary(manifest, adapter=adapter, platform_id=platform_id)
        return True
    except Exception:
        return False


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


def _audit(action: str, profile: str, details: dict[str, Any], path: Path) -> None:
    write_audit_log(action, profile, details, path)
