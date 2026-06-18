"""Controlled manifest-based binary provider workflow."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import write_audit_log
from .binaries import all_binary_adapters, binary_spec, cache_layout, current_platform_id, import_binary, provider_required_adapters, supported_platforms
from .state import AppState

SCHEMA = "pilottunnel-binary-provider-v1"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


@dataclass(frozen=True)
class ProviderBinary:
    adapter: str
    binary_name: str
    version: str
    platform: str
    url: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class ProviderManifest:
    schema: str
    provider: str
    generated_at: str
    binaries: tuple[ProviderBinary, ...]
    source: str


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(newurl, code, "Redirects are not allowed", headers, fp)


def inspect_manifest(
    *,
    manifest_url: str | None = None,
    manifest_file: Path | None = None,
    allow_provider_host: str | None = None,
    requested_platform: str | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(
        manifest_url=manifest_url,
        manifest_file=manifest_file,
        allow_provider_host=allow_provider_host,
    )
    platform_id = resolve_platform_id(requested_platform)
    by_adapter = [entry_to_dict(entry) for entry in manifest.binaries if entry.platform == platform_id]
    return {
        "ok": True,
        "schema": manifest.schema,
        "provider": manifest.provider,
        "generated_at": manifest.generated_at,
        "source": manifest.source,
        "platform": platform_id,
        "allow_provider_host": allow_provider_host or "",
        "binaries": by_adapter,
        "required_provider_adapters": list(provider_required_adapters()),
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def generate_manifest(
    *,
    provider_name: str,
    base_url: str,
    source_dir: Path,
    output_path: Path,
) -> dict[str, Any]:
    validated_provider = _validated_text(provider_name, "provider")
    validated_base_url = _validated_generation_base_url(base_url)
    source_root = _validated_source_dir(source_dir)
    validated_output = _validated_output_path(output_path)
    entries = _manifest_entries_from_source(source_root, validated_base_url)
    if not entries:
        raise ValueError("Source directory does not contain any supported provider binaries")

    payload = {
        "schema": SCHEMA,
        "provider": validated_provider,
        "generated_at": _timestamp(),
        "binaries": [entry_to_dict(entry) for entry in entries],
    }
    validated_output.parent.mkdir(parents=True, exist_ok=True)
    validated_output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "ok": True,
        "schema": SCHEMA,
        "provider": validated_provider,
        "source_dir": str(source_root),
        "output": str(validated_output),
        "base_url": validated_base_url,
        "entries": len(entries),
        "binaries": [entry_to_dict(entry) for entry in entries],
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def verify_manifest_file(*, manifest_file: Path) -> dict[str, Any]:
    manifest = load_manifest(
        manifest_file=manifest_file,
        allow_provider_host=None,
        require_allowlisted_remote_host=False,
    )
    duplicates = _duplicate_manifest_keys(manifest.binaries)
    missing_required = _missing_required_entries(manifest.binaries)
    warnings: list[str] = []
    if missing_required:
        warnings.append("Manifest is missing required provider binaries")
    payload = {
        "ok": not duplicates and not missing_required,
        "schema": manifest.schema,
        "provider": manifest.provider,
        "source": manifest.source,
        "generated_at": manifest.generated_at,
        "entries": len(manifest.binaries),
        "duplicates": duplicates,
        "missing_required": missing_required,
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        "warnings": warnings,
    }
    return payload


def download_binary(
    *,
    adapter: str,
    manifest_url: str | None,
    manifest_file: Path | None,
    allow_provider_host: str | None,
    cache_root: Path,
    state: AppState,
    confirm: str | None,
    force: bool,
    run_version: bool,
    audit_path: Path,
    requested_platform: str | None = None,
) -> dict[str, Any]:
    platform_id = resolve_platform_id(requested_platform)
    attempt = {
        "adapter": adapter,
        "platform": platform_id,
        "manifest_url": manifest_url or "",
        "manifest_file": str(manifest_file) if manifest_file else "",
        "allow_provider_host": allow_provider_host or "",
        "confirm": confirm or "",
        "force": force,
        "run_version": run_version,
    }
    if confirm != "DOWNLOAD_BINARY":
        payload = {
            "ok": False,
            "message": "Refusing binary download without --confirm DOWNLOAD_BINARY",
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("binary-download", adapter, payload, audit_path)
        return payload

    try:
        manifest = load_manifest(manifest_url=manifest_url, manifest_file=manifest_file, allow_provider_host=allow_provider_host)
        binary = select_manifest_binary(manifest, adapter=adapter, platform_id=platform_id)
        payload = _download_and_import(
            entry=binary,
            provider=manifest.provider,
            cache_root=cache_root,
            state=state,
            force=force,
            run_version=run_version,
        )
    except Exception as exc:
        payload = {
            "ok": False,
            "message": str(exc),
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
        }
        _audit("binary-download", adapter, {**attempt, **payload}, audit_path)
        raise
    _audit("binary-download", adapter, {**attempt, **payload}, audit_path)
    return payload


def download_all_binaries(
    *,
    manifest_url: str | None,
    manifest_file: Path | None,
    allow_provider_host: str | None,
    cache_root: Path,
    state: AppState,
    confirm: str | None,
    force: bool,
    run_version: bool,
    audit_path: Path,
    requested_platform: str | None = None,
) -> dict[str, Any]:
    platform_id = resolve_platform_id(requested_platform)
    attempt = {
        "platform": platform_id,
        "manifest_url": manifest_url or "",
        "manifest_file": str(manifest_file) if manifest_file else "",
        "allow_provider_host": allow_provider_host or "",
        "confirm": confirm or "",
        "force": force,
        "run_version": run_version,
    }
    if confirm != "DOWNLOAD_ALL_BINARIES":
        payload = {
            "ok": False,
            "message": "Refusing binary download-all without --confirm DOWNLOAD_ALL_BINARIES",
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("binary-download-all", "all", payload, audit_path)
        return payload

    try:
        manifest = load_manifest(manifest_url=manifest_url, manifest_file=manifest_file, allow_provider_host=allow_provider_host)
    except Exception as exc:
        payload = {
            "ok": False,
            "message": str(exc),
            "failed_adapters": list(provider_required_adapters()),
            "results": [],
            "downloads_performed": False,
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            **attempt,
        }
        _audit("binary-download-all", "all", payload, audit_path)
        raise
    entries_by_adapter = {
        entry.adapter: entry
        for entry in manifest.binaries
        if entry.platform == platform_id
    }
    results: list[dict[str, Any]] = []
    failed: list[str] = []

    for adapter_name in binary_spec_map():
        spec = binary_spec(adapter_name)
        if platform_id not in spec.supported_platforms:
            results.append(
                {
                    "adapter": adapter_name,
                    "result": "skipped_unsupported_platform",
                    "binary_name": spec.binary_name,
                    "platform": platform_id,
                }
            )
            continue
        if spec.coverage == "system_dependency":
            results.append({"adapter": adapter_name, "result": "skipped_system_dependency", "binary_name": spec.binary_name})
            continue
        if spec.coverage in {"template_only", "listed_only"}:
            results.append({"adapter": adapter_name, "result": "skipped_template_only", "binary_name": spec.binary_name})
            continue
        entry = entries_by_adapter.get(adapter_name)
        if entry is None:
            results.append(
                {
                    "adapter": adapter_name,
                    "result": "missing_from_manifest",
                    "message": f"Manifest missing adapter '{adapter_name}' for platform '{platform_id}'",
                }
            )
            failed.append(adapter_name)
            continue
        existing = state.binaries.get(adapter_name)
        if existing and not force and existing.sha256 == entry.sha256 and existing.version == entry.version:
            result_name = "already_present" if existing.source_type == "provider" else "imported"
            results.append(
                {
                    "adapter": adapter_name,
                    "result": result_name,
                    "version": existing.version,
                    "sha256": existing.sha256,
                    "imported_path": existing.imported_path,
                }
            )
            continue
        try:
            payload = _download_and_import(
                entry=entry,
                provider=manifest.provider,
                cache_root=cache_root,
                state=state,
                force=force,
                run_version=run_version,
            )
            payload["result"] = "downloaded"
            results.append(payload)
        except Exception as exc:
            results.append({"adapter": adapter_name, "result": "failed", "message": str(exc)})
            failed.append(adapter_name)

    payload = {
        "ok": not failed,
        "action": "binary-download-all",
        "platform": platform_id,
        "provider": manifest.provider,
        "results": results,
        "failed_adapters": failed,
        "downloads_performed": any(item.get("result") == "downloaded" for item in results),
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        **attempt,
    }
    _audit("binary-download-all", "all", payload, audit_path)
    return payload


def load_manifest(
    *,
    manifest_url: str | None = None,
    manifest_file: Path | None = None,
    allow_provider_host: str | None = None,
    require_allowlisted_remote_host: bool = True,
) -> ProviderManifest:
    if bool(manifest_url) == bool(manifest_file):
        raise ValueError("Use exactly one of --manifest-url or --manifest-file")
    if manifest_url:
        manifest_source = validate_manifest_url(manifest_url, allow_provider_host, require_allowlisted_remote_host=require_allowlisted_remote_host)
        payload = _read_json_url(manifest_source)
        return parse_manifest(
            payload,
            source=manifest_source,
            allow_provider_host=allow_provider_host,
            require_allowlisted_remote_host=require_allowlisted_remote_host,
        )
    file_path = validate_manifest_file(manifest_file)
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    return parse_manifest(
        payload,
        source=str(file_path),
        allow_provider_host=allow_provider_host,
        require_allowlisted_remote_host=require_allowlisted_remote_host,
    )


def parse_manifest(
    payload: Any,
    *,
    source: str,
    allow_provider_host: str | None = None,
    require_allowlisted_remote_host: bool = True,
) -> ProviderManifest:
    if not isinstance(payload, dict):
        raise ValueError("Binary provider manifest must be a JSON object")
    if payload.get("schema") != SCHEMA:
        raise ValueError("Unsupported binary provider manifest schema")
    provider = _validated_text(payload.get("provider"), "provider")
    generated_at = _validated_text(payload.get("generated_at"), "generated_at")
    items = payload.get("binaries")
    if not isinstance(items, list) or not items:
        raise ValueError("Binary provider manifest requires a non-empty binaries list")

    binaries: list[ProviderBinary] = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Binary provider entry must be an object")
        adapter = _validated_text(item.get("adapter"), "adapter")
        spec = binary_spec(adapter)
        entry = ProviderBinary(
            adapter=adapter,
            binary_name=_validated_text(item.get("binary_name"), "binary_name"),
            version=_validated_text(item.get("version"), "version"),
            platform=_validated_text(item.get("platform"), "platform"),
            url=_validated_url(
                item.get("url"),
                allow_provider_host,
                require_allowlisted_remote_host=require_allowlisted_remote_host,
            ),
            sha256=_validated_sha256(item.get("sha256")),
            size_bytes=_validated_size(item.get("size_bytes")),
        )
        if entry.platform not in spec.supported_platforms:
            raise ValueError(f"Unsupported platform '{entry.platform}' for adapter '{adapter}'")
        if entry.binary_name != spec.binary_name:
            raise ValueError(f"binary_name '{entry.binary_name}' does not match adapter '{adapter}'")
        binaries.append(entry)
    return ProviderManifest(schema=SCHEMA, provider=provider, generated_at=generated_at, binaries=tuple(binaries), source=source)


def select_manifest_binary(manifest: ProviderManifest, *, adapter: str, platform_id: str) -> ProviderBinary:
    binary_spec(adapter)
    for entry in manifest.binaries:
        if entry.adapter == adapter and entry.platform == platform_id:
            return entry
    raise ValueError(f"Manifest does not include adapter '{adapter}' for platform '{platform_id}'")


def resolve_platform_id(requested_platform: str | None) -> str:
    if requested_platform in {None, "", "auto", "default"}:
        return current_platform_id()
    if requested_platform not in supported_platforms():
        raise ValueError(f"Unsupported platform '{requested_platform}'")
    return requested_platform


def entry_to_dict(entry: ProviderBinary) -> dict[str, Any]:
    return {
        "adapter": entry.adapter,
        "binary_name": entry.binary_name,
        "version": entry.version,
        "platform": entry.platform,
        "url": entry.url,
        "sha256": entry.sha256,
        "size_bytes": entry.size_bytes,
    }


def binary_spec_map() -> tuple[str, ...]:
    return all_binary_adapters()


def _download_and_import(
    *,
    entry: ProviderBinary,
    provider: str,
    cache_root: Path,
    state: AppState,
    force: bool,
    run_version: bool,
) -> dict[str, Any]:
    layout = cache_layout(cache_root)
    downloads_dir = layout["downloads_dir"]
    downloads_dir.mkdir(parents=True, exist_ok=True)
    temp_path = (downloads_dir / f"{entry.adapter}-{entry.platform}-{entry.version}.tmp").resolve()
    if layout["root"] not in temp_path.parents:
        raise ValueError(f"Refusing to write outside cache root: {temp_path}")
    try:
        data = _read_bytes_url(entry.url)
        temp_path.write_bytes(data)
        actual_sha = _sha256_bytes(data)
        if actual_sha.lower() != entry.sha256.lower():
            temp_path.unlink(missing_ok=True)
            raise ValueError(f"Checksum verification failed for adapter '{entry.adapter}'")
        if entry.size_bytes and len(data) != entry.size_bytes:
            temp_path.unlink(missing_ok=True)
            raise ValueError(f"Size verification failed for adapter '{entry.adapter}'")
        provider_host = urllib.parse.urlparse(entry.url).hostname or ""
        imported = import_binary(
            adapter=entry.adapter,
            source=temp_path,
            version=entry.version,
            cache_root=cache_root,
            state=state,
            sha256_expected=entry.sha256,
            force=force,
            source_type="provider",
            source_provider=provider,
            provider_host=provider_host,
            downloaded_at=_timestamp(),
        )
    finally:
        temp_path.unlink(missing_ok=True)
    payload = {
        "ok": True,
        "adapter": entry.adapter,
        "binary_name": entry.binary_name,
        "version": entry.version,
        "sha256": entry.sha256,
        "provider": provider,
        "provider_host": urllib.parse.urlparse(entry.url).hostname or "",
        "downloaded": True,
        "imported": True,
        "downloads_performed": True,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        **imported,
    }
    if run_version:
        from .binaries import verify_binary

        payload["run_version_result"] = verify_binary(
            adapter=entry.adapter,
            cache_root=cache_root,
            state=state,
            run_version=True,
        )["run_version_result"]
    return payload


def validate_manifest_file(path: Path | None) -> Path:
    if path is None:
        raise ValueError("Manifest file is required")
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for manifest file: {path!r}")
    resolved = path.resolve()
    if not resolved.exists():
        raise ValueError(f"Manifest file does not exist: {path}")
    return resolved


def validate_manifest_url(url: str, allow_provider_host: str | None, *, require_allowlisted_remote_host: bool = True) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        if parsed.scheme == "http" and (parsed.hostname or "") in LOCAL_HOSTS:
            pass
        else:
            raise ValueError("Manifest URL must use HTTPS unless it targets localhost test fixtures")
    if parsed.scheme == "file":
        raise ValueError("file:// manifest URLs are not supported")
    if not parsed.hostname:
        raise ValueError("Manifest URL host is required")
    if ".." in parsed.path.split("/"):
        raise ValueError("Path traversal blocked in manifest URL")
    if require_allowlisted_remote_host and parsed.hostname not in LOCAL_HOSTS and not allow_provider_host:
        raise ValueError("Remote binary provider usage requires --allow-provider-host")
    if allow_provider_host and parsed.hostname.lower() != allow_provider_host.lower():
        raise ValueError(f"Manifest URL host '{parsed.hostname}' does not match allowlisted host '{allow_provider_host}'")
    return url


def _validated_url(value: Any, allow_provider_host: str | None, *, require_allowlisted_remote_host: bool = True) -> str:
    url = _validated_text(value, "url")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "file":
        raise ValueError("file:// binary URLs are not supported")
    if parsed.scheme != "https":
        if parsed.scheme == "http" and (parsed.hostname or "") in LOCAL_HOSTS:
            pass
        else:
            raise ValueError("Binary URL must use HTTPS unless it targets localhost test fixtures")
    if not parsed.hostname:
        raise ValueError("Binary URL host is required")
    if allow_provider_host:
        if parsed.hostname.lower() != allow_provider_host.lower():
            raise ValueError(f"Binary URL host '{parsed.hostname}' does not match allowlisted host '{allow_provider_host}'")
    elif require_allowlisted_remote_host and parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("Remote binary URLs require --allow-provider-host")
    if ".." in parsed.path.split("/"):
        raise ValueError("Path traversal blocked in binary URL")
    return url


def _validated_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Manifest field '{label}' is required")
    text = value.strip()
    if "/" in text or "\\" in text or ".." in text:
        if label not in {"version", "generated_at", "provider", "url"}:
            raise ValueError(f"Path traversal blocked in manifest field '{label}'")
    return text


def _validated_sha256(value: Any) -> str:
    if not isinstance(value, str) or len(value.strip()) != 64:
        raise ValueError("Binary provider manifest requires a 64-character sha256 checksum")
    checksum = value.strip().lower()
    if any(ch not in "0123456789abcdef" for ch in checksum):
        raise ValueError("Binary provider manifest sha256 must be hexadecimal")
    return checksum


def _validated_size(value: Any) -> int:
    if not isinstance(value, int) or value < 0:
        raise ValueError("Binary provider manifest size_bytes must be a non-negative integer")
    return value


def _validated_generation_base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https":
        if parsed.scheme == "http" and (parsed.hostname or "") in LOCAL_HOSTS:
            pass
        else:
            raise ValueError("Manifest base URL must use HTTPS unless it targets localhost test fixtures")
    if not parsed.hostname:
        raise ValueError("Manifest base URL host is required")
    if ".." in parsed.path.split("/"):
        raise ValueError("Path traversal blocked in manifest base URL")
    base = value.rstrip("/")
    if not base:
        raise ValueError("Manifest base URL cannot be empty")
    return base


def _validated_source_dir(source_dir: Path) -> Path:
    if ".." in source_dir.parts:
        raise ValueError(f"Path traversal blocked for source dir: {source_dir!r}")
    resolved = source_dir.resolve()
    if not resolved.exists():
        raise ValueError(f"Source directory does not exist: {source_dir}")
    if not resolved.is_dir():
        raise ValueError(f"Source directory must be a directory: {source_dir}")
    return resolved


def _validated_output_path(output_path: Path) -> Path:
    if ".." in output_path.parts:
        raise ValueError(f"Path traversal blocked for output path: {output_path!r}")
    return output_path.resolve()


def _manifest_entries_from_source(source_root: Path, base_url: str) -> tuple[ProviderBinary, ...]:
    binaries: list[ProviderBinary] = []
    for path in sorted(source_root.rglob("*")):
        if path.is_dir():
            continue
        if path.is_symlink():
            resolved = path.resolve()
            if not _is_relative_to(resolved, source_root):
                raise ValueError(f"Symlink escape blocked for source path: {path}")
        resolved_path = path.resolve()
        if not _is_relative_to(resolved_path, source_root):
            raise ValueError(f"Source file escapes source dir: {path}")
        relative = path.relative_to(source_root)
        if len(relative.parts) != 3:
            raise ValueError(f"Unknown file layout under source dir: {relative.as_posix()}")
        adapter_name, platform_id, filename = relative.parts
        spec = binary_spec(adapter_name)
        if spec.coverage != "provider_required":
            raise ValueError(f"Adapter '{adapter_name}' is not eligible for provider manifest generation")
        if platform_id not in spec.supported_platforms:
            raise ValueError(f"Unsupported platform '{platform_id}' for adapter '{adapter_name}'")
        expected_filename = spec.binary_name
        if filename != expected_filename:
            raise ValueError(f"Unexpected binary name '{filename}' for adapter '{adapter_name}' on platform '{platform_id}'")
        data = resolved_path.read_bytes()
        binaries.append(
            ProviderBinary(
                adapter=adapter_name,
                binary_name=spec.binary_name,
                version="provider-current",
                platform=platform_id,
                url=_join_manifest_url(base_url, relative.as_posix()),
                sha256=_sha256_bytes(data),
                size_bytes=len(data),
            )
        )
    return tuple(binaries)


def _join_manifest_url(base_url: str, relative_path: str) -> str:
    quoted_parts = [urllib.parse.quote(part) for part in relative_path.split("/")]
    return f"{base_url}/{posixpath.join(*quoted_parts)}"


def _duplicate_manifest_keys(entries: tuple[ProviderBinary, ...]) -> list[dict[str, str]]:
    duplicates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        key = (entry.adapter, entry.platform, entry.version)
        if key in seen:
            duplicates.append({"adapter": entry.adapter, "platform": entry.platform, "version": entry.version})
            continue
        seen.add(key)
    return duplicates


def _missing_required_entries(entries: tuple[ProviderBinary, ...]) -> list[dict[str, str]]:
    present = {(entry.adapter, entry.platform) for entry in entries}
    missing: list[dict[str, str]] = []
    for adapter_name in provider_required_adapters():
        spec = binary_spec(adapter_name)
        for platform_id in spec.supported_platforms:
            if (adapter_name, platform_id) not in present:
                missing.append({"adapter": adapter_name, "platform": platform_id})
    return missing


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_json_url(url: str) -> Any:
    data = _read_bytes_url(url)
    return json.loads(data.decode("utf-8"))


def _read_bytes_url(url: str) -> bytes:
    request = urllib.request.Request(url, method="GET")
    opener = urllib.request.build_opener(_NoRedirectHandler)
    with opener.open(request, timeout=10) as response:
        return response.read()


def _sha256_bytes(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


def _audit(action: str, profile: str, details: dict[str, Any], path: Path) -> None:
    write_audit_log(action, profile, details, path)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
