"""PilotTunnel CLI entrypoint."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .adapters import ADAPTERS
from .config import (
    AppConfig,
    Candidate,
    Profile,
    ProfilePorts,
    ProfileSafety,
    SUPPORTED_LAYERS,
    canonical_role,
    load_config,
    save_config,
)
from .registry import PortRegistry, load_registry, save_registry
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
    parser.add_argument("--apply", action="store_true", help="Allow dangerous operations to write runtime artifacts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")

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
    profile_subparsers.add_parser("list")

    subparsers.add_parser("layer").add_subparsers(dest="layer_command", required=True).add_parser("list")
    subparsers.add_parser("adapter").add_subparsers(dest="adapter_command", required=True).add_parser("list")

    install = subparsers.add_parser("install")
    install.add_argument("--profile", required=True)
    install.add_argument("--adapter", required=True)
    install.add_argument("--transport", required=True)

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
    logs.add_argument("--profile", required=True)

    registry = subparsers.add_parser("registry")
    registry.add_subparsers(dest="registry_command", required=True).add_parser("check")

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--profile", required=True)
    cleanup.add_argument("--dry-run", action="store_true")
    return parser


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path, SwitchPaths]:
    config_path = args.config or Path("/etc/pilottunnel/config.json")
    state_path = args.state or Path("/var/lib/pilottunnel/state.json")
    registry_path = args.registry or Path("/var/lib/pilottunnel/registry.json")
    audit_path = args.audit_log or Path("/var/log/pilottunnel/audit.log")
    lock_dir = args.lock_dir or Path("/var/lib/pilottunnel/locks")
    work_dir = args.work_dir or Path(tempfile.gettempdir()) / "pilottunnel"
    return config_path, state_path, registry_path, SwitchPaths(lock_dir=lock_dir, work_dir=work_dir, audit_path=audit_path)


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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config, state, registry, config_path, state_path, registry_path, switch_paths = _load_runtime(args)
    engine = SwitchEngine(config=config, state=state, registry=registry, paths=switch_paths)

    if args.command == "init":
        _save_runtime(config, state, registry, config_path, state_path, registry_path)
        print(json.dumps({"status": "initialized", "config": str(config_path)}))
        return 0

    if args.command == "profile" and args.profile_command == "create":
        if args.layer not in SUPPORTED_LAYERS:
            parser.error(f"Unknown layer: {args.layer}")
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
        config.profiles = [item for item in config.profiles if item.name != profile.name]
        config.profiles.append(profile)
        _save_runtime(config, state, registry, config_path, state_path, registry_path)
        print(json.dumps({"status": "created", "profile": profile.name}))
        return 0

    if args.command == "profile" and args.profile_command == "list":
        print(json.dumps([profile.name for profile in config.profiles], indent=2))
        return 0

    if args.command == "layer" and args.layer_command == "list":
        print(json.dumps([{"name": name, "supported": supported} for name, supported in SUPPORTED_LAYERS.items()], indent=2))
        return 0

    if args.command == "adapter" and args.adapter_command == "list":
        payload = []
        for name, adapter_cls in ADAPTERS.items():
            meta = adapter_cls().metadata()
            payload.append(
                {
                    "name": name,
                    "layer": meta.layer,
                    "supported": meta.supported,
                    "transports": list(meta.transports),
                    "experimental_transports": list(meta.experimental_transports),
                    "experimental": meta.experimental,
                }
            )
        print(json.dumps(payload, indent=2))
        return 0

    if args.command == "install":
        result = engine.install(args.profile, args.adapter, args.transport, args.apply)
        _save_runtime(engine.config, engine.state, engine.registry, config_path, state_path, registry_path)
        print(json.dumps(result.__dict__, indent=2))
        return 0 if result.ok else 1

    if args.command == "switch":
        result = engine.switch(args.profile, args.adapter, args.transport, args.apply)
        _save_runtime(engine.config, engine.state, engine.registry, config_path, state_path, registry_path)
        print(json.dumps(result.__dict__, indent=2))
        return 0 if result.ok else 1

    if args.command == "status":
        print(json.dumps(engine.status(args.profile), indent=2))
        return 0

    if args.command == "healthcheck":
        result = engine.healthcheck(args.profile)
        _save_runtime(engine.config, engine.state, engine.registry, config_path, state_path, registry_path)
        print(json.dumps(result.__dict__, indent=2))
        return 0 if result.ok else 1

    if args.command == "rollback":
        result = engine.rollback(args.profile, args.apply)
        _save_runtime(engine.config, engine.state, engine.registry, config_path, state_path, registry_path)
        print(json.dumps(result.__dict__, indent=2))
        return 0 if result.ok else 1

    if args.command == "logs":
        audit_path = switch_paths.audit_path
        if not audit_path.exists():
            print("[]")
            return 0
        lines = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(json.dumps([item for item in lines if item["profile"] == args.profile], indent=2))
        return 0

    if args.command == "registry" and args.registry_command == "check":
        conflicts = registry.check_conflicts()
        print(json.dumps({"ok": not conflicts, "conflicts": conflicts}, indent=2))
        return 0 if not conflicts else 1

    if args.command == "cleanup":
        result = engine.cleanup(args.profile, args.apply, args.dry_run)
        print(json.dumps(result.__dict__, indent=2))
        return 0 if result.ok else 1

    parser.error("Unhandled command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
