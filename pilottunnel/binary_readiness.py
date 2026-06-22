"""Strict binary readiness checks for the v0.1 Layer 4 workflow."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any

from .config import AppConfig
from .binaries import all_binary_adapters, binary_components, binary_record_key, binary_spec, provider_required_adapters
from .binary_provider import load_manifest, resolve_platform_id, select_manifest_binary
from .state import AppState


def build_binary_readiness_report(
    *,
    cache_root: Path,
    state: AppState,
    manifest_url: str | None,
    manifest_file: Path | None,
    allow_provider_host: str | None,
    requested_platform: str | None = None,
    require_all: bool = False,
) -> dict[str, Any]:
    platform_id = resolve_platform_id(requested_platform)
    if require_all and not manifest_url and not manifest_file:
        raise ValueError("binary status --require-all requires --manifest-url or --manifest-file")
    if require_all and manifest_url and not allow_provider_host:
        raise ValueError("binary status --require-all with --manifest-url requires --allow-provider-host")

    manifest = None
    if manifest_url or manifest_file:
        manifest = load_manifest(
            manifest_url=manifest_url,
            manifest_file=manifest_file,
            allow_provider_host=allow_provider_host,
        )

    warnings: list[str] = []
    blockers: list[str] = []
    results: list[dict[str, Any]] = []
    required_adapters = list(provider_required_adapters())

    for adapter_name in all_binary_adapters():
        spec = binary_spec(adapter_name)
        if spec.coverage == "provider_required":
            result = _provider_binary_result(
                adapter_name=adapter_name,
                state=state,
                platform_id=platform_id,
                manifest=manifest,
                required=require_all,
            )
        elif spec.coverage == "system_dependency":
            result = _system_dependency_result(adapter_name)
        else:
            result = _not_required_result(adapter_name)

        warnings.extend(result.get("warnings", []))
        if result.get("required_for_v0_1"):
            blockers.extend(result.get("blockers", []))
        results.append(result)

    payload = {
        "ok": not blockers,
        "action": "binary-status",
        "require_all": require_all,
        "platform": platform_id,
        "required_adapters": required_adapters,
        "manifest_source": manifest.source if manifest else "",
        "manifest_provider": manifest.provider if manifest else "",
        "allow_provider_host": allow_provider_host or "",
        "results": results,
        "verified_adapters": [item["adapter"] for item in results if item.get("required_for_v0_1") and item.get("verified")],
        "missing_adapters": [item["adapter"] for item in results if item.get("required_for_v0_1") and not item.get("verified")],
        "warnings": _dedupe(warnings),
        "blockers": _dedupe(blockers),
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }
    return payload


def binary_readiness_failure_message(payload: dict[str, Any]) -> str:
    blockers = [item for item in payload.get("blockers", []) if item]
    if not blockers:
        return "Binary readiness is incomplete"
    return "Binary readiness is incomplete: " + "; ".join(blockers)


def remember_binary_provider_source(
    config: AppConfig,
    *,
    manifest_url: str | None,
    manifest_file: Path | None,
    allow_provider_host: str | None,
) -> None:
    if manifest_url:
        config.binary_resolution.provider_manifest = manifest_url
    elif manifest_file:
        config.binary_resolution.provider_manifest = str(manifest_file)
    if allow_provider_host:
        config.binary_resolution.provider_allow_host = allow_provider_host


def _provider_binary_result(
    *,
    adapter_name: str,
    state: AppState,
    platform_id: str,
    manifest: Any,
    required: bool,
) -> dict[str, Any]:
    spec = binary_spec(adapter_name)
    warnings: list[str] = []
    blockers: list[str] = []
    component_results: list[dict[str, Any]] = []
    verified_components: list[str] = []
    missing_components: list[str] = []

    if platform_id not in spec.supported_platforms:
        if required:
            blockers.append(f"Adapter '{adapter_name}' is not available for platform '{platform_id}'")
        return {
            "adapter": adapter_name,
            "coverage": spec.coverage,
            "required_for_v0_1": required,
            "status": "unsupported_platform",
            "verified": False,
            "platform": platform_id,
            "manifest_present": False,
            "warnings": warnings,
            "blockers": blockers,
        }

    if manifest is None and required:
        blockers.append(f"Binary readiness requires a provider manifest entry for adapter '{adapter_name}'")
        return {
            "adapter": adapter_name,
            "coverage": spec.coverage,
            "required_for_v0_1": True,
            "status": "manifest_required",
            "verified": False,
            "platform": platform_id,
            "manifest_present": False,
            "warnings": warnings,
            "blockers": blockers,
        }

    for component in binary_components(adapter_name):
        result = _provider_component_result(
            adapter_name=adapter_name,
            component=component,
            state=state,
            platform_id=platform_id,
            manifest=manifest,
            required=required,
            spec=spec,
        )
        component_results.append(result)
        warnings.extend(result.get("warnings", []))
        blockers.extend(result.get("blockers", []))
        if result.get("verified"):
            verified_components.append(component)
        else:
            missing_components.append(component)

    return {
        "adapter": adapter_name,
        "coverage": spec.coverage,
        "required_for_v0_1": required,
        "status": ("verified" if required else "imported") if not missing_components else "component_incomplete",
        "verified": not missing_components,
        "platform": platform_id,
        "manifest_present": all(item.get("manifest_present", False) for item in component_results),
        "warnings": warnings,
        "blockers": blockers,
        "required_components": list(binary_components(adapter_name)),
        "verified_components": verified_components,
        "missing_components": missing_components,
        "components": component_results,
    }


def _system_dependency_result(adapter_name: str) -> dict[str, Any]:
    spec = binary_spec(adapter_name)
    available = bool(shutil.which(spec.system_command))
    warnings: list[str] = []
    if not available:
        warnings.append(f"System dependency '{spec.system_command}' is not available on PATH")
    return {
        "adapter": adapter_name,
        "coverage": spec.coverage,
        "required_for_v0_1": False,
        "status": "system_dependency",
        "verified": available,
        "system_command": spec.system_command,
        "system_command_available": available,
        "warnings": warnings,
        "blockers": [],
    }


def _provider_component_result(
    *,
    adapter_name: str,
    component: str,
    state: AppState,
    platform_id: str,
    manifest: Any,
    required: bool,
    spec: Any,
) -> dict[str, Any]:
    warnings: list[str] = []
    blockers: list[str] = []
    manifest_entry = None

    if manifest is not None:
        try:
            manifest_entry = select_manifest_binary(manifest, adapter=adapter_name, platform_id=platform_id, component=component)
        except ValueError as exc:
            if required:
                blockers.append(str(exc))
            return {
                "adapter": adapter_name,
                "component": component,
                "status": "missing_from_manifest",
                "verified": False,
                "manifest_present": False,
                "warnings": warnings,
                "blockers": blockers,
            }

    record = state.binaries.get(binary_record_key(adapter_name, component))
    if record is None:
        if required:
            if len(binary_components(adapter_name)) > 1:
                blockers.append(f"Binary component '{component}' has not been imported for adapter '{adapter_name}'")
            else:
                blockers.append(f"Binary adapter '{adapter_name}' has not been imported")
        return {
            "adapter": adapter_name,
            "component": component,
            "status": "missing_import",
            "verified": False,
            "manifest_present": manifest_entry is not None,
            "warnings": warnings,
            "blockers": blockers,
        }

    imported_path = Path(record.imported_path)
    if not imported_path.exists():
        if required:
            blockers.append(f"Imported path is missing for adapter '{adapter_name}' component '{component}'")
        return {
            "adapter": adapter_name,
            "component": component,
            "status": "missing_file",
            "verified": False,
            "manifest_present": manifest_entry is not None,
            "imported_path": record.imported_path,
            "warnings": warnings,
            "blockers": blockers,
        }

    if record.platform != platform_id:
        if required:
            blockers.append(f"Imported binary platform mismatch for adapter '{adapter_name}' component '{component}'")
        return {
            "adapter": adapter_name,
            "component": component,
            "status": "platform_mismatch",
            "verified": False,
            "manifest_present": manifest_entry is not None,
            "imported_path": record.imported_path,
            "warnings": warnings,
            "blockers": blockers,
        }

    if not record.executable or not os.access(imported_path, os.X_OK):
        if required:
            blockers.append(f"Imported binary is not executable for adapter '{adapter_name}' component '{component}'")
        return {
            "adapter": adapter_name,
            "component": component,
            "status": "not_executable",
            "verified": False,
            "manifest_present": manifest_entry is not None,
            "imported_path": record.imported_path,
            "warnings": warnings,
            "blockers": blockers,
        }

    actual_sha = _sha256_file(imported_path)
    extra_managed_paths = _extra_managed_paths(spec, imported_path)
    if manifest_entry is not None:
        if record.version != manifest_entry.version:
            if required:
                blockers.append(f"Imported binary version mismatch for adapter '{adapter_name}' component '{component}'")
            return {
                "adapter": adapter_name,
                "component": component,
                "status": "version_mismatch",
                "verified": False,
                "manifest_present": True,
                "imported_path": record.imported_path,
                "warnings": warnings,
                "blockers": blockers,
            }
        if actual_sha.lower() != manifest_entry.sha256.lower():
            if required:
                blockers.append(f"Checksum mismatch for adapter '{adapter_name}' component '{component}'")
            return {
                "adapter": adapter_name,
                "component": component,
                "status": "checksum_mismatch",
                "verified": False,
                "manifest_present": True,
                "imported_path": record.imported_path,
                "warnings": warnings,
                "blockers": blockers,
            }
        for extra_path in extra_managed_paths:
            if _sha256_file(extra_path).lower() != manifest_entry.sha256.lower():
                if required:
                    blockers.append(f"Checksum mismatch for adapter '{adapter_name}' component '{component}'")
                return {
                    "adapter": adapter_name,
                    "component": component,
                    "status": "checksum_mismatch",
                    "verified": False,
                    "manifest_present": True,
                    "imported_path": record.imported_path,
                    "warnings": warnings,
                    "blockers": blockers,
                }

    return {
        "adapter": adapter_name,
        "component": component,
        "status": "verified" if required else "imported",
        "verified": True,
        "manifest_present": manifest_entry is not None,
        "imported_path": record.imported_path,
        "version": record.version,
        "sha256": actual_sha,
        "source_type": record.source_type,
        "source_provider": record.source_provider,
        "provider_host": record.provider_host,
        "warnings": warnings,
        "blockers": blockers,
    }


def _not_required_result(adapter_name: str) -> dict[str, Any]:
    spec = binary_spec(adapter_name)
    return {
        "adapter": adapter_name,
        "coverage": spec.coverage,
        "required_for_v0_1": False,
        "status": "not_required_v0_1",
        "verified": False,
        "notes": spec.notes,
        "warnings": [],
        "blockers": [],
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extra_managed_paths(spec: Any, imported_path: Path) -> list[Path]:
    primary_names = {spec.binary_name, f"{spec.binary_name}.exe"}
    if imported_path.name not in primary_names:
        return []
    legacy_path = imported_path.parent / spec.binary_name
    if legacy_path == imported_path or not legacy_path.exists():
        return []
    return [legacy_path]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        output.append(item)
    return output
