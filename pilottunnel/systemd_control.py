"""Guarded systemd reload, status, start, and stop control."""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .audit import redact_secrets, write_audit_log
from .service_install import INSTALL_SUMMARY_FILENAME
from .service_plan import SERVICE_UNIT_MARKER

RELOAD_CONFIRM_TOKEN = "SYSTEMD_DAEMON_RELOAD"
START_CONFIRM_TOKEN = "START_PILOTTUNNEL_SERVICES"
STOP_CONFIRM_TOKEN = "STOP_PILOTTUNNEL_SERVICES"
DEFAULT_TIMEOUT_SECONDS = 2.0
SHOW_PROPERTIES = "LoadState,ActiveState,SubState,FragmentPath"
NEXT_ACTION_HINTS = [
    "start_not_implemented",
    "stop_not_implemented",
    "switch_not_implemented",
]
LIFECYCLE_NEXT_ACTION_HINTS = [
    "manual_switch_not_implemented",
    "rollback_not_implemented",
    "auto_switch_not_implemented",
]


def build_reload_plan(*, target_dir: Path, audit_path: Path) -> dict[str, Any]:
    try:
        resolved_target = _validated_target_dir(target_dir, must_exist=False)
        managed_services = _discover_installed_managed_services(resolved_target)
        action = "would_reload" if managed_services else "reload_not_needed"
        payload = {
            "ok": True,
            "action": action,
            "target_dir": str(resolved_target),
            "managed_service_count": len(managed_services),
            "managed_services": [item["service_name"] for item in managed_services],
            "confirmation_required": True,
            "warnings": [],
            "errors": [],
            "next_action_hints": list(NEXT_ACTION_HINTS),
            "plan_only": True,
            "real_systemd_touched": False,
            "systemctl_executed": False,
            "service_started": False,
            "service_stopped": False,
            "service_enabled": False,
            "service_disabled": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
    except ValueError as exc:
        payload = _failure_payload(
            action="error",
            target_dir=target_dir,
            message=str(exc),
            plan_only=True,
        )
    _audit("systemd-reload-plan", payload, audit_path)
    return payload


def apply_reload(
    *,
    target_dir: Path,
    confirm: str | None,
    audit_path: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    try:
        resolved_target = _validated_target_dir(target_dir, must_exist=False)
        managed_services = _discover_installed_managed_services(resolved_target)
    except ValueError as exc:
        payload = _failure_payload(
            action="error",
            target_dir=target_dir,
            message=str(exc),
            plan_only=False,
        )
        _audit("systemd-reload-apply", payload, audit_path)
        return payload

    if confirm != RELOAD_CONFIRM_TOKEN:
        payload = _failure_payload(
            action="error",
            target_dir=resolved_target,
            message=f"Refusing systemd daemon-reload without --confirm {RELOAD_CONFIRM_TOKEN}",
            plan_only=False,
        )
        payload["managed_service_count"] = len(managed_services)
        payload["managed_services"] = [item["service_name"] for item in managed_services]
        _audit("systemd-reload-apply", payload, audit_path)
        return payload

    if not managed_services:
        payload = {
            "ok": True,
            "action": "reload_not_needed",
            "target_dir": str(resolved_target),
            "managed_service_count": 0,
            "managed_services": [],
            "confirmation_required": False,
            "warnings": [],
            "errors": [],
            "next_action_hints": list(NEXT_ACTION_HINTS),
            "plan_only": False,
            "real_systemd_touched": False,
            "systemctl_executed": False,
            "service_started": False,
            "service_stopped": False,
            "service_enabled": False,
            "service_disabled": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
        _audit("systemd-reload-apply", payload, audit_path)
        return payload

    unavailable = _systemd_unavailable_reason()
    if unavailable:
        payload = _failure_payload(
            action="error",
            target_dir=resolved_target,
            message=unavailable,
            plan_only=False,
        )
        payload["managed_service_count"] = len(managed_services)
        payload["managed_services"] = [item["service_name"] for item in managed_services]
        _audit("systemd-reload-apply", payload, audit_path)
        return payload

    result = _default_command_runner(["systemctl", "daemon-reload"], timeout_seconds=timeout_seconds)
    result = _normalized_result(result)
    payload = {
        "ok": result["returncode"] == 0 and not result["timed_out"],
        "action": "reloaded" if result["returncode"] == 0 and not result["timed_out"] else "error",
        "target_dir": str(resolved_target),
        "managed_service_count": len(managed_services),
        "managed_services": [item["service_name"] for item in managed_services],
        "confirmation_required": False,
        "command": "systemctl daemon-reload",
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "exit_code": result["returncode"],
        "timed_out": result["timed_out"],
        "warnings": [],
        "errors": [] if result["returncode"] == 0 and not result["timed_out"] else [_command_error("systemctl daemon-reload", result)],
        "next_action_hints": list(NEXT_ACTION_HINTS),
        "plan_only": False,
        "real_systemd_touched": result["returncode"] == 0 and not result["timed_out"],
        "systemctl_executed": True,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
    }
    _audit("systemd-reload-apply", payload, audit_path)
    return payload


def inspect_managed_status(
    *,
    service_dir: Path,
    service_name: str | None,
    audit_path: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    try:
        resolved_service_dir = _validated_service_dir(service_dir)
        services = _discover_staged_managed_services(resolved_service_dir)
        selected = _select_services(services, service_name)
    except ValueError as exc:
        payload = {
            "ok": False,
            "action": "error",
            "service_dir": str(service_dir),
            "services": [],
            "warnings": [],
            "errors": [str(exc)],
            "plan_only": False,
            "real_systemd_touched": False,
            "systemctl_executed": False,
            "service_started": False,
            "service_stopped": False,
            "service_enabled": False,
            "service_disabled": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
        _audit("systemd-status", payload, audit_path)
        return payload

    unavailable = _systemd_unavailable_reason()
    if unavailable:
        payload = {
            "ok": False,
            "action": "error",
            "service_dir": str(resolved_service_dir),
            "services": [],
            "warnings": [],
            "errors": [unavailable],
            "plan_only": False,
            "real_systemd_touched": False,
            "systemctl_executed": False,
            "service_started": False,
            "service_stopped": False,
            "service_enabled": False,
            "service_disabled": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
        _audit("systemd-status", payload, audit_path)
        return payload

    service_payloads: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in selected:
        command = [
            "systemctl",
            "show",
            item["service_name"],
            "--property",
            SHOW_PROPERTIES,
            "--no-pager",
        ]
        result = _default_command_runner(command, timeout_seconds=timeout_seconds)
        result = _normalized_result(result)
        parsed = _parse_systemctl_show(result["stdout"])
        entry = {
            "service_name": item["service_name"],
            "tunnel_id": item["tunnel_id"],
            "adapter": item["adapter"],
            "runtime_role": item["runtime_role"],
            "loaded_state": parsed.get("LoadState", ""),
            "active_state": parsed.get("ActiveState", ""),
            "sub_state": parsed.get("SubState", ""),
            "unit_file_path": parsed.get("FragmentPath", ""),
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "exit_code": result["returncode"],
            "timed_out": result["timed_out"],
            "warnings": [],
            "errors": [],
            "read_only": True,
        }
        if result["returncode"] != 0 or result["timed_out"]:
            message = _command_error(" ".join(command), result)
            entry["errors"].append(message)
            errors.append(message)
        service_payloads.append(entry)

    payload = {
        "ok": not errors,
        "action": "systemd-status",
        "service_dir": str(resolved_service_dir),
        "services": service_payloads,
        "warnings": [],
        "errors": errors,
        "plan_only": False,
        "real_systemd_touched": False,
        "systemctl_executed": bool(service_payloads),
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
    }
    _audit("systemd-status", payload, audit_path)
    return payload


def build_start_plan(*, service_dir: Path, service_name: str | None, audit_path: Path) -> dict[str, Any]:
    return _build_lifecycle_plan(
        action="start",
        service_dir=service_dir,
        service_name=service_name,
        audit_path=audit_path,
    )


def apply_start(
    *,
    service_dir: Path,
    service_name: str | None,
    confirm: str | None,
    audit_path: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return _apply_lifecycle_action(
        action="start",
        service_dir=service_dir,
        service_name=service_name,
        confirm=confirm,
        expected_confirm=START_CONFIRM_TOKEN,
        audit_path=audit_path,
        timeout_seconds=timeout_seconds,
    )


def build_stop_plan(*, service_dir: Path, service_name: str | None, audit_path: Path) -> dict[str, Any]:
    return _build_lifecycle_plan(
        action="stop",
        service_dir=service_dir,
        service_name=service_name,
        audit_path=audit_path,
    )


def apply_stop(
    *,
    service_dir: Path,
    service_name: str | None,
    confirm: str | None,
    audit_path: Path,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    return _apply_lifecycle_action(
        action="stop",
        service_dir=service_dir,
        service_name=service_name,
        confirm=confirm,
        expected_confirm=STOP_CONFIRM_TOKEN,
        audit_path=audit_path,
        timeout_seconds=timeout_seconds,
    )


def _discover_installed_managed_services(target_dir: Path) -> list[dict[str, str]]:
    summary_path = target_dir / INSTALL_SUMMARY_FILENAME
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        services = []
        for item in summary.get("services", []):
            if item.get("action") == "skipped_config_only":
                continue
            service_name = item.get("service_name", "")
            target_path = item.get("target_unit_path", "")
            if service_name and target_path and _is_valid_managed_service_name(service_name):
                services.append({"service_name": service_name, "target_unit_path": target_path})
        if services:
            return services

    services = []
    if not target_dir.exists() or not target_dir.is_dir():
        return services
    for candidate in sorted(target_dir.glob("pilottunnel-*.service")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        content = candidate.read_text(encoding="utf-8")
        if SERVICE_UNIT_MARKER not in content:
            continue
        if not _is_valid_managed_service_name(candidate.name):
            continue
        services.append({"service_name": candidate.name, "target_unit_path": str(candidate)})
    return services


def _discover_staged_managed_services(service_dir: Path) -> list[dict[str, str]]:
    services = []
    for candidate in sorted(service_dir.glob("pilottunnel-*.service")):
        if candidate.is_symlink():
            raise ValueError(f"Symlink escape blocked for service dir entry: {candidate}")
        if not candidate.is_file():
            continue
        content = candidate.read_text(encoding="utf-8")
        if SERVICE_UNIT_MARKER not in content:
            continue
        if not _is_valid_managed_service_name(candidate.name):
            continue
        metadata = _parse_unit_metadata(content, candidate.name)
        services.append(metadata)
    return services


def _build_lifecycle_plan(*, action: str, service_dir: Path, service_name: str | None, audit_path: Path) -> dict[str, Any]:
    audit_action = f"systemd-{action}-plan"
    try:
        resolved_service_dir = _validated_service_dir(service_dir)
        selected = _select_services(_discover_staged_managed_services(resolved_service_dir), service_name)
        services, warnings, errors = _lifecycle_entries(selected, action=action, apply_changes=False)
        payload = {
            "ok": not errors,
            "action": f"systemd-{action}-plan",
            "service_dir": str(resolved_service_dir),
            "services": services,
            "warnings": sorted(set(filter(None, warnings))),
            "errors": sorted(set(filter(None, errors))),
            "plan_only": True,
            "real_systemd_touched": False,
            "systemctl_executed": False,
            "service_started": False,
            "service_stopped": False,
            "service_enabled": False,
            "service_disabled": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
    except ValueError as exc:
        payload = _service_dir_failure_payload(
            action=f"systemd-{action}-plan",
            service_dir=service_dir,
            message=str(exc),
            plan_only=True,
        )
    _audit(audit_action, payload, audit_path)
    return payload


def _apply_lifecycle_action(
    *,
    action: str,
    service_dir: Path,
    service_name: str | None,
    confirm: str | None,
    expected_confirm: str,
    audit_path: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    audit_action = f"systemd-{action}-apply"
    try:
        resolved_service_dir = _validated_service_dir(service_dir)
        selected = _select_services(_discover_staged_managed_services(resolved_service_dir), service_name)
    except ValueError as exc:
        payload = _service_dir_failure_payload(
            action=f"systemd-{action}-apply",
            service_dir=service_dir,
            message=str(exc),
            plan_only=False,
        )
        _audit(audit_action, payload, audit_path)
        return payload

    if confirm != expected_confirm:
        payload = _service_dir_failure_payload(
            action=f"systemd-{action}-apply",
            service_dir=resolved_service_dir,
            message=f"Refusing systemd {action} without --confirm {expected_confirm}",
            plan_only=False,
        )
        preview_services, preview_warnings, _preview_errors = _lifecycle_entries(selected, action=action, apply_changes=False)
        payload["services"] = preview_services
        payload["warnings"] = preview_warnings
        _audit(audit_action, payload, audit_path)
        return payload

    unavailable = _systemd_unavailable_reason()
    if unavailable:
        payload = _service_dir_failure_payload(
            action=f"systemd-{action}-apply",
            service_dir=resolved_service_dir,
            message=unavailable,
            plan_only=False,
        )
        preview_services, preview_warnings, _preview_errors = _lifecycle_entries(selected, action=action, apply_changes=False)
        payload["services"] = preview_services
        payload["warnings"] = preview_warnings
        _audit(audit_action, payload, audit_path)
        return payload

    services, warnings, errors = _lifecycle_entries(
        selected,
        action=action,
        apply_changes=True,
        timeout_seconds=timeout_seconds,
    )
    payload = {
        "ok": not errors,
        "action": f"systemd-{action}-apply",
        "service_dir": str(resolved_service_dir),
        "services": services,
        "warnings": sorted(set(filter(None, warnings))),
        "errors": sorted(set(filter(None, errors))),
        "plan_only": False,
        "real_systemd_touched": any(item["action"] in {"started", "stopped"} for item in services),
        "systemctl_executed": any(bool(item.get("command_executed")) for item in services),
        "service_started": action == "start" and any(item["action"] == "started" for item in services),
        "service_stopped": action == "stop" and any(item["action"] == "stopped" for item in services),
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
    }
    _audit(audit_action, payload, audit_path)
    return payload


def _lifecycle_entries(
    services: list[dict[str, str]],
    *,
    action: str,
    apply_changes: bool,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    for item in services:
        entry = _lifecycle_entry(item, action=action, apply_changes=apply_changes, timeout_seconds=timeout_seconds)
        entries.append(entry)
        warnings.extend(entry.get("warnings", []))
        errors.extend(entry.get("errors", []))
    return entries, warnings, errors


def _lifecycle_entry(
    service: dict[str, str],
    *,
    action: str,
    apply_changes: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    service_name = service["service_name"]
    runtime_role = service.get("runtime_role", "")
    entry = {
        "service_name": service_name,
        "tunnel_id": service.get("tunnel_id", ""),
        "adapter": service.get("adapter", ""),
        "runtime_role": runtime_role,
        "action": "",
        "reason": "",
        "command_summary": f"systemctl {action} {service_name}",
        "command_executed": "",
        "stdout": "",
        "stderr": "",
        "exit_code": 0,
        "timed_out": False,
        "warnings": [],
        "errors": [],
        "next_action_hints": list(LIFECYCLE_NEXT_ACTION_HINTS),
    }
    if runtime_role not in {"active", "hot_standby"}:
        entry["action"] = "skipped"
        entry["reason"] = f"runtime_role '{runtime_role or 'unknown'}' is not startable/stoppable in this workflow"
        return entry
    if not apply_changes:
        entry["action"] = "would_start" if action == "start" else "would_stop"
        return entry

    command = ["systemctl", action, service_name]
    result = _normalized_result(_default_command_runner(command, timeout_seconds=timeout_seconds))
    entry["command_executed"] = " ".join(command)
    entry["stdout"] = result["stdout"]
    entry["stderr"] = result["stderr"]
    entry["exit_code"] = result["returncode"]
    entry["timed_out"] = result["timed_out"]
    if result["returncode"] == 0 and not result["timed_out"]:
        entry["action"] = "started" if action == "start" else "stopped"
        return entry
    entry["action"] = "error"
    message = _command_error(" ".join(command), result)
    entry["reason"] = message
    entry["errors"].append(message)
    return entry


def _parse_unit_metadata(content: str, service_name: str) -> dict[str, str]:
    tunnel_id = ""
    adapter = ""
    runtime_role = ""
    for line in content.splitlines():
        if line.startswith("Description=PilotTunnel "):
            remainder = line[len("Description=PilotTunnel ") :]
            parts = remainder.split(" ")
            if len(parts) >= 3:
                tunnel_id = parts[0]
                adapter = parts[1]
                runtime_role = parts[2]
            break
    return {
        "service_name": service_name,
        "tunnel_id": tunnel_id,
        "adapter": adapter,
        "runtime_role": runtime_role,
    }


def _select_services(services: list[dict[str, str]], service_name: str | None) -> list[dict[str, str]]:
    if service_name is None:
        return services
    if not _is_valid_managed_service_name(service_name):
        raise ValueError(f"Refusing non-PilotTunnel managed service name '{service_name}'")
    selected = [item for item in services if item["service_name"] == service_name]
    if not selected:
        raise ValueError(f"Managed staged service not found: {service_name}")
    return selected


def _validated_target_dir(path: Path, *, must_exist: bool) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for target dir: {path!r}")
    _validate_parent_chain(path)
    resolved = path.resolve()
    _validate_parent_chain(resolved)
    if must_exist and not resolved.exists():
        raise ValueError(f"Target dir does not exist: {path}")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"Target dir must be a directory: {path}")
    return resolved


def _validated_service_dir(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for service dir: {path!r}")
    _validate_parent_chain(path)
    resolved = path.resolve()
    _validate_parent_chain(resolved)
    if not resolved.exists():
        raise ValueError(f"Service dir does not exist: {path}")
    if not resolved.is_dir():
        raise ValueError(f"Service dir must be a directory: {path}")
    return resolved


def _validate_parent_chain(path: Path) -> None:
    current = path
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError(f"Symlink escape blocked for path: {current}")
        if current.parent == current:
            return
        current = current.parent


def _default_command_runner(command: list[str], *, timeout_seconds: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": _redact_text(completed.stdout or ""),
            "stderr": _redact_text(completed.stderr or ""),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": -1,
            "stdout": _redact_text(exc.stdout or ""),
            "stderr": _redact_text(exc.stderr or "command timed out"),
            "timed_out": True,
        }
    except OSError as exc:
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": _redact_text(str(exc)),
            "timed_out": False,
        }


def _normalized_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "returncode": int(result.get("returncode", -1)),
        "stdout": _redact_text(str(result.get("stdout", ""))),
        "stderr": _redact_text(str(result.get("stderr", ""))),
        "timed_out": bool(result.get("timed_out", False)),
    }


def _systemd_unavailable_reason() -> str:
    if not _is_linux():
        return "systemd control is Linux-only"
    if not _systemd_available():
        return "systemctl is unavailable on this host"
    return ""


def _systemd_available() -> bool:
    return bool(shutil.which("systemctl"))


def _is_linux() -> bool:
    return platform.system().lower().startswith("linux")


def _parse_systemctl_show(stdout: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def _command_error(command: str, result: dict[str, Any]) -> str:
    if result["timed_out"]:
        return f"Command timed out: {command}"
    if result["stderr"]:
        return f"Command failed: {command}: {result['stderr']}"
    return f"Command failed: {command}"


def _is_valid_managed_service_name(service_name: str) -> bool:
    return bool(re.fullmatch(r"pilottunnel-[A-Za-z0-9._-]+\.service", service_name))


def _redact_text(value: str) -> str:
    text = value.replace("\x00", "").replace("\r", "").strip()
    lines = []
    for line in text.splitlines():
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
    return "\n".join(lines)[:800]


def _failure_payload(*, action: str, target_dir: Path, message: str, plan_only: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "action": action,
        "target_dir": str(target_dir),
        "managed_service_count": 0,
        "managed_services": [],
        "confirmation_required": not plan_only,
        "warnings": [],
        "errors": [message],
        "next_action_hints": list(NEXT_ACTION_HINTS),
        "plan_only": plan_only,
        "real_systemd_touched": False,
        "systemctl_executed": False,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def _service_dir_failure_payload(*, action: str, service_dir: Path, message: str, plan_only: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "action": action,
        "service_dir": str(service_dir),
        "services": [],
        "warnings": [],
        "errors": [message],
        "plan_only": plan_only,
        "real_systemd_touched": False,
        "systemctl_executed": False,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def _audit(action: str, payload: dict[str, Any], path: Path) -> None:
    write_audit_log(action, action, redact_secrets(payload), path)
