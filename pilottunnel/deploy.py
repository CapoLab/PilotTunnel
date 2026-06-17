"""Controlled deployment workflow orchestration."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any

from .audit import write_audit_log
from .config import AppConfig, Profile, canonical_role, get_profile, validate_profile_name
from .healthcheck import DEFAULT_TIMEOUT_SECONDS, build_profile_healthcheck_plan, run_profile_healthchecks, summarize_healthchecks
from .install_plan import REAL_HOST_ROOT, apply_install
from .readiness import build_readiness_report
from .service_lifecycle import enable_service, inspect_service_status, run_daemon_reload, start_service, verify_service_ownership
from .state import AppState
from .switch_engine import SwitchPaths


def build_deploy_plan(
    *,
    config: AppConfig,
    state: AppState,
    registry,
    config_path: Path,
    switch_paths: SwitchPaths,
    profile_name: str,
    adapter_name: str,
    transport: str,
    role: str | None,
    enable_after_start: bool = False,
    require_healthcheck: bool = False,
    staging_root: Path | None = None,
) -> dict[str, Any]:
    profile = get_profile(config, validate_profile_name(profile_name))
    resolved_role = _resolved_role(config, profile, role)
    readiness = build_readiness_report(
        config=config,
        state=state,
        registry=registry,
        config_path=config_path,
        switch_paths=switch_paths,
        profile_name=profile.name,
        adapter_name=adapter_name,
        transport=transport,
        staging_root=staging_root or switch_paths.staging_root,
    )
    healthcheck_plan = build_profile_healthcheck_plan(
        profile=profile,
        node_role=resolved_role,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        include_all=True,
        role_aware=True,
    )
    blockers = list(readiness.get("blockers", []))
    warnings = list(readiness.get("warnings", []))
    steps = [
        _plan_step(
            name="readiness_report",
            required=True,
            enabled=True,
            description="Check node, profile, staged files, binary, and service readiness",
            command=_cli_command("readiness report", profile.name, adapter_name, transport),
        ),
        _plan_step(
            name="staged_file_check",
            required=True,
            enabled=True,
            description="Require staged config and systemd unit files before deploy apply",
            command="internal readiness staged file check",
        ),
        _plan_step(
            name="binary_imported_check",
            required=True,
            enabled=True,
            description="Require imported adapter binary before real-host file apply",
            command="internal readiness imported binary check",
        ),
        _plan_step(
            name="install_apply",
            required=True,
            enabled=True,
            description="Copy PilotTunnel-owned config, unit, and binary files onto the real host",
            command=_install_apply_command(profile.name, adapter_name, transport),
        ),
        _plan_step(
            name="daemon_reload",
            required=True,
            enabled=True,
            description="Reload systemd unit metadata after real-host file apply",
            command="python -m pilottunnel.cli service daemon-reload --real-systemd --confirm DAEMON_RELOAD",
        ),
        _plan_step(
            name="service_start",
            required=True,
            enabled=True,
            description="Start the PilotTunnel-owned service using the existing guarded start gate",
            command=_service_command("start", profile.name, adapter_name, transport, resolved_role, confirm="START_SERVICE"),
        ),
        _plan_step(
            name="healthcheck",
            required=require_healthcheck,
            enabled=require_healthcheck,
            description="Run TCP healthchecks after service start when requested",
            command=_healthcheck_command(profile.name),
        ),
        _plan_step(
            name="service_enable",
            required=False,
            enabled=enable_after_start,
            description="Optionally enable the service after a successful start and healthcheck",
            command=_service_command("enable", profile.name, adapter_name, transport, resolved_role, confirm="ENABLE_SERVICE"),
        ),
    ]
    payload = {
        "ok": not _readiness_blocked(readiness),
        "action": "deploy-plan",
        "profile": profile.name,
        "role": resolved_role,
        "adapter": adapter_name,
        "transport": transport,
        "readiness": readiness,
        "steps": steps,
        "healthcheck_plan": healthcheck_plan,
        "rollback_recovery_suggestions": [
            "If install apply fails, fix staged files or binary import before retrying deploy apply.",
            "If daemon-reload fails, inspect unit content and systemd diagnostics before retrying.",
            "If service start or healthcheck fails, review service status/logs and consider a manual stop or rollback.",
            "If enable fails, review systemd enable diagnostics; deploy apply does not restart or stop automatically.",
        ],
        "exact_commands": [step["command"] for step in steps if step.get("command")],
        "blockers": blockers,
        "warnings": _dedupe(warnings),
        "plan_only": True,
        "real_systemd_touched": False,
        "service_started": False,
        "service_enabled": False,
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
    }
    _audit("deploy-plan", profile.name, payload, path=switch_paths.audit_path)
    return payload


def apply_deploy(
    *,
    config: AppConfig,
    state: AppState,
    registry,
    config_path: Path,
    switch_paths: SwitchPaths,
    profile_name: str,
    adapter_name: str,
    transport: str,
    role: str | None,
    real_host: bool,
    confirm: str | None,
    enable_after_start: bool = False,
    require_healthcheck: bool = False,
    staging_root: Path | None = None,
) -> dict[str, Any]:
    profile = get_profile(config, validate_profile_name(profile_name))
    resolved_role = _resolved_role(config, profile, role)
    attempt = {
        "profile": profile.name,
        "role": resolved_role,
        "adapter": adapter_name,
        "transport": transport,
        "real_host": real_host,
        "confirm": confirm or "",
        "enable_after_start": enable_after_start,
        "require_healthcheck": require_healthcheck,
    }
    if not real_host:
        payload = {
            "ok": False,
            "message": "Refusing deploy apply without --real-host. Use deploy plan for read-only guidance.",
            "plan_only": False,
            "real_systemd_touched": False,
            "service_started": False,
            "service_enabled": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload
    if confirm != "DEPLOY_APPLY":
        payload = {
            "ok": False,
            "message": "Refusing deploy apply without --confirm DEPLOY_APPLY",
            "plan_only": False,
            "real_systemd_touched": False,
            "service_started": False,
            "service_enabled": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload
    if not _is_linux_host():
        payload = {
            "ok": False,
            "message": "Deploy apply is Linux-only",
            "plan_only": False,
            "real_systemd_touched": False,
            "service_started": False,
            "service_enabled": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload
    if not config.node.initialized:
        payload = {
            "ok": False,
            "message": "Deploy apply requires an initialized node role",
            "plan_only": False,
            "real_systemd_touched": False,
            "service_started": False,
            "service_enabled": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload
    if not _is_admin_or_root():
        payload = {
            "ok": False,
            "message": "Deploy apply requires root/admin privileges",
            "plan_only": False,
            "real_systemd_touched": False,
            "service_started": False,
            "service_enabled": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload

    readiness = build_readiness_report(
        config=config,
        state=state,
        registry=registry,
        config_path=config_path,
        switch_paths=switch_paths,
        profile_name=profile.name,
        adapter_name=adapter_name,
        transport=transport,
        staging_root=staging_root or switch_paths.staging_root,
    )
    if _readiness_blocked(readiness):
        payload = _deploy_failure(
            profile=profile.name,
            step="readiness_report",
            message="Deploy apply requires readiness to be unblocked",
            attempt=attempt,
            readiness=readiness,
        )
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload
    if not readiness.get("staged_files_exist"):
        payload = _deploy_failure(
            profile=profile.name,
            step="staged_file_check",
            message="Deploy apply requires staged files to exist",
            attempt=attempt,
            readiness=readiness,
        )
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload
    if not readiness.get("binary_imported"):
        payload = _deploy_failure(
            profile=profile.name,
            step="binary_imported_check",
            message="Deploy apply requires an imported binary for this adapter",
            attempt=attempt,
            readiness=readiness,
        )
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload

    install_result = apply_install(
        profile=profile,
        adapter_name=adapter_name,
        transport=transport,
        role=resolved_role,
        paths=switch_paths,
        state=state,
        install_root=None,
        confirm="REAL_FILES_APPLY",
        dry_run=False,
        require_healthcheck=False,
        real_host_files=True,
        node_initialized=True,
        node_role=resolved_role,
        readiness_report=readiness,
    )
    if not install_result.get("ok"):
        payload = _deploy_failure(
            profile=profile.name,
            step="install_apply",
            message=install_result.get("message", "Install apply failed"),
            attempt=attempt,
            readiness=readiness,
            install_apply=install_result,
        )
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload

    ownership = verify_service_ownership(
        profile=profile,
        adapter_name=adapter_name,
        transport=transport,
        role=resolved_role,
        paths=switch_paths,
    )
    if not ownership.get("ok"):
        payload = _deploy_failure(
            profile=profile.name,
            step="ownership_check",
            message=ownership.get("message", "PilotTunnel ownership verification failed"),
            attempt=attempt,
            readiness=readiness,
            install_apply=install_result,
            ownership=ownership,
        )
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload

    daemon_reload = run_daemon_reload(
        paths=switch_paths,
        confirm="DAEMON_RELOAD",
        real_systemd=True,
    )
    if not daemon_reload.get("ok"):
        payload = _deploy_failure(
            profile=profile.name,
            step="daemon_reload",
            message=daemon_reload.get("message", "Daemon reload failed"),
            attempt=attempt,
            readiness=readiness,
            install_apply=install_result,
            ownership=ownership,
            daemon_reload=daemon_reload,
        )
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload

    service_start = start_service(
        profile=profile,
        adapter_name=adapter_name,
        transport=transport,
        role=resolved_role,
        paths=switch_paths,
        confirm="START_SERVICE",
        real_systemd=True,
        require_healthcheck=require_healthcheck,
        healthcheck_timeout=DEFAULT_TIMEOUT_SECONDS,
    )
    if not service_start.get("ok"):
        payload = _deploy_failure(
            profile=profile.name,
            step="service_start",
            message=service_start.get("message", "Service start failed"),
            attempt=attempt,
            readiness=readiness,
            install_apply=install_result,
            ownership=ownership,
            daemon_reload=daemon_reload,
            service_start=service_start,
        )
        _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
        return payload

    service_enable = None
    if enable_after_start:
        service_enable = enable_service(
            profile=profile,
            adapter_name=adapter_name,
            transport=transport,
            role=resolved_role,
            paths=switch_paths,
            confirm="ENABLE_SERVICE",
            real_systemd=True,
        )
        if not service_enable.get("ok"):
            payload = _deploy_failure(
                profile=profile.name,
                step="service_enable",
                message=service_enable.get("message", "Service enable failed"),
                attempt=attempt,
                readiness=readiness,
                install_apply=install_result,
                ownership=ownership,
                daemon_reload=daemon_reload,
                service_start=service_start,
                service_enable=service_enable,
            )
            _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
            return payload

    payload = {
        "ok": True,
        "action": "deploy-apply",
        "message": "Deploy apply completed",
        "profile": profile.name,
        "role": resolved_role,
        "adapter": adapter_name,
        "transport": transport,
        "readiness": readiness,
        "install_apply": install_result,
        "ownership": ownership,
        "daemon_reload": daemon_reload,
        "service_start": service_start,
        "service_enable": service_enable,
        "status": (service_enable or service_start).get("status"),
        "healthcheck_ok": service_start.get("healthcheck_ok", "skipped"),
        "healthcheck": service_start.get("healthcheck"),
        "enable_after_start": enable_after_start,
        "require_healthcheck": require_healthcheck,
        "real_host": True,
        "real_systemd_touched": True,
        "service_started": bool(service_start.get("service_started")),
        "service_enabled": bool(service_enable and service_enable.get("service_enabled")),
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
        "systemctl_executed": True,
    }
    _audit("deploy-apply", profile.name, payload, path=switch_paths.audit_path)
    return payload


def build_deploy_status(
    *,
    config: AppConfig,
    state: AppState,
    registry,
    config_path: Path,
    switch_paths: SwitchPaths,
    profile_name: str,
    adapter_name: str,
    transport: str,
    role: str | None,
    real_systemd: bool,
    staging_root: Path | None = None,
) -> dict[str, Any]:
    profile = get_profile(config, validate_profile_name(profile_name))
    resolved_role = _resolved_role(config, profile, role)
    if not real_systemd:
        payload = {
            "ok": False,
            "action": "deploy-status",
            "message": "Deploy status requires --real-systemd for live service inspection",
            "profile": profile.name,
            "role": resolved_role,
            "adapter": adapter_name,
            "transport": transport,
            "read_only": True,
            "real_systemd_touched": False,
            "service_started": False,
            "service_enabled": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
        }
        _audit("deploy-status", profile.name, payload, path=switch_paths.audit_path)
        return payload

    readiness = build_readiness_report(
        config=config,
        state=state,
        registry=registry,
        config_path=config_path,
        switch_paths=switch_paths,
        profile_name=profile.name,
        adapter_name=adapter_name,
        transport=transport,
        staging_root=staging_root or switch_paths.staging_root,
    )
    service_status = inspect_service_status(
        profile=profile,
        adapter_name=adapter_name,
        transport=transport,
        role=resolved_role,
        paths=switch_paths,
        real_systemd=True,
    )
    healthcheck = summarize_healthchecks(
        run_profile_healthchecks(
            profile=profile,
            node_role=resolved_role,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            include_all=True,
            role_aware=True,
        ),
        profile=profile.name,
        role=resolved_role,
    )
    manifest = _manifest_status(profile.name, adapter_name, transport, service_status.get("unit_path", ""))
    warnings = _dedupe(
        list(readiness.get("warnings", []))
        + list(service_status.get("warnings", []))
        + list(manifest.get("warnings", []))
        + list(healthcheck.get("warnings", []))
    )
    blockers = list(readiness.get("blockers", []))
    if not service_status.get("ok"):
        blockers.append("Service status inspection failed")
    if not healthcheck.get("ok"):
        blockers.append("Healthcheck summary is failing")
    payload = {
        "ok": not blockers,
        "action": "deploy-status",
        "profile": profile.name,
        "role": resolved_role,
        "adapter": adapter_name,
        "transport": transport,
        "readiness": readiness,
        "service_status": service_status,
        "healthcheck": healthcheck,
        "manifest_status": manifest,
        "warnings": warnings,
        "blockers": _dedupe(blockers),
        "read_only": True,
        "real_systemd_touched": False,
        "service_started": False,
        "service_enabled": False,
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
    }
    _audit("deploy-status", profile.name, payload, path=switch_paths.audit_path)
    return payload


def _resolved_role(config: AppConfig, profile: Profile, role: str | None) -> str:
    requested = canonical_role(role or config.node.normalized_role or profile.role)
    if role and config.node.initialized and requested != config.node.normalized_role:
        raise ValueError(
            f"Requested service role '{requested}' does not match initialized node role '{config.node.normalized_role}'"
        )
    return requested


def _readiness_blocked(readiness: dict[str, Any]) -> bool:
    return bool(readiness.get("blockers")) or readiness.get("readiness_level") == "blocked"


def _plan_step(*, name: str, required: bool, enabled: bool, description: str, command: str) -> dict[str, Any]:
    return {
        "name": name,
        "required": required,
        "enabled": enabled,
        "description": description,
        "command": command,
    }


def _deploy_failure(profile: str, step: str, message: str, attempt: dict[str, Any], **details: Any) -> dict[str, Any]:
    return {
        "ok": False,
        "action": "deploy-apply",
        "message": message,
        "failed_step": step,
        "profile": profile,
        "plan_only": False,
        "real_systemd_touched": step in {"daemon_reload", "service_start", "service_enable"},
        "service_started": bool(details.get("service_start", {}).get("service_started")),
        "service_enabled": bool(details.get("service_enable", {}).get("service_enabled")),
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
        **attempt,
        **details,
    }


def _cli_command(command: str, profile: str, adapter: str, transport: str) -> str:
    return f"python -m pilottunnel.cli {command} --profile {profile} --adapter {adapter} --transport {transport}"


def _install_apply_command(profile: str, adapter: str, transport: str) -> str:
    return (
        "python -m pilottunnel.cli install apply "
        f"--profile {profile} --adapter {adapter} --transport {transport} "
        "--real-host-files --confirm REAL_FILES_APPLY"
    )


def _service_command(action: str, profile: str, adapter: str, transport: str, role: str, *, confirm: str) -> str:
    return (
        f"python -m pilottunnel.cli service {action} "
        f"--profile {profile} --adapter {adapter} --transport {transport} "
        f"--role {role} --real-systemd --confirm {confirm}"
    )


def _healthcheck_command(profile: str) -> str:
    return f"python -m pilottunnel.cli healthcheck --profile {profile} --all --role-aware"


def _manifest_status(profile: str, adapter: str, transport: str, unit_path: str) -> dict[str, Any]:
    validate_profile_name(profile)
    _validate_identifier(adapter, "adapter")
    _validate_identifier(transport, "transport")
    manifest_path = _real_manifest_path(profile, adapter, transport)
    if not manifest_path.exists():
        return {
            "exists": False,
            "path": str(manifest_path),
            "copied_files_count": 0,
            "owned_service_unit": False,
            "warnings": ["No real-host apply manifest found"],
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    copied_files = manifest.get("copied_files", [])
    owned_destinations = {item.get("destination", "") for item in copied_files}
    return {
        "exists": True,
        "path": str(manifest_path),
        "copied_files_count": len(copied_files),
        "owned_service_unit": unit_path in owned_destinations if unit_path else False,
        "real_host_files": manifest.get("real_host_files", False),
        "systemctl_executed": manifest.get("systemctl_executed", False),
        "service_started": manifest.get("service_started", False),
        "service_enabled": manifest.get("service_enabled", False),
        "warnings": [],
    }


def _real_manifest_path(profile: str, adapter: str, transport: str) -> Path:
    filename = f"{profile}-{adapter}-{transport}.json"
    return (REAL_HOST_ROOT.resolve() / "var" / "lib" / "pilottunnel" / "apply-manifests" / filename).resolve()


def _validate_identifier(value: str, label: str) -> None:
    if not value or value in {".", ".."}:
        raise ValueError(f"Invalid {label}: {value!r}")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"Path traversal blocked for {label}: {value!r}")


def _dedupe(items: list[str]) -> list[str]:
    deduped: list[str] = []
    for item in items:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _audit(action: str, profile: str, details: dict[str, Any], *, path: Path) -> None:
    write_audit_log(action, profile, details, path)


def _is_linux_host() -> bool:
    return platform.system().lower().startswith("linux")


def _is_admin_or_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return geteuid() == 0
