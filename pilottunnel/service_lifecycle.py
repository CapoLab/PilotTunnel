"""Read-only service lifecycle planning and inspection."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .adapters import ADAPTERS
from .adapters.base import AdapterContext
from .audit import write_audit_log
from .config import Profile, build_worker_stub, canonical_role, validate_profile_name
from .healthcheck import run_profile_healthchecks, summarize_healthchecks
from .state import AppState
from .switch_engine import SwitchPaths

DEFAULT_SERVICE_TIMEOUT_SECONDS = 2.0
REAL_SYSTEM_ROOT = Path("/")
PILOTTUNNEL_UNIT_MARKER = "# Managed-by: PilotTunnel"


@dataclass
class ServiceLifecyclePlan:
    ok: bool
    action: str
    profile: str
    role: str
    adapter: str
    transport: str
    service_name: str
    unit_path: str
    future_command: str
    plan_steps: list[str]
    warnings: list[str]
    real_systemd_touched: bool = False
    service_started: bool = False
    service_stopped: bool = False
    firewall_touched: bool = False
    routes_touched: bool = False


@dataclass
class ServiceStartRequest:
    profile: str
    role: str
    adapter: str
    transport: str
    service_name: str
    unit_path: str


def build_service_plan(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    action: str,
    role: str | None,
    paths: SwitchPaths,
    state: AppState | None = None,
    install_root: Path | None = None,
) -> dict:
    planned_role = _resolve_role(profile, role)
    _validate_service_inputs(profile.name, adapter_name, transport, planned_role, install_root)
    _validate_action(action)
    adapter = _adapter_for(adapter_name)
    context = AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=paths.work_dir / profile.name,
        staging_root=paths.staging_root,
        apply_changes=False,
        role=planned_role,
        remote_stub=asdict(build_worker_stub(profile)),
    )
    ok, reason = adapter.precheck(context)
    if not ok:
        raise ValueError(reason)

    service_name = adapter.service_name(context)
    unit_path = _service_unit_path(service_name, install_root)
    future_command = _future_command(action, service_name)
    warnings = _service_warnings(paths.staging_root)
    plan = ServiceLifecyclePlan(
        ok=True,
        action=action,
        profile=profile.name,
        role=planned_role,
        adapter=adapter_name,
        transport=transport,
        service_name=service_name,
        unit_path=str(unit_path),
        future_command=future_command,
        plan_steps=_plan_steps(action, service_name),
        warnings=warnings,
    )
    _audit("service-plan", profile.name, {
        "action": action,
        "role": planned_role,
        "adapter": adapter_name,
        "transport": transport,
        "service_name": service_name,
        "unit_path": str(unit_path),
        "future_command": future_command,
        "warnings": warnings,
        "real_systemd_touched": False,
        "service_started": False,
        "service_stopped": False,
        "firewall_touched": False,
        "routes_touched": False,
    }, path=paths.audit_path)
    return asdict(plan)


def inspect_service_status(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str | None,
    paths: SwitchPaths,
    install_root: Path | None = None,
    timeout_seconds: float = DEFAULT_SERVICE_TIMEOUT_SECONDS,
    real_systemd: bool = False,
) -> dict:
    planned_role = _resolve_role(profile, role)
    _validate_service_inputs(profile.name, adapter_name, transport, planned_role, install_root)
    adapter = _adapter_for(adapter_name)
    context = AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=paths.work_dir / profile.name,
        staging_root=paths.staging_root,
        apply_changes=False,
        role=planned_role,
        remote_stub=asdict(build_worker_stub(profile)),
    )
    ok, reason = adapter.precheck(context)
    if not ok:
        raise ValueError(reason)

    service_name = adapter.service_name(context)
    unit_path = _service_unit_path(service_name, install_root)
    if not real_systemd:
        payload = _inspection_disabled_payload(
            action="service-status",
            warning="Real systemd status inspection is disabled by default. Use --real-systemd.",
            service_name=service_name,
            unit_path=str(unit_path),
            profile=profile.name,
            role=planned_role,
            adapter=adapter_name,
            transport=transport,
        )
        _audit("service-status", profile.name, payload, path=paths.audit_path)
        return payload

    unavailable = _real_systemd_unavailable_payload(
        action="service-status",
        service_name=service_name,
        unit_path=str(unit_path),
        profile=profile.name,
        role=planned_role,
        adapter=adapter_name,
        transport=transport,
        require_journal=False,
    )
    if unavailable is not None:
        _audit("service-status", profile.name, unavailable, path=paths.audit_path)
        return unavailable

    status_command = ["systemctl", "status", service_name, "--no-pager"]
    active_command = ["systemctl", "is-active", service_name]
    enabled_command = ["systemctl", "is-enabled", service_name]
    status_result = _run_command(status_command, timeout_seconds=timeout_seconds)
    active_result = _run_command(active_command, timeout_seconds=timeout_seconds)
    enabled_result = _run_command(enabled_command, timeout_seconds=timeout_seconds)
    payload = {
        "ok": status_result["returncode"] == 0 and not status_result.get("timed_out", False),
        "service_name": service_name,
        "unit_path": str(unit_path),
        "profile": profile.name,
        "role": planned_role,
        "adapter": adapter_name,
        "transport": transport,
        "status_command": " ".join(status_command),
        "command_executed": " ".join(status_command),
        "exit_code": status_result["returncode"],
        "stdout": status_result["stdout"],
        "stderr": status_result["stderr"],
        "timed_out": status_result["timed_out"],
        "is_active": _sanitize_output(active_result["stdout"]) if active_result["stdout"] else "",
        "is_enabled": _sanitize_output(enabled_result["stdout"]) if enabled_result["stdout"] else "",
        "read_only": True,
        "real_systemd": True,
        "real_systemd_touched": False,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
        "checked_at": _checked_at(),
    }
    _audit("service-status", profile.name, payload, path=paths.audit_path)
    return payload


def inspect_service_logs(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str | None,
    paths: SwitchPaths,
    install_root: Path | None = None,
    limit: int = 50,
    timeout_seconds: float = DEFAULT_SERVICE_TIMEOUT_SECONDS,
    real_systemd: bool = False,
) -> dict:
    planned_role = _resolve_role(profile, role)
    _validate_service_inputs(profile.name, adapter_name, transport, planned_role, install_root)
    adapter = _adapter_for(adapter_name)
    context = AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=paths.work_dir / profile.name,
        staging_root=paths.staging_root,
        apply_changes=False,
        role=planned_role,
        remote_stub=asdict(build_worker_stub(profile)),
    )
    ok, reason = adapter.precheck(context)
    if not ok:
        raise ValueError(reason)

    service_name = adapter.service_name(context)
    unit_path = _service_unit_path(service_name, install_root)
    if not real_systemd:
        payload = _inspection_disabled_payload(
            action="service-logs",
            warning="Real systemd log inspection is disabled by default. Use --real-systemd.",
            service_name=service_name,
            unit_path=str(unit_path),
            profile=profile.name,
            role=planned_role,
            adapter=adapter_name,
            transport=transport,
            limit=limit,
        )
        _audit("service-logs", profile.name, payload, path=paths.audit_path)
        return payload

    unavailable = _real_systemd_unavailable_payload(
        action="service-logs",
        service_name=service_name,
        unit_path=str(unit_path),
        profile=profile.name,
        role=planned_role,
        adapter=adapter_name,
        transport=transport,
        require_journal=True,
        limit=limit,
    )
    if unavailable is not None:
        _audit("service-logs", profile.name, unavailable, path=paths.audit_path)
        return unavailable

    command = ["journalctl", "-u", service_name, "--no-pager", "-n", str(limit)]
    result = _run_command(command, timeout_seconds=timeout_seconds)
    entries = result["stdout"].splitlines() if result["stdout"] else []
    payload = {
        "ok": result["returncode"] == 0 and not result.get("timed_out", False),
        "service_name": service_name,
        "unit_path": str(unit_path),
        "profile": profile.name,
        "role": planned_role,
        "adapter": adapter_name,
        "transport": transport,
        "limit": limit,
        "logs_command": " ".join(command),
        "command_executed": " ".join(command),
        "exit_code": result["returncode"],
        "entries": entries,
        "stderr": result["stderr"],
        "timed_out": result["timed_out"],
        "read_only": True,
        "real_systemd": True,
        "real_systemd_touched": False,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
        "checked_at": _checked_at(),
    }
    _audit("service-logs", profile.name, payload, path=paths.audit_path)
    return payload


def run_daemon_reload(
    *,
    paths: SwitchPaths,
    confirm: str | None,
    real_systemd: bool,
    timeout_seconds: float = DEFAULT_SERVICE_TIMEOUT_SECONDS,
) -> dict:
    attempt = {
        "real_systemd": real_systemd,
        "confirm": confirm or "",
        "read_only": False,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
        "systemctl_executed": False,
    }
    if not real_systemd:
        payload = {
            "ok": False,
            "message": "Refusing daemon-reload without --real-systemd",
            "daemon_reload_executed": False,
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-daemon-reload", "local-node", payload, path=paths.audit_path)
        return payload
    if confirm != "DAEMON_RELOAD":
        payload = {
            "ok": False,
            "message": "Refusing daemon-reload without --confirm DAEMON_RELOAD",
            "daemon_reload_executed": False,
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-daemon-reload", "local-node", payload, path=paths.audit_path)
        return payload
    if not _is_linux():
        payload = {
            "ok": False,
            "message": "Real systemd daemon-reload is Linux-only",
            "daemon_reload_executed": False,
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-daemon-reload", "local-node", payload, path=paths.audit_path)
        return payload
    if not _systemd_available():
        payload = {
            "ok": False,
            "message": "systemd is unavailable on this host",
            "daemon_reload_executed": False,
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-daemon-reload", "local-node", payload, path=paths.audit_path)
        return payload
    if not _is_root():
        payload = {
            "ok": False,
            "message": "systemctl daemon-reload requires root/admin privileges",
            "daemon_reload_executed": False,
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-daemon-reload", "local-node", payload, path=paths.audit_path)
        return payload

    command = ["systemctl", "daemon-reload"]
    result = _run_command(command, timeout_seconds=timeout_seconds)
    payload = {
        "ok": result["returncode"] == 0 and not result["timed_out"],
        "command_executed": " ".join(command),
        "exit_code": result["returncode"],
        "stdout": result["stdout"],
        "stderr": result["stderr"],
        "timed_out": result["timed_out"],
        "real_systemd": True,
        "read_only": False,
        "daemon_reload_executed": result["returncode"] == 0 and not result["timed_out"],
        "real_systemd_touched": True,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
        "systemctl_executed": True,
        "checked_at": _checked_at(),
    }
    _audit("service-daemon-reload", "local-node", payload, path=paths.audit_path)
    return payload


def start_service(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str | None,
    paths: SwitchPaths,
    confirm: str | None,
    real_systemd: bool,
    require_healthcheck: bool = False,
    healthcheck_timeout: float = DEFAULT_SERVICE_TIMEOUT_SECONDS,
) -> dict:
    request = _service_start_request(
        profile=profile,
        adapter_name=adapter_name,
        transport=transport,
        role=role,
        paths=paths,
    )
    attempt = {
        "profile": request.profile,
        "role": request.role,
        "adapter": request.adapter,
        "transport": request.transport,
        "service_name": request.service_name,
        "unit_path": request.unit_path,
        "real_systemd": real_systemd,
        "confirm": confirm or "",
        "require_healthcheck": require_healthcheck,
        "healthcheck_timeout": healthcheck_timeout,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "service_restarted": False,
        "firewall_touched": False,
        "routes_touched": False,
        "systemctl_executed": False,
    }
    if not real_systemd:
        payload = {
            "ok": False,
            "message": "Refusing real service start without --real-systemd. Use service plan --action start for plan-only guidance.",
            "healthcheck_ok": "skipped",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-start", profile.name, payload, path=paths.audit_path)
        return payload
    if confirm != "START_SERVICE":
        payload = {
            "ok": False,
            "message": "Refusing real service start without --confirm START_SERVICE",
            "healthcheck_ok": "skipped",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-start", profile.name, payload, path=paths.audit_path)
        return payload
    if not _is_linux():
        payload = {
            "ok": False,
            "message": "Real service start is Linux-only",
            "healthcheck_ok": "skipped",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-start", profile.name, payload, path=paths.audit_path)
        return payload
    if not _systemd_available():
        payload = {
            "ok": False,
            "message": "systemd is unavailable on this host",
            "healthcheck_ok": "skipped",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-start", profile.name, payload, path=paths.audit_path)
        return payload
    if not _is_root():
        payload = {
            "ok": False,
            "message": "systemctl start requires root/admin privileges",
            "healthcheck_ok": "skipped",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-start", profile.name, payload, path=paths.audit_path)
        return payload

    ownership = _verify_pilottunnel_unit_ownership(request=request)
    if not ownership["ok"]:
        payload = {
            "ok": False,
            "message": ownership["message"],
            "healthcheck_ok": "skipped",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-start", profile.name, payload, path=paths.audit_path)
        return payload

    start_command = ["systemctl", "start", request.service_name]
    start_result = _run_command(start_command, timeout_seconds=healthcheck_timeout)
    status_payload = _service_status_payload(request=request, timeout_seconds=healthcheck_timeout)
    if start_result["returncode"] != 0 or start_result["timed_out"]:
        payload = {
            "ok": False,
            "message": "systemctl start failed; review service status and logs",
            "command_executed": " ".join(start_command),
            "exit_code": start_result["returncode"],
            "stdout": start_result["stdout"],
            "stderr": start_result["stderr"],
            "timed_out": start_result["timed_out"],
            "status": status_payload,
            "healthcheck_ok": "skipped",
            "real_systemd_touched": True,
            "service_started": False,
            "service_stopped": False,
            "service_enabled": False,
            "service_disabled": False,
            "service_restarted": False,
            "firewall_touched": False,
            "routes_touched": False,
            "systemctl_executed": True,
            "real_systemd": True,
            "read_only": False,
            "service_name": request.service_name,
            "unit_path": request.unit_path,
            "profile": request.profile,
            "role": request.role,
            "adapter": request.adapter,
            "transport": request.transport,
        }
        _audit("service-start", profile.name, payload, path=paths.audit_path)
        return payload

    healthcheck_ok: bool | str = "skipped"
    healthcheck_summary: dict[str, Any] | None = None
    ok = True
    message = "Service start completed"
    if require_healthcheck:
        healthcheck_summary = summarize_healthchecks(
            run_profile_healthchecks(
                profile=profile,
                node_role=request.role,
                timeout=healthcheck_timeout,
                include_all=True,
                role_aware=True,
            ),
            profile=profile.name,
            role=request.role,
        )
        healthcheck_ok = healthcheck_summary["ok"]
        if not healthcheck_summary["ok"]:
            ok = False
            message = "Service started but healthcheck failed; review service status and logs manually"

    payload = {
        "ok": ok,
        "message": message,
        "command_executed": " ".join(start_command),
        "exit_code": start_result["returncode"],
        "stdout": start_result["stdout"],
        "stderr": start_result["stderr"],
        "timed_out": start_result["timed_out"],
        "status": status_payload,
        "healthcheck_ok": healthcheck_ok,
        "healthcheck": healthcheck_summary,
        "real_systemd_touched": True,
        "service_started": True,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "service_restarted": False,
        "firewall_touched": False,
        "routes_touched": False,
        "systemctl_executed": True,
        "real_systemd": True,
        "read_only": False,
        "service_name": request.service_name,
        "unit_path": request.unit_path,
        "profile": request.profile,
        "role": request.role,
        "adapter": request.adapter,
        "transport": request.transport,
    }
    _audit("service-start", profile.name, payload, path=paths.audit_path)
    return payload


def stop_service(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str | None,
    paths: SwitchPaths,
    confirm: str | None,
    real_systemd: bool,
    timeout_seconds: float = DEFAULT_SERVICE_TIMEOUT_SECONDS,
) -> dict:
    request = _service_start_request(
        profile=profile,
        adapter_name=adapter_name,
        transport=transport,
        role=role,
        paths=paths,
    )
    attempt = {
        "profile": request.profile,
        "role": request.role,
        "adapter": request.adapter,
        "transport": request.transport,
        "service_name": request.service_name,
        "unit_path": request.unit_path,
        "real_systemd": real_systemd,
        "confirm": confirm or "",
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "service_restarted": False,
        "firewall_touched": False,
        "routes_touched": False,
        "systemctl_executed": False,
    }
    if not real_systemd:
        payload = {
            "ok": False,
            "message": "Refusing real service stop without --real-systemd. Use service plan --action stop for plan-only guidance.",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-stop", profile.name, payload, path=paths.audit_path)
        return payload
    if confirm != "STOP_SERVICE":
        payload = {
            "ok": False,
            "message": "Refusing real service stop without --confirm STOP_SERVICE",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-stop", profile.name, payload, path=paths.audit_path)
        return payload
    if not _is_linux():
        payload = {
            "ok": False,
            "message": "Real service stop is Linux-only",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-stop", profile.name, payload, path=paths.audit_path)
        return payload
    if not _systemd_available():
        payload = {
            "ok": False,
            "message": "systemd is unavailable on this host",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-stop", profile.name, payload, path=paths.audit_path)
        return payload
    if not _is_root():
        payload = {
            "ok": False,
            "message": "systemctl stop requires root/admin privileges",
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-stop", profile.name, payload, path=paths.audit_path)
        return payload

    ownership = _verify_pilottunnel_unit_ownership(request=request)
    if not ownership["ok"]:
        payload = {
            "ok": False,
            "message": ownership["message"],
            "real_systemd_touched": False,
            **attempt,
        }
        _audit("service-stop", profile.name, payload, path=paths.audit_path)
        return payload

    stop_command = ["systemctl", "stop", request.service_name]
    stop_result = _run_command(stop_command, timeout_seconds=timeout_seconds)
    status_payload = _service_status_payload(request=request, timeout_seconds=timeout_seconds)
    if stop_result["returncode"] != 0 or stop_result["timed_out"]:
        payload = {
            "ok": False,
            "message": "systemctl stop failed; review service status and logs",
            "command_executed": " ".join(stop_command),
            "exit_code": stop_result["returncode"],
            "stdout": stop_result["stdout"],
            "stderr": stop_result["stderr"],
            "timed_out": stop_result["timed_out"],
            "status": status_payload,
            "real_systemd_touched": True,
            "service_started": False,
            "service_stopped": False,
            "service_enabled": False,
            "service_disabled": False,
            "service_restarted": False,
            "firewall_touched": False,
            "routes_touched": False,
            "systemctl_executed": True,
            "real_systemd": True,
            "read_only": False,
            "service_name": request.service_name,
            "unit_path": request.unit_path,
            "profile": request.profile,
            "role": request.role,
            "adapter": request.adapter,
            "transport": request.transport,
        }
        _audit("service-stop", profile.name, payload, path=paths.audit_path)
        return payload

    payload = {
        "ok": True,
        "message": "Service stop completed",
        "command_executed": " ".join(stop_command),
        "exit_code": stop_result["returncode"],
        "stdout": stop_result["stdout"],
        "stderr": stop_result["stderr"],
        "timed_out": stop_result["timed_out"],
        "status": status_payload,
        "real_systemd_touched": True,
        "service_started": False,
        "service_stopped": True,
        "service_enabled": False,
        "service_disabled": False,
        "service_restarted": False,
        "firewall_touched": False,
        "routes_touched": False,
        "systemctl_executed": True,
        "real_systemd": True,
        "read_only": False,
        "service_name": request.service_name,
        "unit_path": request.unit_path,
        "profile": request.profile,
        "role": request.role,
        "adapter": request.adapter,
        "transport": request.transport,
    }
    _audit("service-stop", profile.name, payload, path=paths.audit_path)
    return payload


def block_real_service_action(
    *,
    action: str,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str | None,
    paths: SwitchPaths,
    real_systemd: bool,
) -> dict:
    request = _service_start_request(
        profile=profile,
        adapter_name=adapter_name,
        transport=transport,
        role=role,
        paths=paths,
    )
    if real_systemd:
        blocked_actions = {"restart", "enable", "disable"}
        blocked_action_text = "/".join(sorted(blocked_actions, key=lambda item: ["restart", "enable", "disable"].index(item)))
        payload = {
            "ok": False,
            "message": f"Real {blocked_action_text} is not implemented in this safety stage.",
            "action": action,
            "service_name": request.service_name,
            "unit_path": request.unit_path,
            "profile": request.profile,
            "role": request.role,
            "adapter": request.adapter,
            "transport": request.transport,
            "real_systemd": True,
            "read_only": False,
            "real_systemd_touched": False,
            "service_started": False,
            "service_stopped": False,
            "service_enabled": False,
            "service_disabled": False,
            "service_restarted": False,
            "firewall_touched": False,
            "routes_touched": False,
            "systemctl_executed": False,
        }
        _audit(f"service-{action}", profile.name, payload, path=paths.audit_path)
        return payload

    payload = {
        "ok": False,
        "message": f"Real service {action} requires --real-systemd, and remains blocked in this safety stage. Use service plan --action {action} for plan-only guidance.",
        "action": action,
        "service_name": request.service_name,
        "unit_path": request.unit_path,
        "profile": request.profile,
        "role": request.role,
        "adapter": request.adapter,
        "transport": request.transport,
        "real_systemd": False,
        "read_only": False,
        "real_systemd_touched": False,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "service_restarted": False,
        "firewall_touched": False,
        "routes_touched": False,
        "systemctl_executed": False,
    }
    _audit(f"service-{action}", profile.name, payload, path=paths.audit_path)
    return payload


def _adapter_for(adapter_name: str):
    if adapter_name not in ADAPTERS:
        raise KeyError(f"Unknown adapter '{adapter_name}'")
    return ADAPTERS[adapter_name]()


def _resolve_role(profile: Profile, role: str | None) -> str:
    requested = canonical_role(role or profile.role)
    if role is None:
        return requested
    return requested


def _validate_service_inputs(profile_name: str, adapter_name: str, transport: str, role: str, install_root: Path | None) -> None:
    validate_profile_name(profile_name)
    if role not in {"controller", "worker"}:
        raise ValueError(f"Unsupported service role '{role}'")
    for value, label in [(adapter_name, "adapter"), (transport, "transport")]:
        if not value or value in {".", ".."}:
            raise ValueError(f"Invalid {label}: {value!r}")
        if "/" in value or "\\" in value or ".." in value:
            raise ValueError(f"Path traversal blocked for {label}: {value!r}")
    if install_root is not None:
        _validate_install_root(install_root)


def _validate_install_root(install_root: Path) -> Path:
    if ".." in install_root.parts:
        raise ValueError(f"Path traversal blocked for install-root: {install_root!r}")
    root = install_root.resolve()
    if root == Path(root.anchor):
        raise ValueError(f"Refusing dangerous install-root: {root}")
    return root


def _validate_action(action: str) -> None:
    if action not in {"start", "stop", "restart", "enable", "disable"}:
        raise ValueError(f"Unsupported service action '{action}'")


def _service_unit_path(service_name: str, install_root: Path | None) -> Path:
    if install_root is None:
        return Path("/etc/systemd/system") / service_name
    root = _validate_install_root(install_root)
    candidate = (root / "etc" / "systemd" / "system" / service_name).resolve()
    if root not in candidate.parents and candidate != root:
        raise ValueError(f"Refusing to plan outside install root: {candidate}")
    return candidate


def _future_command(action: str, service_name: str) -> str:
    if action == "daemon-reload":
        return "systemctl daemon-reload"
    if action == "status":
        return f"systemctl status {service_name} --no-pager --full"
    if action == "logs":
        return f"journalctl -u {service_name} --no-pager -n 50"
    return f"systemctl {action} {service_name}"


def _plan_steps(action: str, service_name: str) -> list[str]:
    if action == "daemon-reload":
        return ["systemctl daemon-reload"]
    if action == "restart":
        return [f"systemctl stop {service_name}", f"systemctl start {service_name}"]
    return [_future_command(action, service_name)]


def _service_warnings(staging_root: Path) -> list[str]:
    warnings: list[str] = []
    if platform.system().lower().startswith("win"):
        warnings.append("Windows host detected; service lifecycle planning is read-only only")
    elif not shutil.which("systemctl"):
        warnings.append("systemctl is unavailable on this host")
    elif not staging_root.exists():
        warnings.append("Staging root does not exist yet; generated service files may be missing")
    return warnings


def _run_command(command: list[str], *, timeout_seconds: float) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": _sanitize_output(completed.stdout or ""),
            "stderr": _sanitize_output(completed.stderr or ""),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "stdout": _sanitize_output(exc.stdout or ""),
            "stderr": _sanitize_output(exc.stderr or "command timed out"),
            "timed_out": True,
        }
    except OSError as exc:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": _sanitize_output(str(exc)),
            "timed_out": False,
        }


def _audit(action: str, profile: str, details: dict[str, Any], path: Path | None = None) -> None:
    if path is None:
        write_audit_log(action, profile, details)
    else:
        write_audit_log(action, profile, details, path)


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inspection_disabled_payload(
    *,
    action: str,
    warning: str,
    service_name: str,
    unit_path: str,
    profile: str,
    role: str,
    adapter: str,
    transport: str,
    limit: int | None = None,
) -> dict[str, Any]:
    payload = {
        "ok": False,
        "warning": warning,
        "service_name": service_name,
        "unit_path": unit_path,
        "profile": profile,
        "role": role,
        "adapter": adapter,
        "transport": transport,
        "command_executed": "",
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "read_only": True,
        "real_systemd": False,
        "real_systemd_touched": False,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
        "checked_at": _checked_at(),
    }
    if action == "service-status":
        payload["status_command"] = ""
        payload["is_active"] = ""
        payload["is_enabled"] = ""
    if action == "service-logs":
        payload["limit"] = limit or 50
        payload["logs_command"] = ""
        payload["entries"] = []
    return payload


def _real_systemd_unavailable_payload(
    *,
    action: str,
    service_name: str,
    unit_path: str,
    profile: str,
    role: str,
    adapter: str,
    transport: str,
    require_journal: bool,
    limit: int | None = None,
) -> dict[str, Any] | None:
    if platform.system().lower().startswith("win"):
        return _inspection_disabled_payload(
            action=action,
            warning="Windows host detected; real systemd inspection is unavailable",
            service_name=service_name,
            unit_path=unit_path,
            profile=profile,
            role=role,
            adapter=adapter,
            transport=transport,
            limit=limit,
        )
    if not _systemd_available():
        return _inspection_disabled_payload(
            action=action,
            warning="systemd is unavailable on this host",
            service_name=service_name,
            unit_path=unit_path,
            profile=profile,
            role=role,
            adapter=adapter,
            transport=transport,
            limit=limit,
        )
    if require_journal and not shutil.which("journalctl"):
        return _inspection_disabled_payload(
            action=action,
            warning="journalctl is unavailable on this host",
            service_name=service_name,
            unit_path=unit_path,
            profile=profile,
            role=role,
            adapter=adapter,
            transport=transport,
            limit=limit,
        )
    return None


def _service_start_request(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str | None,
    paths: SwitchPaths,
) -> ServiceStartRequest:
    planned_role = _resolve_role(profile, role)
    _validate_service_inputs(profile.name, adapter_name, transport, planned_role, None)
    adapter = _adapter_for(adapter_name)
    context = AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=paths.work_dir / profile.name,
        staging_root=paths.staging_root,
        apply_changes=False,
        role=planned_role,
        remote_stub=asdict(build_worker_stub(profile)),
    )
    ok, reason = adapter.precheck(context)
    if not ok:
        raise ValueError(reason)
    service_name = adapter.service_name(context)
    if not _is_valid_pilottunnel_service_name(service_name):
        raise ValueError(f"Refusing unsafe service name '{service_name}'")
    unit_path = _real_service_unit_path(service_name)
    return ServiceStartRequest(
        profile=profile.name,
        role=planned_role,
        adapter=adapter_name,
        transport=transport,
        service_name=service_name,
        unit_path=str(unit_path),
    )


def _service_status_payload(*, request: ServiceStartRequest, timeout_seconds: float) -> dict[str, Any]:
    status_command = ["systemctl", "status", request.service_name, "--no-pager"]
    active_command = ["systemctl", "is-active", request.service_name]
    status_result = _run_command(status_command, timeout_seconds=timeout_seconds)
    active_result = _run_command(active_command, timeout_seconds=timeout_seconds)
    return {
        "ok": status_result["returncode"] == 0 and not status_result["timed_out"],
        "service_name": request.service_name,
        "unit_path": request.unit_path,
        "profile": request.profile,
        "role": request.role,
        "adapter": request.adapter,
        "transport": request.transport,
        "status_command": " ".join(status_command),
        "command_executed": " ".join(status_command),
        "exit_code": status_result["returncode"],
        "stdout": status_result["stdout"],
        "stderr": status_result["stderr"],
        "timed_out": status_result["timed_out"],
        "is_active": _sanitize_output(active_result["stdout"]) if active_result["stdout"] else "",
        "read_only": True,
        "real_systemd": True,
        "real_systemd_touched": False,
        "service_started": False,
        "service_stopped": False,
        "service_enabled": False,
        "service_disabled": False,
        "firewall_touched": False,
        "routes_touched": False,
        "checked_at": _checked_at(),
    }


def _verify_pilottunnel_unit_ownership(*, request: ServiceStartRequest) -> dict[str, Any]:
    unit_path = Path(request.unit_path)
    if not unit_path.exists():
        return {"ok": False, "message": f"PilotTunnel service unit is missing: {unit_path}"}
    content = unit_path.read_text(encoding="utf-8")
    if PILOTTUNNEL_UNIT_MARKER not in content and f"Description=PilotTunnel {request.profile}" not in content:
        return {"ok": False, "message": f"Service unit is not marked as PilotTunnel-owned: {unit_path}"}
    manifest_path = _real_manifest_path(request.profile, request.adapter, request.transport)
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        owned_destinations = {item.get("destination", "") for item in manifest.get("copied_files", [])}
        if str(unit_path) not in owned_destinations:
            return {"ok": False, "message": "Apply manifest does not mark this service unit as PilotTunnel-owned"}
    return {"ok": True, "message": "PilotTunnel ownership verified"}


def _sanitize_output(value: str) -> str:
    return value.replace("\x00", "").replace("\r", "").strip()[:800]


def _systemd_available() -> bool:
    return bool(shutil.which("systemctl"))


def _is_linux() -> bool:
    return platform.system().lower().startswith("linux")


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return geteuid() == 0


def _real_service_unit_path(service_name: str) -> Path:
    return (REAL_SYSTEM_ROOT / "etc" / "systemd" / "system" / service_name).resolve()


def _real_manifest_path(profile: str, adapter: str, transport: str) -> Path:
    filename = f"{profile}-{adapter}-{transport}.json"
    return (REAL_SYSTEM_ROOT / "var" / "lib" / "pilottunnel" / "apply-manifests" / filename).resolve()


def _is_valid_pilottunnel_service_name(service_name: str) -> bool:
    if not service_name.startswith("pilottunnel-") or not service_name.endswith(".service"):
        return False
    stem = service_name[: -len(".service")]
    parts = stem.split("-")
    if len(parts) < 5:
        return False
    return parts[-1] in {"controller", "worker"}
