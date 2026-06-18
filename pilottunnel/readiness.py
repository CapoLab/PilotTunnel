"""Read-only server readiness reporting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .binaries import get_binary_plan
from .config import AppConfig, Profile, validate_profile_name
from .healthcheck import run_profile_healthchecks, summarize_healthchecks
from .install_plan import build_install_plan
from .node_role import node_status_payload
from .registry import PortRegistry, RegistryEntry
from .preflight import run_preflight
from .service_lifecycle import build_service_plan
from .state import AppState
from .switch_engine import SwitchPaths
from .adapters import ADAPTERS


READINESS_LEVELS = (
    "not_initialized",
    "config_only",
    "staged_ready",
    "install_plan_ready",
    "service_plan_ready",
    "blocked",
)


def build_readiness_report(
    *,
    config: AppConfig,
    state: AppState,
    registry,
    config_path: Path,
    switch_paths: SwitchPaths,
    profile_name: str | None = None,
    adapter_name: str | None = None,
    transport: str | None = None,
    staging_root: Path | None = None,
    install_root: Path | None = None,
) -> dict[str, Any]:
    profile = _resolve_profile(config, profile_name)
    profile_exists = profile is not None
    node_status = node_status_payload(config, str(config_path))
    node_role = config.node.normalized_role or ""
    role_initialized = config.node.initialized

    resolved_adapter, resolved_transport = _resolve_adapter_transport(config, state, profile, adapter_name, transport)
    staging_target = _safe_path(staging_root or switch_paths.staging_root, "staging-root")
    install_target = _safe_path(install_root, "install-root") if install_root else None

    preflight = run_preflight(staging_target, profile, probe_write=False).to_dict()
    warnings = list(preflight.get("warnings", []))
    blockers: list[str] = []

    if not role_initialized:
        blockers.append("Node role is not initialized")

    if profile_name and profile is None:
        blockers.append(f"Profile '{profile_name}' not found")

    registry_conflicts = _registry_issues(config, state, registry)
    for conflict in registry_conflicts:
        blockers.append(conflict)

    binary_plan: dict[str, Any] | None = None
    if resolved_adapter:
        binary_plan = _binary_status(resolved_adapter, switch_paths.work_dir, state, blockers, warnings)
    elif adapter_name:
        blockers.append("Transport or profile context is required to evaluate adapter readiness")

    install_plan: dict[str, Any] | None = None
    service_plan: dict[str, Any] | None = None
    staged_files_summary: dict[str, Any] = {
        "exists": False,
        "count": 0,
        "missing": [],
        "paths": [],
    }

    if profile is not None and resolved_adapter and resolved_transport:
        install_plan = _safe_install_plan(
            profile=profile,
            adapter_name=resolved_adapter,
            transport=resolved_transport,
            role=node_role or profile.role,
            paths=switch_paths,
            state=state,
            install_root=install_target,
            preflight=preflight,
            blockers=blockers,
            warnings=warnings,
        )
        service_plan = _safe_service_plan(
            profile=profile,
            adapter_name=resolved_adapter,
            transport=resolved_transport,
            role=node_role or profile.role,
            paths=switch_paths,
            state=state,
            install_root=install_target,
            blockers=blockers,
            warnings=warnings,
        )
        staged_files_summary = _staged_files_summary(install_plan)

    healthchecks = _healthcheck_summary(profile, node_role=node_role, role_initialized=role_initialized)
    if healthchecks["status"] == "failed":
        blockers.extend(healthchecks.get("blockers", []))

    if binary_plan and binary_plan["status"] == "missing":
        blockers.append(f"Imported binary missing for adapter '{binary_plan['adapter']}'")

    if install_plan and not staged_files_summary["exists"]:
        blockers.append("Staged files are missing")

    if install_plan is None and profile_exists and resolved_adapter and resolved_transport:
        blockers.append("Install plan could not be generated")

    if service_plan is None and profile_exists and resolved_adapter and resolved_transport:
        blockers.append("Service plan could not be generated")

    readiness_level = _readiness_level(
        role_initialized=role_initialized,
        profile_exists=profile_exists,
        staged_files_exist=staged_files_summary["exists"],
        install_plan_available=install_plan is not None,
        service_plan_available=service_plan is not None,
        blocked=bool(blockers),
    )

    recommended_next_steps = _recommended_next_steps(
        readiness_level=readiness_level,
        role=node_role,
        profile=profile,
        adapter_name=resolved_adapter,
        transport=resolved_transport,
        blockers=blockers,
    )

    ok = readiness_level == "service_plan_ready" and not blockers
    report = {
        "ok": ok,
        "readiness_level": readiness_level,
        "role_initialized": role_initialized,
        "node_role": node_role,
        "platform": preflight["host"]["platform"],
        "systemd_available": preflight["systemd_available"],
        "required_commands": {item["name"]: item for item in preflight["commands"]},
        "binary_imported": bool(binary_plan and binary_plan["imported"]),
        "profile_exists": profile_exists,
        "staged_files_exist": staged_files_summary["exists"],
        "install_plan_available": install_plan is not None,
        "service_plan_available": service_plan is not None,
        "healthchecks": healthchecks,
        "blockers": blockers,
        "warnings": _dedupe(warnings),
        "recommended_next_steps": recommended_next_steps,
        "profile": profile.name if profile else profile_name,
        "adapter": resolved_adapter,
        "transport": resolved_transport,
        "node_status": node_status,
        "preflight": preflight,
        "binary": binary_plan,
        "install_plan": install_plan,
        "service_plan": service_plan,
        "staged_files": staged_files_summary,
        "real_systemd_touched": False,
        "firewall_touched": False,
        "routes_touched": False,
        "services_started": False,
        "downloads_performed": False,
    }
    return report


def _resolve_profile(config: AppConfig, profile_name: str | None) -> Profile | None:
    if not profile_name:
        return None
    normalized = validate_profile_name(profile_name)
    for profile in config.profiles:
        if profile.name == normalized:
            return profile
    return None


def _resolve_adapter_transport(
    config: AppConfig,
    state: AppState,
    profile: Profile | None,
    adapter_name: str | None,
    transport: str | None,
) -> tuple[str | None, str | None]:
    resolved_adapter = adapter_name or (profile.active_adapter if profile else "") or ""
    resolved_transport = transport or (profile.active_transport if profile else "") or ""
    if profile and not resolved_adapter:
        record = state.profiles.get(profile.name)
        resolved_adapter = record.active_adapter if record else ""
    if profile and not resolved_transport:
        record = state.profiles.get(profile.name)
        resolved_transport = record.active_transport if record else ""

    if not resolved_adapter:
        return None, None
    if resolved_adapter not in ADAPTERS:
        raise KeyError(f"Unknown adapter '{resolved_adapter}'")

    metadata = ADAPTERS[resolved_adapter]().metadata()
    if resolved_transport and resolved_transport not in metadata.all_transports():
        raise ValueError(f"Transport '{resolved_transport}' is not supported by adapter '{resolved_adapter}'")
    if resolved_transport and resolved_transport in metadata.experimental_transports:
        raise ValueError(f"Transport '{resolved_transport}' is blocked in v0.1 for adapter '{resolved_adapter}'")
    if not resolved_transport:
        return resolved_adapter, None
    return resolved_adapter, resolved_transport


def _binary_status(
    adapter_name: str,
    work_dir: Path,
    state: AppState,
    blockers: list[str],
    warnings: list[str],
) -> dict[str, Any]:
    try:
        plan = get_binary_plan(adapter_name, work_dir, state)
    except KeyError as exc:
        blockers.append(str(exc))
        return {"ok": False, "adapter": adapter_name, "imported": False, "status": "missing", "error": str(exc)}

    status = plan.get("install_status")
    coverage = plan.get("coverage", "")
    imported = status == "imported"
    if status == "system_dependency":
        if not plan.get("system_command_available"):
            blockers.append(f"System dependency '{plan.get('system_command')}' is missing for adapter '{adapter_name}'")
        else:
            warnings.append(f"Adapter '{adapter_name}' uses system dependency '{plan.get('system_command')}'")
    elif status in {"template_only", "listed_only"}:
        warnings.append(f"Adapter '{adapter_name}' is categorized as '{status}'")
    elif not imported:
        warnings.append(f"Binary for adapter '{adapter_name}' is '{coverage or status}' and not imported yet")
    return {
        "ok": imported or status == "system_dependency",
        "adapter": adapter_name,
        "imported": imported,
        "status": status,
        "coverage": coverage,
        "binary_name": plan.get("binary_name"),
        "expected_cache_path": plan.get("expected_cache_path"),
        "expected_bin_path": plan.get("expected_bin_path"),
        "source_type": plan.get("source_type"),
        "version": plan.get("version"),
        "checksum": plan.get("checksum"),
        "imported_path": plan.get("imported_path"),
        "supported_platforms": plan.get("supported_platforms"),
        "download_performed": plan.get("download_performed", False),
        "system_command": plan.get("system_command"),
        "system_command_available": plan.get("system_command_available", False),
    }


def _safe_install_plan(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str,
    paths: SwitchPaths,
    state: AppState,
    install_root: Path | None,
    preflight: dict[str, Any],
    blockers: list[str],
    warnings: list[str],
) -> dict[str, Any] | None:
    try:
        plan = build_install_plan(
            profile=profile,
            adapter_name=adapter_name,
            transport=transport,
            role=role,
            paths=paths,
            state=state,
            install_root=install_root,
            preflight=preflight,
        )
    except (KeyError, ValueError) as exc:
        blockers.append(str(exc))
        return None
    warnings.extend(plan.get("warnings", []))
    return plan


def _safe_service_plan(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str,
    paths: SwitchPaths,
    state: AppState,
    install_root: Path | None,
    blockers: list[str],
    warnings: list[str],
) -> dict[str, Any] | None:
    try:
        plan = build_service_plan(
            profile=profile,
            adapter_name=adapter_name,
            transport=transport,
            action="start",
            role=role,
            paths=paths,
            state=state,
            install_root=install_root,
        )
    except (KeyError, ValueError) as exc:
        blockers.append(str(exc))
        return None
    warnings.extend(plan.get("warnings", []))
    return plan


def _staged_files_summary(install_plan: dict[str, Any]) -> dict[str, Any]:
    files = list(install_plan.get("source_staged_files", []))
    missing = [item["path"] for item in files if not item.get("exists")]
    return {
        "exists": not missing and bool(files),
        "count": len(files),
        "missing": missing,
        "paths": [item["path"] for item in files],
    }


def _healthcheck_summary(profile: Profile | None, *, node_role: str, role_initialized: bool) -> dict[str, Any]:
    if profile is None or not role_initialized:
        return {"status": "skipped", "ok": False, "results": [], "blockers": [], "warnings": []}
    results = run_profile_healthchecks(profile=profile, node_role=node_role or profile.role, timeout=2.0, include_all=False, role_aware=True)
    summary = summarize_healthchecks(results, profile=profile.name, role=node_role or profile.role)
    status = "ok" if summary["ok"] else "failed"
    blockers = [] if summary["ok"] else [f"TCP healthcheck failed for profile '{profile.name}'"]
    warnings = [] if summary["ok"] else [f"TCP healthcheck failed for profile '{profile.name}'"]
    return {
        "status": status,
        "ok": summary["ok"],
        "results": results,
        "summary": summary,
        "blockers": blockers,
        "warnings": warnings,
    }


def _readiness_level(
    *,
    role_initialized: bool,
    profile_exists: bool,
    staged_files_exist: bool,
    install_plan_available: bool,
    service_plan_available: bool,
    blocked: bool,
) -> str:
    if not role_initialized:
        return "not_initialized"
    if blocked:
        return "blocked"
    if service_plan_available:
        return "service_plan_ready"
    if install_plan_available:
        return "install_plan_ready"
    if staged_files_exist:
        return "staged_ready"
    if profile_exists:
        return "config_only"
    return "config_only"


def _recommended_next_steps(
    *,
    readiness_level: str,
    role: str,
    profile: Profile | None,
    adapter_name: str | None,
    transport: str | None,
    blockers: list[str],
) -> list[str]:
    if readiness_level == "not_initialized":
        return [
            "python -m pilottunnel.cli init --role controller",
            "python -m pilottunnel.cli init --role worker",
        ]
    if readiness_level == "blocked":
        return ["Fix blockers and rerun: python -m pilottunnel.cli readiness report"]
    if readiness_level == "config_only":
        if profile is None:
            return ["Create or select a profile, then rerun readiness report"]
        return [
            f"python -m pilottunnel.cli switch --profile {profile.name} --adapter backhaul --transport tcpmux",
            f"python -m pilottunnel.cli healthcheck --profile {profile.name} --all",
        ]
    if readiness_level == "staged_ready":
        if profile is None:
            return ["Run install plan once profile, adapter, and transport are selected"]
        return [f"python -m pilottunnel.cli install plan --profile {profile.name} --adapter {adapter_name or 'backhaul'} --transport {transport or 'tcp'}"]
    if readiness_level == "install_plan_ready":
        if profile is None:
            return ["Run service plan review once the profile is selected"]
        return [f"python -m pilottunnel.cli service plan --profile {profile.name} --adapter {adapter_name or 'backhaul'} --transport {transport or 'tcp'} --action start"]
    if readiness_level == "service_plan_ready":
        if profile is None:
            return ["Ready for future real apply review"]
        return [f"python -m pilottunnel.cli healthcheck --profile {profile.name} --all --role-aware"]
    return [f"Rerun readiness report for node role '{role}'"]


def _safe_path(path: Path | None, label: str) -> Path:
    if path is None:
        raise ValueError(f"{label} is required")
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for {label}: {path!r}")
    return path


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _registry_issues(config: AppConfig, state: AppState, registry: PortRegistry | None) -> list[str]:
    if registry is None:
        return []
    computed = PortRegistry(owners=dict(registry.owners))
    issues: list[str] = []

    for index, profile in enumerate(config.profiles):
        for other in config.profiles[index + 1 :]:
            overlap = sorted(set(profile.ports.owned_ports()) & set(other.ports.owned_ports()))
            if overlap:
                issues.append(f"Profiles '{profile.name}' and '{other.name}' conflict on declared ports {overlap}")

    for profile in config.profiles:
        record = state.profiles.get(profile.name)
        if not record or not record.active_adapter:
            continue
        if profile.name in computed.owners:
            entry = computed.owners[profile.name]
            if entry.transport != record.active_transport:
                issues.append(
                    f"State/registry mismatch for profile '{profile.name}': state transport={record.active_transport}, registry transport={entry.transport}"
                )
            continue
        try:
            computed.claim(
                RegistryEntry(
                    profile=profile.name,
                    main_port=profile.ports.main_port,
                    adapter=record.active_adapter,
                    transport=record.active_transport,
                    role=profile.role,
                    owned_ports=profile.ports.owned_ports(),
                    owned_services=[record.service_name] if record.service_name else [],
                    owned_firewall_rule_tags=[],
                    owned_routes=[],
                )
            )
        except ValueError as exc:
            issues.append(str(exc))

    issues.extend(computed.check_conflicts())
    return _dedupe(issues)
