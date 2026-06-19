"""Guarded manual tunnel switching using managed service abstractions."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .audit import redact_secrets, write_audit_log
from .config import AppConfig, get_profile
from .healthcheck import DEFAULT_TIMEOUT_SECONDS, build_profile_healthcheck_plan, run_profile_healthchecks, summarize_healthchecks
from .service_plan import build_staged_service_plan
from .state import AppState, RuntimeRecord, save_state
from .switch_engine import SwitchPaths
from .systemd_control import (
    START_CONFIRM_TOKEN,
    STOP_CONFIRM_TOKEN,
    apply_start as apply_systemd_start,
    apply_stop as apply_systemd_stop,
)

SWITCH_CONFIRM_TOKEN = "SWITCH_PILOTTUNNEL_TUNNEL"
NEXT_ACTION_HINTS = ["auto_switch_not_implemented"]


def build_manual_switch_plan(
    *,
    config: AppConfig,
    state: AppState,
    target_tunnel: str,
    runtime_dir: Path,
    service_dir: Path,
    audit_path: Path,
) -> dict[str, Any]:
    try:
        switch_view = _switch_view(
            config=config,
            state=state,
            target_tunnel=target_tunnel,
            runtime_dir=runtime_dir,
            service_dir=service_dir,
            write_units=False,
            audit_path=None,
        )
        payload = {
            "ok": True,
            "action": "switch-plan",
            "current_active_tunnel": switch_view["current"]["tunnel_id"],
            "target_tunnel": switch_view["target"]["tunnel_id"],
            "target_adapter": switch_view["target"]["adapter"],
            "target_service_name": switch_view["target"]["service_name"],
            "previous_active_service_name": switch_view["current"]["service_name"],
            "planned_actions": [
                "would_start_target",
                "would_healthcheck_target",
                "would_stop_previous",
                "would_update_state",
            ],
            "apply_actions": [],
            "warnings": switch_view["service_plan"].get("warnings", []),
            "errors": switch_view["service_plan"].get("errors", []),
            "next_action_hints": list(NEXT_ACTION_HINTS),
            "plan_only": True,
            "real_systemd_touched": False,
            "systemctl_executed": False,
            "rollback_attempted": False,
            "rollback_succeeded": False,
            "state_updated": False,
        }
    except (KeyError, ValueError) as exc:
        payload = _failure_payload(
            action="switch-plan",
            target_tunnel=target_tunnel,
            message=str(exc),
            plan_only=True,
        )
    _audit("manual-switch-plan", target_tunnel, payload, audit_path)
    return redact_secrets(payload)


def apply_manual_switch(
    *,
    config: AppConfig,
    state: AppState,
    state_path: Path,
    target_tunnel: str,
    runtime_dir: Path,
    service_dir: Path,
    confirm: str | None,
    paths: SwitchPaths,
) -> dict[str, Any]:
    if confirm != SWITCH_CONFIRM_TOKEN:
        payload = _failure_payload(
            action="switch-apply",
            target_tunnel=target_tunnel,
            message=f"Refusing manual switch without --confirm {SWITCH_CONFIRM_TOKEN}",
            plan_only=False,
        )
        _audit("manual-switch-apply", target_tunnel, payload, paths.audit_path)
        return redact_secrets(payload)

    state_backup = state.clone()
    try:
        with _manual_switch_lock(paths.lock_dir):
            switch_view = _switch_view(
                config=config,
                state=state,
                target_tunnel=target_tunnel,
                runtime_dir=runtime_dir,
                service_dir=service_dir,
                write_units=True,
                audit_path=paths.audit_path,
            )

            if switch_view["current"]["tunnel_id"] == switch_view["target"]["tunnel_id"]:
                payload = {
                    "ok": True,
                    "action": "switch-apply",
                    "current_active_tunnel": switch_view["current"]["tunnel_id"],
                    "target_tunnel": switch_view["target"]["tunnel_id"],
                    "target_adapter": switch_view["target"]["adapter"],
                    "target_service_name": switch_view["target"]["service_name"],
                    "previous_active_service_name": switch_view["current"]["service_name"],
                    "planned_actions": [],
                    "apply_actions": ["already_active"],
                    "warnings": [],
                    "errors": [],
                    "next_action_hints": list(NEXT_ACTION_HINTS),
                    "plan_only": False,
                    "real_systemd_touched": False,
                    "systemctl_executed": False,
                    "rollback_attempted": False,
                    "rollback_succeeded": False,
                    "state_updated": False,
                }
                _audit("manual-switch-apply", target_tunnel, payload, paths.audit_path)
                return redact_secrets(payload)

            apply_actions: list[str] = []
            warnings = list(switch_view["service_plan"].get("warnings", []))
            errors = list(switch_view["service_plan"].get("errors", []))
            rollback_attempted = False
            rollback_succeeded = False

            target_start = apply_systemd_start(
                service_dir=service_dir,
                service_name=switch_view["target"]["service_name"],
                confirm=START_CONFIRM_TOKEN,
                audit_path=paths.audit_path,
            )
            if not target_start["ok"]:
                payload = _failure_payload(
                    action="switch-apply",
                    target_tunnel=target_tunnel,
                    message="Target service failed to start",
                    plan_only=False,
                )
                payload.update(
                    {
                        "current_active_tunnel": switch_view["current"]["tunnel_id"],
                        "target_tunnel": switch_view["target"]["tunnel_id"],
                        "target_adapter": switch_view["target"]["adapter"],
                        "target_service_name": switch_view["target"]["service_name"],
                        "previous_active_service_name": switch_view["current"]["service_name"],
                        "apply_actions": ["target_start_failed"],
                        "target_start": target_start,
                    }
                )
                _audit("manual-switch-apply", target_tunnel, payload, paths.audit_path)
                return redact_secrets(payload)
            apply_actions.append("started_target")

            target_profile = get_profile(config, switch_view["target"]["tunnel_id"])
            healthcheck_plan = build_profile_healthcheck_plan(
                profile=target_profile,
                node_role=config.node.normalized_role or "controller",
                timeout=DEFAULT_TIMEOUT_SECONDS,
                include_all=True,
                role_aware=True,
            )
            target_healthcheck = summarize_healthchecks(
                run_profile_healthchecks(
                    profile=target_profile,
                    node_role=config.node.normalized_role or "controller",
                    timeout=DEFAULT_TIMEOUT_SECONDS,
                    include_all=True,
                    role_aware=True,
                ),
                profile=target_profile.name,
                role=config.node.normalized_role or "controller",
            )
            if not target_healthcheck["ok"]:
                cleanup = apply_systemd_stop(
                    service_dir=service_dir,
                    service_name=switch_view["target"]["service_name"],
                    confirm=STOP_CONFIRM_TOKEN,
                    audit_path=paths.audit_path,
                )
                rollback_attempted = True
                rollback_succeeded = cleanup["ok"]
                payload = _failure_payload(
                    action="switch-apply",
                    target_tunnel=target_tunnel,
                    message="Target healthcheck failed before previous active service was stopped",
                    plan_only=False,
                )
                payload.update(
                    {
                        "current_active_tunnel": switch_view["current"]["tunnel_id"],
                        "target_tunnel": switch_view["target"]["tunnel_id"],
                        "target_adapter": switch_view["target"]["adapter"],
                        "target_service_name": switch_view["target"]["service_name"],
                        "previous_active_service_name": switch_view["current"]["service_name"],
                        "apply_actions": apply_actions + ["target_healthcheck_failed"],
                        "target_healthcheck": target_healthcheck,
                        "target_healthcheck_plan": healthcheck_plan,
                        "rollback_attempted": rollback_attempted,
                        "rollback_succeeded": rollback_succeeded,
                        "rollback_details": cleanup,
                    }
                )
                _audit("manual-switch-apply", target_tunnel, payload, paths.audit_path)
                return redact_secrets(payload)
            apply_actions.append("target_healthcheck_passed")

            previous_stop = apply_systemd_stop(
                service_dir=service_dir,
                service_name=switch_view["current"]["service_name"],
                confirm=STOP_CONFIRM_TOKEN,
                audit_path=paths.audit_path,
            )
            if not previous_stop["ok"]:
                cleanup = apply_systemd_stop(
                    service_dir=service_dir,
                    service_name=switch_view["target"]["service_name"],
                    confirm=STOP_CONFIRM_TOKEN,
                    audit_path=paths.audit_path,
                )
                rollback_attempted = True
                rollback_succeeded = cleanup["ok"]
                payload = _failure_payload(
                    action="switch-apply",
                    target_tunnel=target_tunnel,
                    message="Previous active service could not be stopped; switch aborted",
                    plan_only=False,
                )
                payload.update(
                    {
                        "current_active_tunnel": switch_view["current"]["tunnel_id"],
                        "target_tunnel": switch_view["target"]["tunnel_id"],
                        "target_adapter": switch_view["target"]["adapter"],
                        "target_service_name": switch_view["target"]["service_name"],
                        "previous_active_service_name": switch_view["current"]["service_name"],
                        "apply_actions": apply_actions,
                        "target_healthcheck": target_healthcheck,
                        "target_healthcheck_plan": healthcheck_plan,
                        "rollback_attempted": rollback_attempted,
                        "rollback_succeeded": rollback_succeeded,
                        "rollback_details": cleanup,
                        "previous_stop": previous_stop,
                    }
                )
                _audit("manual-switch-apply", target_tunnel, payload, paths.audit_path)
                return redact_secrets(payload)
            apply_actions.append("stopped_previous")

            try:
                _update_state_for_success(state, config, switch_view)
                save_state(state, state_path)
                apply_actions.append("state_updated")
            except OSError as exc:
                state.profiles = state_backup.profiles
                state.binaries = state_backup.binaries
                state.manual_active_tunnel = state_backup.manual_active_tunnel
                state.manual_previous_tunnel = state_backup.manual_previous_tunnel
                state.last_manual_switch = state_backup.last_manual_switch
                rollback_attempted = True
                rollback_details = _rollback_previous_active(
                    service_dir=service_dir,
                    target_service_name=switch_view["target"]["service_name"],
                    previous_service_name=switch_view["current"]["service_name"],
                    audit_path=paths.audit_path,
                )
                rollback_succeeded = rollback_details["ok"]
                _audit(
                    "manual-switch-rollback",
                    switch_view["current"]["tunnel_id"],
                    {
                        "target_tunnel": switch_view["target"]["tunnel_id"],
                        "target_service_name": switch_view["target"]["service_name"],
                        "previous_active_service_name": switch_view["current"]["service_name"],
                        "rollback_attempted": True,
                        "rollback_succeeded": rollback_succeeded,
                        "rollback_details": rollback_details,
                    },
                    paths.audit_path,
                )
                payload = _failure_payload(
                    action="switch-apply",
                    target_tunnel=target_tunnel,
                    message=f"State update failed after service switch: {exc}",
                    plan_only=False,
                )
                payload.update(
                    {
                        "current_active_tunnel": switch_view["current"]["tunnel_id"],
                        "target_tunnel": switch_view["target"]["tunnel_id"],
                        "target_adapter": switch_view["target"]["adapter"],
                        "target_service_name": switch_view["target"]["service_name"],
                        "previous_active_service_name": switch_view["current"]["service_name"],
                        "apply_actions": apply_actions,
                        "target_healthcheck": target_healthcheck,
                        "target_healthcheck_plan": healthcheck_plan,
                        "rollback_attempted": rollback_attempted,
                        "rollback_succeeded": rollback_succeeded,
                        "rollback_details": rollback_details,
                    }
                )
                _audit("manual-switch-apply", target_tunnel, payload, paths.audit_path)
                return redact_secrets(payload)

            payload = {
                "ok": True,
                "action": "switch-apply",
                "current_active_tunnel": switch_view["current"]["tunnel_id"],
                "target_tunnel": switch_view["target"]["tunnel_id"],
                "target_adapter": switch_view["target"]["adapter"],
                "target_service_name": switch_view["target"]["service_name"],
                "previous_active_service_name": switch_view["current"]["service_name"],
                "planned_actions": [
                    "would_start_target",
                    "would_healthcheck_target",
                    "would_stop_previous",
                    "would_update_state",
                ],
                "apply_actions": apply_actions,
                "warnings": warnings,
                "errors": errors,
                "target_healthcheck": target_healthcheck,
                "target_healthcheck_plan": healthcheck_plan,
                "rollback_attempted": rollback_attempted,
                "rollback_succeeded": rollback_succeeded,
                "state_updated": True,
                "next_action_hints": list(NEXT_ACTION_HINTS),
                "plan_only": False,
                "real_systemd_touched": True,
                "systemctl_executed": True,
                "service_started": True,
                "service_stopped": True,
                "service_enabled": False,
                "service_disabled": False,
                "firewall_touched": False,
                "routes_touched": False,
            }
            _audit("manual-switch-apply", target_tunnel, payload, paths.audit_path)
            return redact_secrets(payload)
    except ValueError as exc:
        payload = _failure_payload(
            action="switch-apply",
            target_tunnel=target_tunnel,
            message=str(exc),
            plan_only=False,
        )
        _audit("manual-switch-apply", target_tunnel, payload, paths.audit_path)
        return redact_secrets(payload)


def _switch_view(
    *,
    config: AppConfig,
    state: AppState,
    target_tunnel: str,
    runtime_dir: Path,
    service_dir: Path,
    write_units: bool,
    audit_path: Path | None,
) -> dict[str, Any]:
    get_profile(config, target_tunnel)
    service_plan = build_staged_service_plan(
        config=config,
        state=state,
        runtime_dir=runtime_dir,
        service_dir=service_dir,
        requested_platform="auto",
        audit_path=audit_path,
        write_units=write_units,
    )
    if not service_plan["ok"]:
        raise ValueError("Current staged service plan is not valid for switching")
    services_by_tunnel = {item["tunnel_id"]: item for item in service_plan["services"]}
    if target_tunnel not in services_by_tunnel:
        raise ValueError(f"Target tunnel '{target_tunnel}' does not exist in the current service plan")
    current = next((item for item in service_plan["services"] if item["runtime_role"] == "active"), None)
    if current is None:
        raise ValueError("Current active tunnel is not available in the staged service plan")
    target = services_by_tunnel[target_tunnel]
    if target["runtime_role"] == "config_only" or not target["service_unit_rendered"]:
        raise ValueError(f"Target tunnel '{target_tunnel}' is config_only and cannot be switched in this workflow")
    return {"current": current, "target": target, "service_plan": service_plan}


def _update_state_for_success(state: AppState, config: AppConfig, switch_view: dict[str, Any]) -> None:
    current = switch_view["current"]
    target = switch_view["target"]
    target_profile = get_profile(config, target["tunnel_id"])
    previous_record = state.profiles.setdefault(current["tunnel_id"], RuntimeRecord(profile=current["tunnel_id"]))
    target_record = state.profiles.setdefault(target["tunnel_id"], RuntimeRecord(profile=target["tunnel_id"]))
    previous_record.healthy = False
    previous_record.last_error = ""
    target_record.active_adapter = target["adapter"]
    target_record.active_transport = target_profile.active_transport
    target_record.active_layer = target_profile.active_layer
    target_record.service_name = target["service_name"]
    target_record.role = target_profile.role
    target_record.healthy = True
    target_record.last_error = ""
    target_record.last_switch_at = _checked_at()
    state.manual_previous_tunnel = current["tunnel_id"]
    state.manual_active_tunnel = target["tunnel_id"]
    state.last_manual_switch = {
        "current_active_tunnel": current["tunnel_id"],
        "target_tunnel": target["tunnel_id"],
        "target_service_name": target["service_name"],
        "previous_active_service_name": current["service_name"],
        "applied_at": _checked_at(),
    }


def _rollback_previous_active(*, service_dir: Path, target_service_name: str, previous_service_name: str, audit_path: Path) -> dict[str, Any]:
    stop_target = apply_systemd_stop(
        service_dir=service_dir,
        service_name=target_service_name,
        confirm=STOP_CONFIRM_TOKEN,
        audit_path=audit_path,
    )
    start_previous = apply_systemd_start(
        service_dir=service_dir,
        service_name=previous_service_name,
        confirm=START_CONFIRM_TOKEN,
        audit_path=audit_path,
    )
    return {
        "ok": stop_target["ok"] and start_previous["ok"],
        "stop_target": stop_target,
        "start_previous": start_previous,
    }


def _failure_payload(*, action: str, target_tunnel: str, message: str, plan_only: bool) -> dict[str, Any]:
    return {
        "ok": False,
        "action": action,
        "target_tunnel": target_tunnel,
        "warnings": [],
        "errors": [message],
        "next_action_hints": list(NEXT_ACTION_HINTS),
        "plan_only": plan_only,
        "real_systemd_touched": False,
        "systemctl_executed": False,
        "rollback_attempted": False,
        "rollback_succeeded": False,
        "state_updated": False,
        "service_started": False,
        "service_stopped": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def _audit(action: str, profile: str, payload: dict[str, Any], path: Path) -> None:
    write_audit_log(action, profile, redact_secrets(payload), path)


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _manual_switch_lock(lock_dir: Path):
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "manual-switch.lock"
    try:
        handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise ValueError(f"Concurrent manual switch blocked by active lock: {lock_path}") from exc
    try:
        os.write(handle, str(os.getpid()).encode("utf-8"))
        yield lock_path
    finally:
        os.close(handle)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
