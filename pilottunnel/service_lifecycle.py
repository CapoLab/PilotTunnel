"""Read-only service lifecycle planning and inspection."""

from __future__ import annotations

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
from .state import AppState
from .switch_engine import SwitchPaths

DEFAULT_SERVICE_TIMEOUT_SECONDS = 2.0


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
