"""PilotTunnel CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from .adapters import ADAPTERS
from .audit import write_audit_log
from .binaries import get_binary_plan, import_binary, list_binary_plans, verify_binary
from .config import (
    AppConfig,
    Candidate,
    Profile,
    ProfilePorts,
    ProfileSafety,
    SUPPORTED_LAYERS,
    build_node_settings,
    canonical_role,
    get_profile,
    load_config,
    save_config,
    validate_profile_name,
)
from .install_plan import apply_install, apply_uninstall, build_install_plan, build_uninstall_plan, rollback_install
from .node_role import action_allowed_for_role, node_status_payload
from .preflight import run_preflight
from .registry import PortRegistry, RegistryEntry, load_registry, save_registry
from .state import AppState, load_state, save_state
from .switch_engine import SwitchEngine, SwitchPaths


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
    install_apply.add_argument("--confirm")
    install_apply.add_argument("--dry-run", action="store_true")
    install_rollback = install_subparsers.add_parser("rollback")
    install_rollback.add_argument("--profile", required=True)
    install_rollback.add_argument("--adapter", required=True)
    install_rollback.add_argument("--transport", required=True)
    install_rollback.add_argument("--install-root", type=Path, default=None)
    install_rollback.add_argument("--confirm")

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
    uninstall_apply.add_argument("--confirm")

    switch = subparsers.add_parser("switch")
    switch.add_argument("--profile", required=True)
    switch.add_argument("--adapter", required=True)
    switch.add_argument("--transport", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--profile", required=True)

    healthcheck = subparsers.add_parser("healthcheck")
    healthcheck.add_argument("--profile", required=True)

    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("--profile", required=True)

    logs = subparsers.add_parser("logs")
    logs.add_argument("--profile")
    logs.add_argument("--limit", type=int, default=20)

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
    binary_verify = binary_subparsers.add_parser("verify")
    binary_verify.add_argument("--adapter", required=True)
    binary_verify.add_argument("--run-version", action="store_true")
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
        return f"binary_{args.binary_command}"
    if args.command == "install":
        return f"install_{args.install_command}"
    if args.command == "uninstall":
        return f"uninstall_{args.uninstall_command}"
    if args.command == "profile":
        return f"profile_{args.profile_command}"
    if args.command == "staged":
        return f"staged_{args.staged_command}"
    if args.command == "registry":
        return f"registry_{args.registry_command}"
    if args.command == "node":
        return "node_status"
    if args.command in {"switch", "status", "healthcheck", "logs", "cleanup", "plan", "preflight", "rollback"}:
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
    print("1. Iran / Controller")
    print("2. Foreign / Worker")
    choice = input("> ").strip()
    if choice == "1":
        return "controller"
    if choice == "2":
        return "worker"
    raise ValueError("Invalid role selection")


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
    config, state, registry, config_path, state_path, registry_path, switch_paths = _load_runtime(args)
    engine = SwitchEngine(config=config, state=state, registry=registry, paths=switch_paths)

    if args.command == "init":
        role_value = args.role
        if config.node.initialized and not args.force:
            print(json.dumps({"ok": False, "message": f"Node role already initialized as '{config.node.normalized_role}'. Use --force to overwrite."}, indent=2))
            return 1
        if not role_value:
            if config.node.initialized and args.force:
                role_value = config.node.normalized_role
            elif not sys.stdin.isatty():
                role_value = "controller"
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
        print(json.dumps({"ok": False, "message": role_error}, indent=2))
        return 1

    if args.command == "node" and args.node_command == "status":
        print(json.dumps(node_status_payload(config, str(config_path)), indent=2))
        return 0

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
            )
        except (KeyError, ValueError) as exc:
            payload = {"ok": False, "action": "uninstall-apply", "message": str(exc)}
        print(json.dumps(payload, indent=2))
        return 0 if payload["ok"] else 1

    if args.command == "switch":
        try:
            result = engine.switch(args.profile, args.adapter, args.transport, args.apply)
        except (KeyError, ValueError) as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        _save_runtime(engine.config, engine.state, engine.registry, config_path, state_path, registry_path)
        print(json.dumps(result.__dict__, indent=2))
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
            result = engine.healthcheck(args.profile)
        except KeyError as exc:
            print(json.dumps({"ok": False, "message": str(exc)}, indent=2))
            return 1
        _save_runtime(engine.config, engine.state, engine.registry, config_path, state_path, registry_path)
        payload = {
            "profile": args.profile,
            "adapter": result.current.get("adapter", ""),
            "transport": result.current.get("transport", ""),
            "dry_run": result.dry_run,
            "result": "ok" if result.ok else "failed",
            "message": result.healthcheck.get("message", result.message),
        }
        print(json.dumps(payload, indent=2))
        return 0 if result.ok else 1

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
