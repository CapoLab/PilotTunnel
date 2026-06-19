"""Controlled installation of staged PilotTunnel systemd unit files."""

from __future__ import annotations

import json
import os
import shlex
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .audit import redact_secrets, write_audit_log
from .config import AppConfig
from .service_plan import SERVICE_UNIT_MARKER, build_staged_service_plan
from .state import AppState
from .systemd import render_unit_file

INSTALL_CONFIRM_TOKEN = "INSTALL_PILOTTUNNEL_SERVICES"
INSTALL_SUMMARY_FILENAME = "pilottunnel-service-install-summary.json"
BACKUP_ROOT_DIRNAME = ".pilottunnel-service-install-backups"
NEXT_ACTION_HINTS = [
    "daemon_reload_not_implemented",
    "start_not_implemented",
    "enable_not_implemented",
]


def build_service_install_plan(
    *,
    config: AppConfig,
    state: AppState,
    runtime_dir: Path,
    service_dir: Path,
    target_dir: Path,
    requested_platform: str | None,
    allow_system_dir: bool,
    audit_path: Path,
) -> dict:
    try:
        assessment = _assess_service_install(
            config=config,
            state=state,
            runtime_dir=runtime_dir,
            service_dir=service_dir,
            target_dir=target_dir,
            requested_platform=requested_platform,
            allow_system_dir=allow_system_dir,
            create_target_dir=False,
        )
    except (KeyError, ValueError) as exc:
        payload = _failure_payload(
            action="service-install-plan",
            message=str(exc),
            service_dir=service_dir,
            target_dir=target_dir,
        )
        _audit("service-install-plan", payload, audit_path)
        return payload

    payload = _payload_from_assessment("service-install-plan", assessment, plan_only=True)
    _audit("service-install-plan", payload, audit_path)
    return payload


def apply_service_install(
    *,
    config: AppConfig,
    state: AppState,
    runtime_dir: Path,
    service_dir: Path,
    target_dir: Path,
    requested_platform: str | None,
    allow_system_dir: bool,
    replace_existing: bool,
    confirm: str | None,
    audit_path: Path,
) -> dict:
    if confirm != INSTALL_CONFIRM_TOKEN:
        payload = _failure_payload(
            action="service-install-apply",
            message=f"Refusing service install without --confirm {INSTALL_CONFIRM_TOKEN}",
            service_dir=service_dir,
            target_dir=target_dir,
        )
        payload["summary_file"] = str(target_dir.resolve() / INSTALL_SUMMARY_FILENAME)
        _audit("service-install-apply", payload, audit_path)
        return payload

    try:
        assessment = _assess_service_install(
            config=config,
            state=state,
            runtime_dir=runtime_dir,
            service_dir=service_dir,
            target_dir=target_dir,
            requested_platform=requested_platform,
            allow_system_dir=allow_system_dir,
            create_target_dir=True,
        )
    except (KeyError, ValueError) as exc:
        payload = _failure_payload(
            action="service-install-apply",
            message=str(exc),
            service_dir=service_dir,
            target_dir=target_dir,
        )
        _audit("service-install-apply", payload, audit_path)
        return payload

    replace_candidates = [item for item in assessment["services"] if item["action"] == "would_replace"]
    if replace_candidates and not replace_existing:
        names = ", ".join(item["service_name"] for item in replace_candidates)
        payload = _payload_from_assessment(
            "service-install-apply",
            assessment,
            plan_only=False,
            ok=False,
            message=f"Refusing to replace existing unit files without --replace-existing: {names}",
        )
        _audit("service-install-apply", payload, audit_path)
        return payload

    applied_services = []
    backup_root = _backup_root(assessment["target_dir"]) if replace_candidates else None
    if backup_root is not None:
        backup_root.mkdir(parents=True, exist_ok=True)
        _set_file_mode(backup_root, 0o700)

    for entry in assessment["services"]:
        applied = dict(entry)
        applied["backup_path"] = ""
        applied["installed"] = False
        if entry["action"] == "install":
            _atomic_write(Path(entry["target_unit_path"]), entry["expected_unit_content"].encode("utf-8"), file_mode=0o644)
            applied["installed"] = True
        elif entry["action"] == "would_replace":
            target_path = Path(entry["target_unit_path"])
            backup_path = _backup_path(assessment["target_dir"], backup_root, target_path)
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target_path, backup_path)
            _set_file_mode(backup_path, 0o600)
            _atomic_write(target_path, entry["expected_unit_content"].encode("utf-8"), file_mode=0o644)
            applied["action"] = "install"
            applied["backup_path"] = str(backup_path)
            applied["installed"] = True
        applied_services.append(applied)

    payload = _payload_from_assessment(
        "service-install-apply",
        {**assessment, "services": applied_services},
        plan_only=False,
    )
    summary_path = Path(payload["summary_file"])
    _atomic_write(summary_path, json.dumps(redact_secrets(payload), indent=2, sort_keys=True).encode("utf-8"), file_mode=0o600)
    _audit("service-install-apply", payload, audit_path)
    return payload


