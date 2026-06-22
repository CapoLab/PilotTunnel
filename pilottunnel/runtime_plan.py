"""Read-only adapter runtime planning."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .adapters import ADAPTERS
from .adapters.base import AdapterContext
from .audit import redact_secrets
from .binary_install import resolve_binary_reference
from .config import AppConfig, Profile, canonical_runtime_role
from .state import AppState

SUPPORTED_RUNTIME_ADAPTERS = {"rathole", "frp", "gost", "chisel"}
SUPPORTED_RUNTIME_TRANSPORTS = {
    "rathole": {"tcp"},
    "frp": {"tcp"},
    "gost": {"tcp"},
    "chisel": {"tcp"},
}


def build_runtime_plan(
    *,
    config: AppConfig,
    state: AppState,
    runtime_dir: Path,
    requested_platform: str | None = None,
) -> dict[str, Any]:
    resolved_runtime_dir = _validated_runtime_dir(runtime_dir)
    role_assignments = _assign_runtime_roles(config, state)
    tunnels: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []

    for profile in config.profiles:
        assignment = role_assignments[profile.name]
        tunnel = _profile_runtime_plan(
            profile=profile,
            state=state,
            config=config,
            runtime_dir=resolved_runtime_dir,
            requested_platform=requested_platform,
            assigned_role=assignment,
        )
        tunnels.append(tunnel)
        warnings.extend(tunnel.get("warnings", []))
        errors.extend(tunnel.get("errors", []))

    return {
        "ok": not errors,
        "action": "runtime-plan",
        "runtime_dir": str(resolved_runtime_dir),
        "platform": tunnels[0]["platform"] if tunnels else "",
        "active_tunnels": [item["tunnel_id"] for item in tunnels if item["role"] == "active"],
        "hot_standby_tunnels": [item["tunnel_id"] for item in tunnels if item["role"] == "hot_standby"],
        "config_only_tunnels": [item["tunnel_id"] for item in tunnels if item["role"] == "config_only"],
        "tunnels": tunnels,
        "warnings": sorted(set(filter(None, warnings))),
        "errors": sorted(set(filter(None, errors))),
        "dry_run": True,
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def _assign_runtime_roles(config: AppConfig, state: AppState) -> dict[str, str]:
    assignments: dict[str, str] = {}
    explicit_active: list[str] = []
    inferred_active: list[str] = []
    hot_standby: list[str] = []

    for profile in config.profiles:
        selected_adapter, selected_transport = _selected_target(profile, state)
        runtime_role = canonical_runtime_role(profile.runtime_role)
        if runtime_role:
            assignments[profile.name] = runtime_role
            if runtime_role == "active":
                explicit_active.append(profile.name)
            elif runtime_role == "hot_standby":
                hot_standby.append(profile.name)
            continue
        if selected_adapter and selected_transport:
            inferred_active.append(profile.name)
            assignments[profile.name] = "active"
        else:
            assignments[profile.name] = "config_only"

    active_names = explicit_active or inferred_active
    if len(active_names) != 1:
        raise ValueError("Runtime planning requires exactly one active tunnel")
    active_name = active_names[0]
    for name in list(assignments):
        if assignments[name] == "active" and name != active_name:
            if name in explicit_active or explicit_active:
                raise ValueError("Only one tunnel can be marked active")
            assignments[name] = "config_only"
    assignments[active_name] = "active"
    if len([name for name, role in assignments.items() if role == "active"]) != 1:
        raise ValueError("Only one tunnel can be active")
    if len([name for name, role in assignments.items() if role == "hot_standby"]) > 2:
        raise ValueError("At most two tunnels can be marked hot_standby")
    override_target = (state.manual_active_tunnel or "").strip()
    if override_target:
        if override_target not in assignments:
            raise ValueError(f"Manual active tunnel override references unknown tunnel '{override_target}'")
        if assignments[override_target] == "config_only":
            raise ValueError(f"Manual active tunnel override requires a rendered managed tunnel: '{override_target}'")
        previous_active = next((name for name, role in assignments.items() if role == "active"), "")
        for name in list(assignments):
            if name == override_target:
                assignments[name] = "active"
            elif name == previous_active:
                assignments[name] = "hot_standby"
        if len([name for name, role in assignments.items() if role == "active"]) != 1:
            raise ValueError("Manual active tunnel override produced an invalid active tunnel set")
        if len([name for name, role in assignments.items() if role == "hot_standby"]) > 2:
            raise ValueError("Manual active tunnel override exceeds the hot_standby limit")
    return assignments


def _profile_runtime_plan(
    *,
    profile: Profile,
    state: AppState,
    config: AppConfig,
    runtime_dir: Path,
    requested_platform: str | None,
    assigned_role: str,
) -> dict[str, Any]:
    adapter_name, transport = _selected_target(profile, state)
    warnings: list[str] = []
    errors: list[str] = []
    platform = ""

    if not adapter_name or not transport:
        message = "No active adapter/transport is configured for this tunnel"
        if assigned_role == "active":
            errors.append(message)
        else:
            warnings.append(message)
        return {
            "tunnel_id": profile.name,
            "adapter": adapter_name,
            "transport": transport,
            "role": assigned_role,
            "platform": platform,
            "binary_resolution": {"ok": False, "resolved": False, "skipped": True, "message": message},
            "config_file_path": "",
            "command_argv": [],
            "healthcheck_target_summary": {},
            "redacted_environment_summary": {},
            "redacted_config_summary": "",
            "warnings": warnings,
            "errors": errors,
        }

    if adapter_name not in SUPPORTED_RUNTIME_ADAPTERS:
        errors.append(f"Runtime planning is not implemented for adapter '{adapter_name}'")
    elif transport not in SUPPORTED_RUNTIME_TRANSPORTS[adapter_name]:
        errors.append(f"Runtime planning only supports TCP transport for adapter '{adapter_name}'")

    if assigned_role == "config_only":
        return {
            "tunnel_id": profile.name,
            "adapter": adapter_name,
            "transport": transport,
            "role": assigned_role,
            "platform": platform,
            "binary_resolution": {"ok": False, "resolved": False, "skipped": True, "message": "Config-only tunnel is not prepared as runnable"},
            "config_file_path": "",
            "command_argv": [],
            "healthcheck_target_summary": _healthcheck_summary(profile),
            "redacted_environment_summary": {},
            "redacted_config_summary": "",
            "warnings": warnings,
            "errors": errors,
        }

    try:
        binary_resolution = resolve_binary_reference(
            adapter=adapter_name,
            component=_runtime_component(adapter_name, profile.role),
            config=config,
            state=state,
            requested_platform=requested_platform,
        )
        platform = binary_resolution.get("platform", "")
    except (KeyError, ValueError) as exc:
        binary_resolution = {"ok": False, "resolved": False, "message": str(exc), "source": "", "path": ""}
        errors.append(str(exc))

    if not binary_resolution.get("ok"):
        errors.append(binary_resolution.get("message", f"Binary resolution failed for adapter '{adapter_name}'"))
        return {
            "tunnel_id": profile.name,
            "adapter": adapter_name,
            "transport": transport,
            "role": assigned_role,
            "platform": platform,
            "binary_resolution": binary_resolution,
            "config_file_path": "",
            "command_argv": [],
            "healthcheck_target_summary": _healthcheck_summary(profile),
            "redacted_environment_summary": {},
            "redacted_config_summary": "",
            "warnings": warnings,
            "errors": sorted(set(errors)),
        }

    adapter = ADAPTERS[adapter_name]()
    context = AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=runtime_dir / "work" / profile.name,
        staging_root=runtime_dir / "staging-placeholder",
        apply_changes=False,
        role=profile.role,
    )
    try:
        rendered = adapter.render_runtime_plan(context, runtime_dir, binary_resolution["path"])
    except (KeyError, ValueError) as exc:
        errors.append(str(exc))
        return {
            "tunnel_id": profile.name,
            "adapter": adapter_name,
            "transport": transport,
            "role": assigned_role,
            "platform": platform,
            "binary_resolution": binary_resolution,
            "config_file_path": "",
            "command_argv": [],
            "healthcheck_target_summary": _healthcheck_summary(profile),
            "redacted_environment_summary": {},
            "redacted_config_summary": "",
            "warnings": warnings,
            "errors": sorted(set(errors)),
        }

    return {
        "tunnel_id": profile.name,
        "adapter": adapter_name,
        "transport": transport,
        "role": assigned_role,
        "platform": platform,
        "binary_resolution": binary_resolution,
        "config_file_path": rendered["config_path"],
        "command_argv": list(rendered.get("argv", [])),
        "healthcheck_target_summary": rendered.get("healthcheck_target_summary") or _healthcheck_summary(profile),
        "redacted_environment_summary": redact_secrets(rendered.get("environment", {})),
        "redacted_config_summary": _redact_text(rendered.get("content", "")),
        "warnings": warnings,
        "errors": sorted(set(errors)),
    }


def _selected_target(profile: Profile, state: AppState) -> tuple[str, str]:
    record = state.profiles.get(profile.name)
    adapter_name = ""
    transport = ""
    if record and record.active_adapter and record.active_transport:
        adapter_name = record.active_adapter
        transport = record.active_transport
    elif profile.active_adapter and profile.active_transport:
        adapter_name = profile.active_adapter
        transport = profile.active_transport
    return adapter_name, transport


def _healthcheck_summary(profile: Profile) -> dict[str, Any]:
    return {
        "target_host": profile.target_host,
        "target_port": profile.target_port,
        "main_port": profile.ports.main_port,
        "service_port": profile.ports.service_port,
        "check_port": profile.ports.check_port,
    }


def _runtime_component(adapter_name: str, role: str) -> str | None:
    if adapter_name != "frp":
        return None
    return "frps" if role == "controller" else "frpc"


def _redact_text(value: str) -> str:
    redacted_lines: list[str] = []
    for line in value.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ("token", "password", "private_key", "apikey", "api_key", "secret")):
            if "=" in line:
                key = line.split("=", 1)[0].rstrip()
                redacted_lines.append(f"{key} = ***REDACTED***")
            elif ":" in line:
                key = line.split(":", 1)[0].rstrip()
                redacted_lines.append(f"{key}: ***REDACTED***")
            else:
                redacted_lines.append("***REDACTED***")
            continue
        redacted_lines.append(line)
    return "\n".join(redacted_lines)


def _validated_runtime_dir(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for runtime dir: {path!r}")
    _validate_parent_chain(path)
    resolved = path.resolve()
    _validate_parent_chain(resolved)
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"Runtime dir must be a directory: {path}")
    resolved.mkdir(parents=True, exist_ok=True)
    _validate_parent_chain(resolved)
    return resolved


def _validate_parent_chain(path: Path) -> None:
    current = path
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError(f"Symlink escape blocked for runtime dir: {current}")
        if current.parent == current:
            return
        current = current.parent
