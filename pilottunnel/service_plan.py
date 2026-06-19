"""Staged service unit planning from runtime plans."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Any

from .audit import redact_secrets, write_audit_log
from .config import AppConfig
from .runtime_plan import build_runtime_plan
from .state import AppState
from .systemd import render_unit_file

NEXT_ACTION_HINT = "install_not_implemented"
SERVICE_UNIT_MARKER = "# Managed-by: PilotTunnel"


def build_staged_service_plan(
    *,
    config: AppConfig,
    state: AppState,
    runtime_dir: Path,
    service_dir: Path,
    requested_platform: str | None,
    audit_path: Path | None,
    write_units: bool = True,
) -> dict[str, Any]:
    resolved_service_dir = _validated_service_dir(service_dir, create=write_units)
    runtime_plan = build_runtime_plan(
        config=config,
        state=state,
        runtime_dir=runtime_dir,
        requested_platform=requested_platform,
    )
    if not runtime_plan["ok"]:
        payload = {
            "ok": False,
            "action": "service-render",
            "runtime_plan": runtime_plan,
            "message": "Runtime plan must succeed before service units can be staged",
            "service_dir": str(resolved_service_dir),
            "services": [],
            "warnings": runtime_plan.get("warnings", []),
            "errors": runtime_plan.get("errors", []),
            "dry_run": True,
            "downloads_performed": False,
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
        if audit_path is not None:
            _audit("service-render", "service-render", payload, audit_path)
        return payload

    services: list[dict[str, Any]] = []
    warnings: list[str] = list(runtime_plan.get("warnings", []))
    errors: list[str] = list(runtime_plan.get("errors", []))
    rendered_active: list[str] = []
    rendered_standby: list[str] = []
    config_only: list[str] = []

    for tunnel in runtime_plan["tunnels"]:
        role = tunnel["role"]
        if role == "config_only":
            config_only.append(tunnel["tunnel_id"])
            services.append(
                {
                    "tunnel_id": tunnel["tunnel_id"],
                    "adapter": tunnel["adapter"],
                    "runtime_role": role,
                    "service_unit_rendered": False,
                    "service_name": "",
                    "staged_unit_file_path": "",
                    "exec_start_argv_summary": [],
                    "runtime_config_path": tunnel["config_file_path"],
                    "warnings": tunnel.get("warnings", []),
                    "errors": tunnel.get("errors", []),
                    "next_action_hint": NEXT_ACTION_HINT,
                }
            )
            continue

        service_name = _service_name(tunnel["tunnel_id"], tunnel["adapter"], tunnel["transport"])
        unit_payload = _render_service_unit(
            service_dir=resolved_service_dir,
            service_name=service_name,
            tunnel=tunnel,
            apply_changes=write_units,
        )
        service_entry = {
            "tunnel_id": tunnel["tunnel_id"],
            "adapter": tunnel["adapter"],
            "runtime_role": role,
            "service_unit_rendered": True,
            "service_name": service_name,
            "staged_unit_file_path": unit_payload["path"],
            "exec_start_argv_summary": tunnel["command_argv"],
            "exec_start_summary": _redacted_exec_start_summary(tunnel["command_argv"]),
            "runtime_config_path": tunnel["config_file_path"],
            "warnings": tunnel.get("warnings", []),
            "errors": tunnel.get("errors", []),
            "next_action_hint": NEXT_ACTION_HINT,
            "unit_preview": _redact_text(unit_payload["content"]),
        }
        services.append(service_entry)
        if role == "active":
            rendered_active.append(tunnel["tunnel_id"])
        elif role == "hot_standby":
            rendered_standby.append(tunnel["tunnel_id"])

    payload = {
        "ok": not errors,
        "action": "service-render",
        "runtime_plan": runtime_plan,
        "service_dir": str(resolved_service_dir),
        "active_services": rendered_active,
        "hot_standby_services": rendered_standby,
        "config_only_tunnels": config_only,
        "services": services,
        "warnings": sorted(set(filter(None, warnings))),
        "errors": sorted(set(filter(None, errors))),
        "dry_run": True,
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }
    if audit_path is not None:
        _audit("service-render", "service-render", payload, audit_path)
    return payload


def _render_service_unit(*, service_dir: Path, service_name: str, tunnel: dict[str, Any], apply_changes: bool) -> dict[str, str]:
    command = _exec_start_command(tunnel["command_argv"])
    rendered = render_unit_file(
        unit_name=service_name,
        description=f"PilotTunnel {tunnel['tunnel_id']} {tunnel['adapter']} {tunnel['role']}",
        command=command,
        output_dir=service_dir,
        apply_changes=apply_changes,
    )
    path = Path(rendered.path)
    _validate_service_file_path(path, service_dir)
    if apply_changes and os.name != "nt":
        path.chmod(0o644)
    return {"path": str(path), "content": rendered.content}


def _service_name(tunnel_id: str, adapter: str, transport: str) -> str:
    for value, label in [
        (tunnel_id, "tunnel_id"),
        (adapter, "adapter"),
        (transport, "transport"),
    ]:
        _validate_component(value, label)
    return f"pilottunnel-{tunnel_id}-{adapter}-{transport}.service"


def _exec_start_command(argv: list[str]) -> str:
    if not argv:
        raise ValueError("Runtime plan did not provide ExecStart argv")
    return shlex.join(argv)


def _redacted_exec_start_summary(argv: list[str]) -> str:
    return _redact_text(shlex.join(argv))


def _validated_service_dir(path: Path, *, create: bool) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for service staging dir: {path!r}")
    if _targets_real_systemd(path):
        raise ValueError("Refusing to stage service files under /etc/systemd/system")
    _validate_parent_chain(path)
    resolved = path.resolve()
    _validate_parent_chain(resolved)
    if not create and not resolved.exists():
        raise ValueError(f"Service staging dir does not exist: {path}")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"Service staging dir must be a directory: {path}")
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _validate_service_file_path(path: Path, service_dir: Path) -> None:
    _validate_parent_chain(path)
    if service_dir not in path.parents:
        raise ValueError(f"Refusing to write outside service staging dir: {path}")


def _validate_parent_chain(path: Path) -> None:
    current = path
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError(f"Symlink escape blocked for service staging path: {current}")
        if current.parent == current:
            return
        current = current.parent


def _targets_real_systemd(path: Path) -> bool:
    normalized = path.as_posix().replace("\\", "/").lower().rstrip("/")
    return normalized == "/etc/systemd/system" or normalized.startswith("/etc/systemd/system/")


def _validate_component(value: str, label: str) -> None:
    if not value or value in {".", ".."}:
        raise ValueError(f"Invalid {label}: {value!r}")
    if any(part in {"", ".."} for part in value.split("/")) or any(part in {"", ".."} for part in value.split("\\")):
        raise ValueError(f"Path traversal blocked for {label}: {value!r}")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"Unsafe {label}: {value!r}")


def _redact_text(value: str) -> str:
    lines: list[str] = []
    for line in value.splitlines():
        lowered = line.lower()
        if any(token in lowered for token in ("token", "password", "private_key", "apikey", "api_key", "secret")):
            if "=" in line:
                key = line.split("=", 1)[0].rstrip()
                lines.append(f"{key}=***REDACTED***")
            elif ":" in line:
                key = line.split(":", 1)[0].rstrip()
                lines.append(f"{key}: ***REDACTED***")
            else:
                lines.append("***REDACTED***")
            continue
        lines.append(line)
    return "\n".join(lines)


def _audit(action: str, profile: str, details: dict[str, Any], path: Path) -> None:
    write_audit_log(action, profile, redact_secrets(details), path)