def _assess_service_install(
    *,
    config: AppConfig,
    state: AppState,
    runtime_dir: Path,
    service_dir: Path,
    target_dir: Path,
    requested_platform: str | None,
    allow_system_dir: bool,
    create_target_dir: bool,
) -> dict:
    resolved_service_dir = _validated_service_dir(service_dir)
    resolved_target_dir = _validated_target_dir(target_dir, allow_system_dir=allow_system_dir, create=create_target_dir)
    service_plan = build_staged_service_plan(
        config=config,
        state=state,
        runtime_dir=runtime_dir,
        service_dir=resolved_service_dir,
        requested_platform=requested_platform,
        audit_path=None,
        write_units=False,
    )
    if not service_plan["ok"]:
        raise ValueError("Service render plan must succeed before staged units can be installed")

    services = []
    warnings = list(service_plan.get("warnings", []))
    errors = list(service_plan.get("errors", []))
    installable = []

    for service in service_plan["services"]:
        role = service["runtime_role"]
        service_name = service["service_name"]
        target_path = str(_target_unit_path(resolved_target_dir, service_name)) if service_name else ""
        if role == "config_only":
            services.append(
                {
                    "tunnel_id": service["tunnel_id"],
                    "adapter": service["adapter"],
                    "runtime_role": role,
                    "service_name": "",
                    "staged_unit_path": "",
                    "target_unit_path": "",
                    "action": "skipped_config_only",
                    "backup_path": "",
                    "warnings": list(service.get("warnings", [])),
                    "errors": list(service.get("errors", [])),
                    "next_action_hints": list(NEXT_ACTION_HINTS),
                }
            )
            continue

        expected_content = _expected_unit_content(service)
        staged_path = _validated_staged_unit_path(Path(service["staged_unit_file_path"]), resolved_service_dir)
        staged_errors = _verify_staged_unit(staged_path, expected_content)
        target_action = _target_action(resolved_target_dir, service_name, expected_content)
        action = target_action["action"] if not staged_errors else "error"
        entry = {
            "tunnel_id": service["tunnel_id"],
            "adapter": service["adapter"],
            "runtime_role": role,
            "service_name": service_name,
            "staged_unit_path": str(staged_path),
            "target_unit_path": target_path,
            "action": action,
            "backup_path": "",
            "warnings": list(service.get("warnings", [])) + target_action.get("warnings", []),
            "errors": list(service.get("errors", [])) + staged_errors + target_action.get("errors", []),
            "next_action_hints": list(NEXT_ACTION_HINTS),
            "expected_unit_content": expected_content,
        }
        if entry["errors"]:
            errors.extend(entry["errors"])
        installable.append(service["service_name"])
        services.append(entry)

    return {
        "service_dir": resolved_service_dir,
        "target_dir": resolved_target_dir,
        "summary_file": resolved_target_dir / INSTALL_SUMMARY_FILENAME,
        "service_plan": service_plan,
        "services": services,
        "warnings": sorted(set(filter(None, warnings))),
        "errors": sorted(set(filter(None, errors))),
        "installable_services": installable,
    }


def _payload_from_assessment(action: str, assessment: dict, *, plan_only: bool, ok: bool | None = None, message: str = "") -> dict:
    services = []
    visible_errors: list[str] = list(assessment.get("errors", []))
    visible_warnings: list[str] = list(assessment.get("warnings", []))
    for entry in assessment["services"]:
        item = {key: value for key, value in entry.items() if key != "expected_unit_content"}
        services.append(item)
        visible_errors.extend(item.get("errors", []))
        visible_warnings.extend(item.get("warnings", []))
    return {
        "ok": (not visible_errors) if ok is None else ok,
        "action": action,
        "message": message,
        "service_dir": str(assessment["service_dir"]),
        "target_dir": str(assessment["target_dir"]),
        "summary_file": str(assessment["summary_file"]),
        "services": services,
        "warnings": sorted(set(filter(None, visible_warnings))),
        "errors": sorted(set(filter(None, visible_errors))),
        "plan_only": plan_only,
        "real_systemd_touched": False,
        "service_started": False,
        "service_enabled": False,
        "systemctl_executed": False,
        "downloads_performed": False,
        "firewall_touched": False,
        "routes_touched": False,
        "target_service_count": len([item for item in services if item["action"] != "skipped_config_only"]),
    }


