"""PilotTunnel CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from .adapters import ADAPTERS
from .adapters.base import AdapterContext
from .__version__ import version_payload
from .audit import write_audit_log
from .backup import apply_restore, build_backup_plan, build_restore_plan, create_backup, inspect_backup, list_backups, verify_backup
from .binary_provider import (
    build_provider_release_plan,
    download_all_binaries,
    download_binary,
    generate_manifest,
    inspect_manifest,
    verify_manifest_file,
    write_provider_release_assets,
)
from .binary_readiness import build_binary_readiness_report, remember_binary_provider_source
from .binary_install import apply_binary_install, build_binary_install_plan, list_binary_installations
from .bootstrap import apply_bootstrap, build_bootstrap_command, build_bootstrap_plan
from .bundles import build_worker_bundle, import_bundle, inspect_bundle
from .binaries import get_binary_plan, import_binary, list_binary_plans, verify_binary
from .config import (
    AppConfig,
    Candidate,
    Profile,
    ProfilePorts,
    ProfileSafety,
    SUPPORTED_LAYERS,
    build_worker_stub,
    build_node_settings,
    canonical_role,
    get_profile,
    load_config,
    save_config,
    validate_profile_name,
)
from .deploy import apply_deploy, build_deploy_plan, build_deploy_status
from .install_plan import apply_install, apply_uninstall, build_install_plan, build_uninstall_plan, rollback_install
from .node_role import action_allowed_for_role, node_status_payload
from .healthcheck import DEFAULT_TIMEOUT_SECONDS, build_profile_healthcheck_plan, run_profile_healthchecks, summarize_healthchecks, tcp_healthcheck
from .preflight import run_preflight
from .readiness import build_readiness_report
from .rc import build_rc_check, build_rc_smoke
from .runtime_plan import build_runtime_plan
from .manual_switch import apply_manual_switch, build_manual_switch_plan
from .service_install import apply_service_install, build_service_install_plan
from .service_plan import build_staged_service_plan
from .systemd_control import (
    DEFAULT_TIMEOUT_SECONDS as SYSTEMD_TIMEOUT_SECONDS,
    apply_start as apply_systemd_start,
    apply_stop as apply_systemd_stop,
    apply_reload as apply_systemd_reload,
    build_start_plan as build_systemd_start_plan,
    build_stop_plan as build_systemd_stop_plan,
    build_reload_plan as build_systemd_reload_plan,
    inspect_managed_status as inspect_systemd_status,
)
from .simulation import run_e2e_simulation
from .service_lifecycle import block_real_service_action, build_service_plan, disable_service, enable_service, inspect_service_logs, inspect_service_status, restart_service, run_daemon_reload, start_service, stop_service
from .registry import PortRegistry, RegistryEntry, load_registry, save_registry
from .state import AppState, load_state, save_state
from .switch_engine import SwitchEngine, SwitchPaths
from .upstream_sources import fetch_upstream_sources, list_upstream_sources, prepare_provider_binaries


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pilottunnel")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--registry", type=Path, default=None)
    parser.add_argument("--audit-log", type=Path, default=None)
    parser.add_argument("--lock-dir", type=Path, default=None)
    parser.add_argument("--work-dir", type=Path, default=None)
    parser.add_argument("--staging-root", type=Path, default=None)
    parser.add_argument("--cache-root", type=Path, default=None)
    parser.add_argument("--apply", action="store_true", help="Allow dangerous operations to write runtime artifacts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("version")

    init = subparsers.add_parser("init")
    init.add_argument("--role")
    init.add_argument("--force", action="store_true")

    node = subparsers.add_parser("node")
    node_subparsers = node.add_subparsers(dest="node_command", required=True)
    node_subparsers.add_parser("status")

    profile = subparsers.add_parser("profile")
    profile_subparsers = profile.add_subparsers(dest="profile_command", required=True)
    profile_create = profile_subparsers.add_parser("create")
    profile_create.add_argument("--name", required=True)
    profile_create.add_argument("--main-port", type=int, required=True)
    profile_create.add_argument("--target-host", default="127.0.0.1")
    profile_create.add_argument("--target-port", type=int, required=True)
    profile_create.add_argument("--role", default="controller")
    profile_create.add_argument("--control-port", type=int)
    profile_create.add_argument("--service-port", type=int)
    profile_create.add_argument("--check-port", type=int)
    profile_create.add_argument("--layer", default="layer4")
    profile_create.add_argument("--candidate", action="append", default=[], help="adapter:transport")
    profile_create.add_argument("--force", action="store_true")
    profile_create.add_argument("--update", action="store_true")
    profile_subparsers.add_parser("list")
    profile_show = profile_subparsers.add_parser("show")
    profile_show.add_argument("--name", required=True)

    subparsers.add_parser("layer").add_subparsers(dest="layer_command", required=True).add_parser("list")
    adapter = subparsers.add_parser("adapter")
    adapter_subparsers = adapter.add_subparsers(dest="adapter_command", required=True)
    adapter_subparsers.add_parser("list")
    adapter_show = adapter_subparsers.add_parser("show")
    adapter_show.add_argument("--name", required=True)

    install = subparsers.add_parser("install")
    install_subparsers = install.add_subparsers(dest="install_command", required=True)
    install_plan = install_subparsers.add_parser("plan")
    install_plan.add_argument("--profile", required=True)
    install_plan.add_argument("--adapter", required=True)
    install_plan.add_argument("--transport", required=True)
    install_plan.add_argument("--role")
    install_plan.add_argument("--staging-root", dest="command_staging_root", type=Path, default=None)
    install_plan.add_argument("--install-root", type=Path, default=None)
    install_plan.add_argument("--json", action="store_true")
    install_apply = install_subparsers.add_parser("apply")
    install_apply.add_argument("--profile", required=True)
    install_apply.add_argument("--adapter", required=True)
    install_apply.add_argument("--transport", required=True)
    install_apply.add_argument("--role")
    install_apply.add_argument("--staging-root", dest="command_staging_root", type=Path, default=None)
    install_apply.add_argument("--install-root", type=Path, default=None)
    install_apply.add_argument("--real-host-files", action="store_true")
    install_apply.add_argument("--confirm")
    install_apply.add_argument("--dry-run", action="store_true")
    install_apply.add_argument("--require-healthcheck", action="store_true")
    install_rollback = install_subparsers.add_parser("rollback")
    install_rollback.add_argument("--profile", required=True)
    install_rollback.add_argument("--adapter", required=True)
    install_rollback.add_argument("--transport", required=True)
    install_rollback.add_argument("--install-root", type=Path, default=None)
    install_rollback.add_argument("--real-host-files", action="store_true")
    install_rollback.add_argument("--confirm")
    install_rollback.add_argument("--dry-run", action="store_true")

    uninstall = subparsers.add_parser("uninstall")
    uninstall_subparsers = uninstall.add_subparsers(dest="uninstall_command", required=True)
    uninstall_plan = uninstall_subparsers.add_parser("plan")
    uninstall_plan.add_argument("--profile", required=True)
    uninstall_plan.add_argument("--adapter", required=True)
    uninstall_plan.add_argument("--transport", required=True)
    uninstall_plan.add_argument("--role")
    uninstall_plan.add_argument("--staging-root", dest="command_staging_root", type=Path, default=None)
    uninstall_plan.add_argument("--install-root", type=Path, default=None)
    uninstall_plan.add_argument("--json", action="store_true")
    uninstall_apply = uninstall_subparsers.add_parser("apply")
    uninstall_apply.add_argument("--profile", required=True)
    uninstall_apply.add_argument("--adapter", required=True)
    uninstall_apply.add_argument("--transport", required=True)
    uninstall_apply.add_argument("--role")
    uninstall_apply.add_argument("--staging-root", dest="command_staging_root", type=Path, default=None)
    uninstall_apply.add_argument("--install-root", type=Path, default=None)
    uninstall_apply.add_argument("--real-host-files", action="store_true")
    uninstall_apply.add_argument("--confirm")
    uninstall_apply.add_argument("--dry-run", action="store_true")

    service = subparsers.add_parser("service")
    service_subparsers = service.add_subparsers(dest="service_command", required=True)
    service_plan = service_subparsers.add_parser("plan")
    service_plan.add_argument("--profile", required=True)
    service_plan.add_argument("--adapter", required=True)
    service_plan.add_argument("--transport", required=True)
    service_plan.add_argument("--action", required=True, choices=["start", "stop", "restart", "enable", "disable"])
    service_plan.add_argument("--role")
    service_plan.add_argument("--install-root", type=Path, default=None)
    service_plan.add_argument("--json", action="store_true")
    service_render = service_subparsers.add_parser("render")
    service_render.add_argument("--runtime-dir", type=Path, required=True)
    service_render.add_argument("--service-dir", type=Path, required=True)
    service_render.add_argument("--platform", default="auto")
    service_render.add_argument("--json", action="store_true")
    service_install = service_subparsers.add_parser("install")
    service_install_subparsers = service_install.add_subparsers(dest="service_install_command", required=True)
    service_install_plan = service_install_subparsers.add_parser("plan")
    service_install_plan.add_argument("--runtime-dir", type=Path, required=True)
    service_install_plan.add_argument("--service-dir", type=Path, required=True)
    service_install_plan.add_argument("--target-dir", type=Path, required=True)
    service_install_plan.add_argument("--platform", default="auto")
    service_install_plan.add_argument("--allow-system-dir", action="store_true")
    service_install_plan.add_argument("--json", action="store_true")
    service_install_apply = service_install_subparsers.add_parser("apply")
    service_install_apply.add_argument("--runtime-dir", type=Path, required=True)
    service_install_apply.add_argument("--service-dir", type=Path, required=True)
    service_install_apply.add_argument("--target-dir", type=Path, required=True)
    service_install_apply.add_argument("--platform", default="auto")
    service_install_apply.add_argument("--allow-system-dir", action="store_true")
    service_install_apply.add_argument("--replace-existing", action="store_true")
    service_install_apply.add_argument("--confirm")
    service_install_apply.add_argument("--json", action="store_true")
    service_status = service_subparsers.add_parser("status")
    service_status.add_argument("--profile", required=True)
    service_status.add_argument("--adapter", required=True)
    service_status.add_argument("--transport", required=True)
    service_status.add_argument("--role")
    service_status.add_argument("--install-root", type=Path, default=None)
    service_status.add_argument("--real-systemd", action="store_true")
    service_status.add_argument("--json", action="store_true")
    service_logs = service_subparsers.add_parser("logs")
    service_logs.add_argument("--profile", required=True)
    service_logs.add_argument("--adapter", required=True)
    service_logs.add_argument("--transport", required=True)
    service_logs.add_argument("--role")
    service_logs.add_argument("--install-root", type=Path, default=None)
    service_logs.add_argument("--limit", type=int, default=50)
    service_logs.add_argument("--real-systemd", action="store_true")
    service_logs.add_argument("--json", action="store_true")
    service_daemon_reload = service_subparsers.add_parser("daemon-reload")
    service_daemon_reload.add_argument("--real-systemd", action="store_true")
    service_daemon_reload.add_argument("--confirm")
    service_daemon_reload.add_argument("--json", action="store_true")
    service_start = service_subparsers.add_parser("start")
    service_start.add_argument("--profile", required=True)
    service_start.add_argument("--adapter", required=True)
    service_start.add_argument("--transport", required=True)
    service_start.add_argument("--role")
    service_start.add_argument("--real-systemd", action="store_true")
    service_start.add_argument("--confirm")
    service_start.add_argument("--require-healthcheck", action="store_true")
    service_start.add_argument("--healthcheck-timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    service_start.add_argument("--json", action="store_true")
    service_restart = service_subparsers.add_parser("restart")
    service_restart.add_argument("--profile", required=True)
    service_restart.add_argument("--adapter", required=True)
    service_restart.add_argument("--transport", required=True)
    service_restart.add_argument("--role")
    service_restart.add_argument("--real-systemd", action="store_true")
    service_restart.add_argument("--confirm")
    service_restart.add_argument("--require-healthcheck", action="store_true")
    service_restart.add_argument("--healthcheck-timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    service_restart.add_argument("--json", action="store_true")
    for blocked_action in ("stop", "enable", "disable"):
        blocked_parser = service_subparsers.add_parser(blocked_action)
        blocked_parser.add_argument("--profile", required=True)
        blocked_parser.add_argument("--adapter", required=True)
        blocked_parser.add_argument("--transport", required=True)
        blocked_parser.add_argument("--role")
        blocked_parser.add_argument("--real-systemd", action="store_true")
        blocked_parser.add_argument("--confirm")
        blocked_parser.add_argument("--json", action="store_true")

    switch = subparsers.add_parser("switch")
    switch.add_argument("--profile")
    switch.add_argument("--adapter")
    switch.add_argument("--transport")
    switch.add_argument("--require-healthcheck", action="store_true")
    switch_subparsers = switch.add_subparsers(dest="switch_command", required=False)
    switch_plan = switch_subparsers.add_parser("plan")
    switch_plan.add_argument("--target", required=True)
    switch_plan.add_argument("--runtime-dir", type=Path, required=True)
    switch_plan.add_argument("--service-dir", type=Path, required=True)
    switch_plan.add_argument("--json", action="store_true")
    switch_apply = switch_subparsers.add_parser("apply")
    switch_apply.add_argument("--target", required=True)
    switch_apply.add_argument("--runtime-dir", type=Path, required=True)
    switch_apply.add_argument("--service-dir", type=Path, required=True)
    switch_apply.add_argument("--confirm")
    switch_apply.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("--profile", required=True)

    healthcheck = subparsers.add_parser("healthcheck")
    healthcheck.add_argument("--profile")
    healthcheck.add_argument("--host")
    healthcheck.add_argument("--port", type=int)
    healthcheck.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    healthcheck.add_argument("--json", action="store_true")
    healthcheck.add_argument("--all", action="store_true")
    healthcheck.add_argument("--role-aware", action="store_true")

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--profile", required=True)

    logs = subparsers.add_parser("logs")
    logs.add_argument("--profile")
    logs.add_argument("--limit", type=int, default=20)

    deploy = subparsers.add_parser("deploy")
    deploy_subparsers = deploy.add_subparsers(dest="deploy_command", required=True)
    deploy_plan = deploy_subparsers.add_parser("plan")
    deploy_plan.add_argument("--profile", required=True)
    deploy_plan.add_argument("--adapter", required=True)
    deploy_plan.add_argument("--transport", required=True)
    deploy_plan.add_argument("--role")
    deploy_plan.add_argument("--enable-after-start", action="store_true")
    deploy_plan.add_argument("--require-healthcheck", action="store_true")
    deploy_plan.add_argument("--staging-root", dest="command_staging_root", type=Path, default=None)
    deploy_plan.add_argument("--json", action="store_true")
    deploy_apply = deploy_subparsers.add_parser("apply")
    deploy_apply.add_argument("--profile", required=True)
    deploy_apply.add_argument("--adapter", required=True)
    deploy_apply.add_argument("--transport", required=True)
    deploy_apply.add_argument("--role")
    deploy_apply.add_argument("--real-host", action="store_true")
    deploy_apply.add_argument("--confirm")
    deploy_apply.add_argument("--enable-after-start", action="store_true")
    deploy_apply.add_argument("--require-healthcheck", action="store_true")
    deploy_apply.add_argument("--staging-root", dest="command_staging_root", type=Path, default=None)
    deploy_apply.add_argument("--json", action="store_true")
    deploy_status = deploy_subparsers.add_parser("status")
    deploy_status.add_argument("--profile", required=True)
    deploy_status.add_argument("--adapter", required=True)
    deploy_status.add_argument("--transport", required=True)
    deploy_status.add_argument("--role")
    deploy_status.add_argument("--real-systemd", action="store_true")
    deploy_status.add_argument("--staging-root", dest="command_staging_root", type=Path, default=None)
    deploy_status.add_argument("--json", action="store_true")

    backup = subparsers.add_parser("backup")
    backup_subparsers = backup.add_subparsers(dest="backup_command", required=True)
    backup_plan = backup_subparsers.add_parser("plan")
    backup_plan.add_argument("--profile")
    backup_plan.add_argument("--adapter")
    backup_plan.add_argument("--transport")
    backup_plan.add_argument("--install-root", type=Path, default=None)
    backup_plan.add_argument("--backup-root", type=Path, default=None)
    backup_plan.add_argument("--json", action="store_true")
    backup_create = backup_subparsers.add_parser("create")
    backup_create.add_argument("--profile")
    backup_create.add_argument("--adapter")
    backup_create.add_argument("--transport")
    backup_create.add_argument("--install-root", type=Path, default=None)
    backup_create.add_argument("--backup-root", type=Path, default=None)
    backup_create.add_argument("--confirm")
    backup_create.add_argument("--json", action="store_true")
    backup_list = backup_subparsers.add_parser("list")
    backup_list.add_argument("--backup-root", type=Path, default=None)
    backup_list.add_argument("--json", action="store_true")
    backup_inspect = backup_subparsers.add_parser("inspect")
    backup_inspect.add_argument("--backup-id", required=True)
    backup_inspect.add_argument("--backup-root", type=Path, default=None)
    backup_inspect.add_argument("--json", action="store_true")
    backup_verify = backup_subparsers.add_parser("verify")
    backup_verify.add_argument("--backup-id", required=True)
    backup_verify.add_argument("--backup-root", type=Path, default=None)
    backup_verify.add_argument("--json", action="store_true")

    restore = subparsers.add_parser("restore")
    restore_subparsers = restore.add_subparsers(dest="restore_command", required=True)
    restore_plan = restore_subparsers.add_parser("plan")
    restore_plan.add_argument("--backup-id", required=True)
    restore_plan.add_argument("--install-root", type=Path, default=None)
    restore_plan.add_argument("--backup-root", type=Path, default=None)
    restore_plan.add_argument("--json", action="store_true")
    restore_apply = restore_subparsers.add_parser("apply")
    restore_apply.add_argument("--backup-id", required=True)
    restore_apply.add_argument("--install-root", type=Path, default=None)
    restore_apply.add_argument("--backup-root", type=Path, default=None)
    restore_apply.add_argument("--confirm")
    restore_apply.add_argument("--json", action="store_true")

    registry = subparsers.add_parser("registry")
    registry.add_subparsers(dest="registry_command", required=True).add_parser("check")

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--profile", required=True)
    cleanup.add_argument("--dry-run", action="store_true")

    plan = subparsers.add_parser("plan")
    plan.add_argument("--profile", required=True)
    plan.add_argument("--adapter", required=True)
    plan.add_argument("--transport", required=True)

    staged = subparsers.add_parser("staged")
    staged_subparsers = staged.add_subparsers(dest="staged_command", required=True)
    staged_subparsers.add_parser("list")
    staged_show = staged_subparsers.add_parser("show")
    staged_show.add_argument("--profile", required=True)
    staged_show.add_argument("--adapter", required=True)
    staged_show.add_argument("--transport", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--profile")
    preflight.add_argument("--json", action="store_true")

    readiness = subparsers.add_parser("readiness")
    readiness_subparsers = readiness.add_subparsers(dest="readiness_command", required=True)
    readiness_report = readiness_subparsers.add_parser("report")
    readiness_report.add_argument("--profile")
    readiness_report.add_argument("--adapter")
    readiness_report.add_argument("--transport")
    readiness_report.add_argument("--staging-root", dest="command_staging_root", type=Path, default=None)
    readiness_report.add_argument("--install-root", type=Path, default=None)
    readiness_report.add_argument("--json", action="store_true")

    rc = subparsers.add_parser("rc")
    rc_subparsers = rc.add_subparsers(dest="rc_command", required=True)
    rc_check = rc_subparsers.add_parser("check")
    rc_check.add_argument("--profile")
    rc_check.add_argument("--target")
    rc_check.add_argument("--runtime-dir", type=Path, required=True)
    rc_check.add_argument("--service-dir", type=Path, required=True)
    rc_check.add_argument("--target-dir", type=Path, required=True)
    rc_check.add_argument("--allow-system-dir", action="store_true")
    rc_check.add_argument("--json", action="store_true")
    rc_smoke = rc_subparsers.add_parser("smoke")
    rc_smoke.add_argument("--profile")
    rc_smoke.add_argument("--target")
    rc_smoke.add_argument("--runtime-dir", type=Path, required=True)
    rc_smoke.add_argument("--service-dir", type=Path, required=True)
    rc_smoke.add_argument("--target-dir", type=Path, required=True)
    rc_smoke.add_argument("--allow-system-dir", action="store_true")
    rc_smoke.add_argument("--json", action="store_true")

    runtime = subparsers.add_parser("runtime")
    runtime_subparsers = runtime.add_subparsers(dest="runtime_command", required=True)
    runtime_plan = runtime_subparsers.add_parser("plan")
    runtime_plan.add_argument("--runtime-dir", type=Path, required=True)
    runtime_plan.add_argument("--platform", default="auto")
    runtime_plan.add_argument("--json", action="store_true")

    systemd = subparsers.add_parser("systemd")
    systemd_subparsers = systemd.add_subparsers(dest="systemd_command", required=True)
    systemd_reload = systemd_subparsers.add_parser("reload")
    systemd_reload_subparsers = systemd_reload.add_subparsers(dest="systemd_reload_command", required=True)
    systemd_reload_plan = systemd_reload_subparsers.add_parser("plan")
    systemd_reload_plan.add_argument("--target-dir", type=Path, required=True)
    systemd_reload_plan.add_argument("--json", action="store_true")
    systemd_reload_apply = systemd_reload_subparsers.add_parser("apply")
    systemd_reload_apply.add_argument("--target-dir", type=Path, required=True)
    systemd_reload_apply.add_argument("--confirm")
    systemd_reload_apply.add_argument("--json", action="store_true")
    systemd_start = systemd_subparsers.add_parser("start")
    systemd_start_subparsers = systemd_start.add_subparsers(dest="systemd_start_command", required=True)
    systemd_start_plan = systemd_start_subparsers.add_parser("plan")
    systemd_start_plan.add_argument("--service-dir", type=Path, required=True)
    systemd_start_plan.add_argument("--service-name")
    systemd_start_plan.add_argument("--json", action="store_true")
    systemd_start_apply = systemd_start_subparsers.add_parser("apply")
    systemd_start_apply.add_argument("--service-dir", type=Path, required=True)
    systemd_start_apply.add_argument("--service-name")
    systemd_start_apply.add_argument("--timeout", type=float, default=SYSTEMD_TIMEOUT_SECONDS)
    systemd_start_apply.add_argument("--confirm")
    systemd_start_apply.add_argument("--json", action="store_true")
    systemd_stop = systemd_subparsers.add_parser("stop")
    systemd_stop_subparsers = systemd_stop.add_subparsers(dest="systemd_stop_command", required=True)
    systemd_stop_plan = systemd_stop_subparsers.add_parser("plan")
    systemd_stop_plan.add_argument("--service-dir", type=Path, required=True)
    systemd_stop_plan.add_argument("--service-name")
    systemd_stop_plan.add_argument("--json", action="store_true")
    systemd_stop_apply = systemd_stop_subparsers.add_parser("apply")
    systemd_stop_apply.add_argument("--service-dir", type=Path, required=True)
    systemd_stop_apply.add_argument("--service-name")
    systemd_stop_apply.add_argument("--timeout", type=float, default=SYSTEMD_TIMEOUT_SECONDS)
    systemd_stop_apply.add_argument("--confirm")
    systemd_stop_apply.add_argument("--json", action="store_true")
    systemd_status = systemd_subparsers.add_parser("status")
    systemd_status.add_argument("--service-dir", type=Path, required=True)
    systemd_status.add_argument("--service-name")
    systemd_status.add_argument("--timeout", type=float, default=SYSTEMD_TIMEOUT_SECONDS)
    systemd_status.add_argument("--json", action="store_true")

    binary = subparsers.add_parser("binary")
    binary_subparsers = binary.add_subparsers(dest="binary_command", required=True)
    binary_subparsers.add_parser("list")
    binary_plan = binary_subparsers.add_parser("plan")
    binary_plan.add_argument("--adapter", required=True)
    binary_import = binary_subparsers.add_parser("import")
    binary_import.add_argument("--adapter", required=True)
    binary_import.add_argument("--source", type=Path, required=True)
    binary_import.add_argument("--version", required=True)
    binary_import.add_argument("--sha256")
    binary_import.add_argument("--force", action="store_true")
    binary_status = binary_subparsers.add_parser("status")
    binary_status.add_argument("--adapter")
    binary_status.add_argument("--require-all", action="store_true")
    binary_status.add_argument("--manifest-url")
    binary_status.add_argument("--manifest-file", type=Path)
    binary_status.add_argument("--allow-provider-host")
    binary_status.add_argument("--platform", default="auto")
    binary_verify = binary_subparsers.add_parser("verify")
    binary_verify.add_argument("--adapter", required=True)
    binary_verify.add_argument("--run-version", action="store_true")
    binary_download = binary_subparsers.add_parser("download")
    binary_download.add_argument("--adapter", required=True)
    binary_download.add_argument("--manifest-url")
    binary_download.add_argument("--manifest-file", type=Path)
    binary_download.add_argument("--allow-provider-host")
    binary_download.add_argument("--platform", default="auto")
    binary_download.add_argument("--confirm")
    binary_download.add_argument("--force", action="store_true")
    binary_download.add_argument("--run-version", action="store_true")
    binary_download.add_argument("--json", action="store_true")
    binary_download_all = binary_subparsers.add_parser("download-all")
    binary_download_all.add_argument("--manifest-url")
    binary_download_all.add_argument("--manifest-file", type=Path)
    binary_download_all.add_argument("--allow-provider-host")
    binary_download_all.add_argument("--platform", default="auto")
    binary_download_all.add_argument("--confirm")
    binary_download_all.add_argument("--force", action="store_true")
    binary_download_all.add_argument("--run-version", action="store_true")
    binary_download_all.add_argument("--json", action="store_true")
    binary_source = binary_subparsers.add_parser("source")
    binary_source_subparsers = binary_source.add_subparsers(dest="binary_source_command", required=True)
    binary_source_list = binary_source_subparsers.add_parser("list")
    binary_source_list.add_argument("--json", action="store_true")
    binary_source_fetch = binary_source_subparsers.add_parser("fetch")
    binary_source_fetch.add_argument("--source-dir", type=Path, required=True)
    binary_source_fetch.add_argument("--platform", default="auto")
    binary_source_fetch.add_argument("--adapter", action="append", default=[])
    binary_source_fetch.add_argument("--version", action="append", default=[])
    binary_source_fetch.add_argument("--confirm")
    binary_source_fetch.add_argument("--force", action="store_true")
    binary_source_fetch.add_argument("--dry-run", action="store_true")
    binary_source_fetch.add_argument("--json", action="store_true")
    binary_install = binary_subparsers.add_parser("install")
    binary_install_subparsers = binary_install.add_subparsers(dest="binary_install_command", required=True)
    binary_install_plan = binary_install_subparsers.add_parser("plan")
    binary_install_plan.add_argument("--manifest", dest="manifest_file", type=Path, required=True)
    binary_install_plan.add_argument("--platform", default="auto")
    binary_install_plan.add_argument("--json", action="store_true")
    binary_install_apply = binary_install_subparsers.add_parser("apply")
    binary_install_apply.add_argument("--manifest", dest="manifest_file", type=Path, required=True)
    binary_install_apply.add_argument("--platform", default="auto")
    binary_install_apply.add_argument("--install-dir", type=Path, required=True)
    binary_install_apply.add_argument("--confirm")
    binary_install_apply.add_argument("--json", action="store_true")
    binary_install_list = binary_install_subparsers.add_parser("list")
    binary_install_list.add_argument("--install-dir", type=Path, required=True)
    binary_install_list.add_argument("--json", action="store_true")
    binary_provider = binary_subparsers.add_parser("provider")
    binary_provider_subparsers = binary_provider.add_subparsers(dest="binary_provider_command", required=True)
    binary_provider_inspect = binary_provider_subparsers.add_parser("inspect")
    binary_provider_inspect.add_argument("--manifest-url")
    binary_provider_inspect.add_argument("--manifest-file", type=Path)
    binary_provider_inspect.add_argument("--allow-provider-host")
    binary_provider_inspect.add_argument("--platform", default="auto")
    binary_provider_inspect.add_argument("--json", action="store_true")
    binary_provider_generate = binary_provider_subparsers.add_parser("generate-manifest")
    binary_provider_generate.add_argument("--provider-name", required=True)
    binary_provider_generate.add_argument("--base-url", required=True)
    binary_provider_generate.add_argument("--source-dir", type=Path, required=True)
    binary_provider_generate.add_argument("--output", type=Path, required=True)
    binary_provider_generate.add_argument("--json", action="store_true")
    binary_provider_verify = binary_provider_subparsers.add_parser("verify-manifest")
    binary_provider_verify.add_argument("--manifest-file", type=Path, required=True)
    binary_provider_verify.add_argument("--json", action="store_true")
    binary_provider_prepare = binary_provider_subparsers.add_parser("prepare")
    binary_provider_prepare.add_argument("--source-dir", type=Path, required=True)
    binary_provider_prepare.add_argument("--provider-name", required=True)
    binary_provider_prepare.add_argument("--base-url", required=True)
    binary_provider_prepare.add_argument("--platform", default="auto")
    binary_provider_prepare.add_argument("--output", type=Path, required=True)
    binary_provider_prepare.add_argument("--version", action="append", default=[])
    binary_provider_prepare.add_argument("--confirm")
    binary_provider_prepare.add_argument("--json", action="store_true")
    binary_provider_release_plan = binary_provider_subparsers.add_parser("release-plan")
    binary_provider_release_plan.add_argument("--source-dir", type=Path, required=True)
    binary_provider_release_plan.add_argument("--provider-name", required=True)
    binary_provider_release_plan.add_argument("--repo-slug", required=True)
    binary_provider_release_plan.add_argument("--release-tag", required=True)
    binary_provider_release_plan.add_argument("--output-dir", type=Path, required=True)
    binary_provider_release_plan.add_argument("--version", action="append", default=[])
    binary_provider_release_plan.add_argument("--json", action="store_true")
    binary_provider_release_assets = binary_provider_subparsers.add_parser("release-assets")
    binary_provider_release_assets.add_argument("--source-dir", type=Path, required=True)
    binary_provider_release_assets.add_argument("--provider-name", required=True)
    binary_provider_release_assets.add_argument("--repo-slug", required=True)
    binary_provider_release_assets.add_argument("--release-tag", required=True)
    binary_provider_release_assets.add_argument("--output-dir", type=Path, required=True)
    binary_provider_release_assets.add_argument("--version", action="append", default=[])
    binary_provider_release_assets.add_argument("--confirm")
    binary_provider_release_assets.add_argument("--force", action="store_true")
    binary_provider_release_assets.add_argument("--json", action="store_true")

    bundle = subparsers.add_parser("bundle")
    bundle_subparsers = bundle.add_subparsers(dest="bundle_command", required=True)
    bundle_export = bundle_subparsers.add_parser("export-worker")
    bundle_export.add_argument("--profile", required=True)
    bundle_export.add_argument("--adapter", required=True)
    bundle_export.add_argument("--transport", required=True)
    bundle_export.add_argument("--output", type=Path, required=True)
    bundle_export.add_argument("--include-staged-paths", action="store_true")
    bundle_export.add_argument("--force", action="store_true")
    bundle_export.add_argument("--json", action="store_true")
    bundle_inspect = bundle_subparsers.add_parser("inspect")
    bundle_inspect.add_argument("--input", type=Path, required=True)
    bundle_inspect.add_argument("--json", action="store_true")
    bundle_import = bundle_subparsers.add_parser("import")
    bundle_import.add_argument("--input", type=Path, required=True)
    bundle_import.add_argument("--staging-root", type=Path, default=None)
    bundle_import.add_argument("--confirm")
    bundle_import.add_argument("--dry-run", action="store_true")
    bundle_import.add_argument("--force", action="store_true")
    bundle_import.add_argument("--json", action="store_true")

    simulate = subparsers.add_parser("simulate")
    simulate_subparsers = simulate.add_subparsers(dest="simulate_command", required=True)
    simulate_e2e = simulate_subparsers.add_parser("e2e")
    simulate_e2e.add_argument("--profile", required=True)
    simulate_e2e.add_argument("--adapter", required=True)
    simulate_e2e.add_argument("--transport", required=True)
    simulate_e2e.add_argument("--base-root", type=Path, default=None)
    simulate_e2e.add_argument("--json", action="store_true")
    simulate_e2e.add_argument("--keep-files", action="store_true")

    bootstrap = subparsers.add_parser("bootstrap")
    bootstrap_subparsers = bootstrap.add_subparsers(dest="bootstrap_command", required=True)
    bootstrap_plan = bootstrap_subparsers.add_parser("plan")
    bootstrap_apply = bootstrap_subparsers.add_parser("apply")
    bootstrap_command = bootstrap_subparsers.add_parser("command")
    for bootstrap_parser in (bootstrap_plan, bootstrap_apply):
        bootstrap_parser.add_argument("--profile")
        bootstrap_parser.add_argument("--adapter")
        bootstrap_parser.add_argument("--transport")
        bootstrap_parser.add_argument("--role")
        bootstrap_parser.add_argument("--create-profile", action="store_true")
        bootstrap_parser.add_argument("--update-profile", action="store_true")
        bootstrap_parser.add_argument("--ports", choices=("auto",))
        bootstrap_parser.add_argument("--target-host")
        bootstrap_parser.add_argument("--main-port", type=int)
        bootstrap_parser.add_argument("--target-port", type=int)
        bootstrap_parser.add_argument("--control-port", type=int)
        bootstrap_parser.add_argument("--service-port", type=int)
        bootstrap_parser.add_argument("--check-port", type=int)
        bootstrap_parser.add_argument("--manifest-url")
        bootstrap_parser.add_argument("--manifest-file", type=Path)
        bootstrap_parser.add_argument("--allow-provider-host")
        bootstrap_parser.add_argument("--bundle-output", type=Path)
        bootstrap_parser.add_argument("--bundle-file", type=Path)
        bootstrap_parser.add_argument("--bundle-input", dest="bundle_file", type=Path, help=argparse.SUPPRESS)
        bootstrap_parser.add_argument("--backup-root", type=Path, default=None)
        bootstrap_parser.add_argument("--platform", default="auto")
        bootstrap_parser.add_argument("--force", action="store_true")
        bootstrap_parser.add_argument("--allow-incomplete-binaries-for-tests-only", action="store_true", help=argparse.SUPPRESS)
        bootstrap_parser.add_argument("--json", action="store_true")
    bootstrap_apply.add_argument("--confirm")
    bootstrap_apply.add_argument("--run-version", action="store_true")
    bootstrap_command.add_argument("--profile", required=True)
    bootstrap_command.add_argument("--adapter", required=True)
    bootstrap_command.add_argument("--transport", required=True)
    bootstrap_command.add_argument("--ports", choices=("auto",))
    bootstrap_command.add_argument("--manifest-url", required=True)
    bootstrap_command.add_argument("--allow-provider-host", dest="allow_provider_host", required=True)
    bootstrap_command.add_argument("--provider-host", dest="allow_provider_host", help=argparse.SUPPRESS)
    bootstrap_command.add_argument("--bundle-output", type=Path)
    bootstrap_command.add_argument("--bundle-file", type=Path)
    bootstrap_command.add_argument("--json", action="store_true")
    return parser


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path, SwitchPaths]:
    config_path = args.config or Path("/etc/pilottunnel/config.json")
    state_path = args.state or Path("/var/lib/pilottunnel/state.json")
    registry_path = args.registry or Path("/var/lib/pilottunnel/registry.json")
    audit_path = args.audit_log or Path("/var/log/pilottunnel/audit.log")
    lock_dir = args.lock_dir or Path("/var/lib/pilottunnel/locks")
    work_dir = args.work_dir or Path(tempfile.gettempdir()) / "pilottunnel"
    cache_root = args.cache_root or work_dir
    command_staging_root = getattr(args, "command_staging_root", None)
    staging_root = command_staging_root or args.staging_root or (work_dir / ".var" / "pilottunnel" / "staging")
    return config_path, state_path, registry_path, SwitchPaths(lock_dir=lock_dir, work_dir=cache_root, audit_path=audit_path, staging_root=staging_root)


def _load_runtime(args: argparse.Namespace) -> tuple[AppConfig, AppState, PortRegistry, Path, Path, Path, SwitchPaths]:
    config_path, state_path, registry_path, switch_paths = _paths(args)
    return (
        load_config(config_path),
        load_state(state_path),
        load_registry(registry_path),
        config_path,
        state_path,
        registry_path,
        switch_paths,
    )


def _action_name(args: argparse.Namespace) -> str | None:
    if args.command == "adapter":
        return f"adapter_{args.adapter_command}"
    if args.command == "binary":
        if args.binary_command == "install":
            return f"binary_install_{args.binary_install_command}"
        if args.binary_command == "source":
            return f"binary_source_{args.binary_source_command}"
        if args.binary_command == "provider":
            return f"binary_provider_{args.binary_provider_command}"
        return f"binary_{args.binary_command.replace('-', '_')}"
    if args.command == "backup":
        return f"backup_{args.backup_command}"
    if args.command == "bundle":
        return f"bundle_{args.bundle_command.replace('-', '_')}"
    if args.command == "simulate":
        return f"simulate_{args.simulate_command.replace('-', '_')}"
    if args.command == "install":
        return f"install_{args.install_command}"
    if args.command == "uninstall":
        return f"uninstall_{args.uninstall_command}"
    if args.command == "service":
        if args.service_command == "install":
            return f"service_install_{args.service_install_command.replace('-', '_')}"
        return f"service_{args.service_command.replace('-', '_')}"
    if args.command == "profile":
        return f"profile_{args.profile_command}"
    if args.command == "staged":
        return f"staged_{args.staged_command}"
    if args.command == "registry":
        return f"registry_{args.registry_command}"
    if args.command == "node":
        return "node_status"
    if args.command == "readiness":
        return f"readiness_{args.readiness_command}"
    if args.command == "rc":
        return f"rc_{args.rc_command}"
    if args.command == "runtime":
        return f"runtime_{args.runtime_command}"
    if args.command == "systemd":
        if args.systemd_command == "reload":
            return f"systemd_reload_{args.systemd_reload_command}"
        if args.systemd_command == "start":
            return f"systemd_start_{args.systemd_start_command}"
        if args.systemd_command == "stop":
            return f"systemd_stop_{args.systemd_stop_command}"
        return f"systemd_{args.systemd_command}"
    if args.command == "restore":
        return f"restore_{args.restore_command}"
    if args.command == "deploy":
        return f"deploy_{args.deploy_command}"
    if args.command == "bootstrap":
        return f"bootstrap_{args.bootstrap_command}"
    if args.command == "switch":
        if getattr(args, "switch_command", None):
            return f"switch_{args.switch_command}"
        return "switch"
    if args.command in {"status", "healthcheck", "logs", "cleanup", "plan", "preflight", "rollback"}:
        return args.command
    return None


def _guard_role(config: AppConfig, action: str | None) -> str | None:
    role = config.node.normalized_role
    if not role or action is None:
        return None
    if action_allowed_for_role(action, role):
        return None
    return f"Action '{action}' is blocked for node role '{role}'"


def _prompt_for_role() -> str:
    print("Select this server role:")
    print("")
    print("1. Controller")
    print("2. Worker")
    choice = input("> ").strip()
    if choice == "1":
        return "controller"
    if choice == "2":
        return "worker"
    raise ValueError("Invalid role selection")


def _interactive_console_available() -> bool:
    stdin_tty = getattr(sys.stdin, "isatty", lambda: False)()
    stdout_tty = getattr(sys.stdout, "isatty", lambda: False)()
    return bool(stdin_tty and stdout_tty)


def _save_runtime(
    config: AppConfig,
    state: AppState,
    registry: PortRegistry,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
) -> None:
    save_config(config, config_path)
    save_state(state, state_path)
    save_registry(registry, registry_path)


def _profile_candidates(values: list[str]) -> list[Candidate]:
    items: list[Candidate] = []
    for value in values:
        adapter, transport = value.split(":", 1)
        items.append(Candidate(adapter=adapter, transport=transport))
    return items


def _validate_port(value: int | None, label: str) -> None:
    if value is None:
        return
    if value < 1 or value > 65535:
        raise ValueError(f"{label} must be between 1 and 65535")


def _validate_profile_ports(profile: Profile) -> None:
    for label, value in [
        ("main_port", profile.main_port),
        ("target_port", profile.target_port),
        ("control_port", profile.ports.control_port),
        ("service_port", profile.ports.service_port),
        ("check_port", profile.ports.check_port),
    ]:
        _validate_port(value, label)


def _validate_bundle_output_path(output: Path) -> Path:
    if ".." in output.parts:
        raise ValueError(f"Path traversal blocked for output path: {output!r}")
    return output


def _profile_from_bundle_profile(data: dict) -> Profile:
    ports_data = data.get("ports") or {}
    safety_data = data.get("safety") or {}
    return Profile(
        name=data["name"],
        main_port=data.get("main_port", ports_data.get("main_port")),
        target_host=data.get("target_host", ""),
        target_port=data.get("target_port", ports_data.get("target_port")),
        role=data.get("role", "worker"),
        active_layer=data.get("active_layer", "layer4"),
        active_adapter=data.get("active_adapter", ""),
        active_transport=data.get("active_transport", ""),
        candidates=[Candidate(**item) for item in data.get("candidates", [])],
        ports=ProfilePorts(
            main_port=data.get("main_port", ports_data.get("main_port")),
            control_port=ports_data.get("control_port"),
            service_port=ports_data.get("service_port"),
            check_port=ports_data.get("check_port"),
        ),
        safety=ProfileSafety(
            cooldown_seconds=safety_data.get("cooldown_seconds", 30),
            rollback_on_failure=safety_data.get("rollback_on_failure", True),
            dry_run_default=safety_data.get("dry_run_default", True),
        ),
    )


def _stage_bundle_import(profile: Profile, adapter_name: str, transport: str, switch_paths: SwitchPaths) -> list[str]:
    adapter = ADAPTERS[adapter_name]()
    context = AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=switch_paths.work_dir / profile.name,
        staging_root=switch_paths.staging_root,
        apply_changes=True,
        role="worker",
        remote_stub=asdict(build_worker_stub(profile)),
    )
    rendered_config = adapter.render_config(context)
    rendered_unit = adapter.render_systemd_unit(context)
    return [rendered_config["config_path"], rendered_unit["unit"]["path"]]


def _adapter_payload(name: str) -> dict:
    if name not in ADAPTERS:
        raise KeyError(f"Unknown adapter '{name}'")
    meta = ADAPTERS[name]().metadata()
    return {
        "id": name,
        "layer": meta.layer,
        "status": "usable" if meta.supported else "listed-only",
        "supported_transports": list(meta.all_transports()),
        "usable_in_v0_1": list(meta.transports),
        "experimental_blocked": list(meta.experimental_transports),
        "notes": meta.notes,
    }


def _registry_view(config: AppConfig, state: AppState, registry: PortRegistry) -> tuple[PortRegistry, list[str]]:
    computed = PortRegistry(owners=dict(registry.owners))
    issues: list[str] = []
    for index, profile in enumerate(config.profiles):
        for other in config.profiles[index + 1 :]:
            overlap = sorted(set(profile.ports.owned_ports()) & set(other.ports.owned_ports()))
            if overlap:
                issues.append(f"Profiles '{profile.name}' and '{other.name}' conflict on declared ports {overlap}")
    for profile in config.profiles:
        record = state.profiles.get(profile.name)
        if not record or not record.active_adapter:
            continue
        if profile.name in computed.owners:
            entry = computed.owners[profile.name]
            if entry.transport != record.active_transport:
                issues.append(
                    f"State/registry mismatch for profile '{profile.name}': state transport={record.active_transport}, registry transport={entry.transport}"
                )
            continue
        try:
            computed.claim(
                RegistryEntry(
                    profile=profile.name,
                    main_port=profile.ports.main_port,
                    adapter=record.active_adapter,
                    transport=record.active_transport,
                    role=profile.role,
                    owned_ports=profile.ports.owned_ports(),
                    owned_services=[record.service_name] if record.service_name else [],
                    owned_firewall_rule_tags=[],
                    owned_routes=[],
                )
            )
        except ValueError as exc:
            issues.append(str(exc))
    return computed, issues


def _status_payload(config: AppConfig, state: AppState, registry: PortRegistry, profile_name: str) -> dict:
    profile = get_profile(config, profile_name)
    record = state.profiles.get(profile_name)
    entry = registry.owners.get(profile_name)
    return {
        "profile": profile.name,
        "main_port": profile.ports.main_port,
        "role": profile.role,
        "target_host": profile.target_host,
        "target_port": profile.target_port,
        "active_layer": record.active_layer if record else profile.active_layer,
        "active_adapter": record.active_adapter if record else profile.active_adapter,
        "active_transport": record.active_transport if record else profile.active_transport,
        "owned_ports": entry.owned_ports if entry else profile.ports.owned_ports(),
        "owned_services": entry.owned_services if entry else ([record.service_name] if record and record.service_name else []),
        "last_switch_result": {
            "healthy": record.healthy if record else False,
            "last_error": record.last_error if record else "",
            "last_switch_at": record.last_switch_at if record else "",
        },
    }


def _staged_list(paths: SwitchPaths) -> list[str]:
    if not paths.staging_root.exists():
        return []
    return [str(path) for path in sorted(paths.staging_root.rglob("*")) if path.is_file()]


def _staged_show(paths: SwitchPaths, profile: str, adapter: str, transport: str) -> dict:
    base = paths.staging_root / "configs" / profile / adapter / transport
    systemd_dir = paths.staging_root / "systemd"
    if not base.exists() and not systemd_dir.exists():
        raise FileNotFoundError("No staged files found")
    configs: dict[str, str] = {}
    if base.exists():
        for path in sorted(base.rglob("*.toml")):
            configs[str(path)] = path.read_text(encoding="utf-8")
    units: dict[str, str] = {}
    if systemd_dir.exists():
        for path in sorted(systemd_dir.glob(f"pilottunnel-{profile}-{adapter}-{transport}-*.service")):
            units[str(path)] = path.read_text(encoding="utf-8")
    return {"configs": configs, "units": units}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "version":
        print(json.dumps(version_payload(), indent=2))
        return 0
    config, state, registry, config_path, state_path, registry_path, switch_paths = _load_runtime(args)
    audit_path = switch_paths.audit_path
    engine = SwitchEngine(config=config, state=state, registry=registry, paths=switch_paths)

    if args.command == "init":
        role_value = args.role
        if config.node.initialized and not args.force:
            print(json.dumps({"ok": False, "message": f"Node role already initialized as '{config.node.normalized_role}'. Use --force to overwrite."}, indent=2))
            return 1
        if not role_value:
            if config.node.initialized and args.force:
                role_value = config.node.normalized_role
            elif not _interactive_console_available():
                print(json.dumps({"ok": False, "message": "Role is required in non-interactive mode. Use --role controller or --role worker."}, indent=2))
                return 1
            else:
                try:
                    role_value = _prompt_for_role()
                except EOFError:
                    role_value = "controller"
                except ValueError as exc:
                    print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
                    return 1
        try:
            node = build_node_settings(role_value, existing_node_id=config.node.node_id)
        except ValueError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        old_role = config.node.normalized_role
        config.node = node
        _save_runtime(config, state, registry, config_path, state_path, registry_path)
        write_audit_log(
            "init_role",
            "local-node",
            {
                "old_role": old_role,
                "new_role": node.normalized_role,
                "force": args.force,
                "role_alias_used": node.role_alias_used,
                "node_id": node.node_id,
            },
            switch_paths.audit_path,
        )
        print(
            json.dumps(
                {
                    "status": "initialized",
                    "config": str(config_path),
                    "node_role": node.node_role,
                    "normalized_role": node.normalized_role,
                    "initialized": True,
                },
                indent=2,
            )
        )
        return 0

    role_error = _guard_role(config, _action_name(args))
    if role_error:
        if args.command == "bundle" and args.bundle_command == "export-worker":
            write_audit_log(
                "bundle-export-worker",
                args.profile,
                {"result": "failed", "reason": role_error, "adapter": args.adapter, "transport": args.transport},
                switch_paths.audit_path,
            )
        print(json.dumps({"ok": False, "message": role_error}, indent=2))
        return 1

    if args.command == "node" and args.node_command == "status":
        print(json.dumps(node_status_payload(config, str(config_path)), indent=2))
        return 0

    if args.command == "runtime" and args.runtime_command == "plan":
        try:
            payload = build_runtime_plan(
                config=config,
                state=state,
                runtime_dir=args.runtime_dir,
                requested_platform=args.platform,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "systemd" and args.systemd_command == "reload" and args.systemd_reload_command == "plan":
        payload = build_systemd_reload_plan(target_dir=args.target_dir, audit_path=switch_paths.audit_path)
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "systemd" and args.systemd_command == "reload" and args.systemd_reload_command == "apply":
        payload = apply_systemd_reload(
            target_dir=args.target_dir,
            confirm=args.confirm,
            audit_path=switch_paths.audit_path,
        )
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "systemd" and args.systemd_command == "start" and args.systemd_start_command == "plan":
        payload = build_systemd_start_plan(
            service_dir=args.service_dir,
            service_name=args.service_name,
            audit_path=switch_paths.audit_path,
        )
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "systemd" and args.systemd_command == "start" and args.systemd_start_command == "apply":
        payload = apply_systemd_start(
            service_dir=args.service_dir,
            service_name=args.service_name,
            confirm=args.confirm,
            audit_path=switch_paths.audit_path,
            timeout_seconds=args.timeout,
        )
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "systemd" and args.systemd_command == "stop" and args.systemd_stop_command == "plan":
        payload = build_systemd_stop_plan(
            service_dir=args.service_dir,
            service_name=args.service_name,
            audit_path=switch_paths.audit_path,
        )
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "systemd" and args.systemd_command == "stop" and args.systemd_stop_command == "apply":
        payload = apply_systemd_stop(
            service_dir=args.service_dir,
            service_name=args.service_name,
            confirm=args.confirm,
            audit_path=switch_paths.audit_path,
            timeout_seconds=args.timeout,
        )
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "systemd" and args.systemd_command == "status":
        payload = inspect_systemd_status(
            service_dir=args.service_dir,
            service_name=args.service_name,
            audit_path=switch_paths.audit_path,
            timeout_seconds=args.timeout,
        )
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "profile" and args.profile_command == "create":
        if args.layer not in SUPPORTED_LAYERS:
            parser.error(f"Unknown layer: {args.layer}")
        existing = [item for item in config.profiles if item.name == args.name]
        if existing and not (args.force or args.update):
            print(json.dumps({"ok": False, "message": f"Profile '{args.name}' already exists. Use --force or --update."}, indent=2))
            return 1
        try:
            profile = Profile(
                name=args.name,
                main_port=args.main_port,
                target_host=args.target_host,
                target_port=args.target_port,
                role=canonical_role(args.role),
                active_layer=args.layer,
                candidates=_profile_candidates(args.candidate),
                ports=ProfilePorts(
                    main_port=args.main_port,
                    control_port=args.control_port,
                    service_port=args.service_port,
                    check_port=args.check_port,
                ),
                safety=ProfileSafety(),
            )
        except ValueError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        try:
            profile.name = validate_profile_name(profile.name)
        except ValueError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        try:
            _validate_profile_ports(profile)
        except ValueError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        for item in config.profiles:
            if item.name == profile.name:
                continue
            overlap = set(item.ports.owned_ports()) & set(profile.ports.owned_ports())
            if overlap:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "message": f"Profile '{profile.name}' conflicts with '{item.name}' on ports {sorted(overlap)}",
                        },
                        indent=2,
                    )
                )
                return 1
        config.profiles = [item for item in config.profiles if item.name != profile.name]
        config.profiles.append(profile)
        _save_runtime(config, state, registry, config_path, state_path, registry_path)
        print(json.dumps({"ok": True, "status": "created" if not existing else "updated", "profile": asdict(profile)}, indent=2))
        return 0

    if args.command == "profile" and args.profile_command == "list":
        print(json.dumps([{"name": profile.name, "role": profile.role, "main_port": profile.ports.main_port} for profile in config.profiles], indent=2))
        return 0

    if args.command == "profile" and args.profile_command == "show":
        try:
            profile = get_profile(config, args.name)
        except KeyError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(asdict(profile), indent=2))
        return 0

    if args.command == "layer" and args.layer_command == "list":
        print(json.dumps([{"name": name, "supported": supported} for name, supported in SUPPORTED_LAYERS.items()], indent=2))
        return 0

    if args.command == "adapter" and args.adapter_command == "list":
        print(json.dumps([_adapter_payload(name) for name in ADAPTERS], indent=2))
        return 0

    if args.command == "adapter" and args.adapter_command == "show":
        try:
            print(json.dumps(_adapter_payload(args.name), indent=2))
        except KeyError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        return 0

    if args.command == "install" and args.install_command == "plan":
        if args.apply:
            print(json.dumps({"ok": False, "message": "Real apply execution is not supported for install planning"}, indent=2))
            return 1
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            payload = build_install_plan(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=args.role,
                paths=switch_paths,
                state=state,
                install_root=args.install_root,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "install" and args.install_command == "apply":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            readiness_report = None
            if args.real_host_files:
                readiness_report = build_readiness_report(
                    config=config,
                    state=state,
                    registry=registry,
                    config_path=config_path,
                    switch_paths=switch_paths,
                    profile_name=profile_name,
                    adapter_name=args.adapter,
                    transport=args.transport,
                    staging_root=switch_paths.staging_root,
                )
            payload = apply_install(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=args.role,
                paths=switch_paths,
                state=state,
                install_root=args.install_root,
                confirm=args.confirm,
                dry_run=args.dry_run,
                require_healthcheck=args.require_healthcheck,
                real_host_files=args.real_host_files,
                node_initialized=config.node.initialized,
                node_role=config.node.normalized_role,
                readiness_report=readiness_report,
            )
        except (KeyError, ValueError) as exc:
            payload = {"ok": False, "action": "install-apply", "message": str(exc)}
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "install" and args.install_command == "rollback":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            payload = rollback_install(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                paths=switch_paths,
                install_root=args.install_root,
                confirm=args.confirm,
                real_host_files=args.real_host_files,
                dry_run=args.dry_run,
            )
        except (KeyError, ValueError) as exc:
            payload = {"ok": False, "action": "install-rollback", "message": str(exc)}
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "uninstall" and args.uninstall_command == "plan":
        if args.apply:
            print(json.dumps({"ok": False, "message": "Real apply execution is not supported for uninstall planning"}, indent=2))
            return 1
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            payload = build_uninstall_plan(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=args.role,
                paths=switch_paths,
                state=state,
                install_root=args.install_root,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "uninstall" and args.uninstall_command == "apply":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            payload = apply_uninstall(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=args.role,
                paths=switch_paths,
                state=state,
                install_root=args.install_root,
                confirm=args.confirm,
                real_host_files=args.real_host_files,
                dry_run=args.dry_run,
            )
        except (KeyError, ValueError) as exc:
            payload = {"ok": False, "action": "uninstall-apply", "message": str(exc)}
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "service" and args.service_command == "plan":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            requested_role = canonical_role(args.role) if args.role else None
            if requested_role and config.node.initialized and requested_role != config.node.normalized_role:
                raise ValueError(f"Requested service role '{requested_role}' does not match initialized node role '{config.node.normalized_role}'")
            payload = build_service_plan(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                action=args.action,
                role=requested_role or config.node.normalized_role or profile.role,
                paths=switch_paths,
                state=state,
                install_root=args.install_root,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "service" and args.service_command == "render":
        try:
            payload = build_staged_service_plan(
                config=config,
                state=state,
                runtime_dir=args.runtime_dir,
                service_dir=args.service_dir,
                requested_platform=args.platform,
                audit_path=switch_paths.audit_path,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "service" and args.service_command == "install" and args.service_install_command == "plan":
        try:
            payload = build_service_install_plan(
                config=config,
                state=state,
                runtime_dir=args.runtime_dir,
                service_dir=args.service_dir,
                target_dir=args.target_dir,
                requested_platform=args.platform,
                allow_system_dir=args.allow_system_dir,
                audit_path=switch_paths.audit_path,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "service" and args.service_command == "install" and args.service_install_command == "apply":
        try:
            payload = apply_service_install(
                config=config,
                state=state,
                runtime_dir=args.runtime_dir,
                service_dir=args.service_dir,
                target_dir=args.target_dir,
                requested_platform=args.platform,
                allow_system_dir=args.allow_system_dir,
                replace_existing=args.replace_existing,
                confirm=args.confirm,
                audit_path=switch_paths.audit_path,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "service" and args.service_command == "status":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            requested_role = canonical_role(args.role) if args.role else None
            if requested_role and config.node.initialized and requested_role != config.node.normalized_role:
                raise ValueError(f"Requested service role '{requested_role}' does not match initialized node role '{config.node.normalized_role}'")
            payload = inspect_service_status(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=requested_role or config.node.normalized_role or profile.role,
                paths=switch_paths,
                install_root=args.install_root,
                real_systemd=args.real_systemd,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "service" and args.service_command == "logs":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            requested_role = canonical_role(args.role) if args.role else None
            if requested_role and config.node.initialized and requested_role != config.node.normalized_role:
                raise ValueError(f"Requested service role '{requested_role}' does not match initialized node role '{config.node.normalized_role}'")
            payload = inspect_service_logs(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=requested_role or config.node.normalized_role or profile.role,
                paths=switch_paths,
                install_root=args.install_root,
                limit=args.limit,
                real_systemd=args.real_systemd,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "service" and args.service_command == "daemon-reload":
        payload = run_daemon_reload(
            paths=switch_paths,
            confirm=args.confirm,
            real_systemd=args.real_systemd,
        )
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "service" and args.service_command == "start":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            requested_role = canonical_role(args.role) if args.role else None
            if requested_role and config.node.initialized and requested_role != config.node.normalized_role:
                raise ValueError(f"Requested service role '{requested_role}' does not match initialized node role '{config.node.normalized_role}'")
            if not config.node.initialized:
                raise ValueError("Real service start requires an initialized node role")
            payload = start_service(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=requested_role or config.node.normalized_role or profile.role,
                paths=switch_paths,
                confirm=args.confirm,
                real_systemd=args.real_systemd,
                require_healthcheck=args.require_healthcheck,
                healthcheck_timeout=args.healthcheck_timeout,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "service" and args.service_command == "stop":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            requested_role = canonical_role(args.role) if args.role else None
            if requested_role and config.node.initialized and requested_role != config.node.normalized_role:
                raise ValueError(f"Requested service role '{requested_role}' does not match initialized node role '{config.node.normalized_role}'")
            if not config.node.initialized:
                raise ValueError("Real service stop requires an initialized node role")
            payload = stop_service(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=requested_role or config.node.normalized_role or profile.role,
                paths=switch_paths,
                confirm=args.confirm,
                real_systemd=args.real_systemd,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "service" and args.service_command == "enable":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            requested_role = canonical_role(args.role) if args.role else None
            if requested_role and config.node.initialized and requested_role != config.node.normalized_role:
                raise ValueError(f"Requested service role '{requested_role}' does not match initialized node role '{config.node.normalized_role}'")
            if not config.node.initialized:
                raise ValueError("Real service enable requires an initialized node role")
            payload = enable_service(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=requested_role or config.node.normalized_role or profile.role,
                paths=switch_paths,
                confirm=args.confirm,
                real_systemd=args.real_systemd,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "service" and args.service_command == "disable":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            requested_role = canonical_role(args.role) if args.role else None
            if requested_role and config.node.initialized and requested_role != config.node.normalized_role:
                raise ValueError(f"Requested service role '{requested_role}' does not match initialized node role '{config.node.normalized_role}'")
            if not config.node.initialized:
                raise ValueError("Real service disable requires an initialized node role")
            payload = disable_service(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=requested_role or config.node.normalized_role or profile.role,
                paths=switch_paths,
                confirm=args.confirm,
                real_systemd=args.real_systemd,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "service" and args.service_command == "restart":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            requested_role = canonical_role(args.role) if args.role else None
            if requested_role and config.node.initialized and requested_role != config.node.normalized_role:
                raise ValueError(f"Requested service role '{requested_role}' does not match initialized node role '{config.node.normalized_role}'")
            if not config.node.initialized:
                raise ValueError("Real service restart requires an initialized node role")
            payload = restart_service(
                profile=profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=requested_role or config.node.normalized_role or profile.role,
                paths=switch_paths,
                confirm=args.confirm,
                real_systemd=args.real_systemd,
                require_healthcheck=args.require_healthcheck,
                healthcheck_timeout=args.healthcheck_timeout,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "bundle" and args.bundle_command == "export-worker":
        try:
            profile_name = validate_profile_name(args.profile)
            profile = get_profile(config, profile_name)
            requested_role = config.node.normalized_role
            if requested_role == "worker":
                raise PermissionError("bundle export-worker is blocked for node role 'worker'")
            output_path = _validate_bundle_output_path(args.output)
            if output_path.exists() and not args.force:
                raise ValueError(f"Bundle output '{output_path}' already exists. Use --force to overwrite.")
            bundle = build_worker_bundle(
                profile,
                args.adapter,
                args.transport,
                include_staged_paths=args.include_staged_paths,
                audit_path=switch_paths.audit_path,
            )
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
            payload = dict(bundle)
            payload["output_path"] = str(output_path)
            payload["written"] = True
        except (KeyError, ValueError, PermissionError, json.JSONDecodeError) as exc:
            write_audit_log(
                "bundle-export-worker",
                args.profile if getattr(args, "profile", None) else "bundle-export",
                {"result": "failed", "reason": str(exc), "adapter": getattr(args, "adapter", ""), "transport": getattr(args, "transport", "")},
                switch_paths.audit_path,
            )
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "bundle" and args.bundle_command == "inspect":
        try:
            payload = inspect_bundle(args.input)
            payload["node_role"] = config.node.normalized_role
            payload["node_role_matches_worker"] = config.node.normalized_role == "worker"
        except (KeyError, ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "bundle" and args.bundle_command == "import":
        bundle_profile_name = "bundle-import"
        try:
            bundle_data = import_bundle(args.input)
            bundle_profile_name = bundle_data["profile"]["name"]
            if config.node.initialized and config.node.normalized_role == "controller" and not args.force:
                raise PermissionError("bundle import is blocked for controller nodes without --force")
            if args.confirm != "IMPORT":
                raise ValueError("Refusing to import bundle without --confirm IMPORT")

            imported_profile = _profile_from_bundle_profile(bundle_data["profile"])
            _validate_profile_ports(imported_profile)
            for existing in config.profiles:
                if existing.name == imported_profile.name:
                    continue
                overlap = set(existing.ports.owned_ports()) & set(imported_profile.ports.owned_ports())
                if overlap:
                    raise ValueError(f"Profile '{imported_profile.name}' conflicts with '{existing.name}' on ports {sorted(overlap)}")

            preview_paths = bundle_data.get("staged_paths") or bundle_data.get("expected_paths") or {}
            staged_files: list[str] = []
            if not args.dry_run:
                config.profiles = [item for item in config.profiles if item.name != imported_profile.name]
                config.profiles.append(imported_profile)
                _save_runtime(config, state, registry, config_path, state_path, registry_path)
                staged_files = _stage_bundle_import(imported_profile, bundle_data["adapter"], bundle_data["transport"], switch_paths)
            else:
                staged_files = list(preview_paths.values())
            audit_details = {
                "result": "ok",
                "dry_run": args.dry_run,
                "force": args.force,
                "staged_files": staged_files,
                "bundle_type": bundle_data["bundle_type"],
                "adapter": bundle_data["adapter"],
                "transport": bundle_data["transport"],
            }
            write_audit_log("bundle-import", bundle_profile_name, audit_details, switch_paths.audit_path)
            result_payload = {
                "ok": True,
                "dry_run": args.dry_run,
                "profile": imported_profile.name,
                "adapter": bundle_data["adapter"],
                "transport": bundle_data["transport"],
                "staged_files": staged_files,
                "config_written": not args.dry_run,
                "no_system_changes": True,
                "worker_role": "worker",
            }
        except (KeyError, ValueError, PermissionError, FileNotFoundError, json.JSONDecodeError) as exc:
            write_audit_log(
                "bundle-import",
                bundle_profile_name,
                {"result": "failed", "reason": str(exc), "dry_run": args.dry_run, "force": args.force},
                switch_paths.audit_path,
            )
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(result_payload, indent=2))
        return 0

    if args.command == "simulate" and args.simulate_command == "e2e":
        try:
            payload = run_e2e_simulation(
                profile=args.profile,
                adapter=args.adapter,
                transport=args.transport,
                base_root=args.base_root,
                keep_files=args.keep_files,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "switch":
        if args.switch_command == "plan":
            try:
                payload = build_manual_switch_plan(
                    config=config,
                    state=state,
                    target_tunnel=args.target,
                    runtime_dir=args.runtime_dir,
                    service_dir=args.service_dir,
                    audit_path=switch_paths.audit_path,
                )
            except (KeyError, ValueError) as exc:
                print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
                return 1
            print(json.dumps(payload, indent=2))
            return 0 if payload["ok"] else 1

        if args.switch_command == "apply":
            try:
                payload = apply_manual_switch(
                    config=config,
                    state=state,
                    state_path=state_path,
                    target_tunnel=args.target,
                    runtime_dir=args.runtime_dir,
                    service_dir=args.service_dir,
                    confirm=args.confirm,
                    paths=switch_paths,
                )
            except (KeyError, ValueError) as exc:
                print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
                return 1
            if payload["ok"]:
                _save_runtime(config, state, registry, config_path, state_path, registry_path)
            print(json.dumps(payload, indent=2))
            return 0 if payload["ok"] else 1

        if not args.profile or not args.adapter or not args.transport:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "message": "Legacy switch requires --profile, --adapter, and --transport, or use 'switch plan/apply'.",
                    },
                    indent=2,
                )
            )
            return 1
        try:
            profile = get_profile(config, args.profile)
            tcp_plan = build_profile_healthcheck_plan(
                profile=profile,
                node_role=config.node.normalized_role or "controller",
                timeout=DEFAULT_TIMEOUT_SECONDS,
                include_all=True,
                role_aware=True,
            )
            tcp_summary = None
            if args.require_healthcheck:
                tcp_summary = summarize_healthchecks(
                    run_profile_healthchecks(
                        profile=profile,
                        node_role=config.node.normalized_role or "controller",
                        timeout=DEFAULT_TIMEOUT_SECONDS,
                        include_all=True,
                        role_aware=True,
                    ),
                    profile=profile.name,
                    role=config.node.normalized_role or "controller",
                )
            if args.require_healthcheck and not tcp_summary["ok"]:
                write_audit_log(
                    "healthcheck",
                    profile.name,
                    {"result": "failed", "reason": "require-healthcheck blocked switch", "healthcheck": tcp_summary},
                    switch_paths.audit_path,
                )
                print(json.dumps({"ok": False, "message": "Healthcheck requirement failed before switch", "healthcheck": tcp_summary}, indent=2))
                return 1
            result = engine.switch(args.profile, args.adapter, args.transport, args.apply)
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        _save_runtime(engine.config, engine.state, engine.registry, config_path, state_path, registry_path)
        payload = dict(result.__dict__)
        payload["tcp_healthcheck"] = tcp_summary
        payload["tcp_healthcheck_plan"] = tcp_plan
        print(json.dumps(payload, indent=2))
        return 0 if result.ok else 1

    if args.command == "status":
        try:
            print(json.dumps(_status_payload(config, state, registry, args.profile), indent=2))
        except KeyError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        return 0

    if args.command == "healthcheck":
        try:
            if args.host or args.port:
                if not (args.host and args.port):
                    raise ValueError("Both --host and --port are required for direct endpoint checks")
                role = config.node.normalized_role or "controller"
                profile_name = args.profile or ""
                payload = tcp_healthcheck(
                    host=args.host,
                    port=args.port,
                    timeout=args.timeout,
                    role=role,
                    profile=profile_name,
                ).to_dict()
                payload["result"] = "ok" if payload["ok"] else "failed"
                if not payload["ok"]:
                    write_audit_log("healthcheck", profile_name or "direct", {"result": "failed", "healthcheck": payload}, switch_paths.audit_path)
                print(json.dumps(payload, indent=2))
                return 0 if payload["ok"] else 1
            if not args.profile:
                raise ValueError("healthcheck requires --profile or both --host and --port")
            profile = get_profile(config, args.profile)
            node_role = config.node.normalized_role or "controller"
            results = run_profile_healthchecks(
                profile=profile,
                node_role=node_role,
                timeout=args.timeout,
                include_all=args.all,
                role_aware=args.role_aware,
            )
            payload = summarize_healthchecks(results, profile=profile.name, role=node_role)
            if not payload["ok"]:
                write_audit_log("healthcheck", profile.name, {"result": "failed", "healthcheck": payload}, switch_paths.audit_path)
            print(json.dumps(payload, indent=2))
            return 0 if payload["ok"] else 1
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1

    if args.command == "rollback":
        try:
            result = engine.rollback(args.profile, args.apply)
        except KeyError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        _save_runtime(engine.config, engine.state, engine.registry, config_path, state_path, registry_path)
        print(json.dumps(result.__dict__, indent=2))
        return 0 if result.ok else 1

    if args.command == "logs":
        audit_path = switch_paths.audit_path
        if not audit_path.exists():
            print("[]")
            return 0
        lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if args.profile:
            lines = [item for item in lines if item["profile"] == args.profile]
        print(json.dumps(lines[-args.limit :], indent=2))
        return 0

    if args.command == "registry" and args.registry_command == "check":
        computed_registry, issues = _registry_view(config, state, registry)
        conflicts = issues + computed_registry.check_conflicts()
        print(json.dumps({"ok": not conflicts, "conflicts": conflicts, "owners": {k: asdict(v) for k, v in computed_registry.owners.items()}}, indent=2))
        return 0 if not conflicts else 1

    if args.command == "cleanup":
        try:
            result = engine.cleanup(args.profile, args.apply, args.dry_run)
        except KeyError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(result.__dict__, indent=2))
        return 0 if result.ok else 1

    if args.command == "plan":
        try:
            payload = engine.plan(args.profile, args.adapter, args.transport, apply_changes=False)
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["supported_in_v0_1"] else 1

    if args.command == "preflight":
        try:
            profile = get_profile(config, args.profile) if args.profile else None
        except KeyError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        payload = run_preflight(switch_paths.staging_root, profile).to_dict()
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "backup" and args.backup_command == "plan":
        try:
            payload = build_backup_plan(
                config=config,
                state=state,
                switch_paths=switch_paths,
                config_path=config_path,
                state_path=state_path,
                registry_path=registry_path,
                audit_path=audit_path,
                profile_name=args.profile,
                adapter_name=args.adapter,
                transport=args.transport,
                install_root=args.install_root,
                backup_root=args.backup_root,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "backup" and args.backup_command == "create":
        try:
            payload = create_backup(
                config=config,
                switch_paths=switch_paths,
                config_path=config_path,
                state_path=state_path,
                registry_path=registry_path,
                audit_path=audit_path,
                profile_name=args.profile,
                adapter_name=args.adapter,
                transport=args.transport,
                install_root=args.install_root,
                backup_root=args.backup_root,
                confirm=args.confirm,
            )
        except (KeyError, ValueError, FileExistsError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "backup" and args.backup_command == "list":
        try:
            payload = list_backups(switch_paths=switch_paths, backup_root=args.backup_root)
        except ValueError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "backup" and args.backup_command == "inspect":
        try:
            payload = inspect_backup(switch_paths=switch_paths, backup_root=args.backup_root, backup_id=args.backup_id)
        except (ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "backup" and args.backup_command == "verify":
        try:
            payload = verify_backup(switch_paths=switch_paths, backup_root=args.backup_root, backup_id=args.backup_id)
        except (ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "restore" and args.restore_command == "plan":
        try:
            payload = build_restore_plan(
                config_path=config_path,
                state_path=state_path,
                registry_path=registry_path,
                audit_path=audit_path,
                switch_paths=switch_paths,
                backup_root=args.backup_root,
                backup_id=args.backup_id,
                install_root=args.install_root,
            )
        except (ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "restore" and args.restore_command == "apply":
        try:
            payload = apply_restore(
                config=config,
                config_path=config_path,
                state_path=state_path,
                registry_path=registry_path,
                audit_path=audit_path,
                switch_paths=switch_paths,
                backup_root=args.backup_root,
                backup_id=args.backup_id,
                install_root=args.install_root,
                confirm=args.confirm,
            )
        except (ValueError, json.JSONDecodeError, FileExistsError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "deploy" and args.deploy_command == "plan":
        try:
            payload = build_deploy_plan(
                config=config,
                state=state,
                registry=registry,
                config_path=config_path,
                switch_paths=switch_paths,
                profile_name=args.profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=args.role,
                enable_after_start=args.enable_after_start,
                require_healthcheck=args.require_healthcheck,
                staging_root=getattr(args, "command_staging_root", None) or args.staging_root,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "deploy" and args.deploy_command == "apply":
        try:
            payload = apply_deploy(
                config=config,
                state=state,
                registry=registry,
                config_path=config_path,
                switch_paths=switch_paths,
                profile_name=args.profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=args.role,
                real_host=args.real_host,
                confirm=args.confirm,
                enable_after_start=args.enable_after_start,
                require_healthcheck=args.require_healthcheck,
                staging_root=getattr(args, "command_staging_root", None) or args.staging_root,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "deploy" and args.deploy_command == "status":
        try:
            payload = build_deploy_status(
                config=config,
                state=state,
                registry=registry,
                config_path=config_path,
                switch_paths=switch_paths,
                profile_name=args.profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role=args.role,
                real_systemd=args.real_systemd,
                staging_root=getattr(args, "command_staging_root", None) or args.staging_root,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "readiness" and args.readiness_command == "report":
        try:
            payload = build_readiness_report(
                config=config,
                state=state,
                registry=registry,
                config_path=config_path,
                switch_paths=switch_paths,
                profile_name=args.profile,
                adapter_name=args.adapter,
                transport=args.transport,
                staging_root=getattr(args, "command_staging_root", None) or args.staging_root,
                install_root=args.install_root,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "rc" and args.rc_command == "check":
        try:
            payload = build_rc_check(
                config=config,
                state=state,
                registry=registry,
                config_path=config_path,
                switch_paths=switch_paths,
                runtime_dir=args.runtime_dir,
                service_dir=args.service_dir,
                target_dir=args.target_dir,
                profile_name=args.profile,
                target_tunnel=args.target,
                allow_system_dir=args.allow_system_dir,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "rc" and args.rc_command == "smoke":
        try:
            payload = build_rc_smoke(
                config=config,
                state=state,
                registry=registry,
                config_path=config_path,
                switch_paths=switch_paths,
                runtime_dir=args.runtime_dir,
                service_dir=args.service_dir,
                target_dir=args.target_dir,
                profile_name=args.profile,
                target_tunnel=args.target,
                allow_system_dir=args.allow_system_dir,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "bootstrap" and args.bootstrap_command == "plan":
        try:
            payload = build_bootstrap_plan(
                config=config,
                state=state,
                registry=registry,
                config_path=config_path,
                switch_paths=switch_paths,
                profile_name=args.profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role_value=args.role,
                create_profile_flag=args.create_profile,
                update_profile_flag=args.update_profile,
                target_host=args.target_host,
                main_port=args.main_port,
                target_port=args.target_port,
                control_port=args.control_port,
                service_port=args.service_port,
                check_port=args.check_port,
                ports_mode=args.ports,
                manifest_url=args.manifest_url,
                manifest_file=args.manifest_file,
                allow_provider_host=args.allow_provider_host,
                bundle_output=args.bundle_output,
                bundle_file=args.bundle_file,
                backup_root=args.backup_root,
                requested_platform=args.platform,
                allow_incomplete_binaries_for_tests_only=args.allow_incomplete_binaries_for_tests_only,
            )
        except (KeyError, ValueError, PermissionError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "bootstrap" and args.bootstrap_command == "apply":
        try:
            payload = apply_bootstrap(
                config=config,
                state=state,
                registry=registry,
                config_path=config_path,
                state_path=state_path,
                registry_path=registry_path,
                switch_paths=switch_paths,
                profile_name=args.profile,
                adapter_name=args.adapter,
                transport=args.transport,
                role_value=args.role,
                create_profile_flag=args.create_profile,
                update_profile_flag=args.update_profile,
                target_host=args.target_host,
                main_port=args.main_port,
                target_port=args.target_port,
                control_port=args.control_port,
                service_port=args.service_port,
                check_port=args.check_port,
                ports_mode=args.ports,
                manifest_url=args.manifest_url,
                manifest_file=args.manifest_file,
                allow_provider_host=args.allow_provider_host,
                bundle_output=args.bundle_output,
                bundle_file=args.bundle_file,
                backup_root=args.backup_root,
                requested_platform=args.platform,
                confirm=args.confirm,
                force=args.force,
                run_version=args.run_version,
                allow_incomplete_binaries_for_tests_only=args.allow_incomplete_binaries_for_tests_only,
            )
        except (KeyError, ValueError, PermissionError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        _save_runtime(config, state, registry, config_path, state_path, registry_path)
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "bootstrap" and args.bootstrap_command == "command":
        try:
            payload = build_bootstrap_command(
                profile_name=args.profile,
                adapter_name=args.adapter,
                transport=args.transport,
                ports_mode=args.ports,
                manifest_url=args.manifest_url,
                provider_host=args.allow_provider_host,
                bundle_output=args.bundle_output,
                bundle_file=args.bundle_file,
            )
        except ValueError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "source" and args.binary_source_command == "list":
        print(json.dumps(list_upstream_sources(), indent=2))
        return 0

    if args.command == "binary" and args.binary_command == "source" and args.binary_source_command == "fetch":
        try:
            payload = fetch_upstream_sources(
                source_dir=args.source_dir,
                platform_id=args.platform,
                cache_root=switch_paths.work_dir,
                confirm=args.confirm,
                force=args.force,
                dry_run=args.dry_run,
                adapter_filters=args.adapter,
                version_filters=args.version,
                audit_path=switch_paths.audit_path,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "install" and args.binary_install_command == "plan":
        try:
            payload = build_binary_install_plan(
                manifest_file=args.manifest_file,
                requested_platform=args.platform,
                install_dir=None,
                config=config,
                state=state,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "install" and args.binary_install_command == "apply":
        try:
            payload = apply_binary_install(
                manifest_file=args.manifest_file,
                requested_platform=args.platform,
                install_dir=args.install_dir,
                config=config,
                state=state,
                confirm=args.confirm,
                audit_path=switch_paths.audit_path,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        if payload["ok"]:
            _save_runtime(config, state, registry, config_path, state_path, registry_path)
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "install" and args.binary_install_command == "list":
        try:
            payload = list_binary_installations(install_dir=args.install_dir)
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "provider" and args.binary_provider_command == "inspect":
        try:
            payload = inspect_manifest(
                manifest_url=args.manifest_url,
                manifest_file=args.manifest_file,
                allow_provider_host=args.allow_provider_host,
                requested_platform=args.platform,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "binary" and args.binary_command == "provider" and args.binary_provider_command == "generate-manifest":
        try:
            payload = generate_manifest(
                provider_name=args.provider_name,
                base_url=args.base_url,
                source_dir=args.source_dir,
                output_path=args.output,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "provider" and args.binary_provider_command == "verify-manifest":
        try:
            payload = verify_manifest_file(manifest_file=args.manifest_file)
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "provider" and args.binary_provider_command == "prepare":
        try:
            payload = prepare_provider_binaries(
                source_dir=args.source_dir,
                provider_name=args.provider_name,
                base_url=args.base_url,
                platform_id=args.platform,
                output_path=args.output,
                cache_root=switch_paths.work_dir,
                confirm=args.confirm,
                version_filters=args.version,
                audit_path=switch_paths.audit_path,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "provider" and args.binary_provider_command == "release-plan":
        try:
            payload = build_provider_release_plan(
                source_dir=args.source_dir,
                provider_name=args.provider_name,
                repo_slug=args.repo_slug,
                release_tag=args.release_tag,
                output_dir=args.output_dir,
                version_overrides=args.version,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "provider" and args.binary_provider_command == "release-assets":
        try:
            payload = write_provider_release_assets(
                source_dir=args.source_dir,
                provider_name=args.provider_name,
                repo_slug=args.repo_slug,
                release_tag=args.release_tag,
                output_dir=args.output_dir,
                version_overrides=args.version,
                confirm=args.confirm,
                force=args.force,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "list":
        print(json.dumps(list_binary_plans(switch_paths.work_dir, state), indent=2))
        return 0

    if args.command == "binary" and args.binary_command == "plan":
        try:
            print(json.dumps(get_binary_plan(args.adapter, switch_paths.work_dir, state), indent=2))
        except KeyError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        return 0

    if args.command == "binary" and args.binary_command == "import":
        try:
            payload = import_binary(
                adapter=args.adapter,
                source=args.source,
                version=args.version,
                cache_root=switch_paths.work_dir,
                state=state,
                sha256_expected=args.sha256,
                force=args.force,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        _save_runtime(config, state, registry, config_path, state_path, registry_path)
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "binary" and args.binary_command == "status":
        if args.require_all:
            try:
                payload = build_binary_readiness_report(
                    cache_root=switch_paths.work_dir,
                    state=state,
                    manifest_url=args.manifest_url,
                    manifest_file=args.manifest_file,
                    allow_provider_host=args.allow_provider_host,
                    requested_platform=args.platform,
                    require_all=True,
                )
            except (KeyError, ValueError) as exc:
                print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
                return 1
            print(json.dumps(payload, indent=2))
            return 0 if payload["ok"] else 1
        if args.adapter:
            try:
                print(json.dumps(get_binary_plan(args.adapter, switch_paths.work_dir, state), indent=2))
            except KeyError as exc:
                print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
                return 1
            return 0
        print(json.dumps(list_binary_plans(switch_paths.work_dir, state), indent=2))
        return 0

    if args.command == "binary" and args.binary_command == "verify":
        try:
            payload = verify_binary(
                adapter=args.adapter,
                cache_root=switch_paths.work_dir,
                state=state,
                run_version=args.run_version,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        _save_runtime(config, state, registry, config_path, state_path, registry_path)
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "binary" and args.binary_command == "download":
        try:
            payload = download_binary(
                adapter=args.adapter,
                manifest_url=args.manifest_url,
                manifest_file=args.manifest_file,
                allow_provider_host=args.allow_provider_host,
                cache_root=switch_paths.work_dir,
                state=state,
                confirm=args.confirm,
                force=args.force,
                run_version=args.run_version,
                audit_path=switch_paths.audit_path,
                requested_platform=args.platform,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        if payload["ok"]:
            remember_binary_provider_source(
                config,
                manifest_url=args.manifest_url,
                manifest_file=args.manifest_file,
                allow_provider_host=args.allow_provider_host,
            )
        _save_runtime(config, state, registry, config_path, state_path, registry_path)
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "binary" and args.binary_command == "download-all":
        try:
            payload = download_all_binaries(
                manifest_url=args.manifest_url,
                manifest_file=args.manifest_file,
                allow_provider_host=args.allow_provider_host,
                cache_root=switch_paths.work_dir,
                state=state,
                confirm=args.confirm,
                force=args.force,
                run_version=args.run_version,
                audit_path=switch_paths.audit_path,
                requested_platform=args.platform,
            )
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        if payload["ok"]:
            remember_binary_provider_source(
                config,
                manifest_url=args.manifest_url,
                manifest_file=args.manifest_file,
                allow_provider_host=args.allow_provider_host,
            )
        _save_runtime(config, state, registry, config_path, state_path, registry_path)
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "staged" and args.staged_command == "list":
        print(json.dumps(_staged_list(switch_paths), indent=2))
        return 0

    if args.command == "staged" and args.staged_command == "show":
        try:
            payload = _staged_show(switch_paths, args.profile, args.adapter, args.transport)
        except FileNotFoundError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        print(json.dumps(payload, indent=2))
        return 0

    parser.error("Unhandled command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
