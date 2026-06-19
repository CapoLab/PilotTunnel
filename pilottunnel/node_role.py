"""Unified node role selection and command guards."""

from __future__ import annotations

from .config import AppConfig

SAFE_INSPECT_ACTIONS = {
    "adapter_list",
    "adapter_show",
    "bundle_inspect",
    "binary_install_plan",
    "binary_install_list",
    "binary_provider_inspect",
    "binary_source_list",
    "binary_provider_generate-manifest",
    "binary_provider_verify-manifest",
    "binary_status",
    "binary_verify",
    "binary_list",
    "binary_plan",
    "bootstrap_command",
    "bootstrap_plan",
    "backup_plan",
    "backup_list",
    "backup_inspect",
    "backup_verify",
    "deploy_plan",
    "deploy_status",
    "logs",
    "node_status",
    "readiness_report",
    "preflight",
    "runtime_plan",
    "service_install_plan",
    "service_render",
    "systemd_start_plan",
    "systemd_stop_plan",
    "systemd_reload_plan",
    "systemd_status",
    "simulate_e2e",
    "status",
}

CONTROLLER_ONLY_ACTIONS = {
    "profile_create",
    "profile_list",
    "profile_show",
    "switch",
    "plan",
    "registry_check",
    "bundle_export_worker",
}

WORKER_ONLY_ACTIONS: set[str] = set()

ROLE_ALLOWED_ACTIONS = {
    "controller": SAFE_INSPECT_ACTIONS
    | CONTROLLER_ONLY_ACTIONS
    | {
        "backup_create",
        "bundle_import",
        "binary_import",
        "binary_download",
        "binary_download_all",
        "binary_install_apply",
        "binary_plan",
        "binary_source_fetch",
        "binary_provider_prepare",
        "bootstrap_apply",
        "cleanup",
        "deploy_apply",
        "healthcheck",
        "install_plan",
        "install_apply",
        "install_rollback",
        "rollback",
        "restore_apply",
        "restore_plan",
        "service_start",
        "service_stop",
        "service_restart",
        "service_enable",
        "service_disable",
        "service_install_apply",
        "service_plan",
        "service_daemon_reload",
        "service_status",
        "service_logs",
        "systemd_start_apply",
        "systemd_stop_apply",
        "systemd_reload_apply",
        "staged_list",
        "staged_show",
        "uninstall_plan",
        "uninstall_apply",
    },
    "worker": SAFE_INSPECT_ACTIONS
    | WORKER_ONLY_ACTIONS
    | {
        "backup_create",
        "bundle_import",
        "binary_import",
        "binary_download",
        "binary_download_all",
        "binary_install_apply",
        "binary_plan",
        "binary_source_fetch",
        "binary_provider_prepare",
        "bootstrap_apply",
        "cleanup",
        "deploy_apply",
        "healthcheck",
        "install_plan",
        "install_apply",
        "install_rollback",
        "restore_apply",
        "restore_plan",
        "service_start",
        "service_stop",
        "service_restart",
        "service_enable",
        "service_disable",
        "service_install_apply",
        "service_plan",
        "service_daemon_reload",
        "service_status",
        "service_logs",
        "systemd_start_apply",
        "systemd_stop_apply",
        "systemd_reload_apply",
        "staged_list",
        "staged_show",
        "uninstall_plan",
        "uninstall_apply",
    },
}

ROLE_BLOCKED_ACTIONS = {
    "controller": sorted(WORKER_ONLY_ACTIONS),
    "worker": sorted(CONTROLLER_ONLY_ACTIONS),
}


def action_allowed_for_role(action: str, role: str | None) -> bool:
    if not role:
        return True
    return action in ROLE_ALLOWED_ACTIONS.get(role, set())


def require_controller(action: str, role: str | None) -> None:
    if role == "worker":
        raise PermissionError(f"Action '{action}' is blocked on worker nodes")


def require_worker(action: str, role: str | None) -> None:
    if role == "controller":
        raise PermissionError(f"Action '{action}' is blocked on controller nodes")


def node_status_payload(config: AppConfig, config_path: str) -> dict:
    role = config.node.normalized_role
    return {
        "node_role": config.node.node_role,
        "normalized_role": config.node.normalized_role,
        "config_path": config_path,
        "initialized": config.node.initialized,
        "allowed_actions": sorted(ROLE_ALLOWED_ACTIONS.get(role, SAFE_INSPECT_ACTIONS)),
        "blocked_actions": ROLE_BLOCKED_ACTIONS.get(role, sorted(CONTROLLER_ONLY_ACTIONS | WORKER_ONLY_ACTIONS)),
        "node_id": config.node.node_id,
        "initialized_at": config.node.initialized_at,
        "role_alias_used": config.node.role_alias_used,
    }
