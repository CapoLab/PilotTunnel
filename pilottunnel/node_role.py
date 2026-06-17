"""Unified node role selection and command guards."""

from __future__ import annotations

from .config import AppConfig

SAFE_INSPECT_ACTIONS = {
    "adapter_list",
    "adapter_show",
    "bundle_inspect",
    "binary_status",
    "binary_verify",
    "binary_list",
    "logs",
    "node_status",
    "preflight",
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
        "bundle_import",
        "binary_import",
        "binary_plan",
        "cleanup",
        "healthcheck",
        "install_plan",
        "install_apply",
        "install_rollback",
        "rollback",
        "service_plan",
        "service_status",
        "service_logs",
        "staged_list",
        "staged_show",
        "uninstall_plan",
        "uninstall_apply",
    },
    "worker": SAFE_INSPECT_ACTIONS
    | WORKER_ONLY_ACTIONS
    | {
        "bundle_import",
        "binary_import",
        "binary_plan",
        "cleanup",
        "healthcheck",
        "install_plan",
        "install_apply",
        "install_rollback",
        "service_plan",
        "service_status",
        "service_logs",
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
