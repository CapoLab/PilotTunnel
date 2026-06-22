"""Controlled manifest-based binary provider workflow."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import write_audit_log
from .binaries import (
    all_binary_adapters,
    binary_components,
    binary_record_key,
    binary_spec,
    cache_layout,
    current_platform_id,
    import_binary,
    normalize_binary_component,
    primary_binary_component,
    provider_required_adapters,
    supported_platforms,
)
from .state import AppState

SCHEMA = "pilottunnel-binary-provider-v1"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
GITHUB_PROVIDER_HOSTS = (
    "github.com",
    "github-releases.githubusercontent.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
)


@dataclass(frozen=True)
class ProviderBinary:
    adapter: str
    binary_name: str
    component: str
    version: str
    platform: str
    filename: str
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
    allow_provider_host: str | list[str] | None = None,
    requested_platform: str | None = None,
) -> dict[str, Any]:
    normalized_allow_provider_host = canonical_provider_host_value(allow_provider_host)
    manifest = load_manifest(
        manifest_url=manifest_url,
        manifest_file=manifest_file,
        allow_provider_host=normalized_allow_provider_host,
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
        "allow_provider_host": normalized_allow_provider_host or "",
        "allow_provider_hosts": list(normalize_provider_hosts(normalized_allow_provider_host)),
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
    versions: dict[str, str] | None = None,
) -> dict[str, Any]:
    validated_provider = _validated_text(provider_name, "provider")
    validated_base_url = _validated_generation_base_url(base_url)
    source_root = _validated_source_dir(source_dir)
    validated_output = _validated_output_path(output_path)
    entries = _manifest_entries_from_source(source_root, validated_base_url, versions=versions)
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


def build_provider_release_plan(
    *,
    source_dir: Path,
    provider_name: str,
    repo_slug: str,
    release_tag: str,
    output_dir: Path,
    version_overrides: list[str],
) -> dict[str, Any]:
    validated_provider = _validated_text(provider_name, "provider")
    validated_repo_slug = _validated_repo_slug(repo_slug)
    validated_release_tag = _validated_release_tag(release_tag)
    resolved_output_dir = _validated_output_dir(output_dir)
    source_root = _validated_source_dir(source_dir)
    versions = _parse_version_overrides(version_overrides)
    release_base_url = _github_release_base_url(validated_repo_slug, validated_release_tag)
    entries, copies = _release_entries_from_source(
        source_root=source_root,
        release_base_url=release_base_url,
        versions=versions,
        output_dir=resolved_output_dir,
    )
    if not entries:
        raise ValueError("Source directory does not contain any supported provider binaries")
    platforms = tuple(sorted({entry.platform for entry in entries}))
    missing_required = _missing_required_entries(
        tuple(entries),
        required_platforms=platforms,
        required_adapters=provider_required_adapters(),
    )
    manifest_path = resolved_output_dir / "provider-manifest.json"
    return {
        "ok": not missing_required,
        "action": "binary-provider-release-plan",
        "provider": validated_provider,
        "repo_slug": validated_repo_slug,
        "release_tag": validated_release_tag,
        "release_base_url": release_base_url,
        "release_dir": str(resolved_output_dir),
        "manifest_output": str(manifest_path),
        "recommended_allow_provider_hosts": list(_recommended_provider_hosts(release_base_url)),
        "binaries": [entry_to_dict(entry) for entry in entries],
        "asset_files": copies,
        "missing_required": missing_required,
        "main_repo_remains_source_only": True,
        "binary_files_committed_to_main_repo": False,
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def write_provider_release_assets(
    *,
    source_dir: Path,
    provider_name: str,
    repo_slug: str,
    release_tag: str,
    output_dir: Path,
    version_overrides: list[str],
    confirm: str | None,
    force: bool,
) -> dict[str, Any]:
    plan = build_provider_release_plan(
        source_dir=source_dir,
        provider_name=provider_name,
        repo_slug=repo_slug,
        release_tag=release_tag,
        output_dir=output_dir,
        version_overrides=version_overrides,
    )
    if confirm != "PREPARE_PROVIDER_RELEASE_ASSETS":
        return {
            **plan,
            "ok": False,
            "message": "Refusing provider release asset preparation without --confirm PREPARE_PROVIDER_RELEASE_ASSETS",
            "files_written": [],
        }
    if not plan["ok"]:
        return {
            **plan,
            "message": "Provider release assets are incomplete",
            "files_written": [],
        }

    manifest_path = Path(plan["manifest_output"])
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    written_files: list[str] = []
    manifest_entries = plan["binaries"]
    asset_files = plan["asset_files"]
    for item in asset_files:
        source_path = Path(item["source_path"])
        output_path = Path(item["output_path"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            if not force and _sha256_file(output_path) != _sha256_file(source_path):
                raise ValueError(f"Release asset already exists with different content: {output_path.name}")
        if not output_path.exists() or force:
            shutil.copy2(source_path, output_path)
        written_files.append(str(output_path))
    manifest_payload = {
        "schema": SCHEMA,
        "provider": plan["provider"],
        "generated_at": _timestamp(),
        "binaries": manifest_entries,
    }
    manifest_path.write_text(json.dumps(manifest_payload, indent=2, sort_keys=True), encoding="utf-8")
    written_files.append(str(manifest_path))
    return {
        **plan,
        "ok": True,
        "action": "binary-provider-release-assets",
        "files_written": written_files,
        "force": force,
    }


def verify_manifest_file(
    *,
    manifest_file: Path,
    required_platforms: tuple[str, ...] | None = None,
    required_adapters: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(
        manifest_file=manifest_file,
        allow_provider_host=None,
        require_allowlisted_remote_host=False,
    )
    represented_platforms = tuple(sorted({entry.platform for entry in manifest.binaries}))
    checked_platforms = _verification_platforms(
        manifest.binaries,
        requested_platforms=required_platforms,
    )
    duplicates = _duplicate_manifest_keys(manifest.binaries)
    missing_required = _missing_required_entries(
        manifest.binaries,
        required_platforms=checked_platforms,
        required_adapters=required_adapters,
    )
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
        "represented_platforms": list(represented_platforms),
        "checked_platforms": list(checked_platforms),
        "required_platforms": list(checked_platforms),
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
    allow_provider_host: str | list[str] | None,
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
        "allow_provider_host": canonical_provider_host_value(allow_provider_host) or "",
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
        normalized_allow_provider_host = canonical_provider_host_value(allow_provider_host)
        manifest = load_manifest(manifest_url=manifest_url, manifest_file=manifest_file, allow_provider_host=normalized_allow_provider_host)
        binary = select_manifest_binary(manifest, adapter=adapter, platform_id=platform_id)
        payload = _download_and_import(
            entry=binary,
            provider=manifest.provider,
            cache_root=cache_root,
            state=state,
            force=force,
            run_version=run_version,
            allow_provider_host=normalized_allow_provider_host,
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
    allow_provider_host: str | list[str] | None,
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
        "allow_provider_host": canonical_provider_host_value(allow_provider_host) or "",
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
        normalized_allow_provider_host = canonical_provider_host_value(allow_provider_host)
        manifest = load_manifest(manifest_url=manifest_url, manifest_file=manifest_file, allow_provider_host=normalized_allow_provider_host)
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
    entries_by_component = {
        (entry.adapter, entry.component): entry
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
        component_payloads: list[dict[str, Any]] = []
        missing_components: list[str] = []
        for component in binary_components(adapter_name):
            entry = entries_by_component.get((adapter_name, component))
            if entry is None:
                message = (
                    f"Manifest missing component '{component}' for adapter '{adapter_name}' for platform '{platform_id}'"
                    if len(binary_components(adapter_name)) > 1
                    else f"Manifest missing adapter '{adapter_name}' for platform '{platform_id}'"
                )
                component_payloads.append({"adapter": adapter_name, "component": component, "result": "missing_from_manifest", "message": message})
                missing_components.append(component)
                continue
            existing = state.binaries.get(binary_record_key(adapter_name, component))
            if existing and not force and existing.sha256 == entry.sha256 and existing.version == entry.version:
                result_name = "already_present" if existing.source_type == "provider" else "imported"
                component_payloads.append(
                    {
                        "adapter": adapter_name,
                        "component": component,
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
                    allow_provider_host=normalized_allow_provider_host,
                )
                payload["result"] = "downloaded"
                component_payloads.append(payload)
            except Exception as exc:
                component_payloads.append({"adapter": adapter_name, "component": component, "result": "failed", "message": str(exc)})
                missing_components.append(component)
        if missing_components:
            failed.append(adapter_name)
        if any(item["result"] == "failed" for item in component_payloads):
            result_name = "failed"
            if adapter_name not in failed:
                failed.append(adapter_name)
        elif any(item["result"] == "missing_from_manifest" for item in component_payloads):
            result_name = "missing_from_manifest"
        elif any(item["result"] == "downloaded" for item in component_payloads):
            result_name = "downloaded"
        elif all(item["result"] == "already_present" for item in component_payloads):
            result_name = "already_present"
        else:
            result_name = "imported"
        results.append(
            {
                "adapter": adapter_name,
                "result": result_name,
                "required_components": list(binary_components(adapter_name)),
                "components": component_payloads,
                "missing_components": missing_components,
            }
        )

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
    from .binary_readiness import build_binary_readiness_report

    readiness = build_binary_readiness_report(
        cache_root=cache_root,
        state=state,
        manifest_url=manifest_url,
        manifest_file=manifest_file,
        allow_provider_host=normalized_allow_provider_host,
        requested_platform=platform_id,
        require_all=True,
    )
    payload["binary_readiness"] = readiness
    if not readiness["ok"]:
        payload["ok"] = False
        payload["failed_adapters"] = sorted(set(payload["failed_adapters"]) | set(readiness["missing_adapters"]))
    _audit("binary-download-all", "all", payload, audit_path)
    return payload


def load_manifest(
    *,
    manifest_url: str | None = None,
    manifest_file: Path | None = None,
    allow_provider_host: str | list[str] | None = None,
    require_allowlisted_remote_host: bool = True,
) -> ProviderManifest:
    normalized_allow_provider_host = canonical_provider_host_value(allow_provider_host)
    if bool(manifest_url) == bool(manifest_file):
        raise ValueError("Use exactly one of --manifest-url or --manifest-file")
    if manifest_url:
        manifest_source = validate_manifest_url(manifest_url, normalized_allow_provider_host, require_allowlisted_remote_host=require_allowlisted_remote_host)
        payload = _read_json_url(manifest_source, allowed_hosts=set(normalize_provider_hosts(normalized_allow_provider_host)))
        return parse_manifest(
            payload,
            source=manifest_source,
            allow_provider_host=normalized_allow_provider_host,
            require_allowlisted_remote_host=require_allowlisted_remote_host,
        )
    file_path = validate_manifest_file(manifest_file)
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    return parse_manifest(
        payload,
        source=str(file_path),
        allow_provider_host=normalized_allow_provider_host,
        require_allowlisted_remote_host=require_allowlisted_remote_host,
    )


def parse_manifest(
    payload: Any,
    *,
    source: str,
    allow_provider_host: str | list[str] | None = None,
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
        component = _validated_component(adapter, item.get("component"), item.get("binary_name"))
        entry = ProviderBinary(
            adapter=adapter,
            binary_name=_validated_text(item.get("binary_name"), "binary_name"),
            component=component,
            version=_validated_text(item.get("version"), "version"),
            platform=_validated_text(item.get("platform"), "platform"),
            filename=_validated_filename(item.get("filename") or _url_filename(item.get("url"))),
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
        if entry.binary_name != normalize_binary_component(adapter, component):
            raise ValueError(f"binary_name '{entry.binary_name}' does not match adapter '{adapter}' component '{component}'")
        if posixpath.basename(urllib.parse.urlparse(entry.url).path) != entry.filename:
            raise ValueError(f"filename '{entry.filename}' does not match URL path for adapter '{adapter}'")
        binaries.append(entry)
    return ProviderManifest(schema=SCHEMA, provider=provider, generated_at=generated_at, binaries=tuple(binaries), source=source)


def select_manifest_binary(manifest: ProviderManifest, *, adapter: str, platform_id: str, component: str | None = None) -> ProviderBinary:
    resolved_component = normalize_binary_component(adapter, component)
    for entry in manifest.binaries:
        if entry.adapter == adapter and entry.platform == platform_id and entry.component == resolved_component:
            return entry
    if len(binary_components(adapter)) > 1:
        raise ValueError(f"Manifest does not include component '{resolved_component}' for adapter '{adapter}' on platform '{platform_id}'")
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
        "component": entry.component,
        "version": entry.version,
        "platform": entry.platform,
        "filename": entry.filename,
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
    allow_provider_host: str | list[str] | None,
) -> dict[str, Any]:
    layout = cache_layout(cache_root)
    downloads_dir = layout["downloads_dir"]
    downloads_dir.mkdir(parents=True, exist_ok=True)
    temp_path = (downloads_dir / f"{entry.adapter}-{entry.component}-{entry.platform}-{entry.version}.tmp").resolve()
    if layout["root"] not in temp_path.parents:
        raise ValueError(f"Refusing to write outside cache root: {temp_path}")
    try:
        data = _read_bytes_url(entry.url, allowed_hosts=set(normalize_provider_hosts(allow_provider_host)))
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
            component=entry.component,
        )
    finally:
        temp_path.unlink(missing_ok=True)
    payload = {
        "ok": True,
        "adapter": entry.adapter,
        "binary_name": entry.binary_name,
        "component": entry.component,
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


def validate_manifest_url(url: str, allow_provider_host: str | list[str] | None, *, require_allowlisted_remote_host: bool = True) -> str:
    parsed = urllib.parse.urlparse(url)
    allowed_hosts = set(normalize_provider_hosts(allow_provider_host))
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
    if require_allowlisted_remote_host and parsed.hostname not in LOCAL_HOSTS and not allowed_hosts:
        raise ValueError("Remote binary provider usage requires --allow-provider-host")
    if allowed_hosts and parsed.hostname.lower() not in allowed_hosts:
        raise ValueError(
            f"Manifest URL host '{parsed.hostname}' does not match allowlisted host --allow-provider-host '{canonical_provider_host_value(allow_provider_host)}'"
        )
    return url


def _validated_url(value: Any, allow_provider_host: str | list[str] | None, *, require_allowlisted_remote_host: bool = True) -> str:
    url = _validated_text(value, "url")
    parsed = urllib.parse.urlparse(url)
    allowed_hosts = set(normalize_provider_hosts(allow_provider_host))
    if parsed.scheme == "file":
        raise ValueError("file:// binary URLs are not supported")
    if parsed.scheme != "https":
        if parsed.scheme == "http" and (parsed.hostname or "") in LOCAL_HOSTS:
            pass
        else:
            raise ValueError("Binary URL must use HTTPS unless it targets localhost test fixtures")
    if not parsed.hostname:
        raise ValueError("Binary URL host is required")
    if allowed_hosts:
        if parsed.hostname.lower() not in allowed_hosts:
            raise ValueError(
                f"Binary URL host '{parsed.hostname}' does not match allowlisted host --allow-provider-host '{canonical_provider_host_value(allow_provider_host)}'"
            )
    elif require_allowlisted_remote_host and parsed.hostname not in LOCAL_HOSTS:
        raise ValueError("Remote binary URLs require --allow-provider-host")
    if ".." in parsed.path.split("/"):
        raise ValueError("Path traversal blocked in binary URL")
    return url


def normalize_provider_hosts(value: str | list[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    raw_values = value if isinstance(value, list) else [value]
    hosts: list[str] = []
    for raw in raw_values:
        for token in raw.split(","):
            candidate = token.strip().lower()
            if not candidate:
                continue
            if any(ch in candidate for ch in "/\\:"):
                raise ValueError(f"Invalid provider host value: {token!r}")
            if ".." in candidate:
                raise ValueError(f"Path traversal blocked in provider host value: {token!r}")
            if candidate not in hosts:
                hosts.append(candidate)
    return tuple(hosts)


def canonical_provider_host_value(value: str | list[str] | None) -> str:
    hosts = normalize_provider_hosts(value)
    return ",".join(hosts)


def _validated_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Manifest field '{label}' is required")
    text = value.strip()
    if "/" in text or "\\" in text or ".." in text:
        if label not in {"version", "generated_at", "provider", "url"}:
            raise ValueError(f"Path traversal blocked in manifest field '{label}'")
    return text


def _validated_filename(value: Any) -> str:
    filename = _validated_text(value, "filename")
    if "/" in filename or "\\" in filename or filename in {".", ".."}:
        raise ValueError(f"Path traversal blocked in manifest field 'filename'")
    return filename


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


def _validated_repo_slug(value: str) -> str:
    parts = [part.strip() for part in value.split("/") if part.strip()]
    if len(parts) != 2:
        raise ValueError("Repository slug must look like <OWNER>/<REPO>")
    for part in parts:
        if ".." in part or any(ch in part for ch in "\\:"):
            raise ValueError("Repository slug contains invalid characters")
    return "/".join(parts)


def _validated_release_tag(value: str) -> str:
    tag = _validated_text(value, "release_tag")
    if "/" in tag or "\\" in tag:
        raise ValueError("Release tag must not contain path separators")
    return tag


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


def _validated_output_dir(output_dir: Path) -> Path:
    if ".." in output_dir.parts:
        raise ValueError(f"Path traversal blocked for output dir: {output_dir!r}")
    return output_dir.resolve()


def _manifest_entries_from_source(source_root: Path, base_url: str, *, versions: dict[str, str] | None = None) -> tuple[ProviderBinary, ...]:
    binaries: list[ProviderBinary] = []
    for item in _source_binaries(source_root, versions=versions):
        url = _join_manifest_url(base_url, item["relative_path"])
        binaries.append(
            ProviderBinary(
                adapter=item["adapter"],
                binary_name=item["binary_name"],
                component=item["component"],
                version=item["version"],
                platform=item["platform"],
                filename=item["filename"],
                url=url,
                sha256=item["sha256"],
                size_bytes=item["size_bytes"],
            )
        )
    return tuple(binaries)


def _join_manifest_url(base_url: str, relative_path: str) -> str:
    quoted_parts = [urllib.parse.quote(part) for part in relative_path.split("/")]
    return f"{base_url}/{posixpath.join(*quoted_parts)}"


def _source_binaries(source_root: Path, versions: dict[str, str] | None = None) -> list[dict[str, Any]]:
    binaries: list[dict[str, Any]] = []
    version_map = versions or {}
    for path in sorted(source_root.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(source_root)
        if relative.as_posix() == "pilottunnel-source-summary.json":
            continue
        if path.is_symlink():
            resolved = path.resolve()
            if not _is_relative_to(resolved, source_root):
                raise ValueError(f"Symlink escape blocked for source path: {path}")
        resolved_path = path.resolve()
        if not _is_relative_to(resolved_path, source_root):
            raise ValueError(f"Source file escapes source dir: {path}")
        if len(relative.parts) != 3:
            raise ValueError(f"Unknown file layout under source dir: {relative.as_posix()}")
        adapter_name, platform_id, filename = relative.parts
        spec = binary_spec(adapter_name)
        if spec.coverage != "provider_required":
            raise ValueError(f"Adapter '{adapter_name}' is not eligible for provider manifest generation")
        if platform_id not in spec.supported_platforms:
            raise ValueError(f"Unsupported platform '{platform_id}' for adapter '{adapter_name}'")
        component = _source_component_for_filename(adapter_name, filename)
        data = resolved_path.read_bytes()
        binaries.append(
            {
                "adapter": adapter_name,
                "binary_name": component,
                "component": component,
                "platform": platform_id,
                "filename": filename,
                "version": version_map.get(adapter_name, "provider-current"),
                "sha256": _sha256_bytes(data),
                "size_bytes": len(data),
                "source_path": str(resolved_path),
                "relative_path": relative.as_posix(),
            }
        )
    return binaries


def _parse_version_overrides(values: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for raw in values:
        for token in raw.split(","):
            candidate = token.strip()
            if not candidate:
                continue
            if "=" not in candidate:
                raise ValueError("Provider release versions must use adapter=version format")
            adapter_name, version = candidate.split("=", 1)
            adapter_key = adapter_name.strip()
            version_value = version.strip()
            binary_spec(adapter_key)
            if not version_value:
                raise ValueError(f"Version override cannot be empty for adapter '{adapter_key}'")
            overrides[adapter_key] = version_value
    return overrides


def _release_entries_from_source(
    *,
    source_root: Path,
    release_base_url: str,
    versions: dict[str, str],
    output_dir: Path,
) -> tuple[list[ProviderBinary], list[dict[str, Any]]]:
    items = _source_binaries(source_root, versions=versions)
    adapters_present = {item["adapter"] for item in items}
    missing_versions = sorted(adapter for adapter in adapters_present if adapter not in versions)
    if missing_versions:
        raise ValueError(f"Release asset preparation requires pinned --version adapter=version for {missing_versions}")
    entries: list[ProviderBinary] = []
    copies: list[dict[str, Any]] = []
    for item in items:
        release_filename = _normalized_release_filename(
            adapter=item["adapter"],
            platform_id=item["platform"],
            version=item["version"],
            source_filename=item["filename"],
        )
        output_path = (output_dir / release_filename).resolve()
        if output_dir not in output_path.parents:
            raise ValueError(f"Refusing to write outside output dir: {output_path}")
        entry = ProviderBinary(
            adapter=item["adapter"],
            binary_name=item["binary_name"],
            component=item["component"],
            version=item["version"],
            platform=item["platform"],
            filename=release_filename,
            url=_join_manifest_url(release_base_url, release_filename),
            sha256=item["sha256"],
            size_bytes=item["size_bytes"],
        )
        entries.append(entry)
        copies.append(
            {
                "adapter": item["adapter"],
                "component": item["component"],
                "platform": item["platform"],
                "version": item["version"],
                "source_path": item["source_path"],
                "output_path": str(output_path),
                "filename": release_filename,
            }
        )
    return entries, copies


def _normalized_release_filename(*, adapter: str, platform_id: str, version: str, source_filename: str) -> str:
    safe_version = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in version)
    component = _source_component_for_filename(adapter, source_filename)
    if len(binary_components(adapter)) > 1:
        return f"{adapter}-{component}-{platform_id}-{safe_version}-{source_filename}"
    return f"{adapter}-{platform_id}-{safe_version}-{source_filename}"


def _github_release_base_url(repo_slug: str, release_tag: str) -> str:
    owner, repo = repo_slug.split("/", 1)
    return (
        f"https://github.com/{urllib.parse.quote(owner)}/{urllib.parse.quote(repo)}/releases/download/"
        f"{urllib.parse.quote(release_tag)}"
    )


def _recommended_provider_hosts(base_url: str) -> tuple[str, ...]:
    host = urllib.parse.urlparse(base_url).hostname or ""
    if host == "github.com":
        return GITHUB_PROVIDER_HOSTS
    return (host,)


def _duplicate_manifest_keys(entries: tuple[ProviderBinary, ...]) -> list[dict[str, str]]:
    duplicates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for entry in entries:
        key = (entry.adapter, entry.component, entry.platform, entry.version)
        if key in seen:
            duplicates.append(
                {
                    "adapter": entry.adapter,
                    "component": entry.component,
                    "platform": entry.platform,
                    "version": entry.version,
                }
            )
            continue
        seen.add(key)
    return duplicates


def _missing_required_entries(
    entries: tuple[ProviderBinary, ...],
    *,
    required_platforms: tuple[str, ...] | None = None,
    required_adapters: tuple[str, ...] | None = None,
) -> list[dict[str, str]]:
    present = {(entry.adapter, entry.component, entry.platform) for entry in entries}
    missing: list[dict[str, str]] = []
    adapters = required_adapters or provider_required_adapters()
    for adapter_name in adapters:
        spec = binary_spec(adapter_name)
        for component in binary_components(adapter_name):
            for platform_id in spec.supported_platforms:
                if required_platforms and platform_id not in required_platforms:
                    continue
                if (adapter_name, component, platform_id) not in present:
                    item = {"adapter": adapter_name, "platform": platform_id}
                    if len(binary_components(adapter_name)) > 1:
                        item["component"] = component
                    missing.append(item)
    return missing


def _verification_platforms(
    entries: tuple[ProviderBinary, ...],
    *,
    requested_platforms: tuple[str, ...] | None,
) -> tuple[str, ...]:
    if requested_platforms:
        normalized: list[str] = []
        for platform_id in requested_platforms:
            resolved = resolve_platform_id(platform_id)
            if resolved not in normalized:
                normalized.append(resolved)
        return tuple(normalized)
    return tuple(sorted({entry.platform for entry in entries}))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_json_url(url: str, *, allowed_hosts: set[str] | None = None) -> Any:
    data = _read_bytes_url(url, allowed_hosts=allowed_hosts)
    return json.loads(data.decode("utf-8"))


def _read_bytes_url(url: str, *, allowed_hosts: set[str] | None = None) -> bytes:
    opener = urllib.request.build_opener(_NoRedirectHandler)
    current_url = url
    for _ in range(6):
        request = urllib.request.Request(current_url, method="GET")
        try:
            with opener.open(request, timeout=10) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in {301, 302, 303, 307, 308}:
                raise
            location = exc.headers.get("Location") if exc.headers else None
            if not location:
                raise ValueError(f"Redirect response did not include a Location header for {current_url}")
            redirect_url = urllib.parse.urljoin(current_url, location)
            redirect_host = urllib.parse.urlparse(redirect_url).hostname or ""
            if allowed_hosts and redirect_host.lower() not in allowed_hosts and redirect_host.lower() not in LOCAL_HOSTS:
                raise ValueError(f"Redirect host '{redirect_host}' is not allowlisted by --allow-provider-host")
            current_url = redirect_url
    raise ValueError(f"Too many redirects while downloading {url}")


def _sha256_bytes(data: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(data)
    return digest.hexdigest()


def _audit(action: str, profile: str, details: dict[str, Any], path: Path) -> None:
    write_audit_log(action, profile, details, path)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _url_filename(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Manifest field 'url' is required")
    filename = posixpath.basename(urllib.parse.urlparse(value).path)
    if not filename:
        raise ValueError("Manifest URL must end with a filename")
    return filename


def _validated_component(adapter: str, component_value: Any, binary_name_value: Any) -> str:
    if isinstance(component_value, str) and component_value.strip():
        return normalize_binary_component(adapter, component_value.strip())
    binary_name = _validated_text(binary_name_value, "binary_name")
    for component in binary_components(adapter):
        if binary_name == component:
            return component
    if len(binary_components(adapter)) == 1:
        return primary_binary_component(adapter)
    raise ValueError(f"Manifest field 'component' is required for adapter '{adapter}'")


def _source_component_for_filename(adapter: str, filename: str) -> str:
    for component in binary_components(adapter):
        if filename == component:
            return component
    raise ValueError(f"Unexpected binary name '{filename}' for adapter '{adapter}'")