def _failure_payload(*, action: str, message: str, service_dir: Path, target_dir: Path) -> dict:
    return {
        "ok": False,
        "action": action,
        "message": message,
        "service_dir": str(service_dir),
        "target_dir": str(target_dir),
        "services": [],
        "warnings": [],
        "errors": [message],
        "plan_only": action.endswith("plan"),
        "real_systemd_touched": False,
        "service_started": False,
        "service_enabled": False,
        "systemctl_executed": False,
        "downloads_performed": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def _expected_unit_content(service: dict) -> str:
    rendered = render_unit_file(
        unit_name=service["service_name"],
        description=f"PilotTunnel {service['tunnel_id']} {service['adapter']} {service['runtime_role']}",
        command=shlex.join(service["exec_start_argv_summary"]),
        output_dir=Path("."),
        apply_changes=False,
    )
    return rendered.content


def _target_action(target_dir: Path, service_name: str, expected_content: str) -> dict:
    target_path = _target_unit_path(target_dir, service_name)
    if not target_path.exists():
        return {"action": "install", "warnings": [], "errors": []}
    if target_path.is_symlink():
        return {"action": "error", "warnings": [], "errors": [f"Symlink escape blocked in target dir: {target_path}"]}
    existing = target_path.read_text(encoding="utf-8")
    if existing == expected_content:
        return {"action": "unchanged", "warnings": [], "errors": []}
    return {"action": "would_replace", "warnings": [], "errors": []}


def _verify_staged_unit(path: Path, expected_content: str) -> list[str]:
    if not path.exists():
        return [f"Staged service unit does not exist: {path}"]
    if path.is_symlink():
        return [f"Symlink escape blocked in staged service dir: {path}"]
    if path.read_text(encoding="utf-8") != expected_content:
        return [f"Staged unit does not match the current service plan: {path}"]
    if not expected_content.startswith(SERVICE_UNIT_MARKER):
        return [f"Refusing to install non-PilotTunnel unit content: {path}"]
    return []


def _validated_service_dir(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for service staging dir: {path!r}")
    _validate_parent_chain(path)
    resolved = path.resolve()
    _validate_parent_chain(resolved)
    if not resolved.exists():
        raise ValueError(f"Service staging dir does not exist: {path}")
    if not resolved.is_dir():
        raise ValueError(f"Service staging dir must be a directory: {path}")
    return resolved


def _validated_target_dir(path: Path, *, allow_system_dir: bool, create: bool) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for target dir: {path!r}")
    if _targets_real_systemd(path) and not allow_system_dir:
        raise ValueError("Refusing to install service files under /etc/systemd/system without --allow-system-dir")
    _validate_parent_chain(path)
    resolved = path.resolve()
    _validate_parent_chain(resolved)
    if resolved == Path(resolved.anchor):
        raise ValueError(f"Refusing unsafe target dir: {path}")
    if resolved.exists() and not resolved.is_dir():
        raise ValueError(f"Target dir must be a directory: {path}")
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _validated_staged_unit_path(path: Path, service_dir: Path) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for staged unit file: {path!r}")
    resolved = path.resolve()
    _validate_parent_chain(resolved)
    if service_dir != resolved.parent and service_dir not in resolved.parents:
        raise ValueError(f"Refusing to read staged unit outside service staging dir: {path}")
    return resolved


def _target_unit_path(target_dir: Path, service_name: str) -> Path:
    path = (target_dir / service_name).resolve()
    _validate_parent_chain(path)
    if target_dir != path.parent and target_dir not in path.parents:
        raise ValueError(f"Refusing to write outside target dir: {path}")
    return path


def _validate_parent_chain(path: Path) -> None:
    current = path
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError(f"Symlink escape blocked for path: {current}")
        if current.parent == current:
            return
        current = current.parent


def _targets_real_systemd(path: Path) -> bool:
    normalized = path.as_posix().replace("\\", "/").lower().rstrip("/")
    return normalized == "/etc/systemd/system" or normalized.startswith("/etc/systemd/system/")


def _backup_root(target_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return target_dir / BACKUP_ROOT_DIRNAME / stamp


def _backup_path(target_dir: Path, backup_root: Path | None, target_path: Path) -> Path:
    if backup_root is None:
        raise ValueError("Backup root is required when replacing existing service units")
    relative = target_path.relative_to(target_dir)
    return backup_root / relative


def _atomic_write(path: Path, data: bytes, *, file_mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_parent_chain(path.parent)
    temp_path = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    temp_path.write_bytes(data)
    _set_file_mode(temp_path, file_mode)
    temp_path.replace(path)
    _set_file_mode(path, file_mode)


def _set_file_mode(path: Path, mode: int) -> None:
    if os.name != "nt":
        path.chmod(mode)


def _audit(action: str, payload: dict, path: Path) -> None:
    write_audit_log(action, action, redact_secrets(payload), path)
