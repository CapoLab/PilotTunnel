"""Unified node role selection and command guards."""

from __future__ import annotations

from .config import AppConfig, side_label_for_role
from .links import get_active_link, link_payload

SAFE_INSPECT_ACTIONS = {
    "adapter_list",
    "adapter_show",
    "bundle_inspect",
    "candidate_plan",
    "candidate_result",
    "binary_install_plan",
    "binary_install_list",
    "binary_provider_inspect",
    "binary_source_list",
    "binary_provider_generate-manifest",
    "binary_provider_release-plan",
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
    "layer_list",
    "layer_status",
    "link_list",
    "link_show",
    "link_inspect_pairing_code",
    "node_status",
    "readiness_report",
    "preflight",
    "rc_check",
    "rc_smoke",
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
    "link_setup_iran",
    "link_create_controller",
    "link_export_pairing_code",
    "profile_create",
    "profile_list",
    "profile_show",
    "switch",
    "switch_plan",
    "switch_apply",
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
        "candidate_prepare_all",
        "candidate_start",
        "candidate_stop",
        "candidate_smoke_test",
        "binary_import",
        "binary_download",
        "binary_download_all",
        "binary_install_apply",
        "binary_plan",
        "binary_source_fetch",
        "binary_provider_prepare",
        "binary_provider_release-assets",
        "bootstrap_apply",
        "cleanup",
        "deploy_apply",
        "healthcheck",
        "layer_select",
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
        "candidate_prepare_all",
        "candidate_start",
        "candidate_stop",
        "binary_import",
        "binary_download",
        "binary_download_all",
        "binary_install_apply",
        "binary_plan",
        "binary_source_fetch",
        "binary_provider_prepare",
        "binary_provider_release-assets",
        "bootstrap_apply",
        "cleanup",
        "deploy_apply",
        "healthcheck",
        "layer_select",
        "install_plan",
        "install_apply",
        "install_rollback",
        "link_setup_kharej",
        "link_import_pairing_code",
        "link_setup_worker_manual",
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
    side_label = config.node.side_label or (side_label_for_role(role) if role else "")
    active_link = get_active_link(config)
    return {
        "node_role": config.node.node_role,
        "normalized_role": config.node.normalized_role,
        "side_label": side_label,
        "config_path": config_path,
        "initialized": config.node.initialized,
        "allowed_actions": sorted(ROLE_ALLOWED_ACTIONS.get(role, SAFE_INSPECT_ACTIONS)),
        "blocked_actions": ROLE_BLOCKED_ACTIONS.get(role, sorted(CONTROLLER_ONLY_ACTIONS | WORKER_ONLY_ACTIONS)),
        "node_id": config.node.node_id,
        "initialized_at": config.node.initialized_at,
        "role_alias_used": config.node.role_alias_used,
        "preferred_layer": config.node.preferred_layer,
        "preferred_layer_selected_at": config.node.preferred_layer_selected_at,
        "link_count": len(config.links),
        "active_link_label": config.node.active_link_label,
        "detected_local_address": config.node.endpoint_address,
        "active_link": link_payload(active_link, active_label=config.node.active_link_label, role=role) if active_link else None,
    }
