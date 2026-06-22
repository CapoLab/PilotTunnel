"""Release-candidate validation and smoke workflow helpers."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .audit import redact_secrets
from .binaries import get_binary_plan
from .binary_install import resolve_binary_reference
from .config import AppConfig, Profile, get_profile, validate_profile_name
from .manual_switch import build_manual_switch_plan
from .registry import PortRegistry, RegistryEntry
from .service_install import build_service_install_plan
from .service_plan import build_staged_service_plan
from .state import AppState
from .switch_engine import SwitchPaths
from .systemd_control import build_reload_plan, build_start_plan, build_stop_plan
from .runtime_plan import build_runtime_plan

V0_1_LIMITATIONS = [
    "auto_switch_not_implemented",
    "background_monitoring_not_implemented",
    "ui_not_implemented",
    "layer4_tcp_only",
    "selected_adapters_only",
    "real_apply_requires_operator_confirmation",
]


def build_rc_check(
    *,
    config: AppConfig,
    state: AppState,
    registry: PortRegistry | None,
    config_path: Path,
    switch_paths: SwitchPaths,
    runtime_dir: Path,
    service_dir: Path,
    target_dir: Path,
    profile_name: str | None = None,
    target_tunnel: str | None = None,
    allow_system_dir: bool = False,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="pilottunnel-rc-check-") as scratch_root:
        scratch = Path(scratch_root)
        return _build_rc_report(
            mode="check",
            config=config,
            state=state,
            registry=registry,
            config_path=config_path,
            switch_paths=switch_paths,
            runtime_dir=scratch / "runtime",
            service_dir=scratch / "service-staging",
            target_dir=target_dir,
            profile_name=profile_name,
            target_tunnel=target_tunnel,
            allow_system_dir=allow_system_dir,
            read_only=True,
            requested_runtime_dir=runtime_dir,
            requested_service_dir=service_dir,
        )


def build_rc_smoke(
    *,
    config: AppConfig,
    state: AppState,
    registry: PortRegistry | None,
    config_path: Path,
    switch_paths: SwitchPaths,
    runtime_dir: Path,
    service_dir: Path,
    target_dir: Path,
    profile_name: str | None = None,
    target_tunnel: str | None = None,
    allow_system_dir: bool = False,
) -> dict[str, Any]:
    return _build_rc_report(
        mode="smoke",
        config=config,
        state=state,
        registry=registry,
        config_path=config_path,
        switch_paths=switch_paths,
        runtime_dir=runtime_dir,
        service_dir=service_dir,
        target_dir=target_dir,
        profile_name=profile_name,
        target_tunnel=target_tunnel,
        allow_system_dir=allow_system_dir,
        read_only=False,
        requested_runtime_dir=runtime_dir,
        requested_service_dir=service_dir,
    )


def _build_rc_report(
    *,
    mode: str,
    config: AppConfig,
    state: AppState,
    registry: PortRegistry | None,
    config_path: Path,
    switch_paths: SwitchPaths,
    runtime_dir: Path,
    service_dir: Path,
    target_dir: Path,
    profile_name: str | None,
    target_tunnel: str | None,
    allow_system_dir: bool,
    read_only: bool,
    requested_runtime_dir: Path,
    requested_service_dir: Path,
) -> dict[str, Any]:
    selected_profile = _selected_profile(config, profile_name)
    config_section = _normalized_section("config", _config_section(config, config_path, selected_profile))
    registry_section = _normalized_section("registry", _registry_section(config, state, registry))
    binary_section = _normalized_section("binaries", _binary_section(config, state, switch_paths.work_dir))
    runtime_section = _safe_payload_section(
        "runtime_plan",
        lambda: build_runtime_plan(
            config=config,
            state=state,
            runtime_dir=runtime_dir,
            requested_platform="auto",
        ),
    )

    service_render_payload: dict[str, Any]
    if runtime_section["ok"]:
        service_render_payload = build_staged_service_plan(
            config=config,
            state=state,
            runtime_dir=runtime_dir,
            service_dir=service_dir,
            requested_platform="auto",
            audit_path=switch_paths.audit_path,
            write_units=True,
        )
    else:
        service_render_payload = {}
    service_render_section = (
        _normalized_section("service_render", service_render_payload)
        if runtime_section["ok"]
        else _dependent_section("service_render", "runtime_plan", "Runtime plan must succeed before service rendering")
    )

    service_install_payload: dict[str, Any]
    if service_render_section["ok"]:
        service_install_payload = build_service_install_plan(
            config=config,
            state=state,
            runtime_dir=runtime_dir,
            service_dir=service_dir,
            target_dir=target_dir,
            requested_platform="auto",
            allow_system_dir=allow_system_dir,
            audit_path=switch_paths.audit_path,
        )
    else:
        service_install_payload = {}
    service_install_section = (
        _normalized_section("service_install_plan", service_install_payload)
        if service_render_section["ok"]
        else _dependent_section(
            "service_install_plan",
            "service_render",
            "Service render must succeed before service install planning",
        )
    )

    systemd_reload_payload = build_reload_plan(target_dir=target_dir, audit_path=switch_paths.audit_path)
    systemd_reload_section = _normalized_section("systemd_reload_plan", systemd_reload_payload)

    lifecycle_section = _lifecycle_section(service_dir, service_render_payload, switch_paths)
    manual_switch_section = _manual_switch_section(
        config=config,
        state=state,
        target_tunnel=target_tunnel,
        selected_profile=selected_profile,
        runtime_dir=runtime_dir,
        service_dir=service_dir,
        service_render_payload=service_render_payload,
        audit_path=switch_paths.audit_path,
    )

    safety_guards = {
        "ok": True,
        "start_stop_not_called": True,
        "daemon_reload_not_called": True,
        "adapter_binaries_not_executed": True,
        "state_not_mutated": True,
        "real_systemd_touched": False,
        "systemctl_executed": False,
        "firewall_touched": False,
        "routes_touched": False,
        "ports_bound": False,
        "auto_switch_implemented": False,
        "background_monitoring_implemented": False,
        "read_only": read_only,
    }

    sections = {
        "config": config_section,
        "registry": registry_section,
        "binaries": binary_section,
        "runtime_plan": runtime_section,
        "service_render": service_render_section,
        "service_install_plan": service_install_section,
        "systemd_reload_plan": systemd_reload_section,
        "lifecycle_plan": lifecycle_section,
        "manual_switch_plan": manual_switch_section,
        "safety_guards": safety_guards,
        "v0_1_limitations": {
            "ok": True,
            "status": "passed",
            "warnings": [],
            "blockers": [],
            "items": list(V0_1_LIMITATIONS),
        },
    }

    warnings = _collect_messages(sections, "warnings")
    blockers = _collect_messages(sections, "blockers")
    warnings = _dedupe(warnings)
    blockers = _dedupe(blockers)
    passed = not blockers

    components = {
        name: {
            "status": section.get("status", "passed" if section.get("ok", False) else "blocked"),
            "ok": bool(section.get("ok", False)),
            "warnings": list(section.get("warnings", [])),
            "blockers": list(section.get("blockers", [])),
            "dependent_on": section.get("dependent_on", ""),
        }
        for name, section in sections.items()
        if isinstance(section, dict) and "ok" in section
    }

    next_safe = _next_safe_hints(selected_profile, target_tunnel)
    next_real = _next_real_hints(selected_profile, target_tunnel)
    next_steps = {
        "safe": next_safe,
        "real_apply": next_real,
    }
    limitations = list(V0_1_LIMITATIONS)

    report = {
        "ok": passed,
        "status": "passed" if passed else "blocked",
        "mode": mode,
        "read_only": read_only,
        "passed": passed,
        "warnings": list(warnings),
        "blockers": list(blockers),
        "components": components,
        "component_checklist": components,
        "next_steps": next_steps,
        "next_safe_command_hints": next_safe,
        "next_real_apply_command_hints": next_real,
        "limitations": limitations,
        "v0_1_limitations": limitations,
        "auto_switch_implemented": False,
        "background_monitoring_implemented": False,
        "requested_runtime_dir": str(requested_runtime_dir),
        "requested_service_dir": str(requested_service_dir),
        "target_dir": str(target_dir),
        "sections": sections,
        "real_systemd_touched": False,
        "systemctl_executed": False,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
        "state_mutated": False,
    }
    return redact_secrets(report)


def _selected_profile(config: AppConfig, profile_name: str | None) -> Profile | None:
    if profile_name:
        return get_profile(config, validate_profile_name(profile_name))
    if len(config.profiles) == 1:
        return config.profiles[0]
    return None


def _config_section(config: AppConfig, config_path: Path, selected_profile: Profile | None) -> dict[str, Any]:
    warnings: list[str] = []
    blockers: list[str] = []
    if not config.profiles:
        blockers.append("No profiles are configured")
    if not config.node.initialized:
        warnings.append("Node role is not initialized")
    if selected_profile is None and len(config.profiles) > 1:
        warnings.append("Multiple profiles exist; rc report is validating the whole config without a selected profile")
    return {
        "ok": not blockers,
        "config_path": str(config_path),
        "profile_count": len(config.profiles),
        "node_role": config.node.normalized_role,
        "selected_profile": selected_profile.name if selected_profile else "",
        "warnings": warnings,
        "blockers": blockers,
    }


def _registry_section(config: AppConfig, state: AppState, registry: PortRegistry | None) -> dict[str, Any]:
    blockers: list[str] = []
    if registry is None:
        return {"ok": True, "warnings": ["Registry file is not initialized yet"], "blockers": []}
    computed = PortRegistry(owners=dict(registry.owners))
    for index, profile in enumerate(config.profiles):
        for other in config.profiles[index + 1 :]:
            overlap = sorted(set(profile.ports.owned_ports()) & set(other.ports.owned_ports()))
            if overlap:
                blockers.append(f"Profiles '{profile.name}' and '{other.name}' conflict on declared ports {overlap}")
    for profile in config.profiles:
        record = state.profiles.get(profile.name)
        if not record or not record.active_adapter:
            continue
        if profile.name in computed.owners:
            entry = computed.owners[profile.name]
            if entry.transport != record.active_transport:
                blockers.append(
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
            blockers.append(str(exc))
    blockers.extend(computed.check_conflicts())
    return {
        "ok": not blockers,
        "warnings": [],
        "blockers": _dedupe(blockers),
    }


def _binary_section(config: AppConfig, state: AppState, work_dir: Path) -> dict[str, Any]:
    warnings: list[str] = []
    blockers: list[str] = []
    plans: list[dict[str, Any]] = []
    adapters = sorted({profile.active_adapter for profile in config.profiles if profile.active_adapter})
    if not adapters:
        warnings.append("No adapter binaries can be checked because no profiles have an active adapter")
    for adapter in adapters:
        try:
            catalog_plan = get_binary_plan(adapter, work_dir, state)
            resolution = resolve_binary_reference(
                adapter=adapter,
                config=config,
                state=state,
                requested_platform="auto",
            )
        except KeyError as exc:
            blockers.append(str(exc))
            continue
        except ValueError as exc:
            blockers.append(str(exc))
            continue
        plan = {
            "adapter": adapter,
            "resolver_ok": bool(resolution.get("ok", False)),
            "resolved": bool(resolution.get("resolved", False)),
            "resolved_source": resolution.get("source", ""),
            "resolved_path": resolution.get("path", ""),
            "platform": resolution.get("platform", ""),
            "provider_manifest": resolution.get("provider_manifest", ""),
            "managed_install_dir": resolution.get("managed_install_dir", ""),
            "catalog_status": catalog_plan.get("install_status", ""),
            "catalog_expected_bin_path": catalog_plan.get("expected_bin_path", ""),
            "catalog_expected_cache_path": catalog_plan.get("expected_cache_path", ""),
            "required_components": catalog_plan.get("required_components", []),
            "verified_components": catalog_plan.get("verified_components", []),
            "missing_components": catalog_plan.get("missing_components", []),
            "system_command_available": catalog_plan.get("system_command_available", False),
            "message": resolution.get("message", ""),
        }
        plans.append(plan)
        if catalog_plan.get("install_status") == "partial_import":
            blockers.append("Binary resolver is not ready")
            blockers.append(
                f"Adapter '{adapter}' is missing required components: {', '.join(catalog_plan.get('missing_components', []))}"
            )
        if not resolution.get("ok"):
            blockers.append("Binary resolver is not ready")
            blockers.append(resolution.get("message", f"Binary resolver is not ready for adapter '{adapter}'"))
    return {
        "ok": not blockers,
        "adapter_count": len(plans),
        "plans": plans,
        "warnings": _dedupe(warnings),
        "blockers": _dedupe(blockers),
    }


def _normalized_section(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    blockers = list(payload.get("blockers", [])) or list(payload.get("errors", []))
    warnings = list(payload.get("warnings", []))
    status = payload.get("status")
    if not status:
        status = "passed" if payload.get("ok", False) else "blocked"
    normalized = dict(payload)
    normalized.update(
        {
            "ok": bool(payload.get("ok", False)),
            "action": payload.get("action", name),
            "status": status,
            "warnings": warnings,
            "blockers": blockers,
        }
    )
    normalized["summary"] = payload
    return normalized


def _exception_section(name: str, message: str) -> dict[str, Any]:
    payload = {
        "ok": False,
        "action": name,
        "status": "blocked",
        "warnings": [],
        "blockers": [message],
        "message": message,
        "errors": [message],
    }
    payload["summary"] = {
        "ok": False,
        "action": name,
        "status": "blocked",
        "message": message,
        "errors": [message],
        "warnings": [],
        "blockers": [message],
    }
    return payload


def _dependent_section(name: str, dependency: str, message: str) -> dict[str, Any]:
    warning = f"Skipped because '{dependency}' did not pass: {message}"
    payload = {
        "ok": False,
        "action": name,
        "status": "skipped",
        "warnings": [warning],
        "blockers": [],
        "dependent_on": dependency,
        "message": message,
        "errors": [],
    }
    payload["summary"] = {
        "ok": False,
        "action": name,
        "status": "skipped",
        "message": message,
        "dependent_on": dependency,
        "errors": [],
        "warnings": [warning],
        "blockers": [],
    }
    return payload


def _lifecycle_section(service_dir: Path, service_render_payload: dict[str, Any], switch_paths: SwitchPaths) -> dict[str, Any]:
    if not service_render_payload.get("ok"):
        return _dependent_section("lifecycle_plan", "service_render", "Service render must succeed before lifecycle planning")
    start_plan = build_start_plan(service_dir=service_dir, service_name=None, audit_path=switch_paths.audit_path)
    stop_plan = build_stop_plan(service_dir=service_dir, service_name=None, audit_path=switch_paths.audit_path)
    blockers = list(start_plan.get("errors", [])) + list(stop_plan.get("errors", []))
    warnings = list(start_plan.get("warnings", [])) + list(stop_plan.get("warnings", []))
    return {
        "ok": not blockers,
        "action": "lifecycle_plan",
        "status": "passed" if not blockers else "blocked",
        "warnings": _dedupe(warnings),
        "blockers": _dedupe(blockers),
        "summary": {
            "start_plan": start_plan,
            "stop_plan": stop_plan,
            "status_inspection": {
                "ok": True,
                "performed": False,
                "reason": "rc safe mode does not call live systemctl status",
                "managed_services": [item["service_name"] for item in service_render_payload.get("services", []) if item.get("service_name")],
            },
        },
    }


def _manual_switch_section(
    *,
    config: AppConfig,
    state: AppState,
    target_tunnel: str | None,
    selected_profile: Profile | None,
    runtime_dir: Path,
    service_dir: Path,
    service_render_payload: dict[str, Any],
    audit_path: Path,
) -> dict[str, Any]:
    if not service_render_payload.get("ok"):
        return _dependent_section("manual_switch_plan", "service_render", "Service render must succeed before manual switch planning")
    if not target_tunnel:
        return {
            "ok": True,
            "action": "manual_switch_plan",
            "status": "skipped",
            "warnings": ["No manual switch target was supplied; switch planning was skipped"],
            "blockers": [],
            "summary": {
                "ok": True,
                "status": "skipped",
                "action": "manual-switch-plan-skipped",
                "selected_profile": selected_profile.name if selected_profile else "",
            },
        }
    payload = build_manual_switch_plan(
        config=config,
        state=state,
        target_tunnel=target_tunnel,
        runtime_dir=runtime_dir,
        service_dir=service_dir,
        audit_path=audit_path,
    )
    return {
        "ok": bool(payload.get("ok", False)),
        "action": "manual_switch_plan",
        "status": "passed" if payload.get("ok", False) else "blocked",
        "warnings": list(payload.get("warnings", [])),
        "blockers": list(payload.get("errors", [])),
        "summary": payload,
    }


def _safe_payload_section(name: str, builder: Any) -> dict[str, Any]:
    try:
        return _normalized_section(name, builder())
    except (KeyError, ValueError) as exc:
        return _exception_section(name, str(exc))


def _collect_messages(sections: dict[str, Any], field: str) -> list[str]:
    messages: list[str] = []
    for section in sections.values():
        if isinstance(section, dict):
            messages.extend(section.get(field, []))
    return messages


def _next_safe_hints(selected_profile: Profile | None, target_tunnel: str | None) -> list[str]:
    hints = [
        "python -m pilottunnel.cli binary source list",
        "python -m pilottunnel.cli --config <CONFIG_FILE> readiness report",
    ]
    if selected_profile:
        hints.extend(
            [
                f"python -m pilottunnel.cli --config <CONFIG_FILE> runtime plan --runtime-dir <RUNTIME_DIR>",
                f"python -m pilottunnel.cli --config <CONFIG_FILE> service render --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR>",
                f"python -m pilottunnel.cli --config <CONFIG_FILE> service install plan --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <TARGET_SYSTEMD_DIR>",
            ]
        )
    if target_tunnel:
        hints.append(
            f"python -m pilottunnel.cli --config <CONFIG_FILE> switch plan --target {target_tunnel} --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR>"
        )
    hints.append(
        "python -m pilottunnel.cli --config <CONFIG_FILE> rc check --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --target-dir <TARGET_SYSTEMD_DIR>"
    )
    return hints


def _next_real_hints(selected_profile: Profile | None, target_tunnel: str | None) -> list[str]:
    hints = [
        "python -m pilottunnel.cli systemd reload apply --target-dir <TARGET_SYSTEMD_DIR> --confirm SYSTEMD_DAEMON_RELOAD",
        "python -m pilottunnel.cli systemd start apply --service-dir <SERVICE_STAGING_DIR> --confirm START_PILOTTUNNEL_SERVICES",
        "python -m pilottunnel.cli systemd stop apply --service-dir <SERVICE_STAGING_DIR> --confirm STOP_PILOTTUNNEL_SERVICES",
    ]
    if target_tunnel:
        hints.append(
            f"python -m pilottunnel.cli --config <CONFIG_FILE> switch apply --target {target_tunnel} --runtime-dir <RUNTIME_DIR> --service-dir <SERVICE_STAGING_DIR> --confirm SWITCH_PILOTTUNNEL_TUNNEL"
        )
    return hints


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
