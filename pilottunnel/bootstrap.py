"""Controlled bootstrap preparation workflow."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .adapters import ADAPTERS
from .adapters.base import AdapterContext
from .audit import write_audit_log
from .backup import create_backup
from .binary_provider import download_all_binaries, inspect_manifest
from .bundles import build_worker_bundle, import_bundle
from .config import AppConfig, Candidate, Profile, ProfilePorts, ProfileSafety, build_node_settings, build_worker_stub, canonical_role, get_profile
from .node_role import require_controller, require_worker
from .readiness import build_readiness_report
from .registry import PortRegistry
from .state import AppState
from .switch_engine import SwitchEngine, SwitchPaths


def build_bootstrap_plan(
    *,
    config: AppConfig,
    state: AppState,
    registry: PortRegistry,
    config_path: Path,
    switch_paths: SwitchPaths,
    profile_name: str | None,
    adapter_name: str | None,
    transport: str | None,
    role_value: str | None,
    create_profile_flag: bool,
    target_host: str | None,
    main_port: int | None,
    target_port: int | None,
    control_port: int | None,
    service_port: int | None,
    check_port: int | None,
    manifest_url: str | None,
    manifest_file: Path | None,
    allow_provider_host: str | None,
    bundle_output: Path | None,
    bundle_input: Path | None,
    backup_root: Path | None,
    requested_platform: str | None,
) -> dict[str, Any]:
    profile = _resolve_profile(config, profile_name)
    node_role = _resolved_role(config, role_value)
    manifest = None
    if manifest_url or manifest_file:
        manifest = inspect_manifest(
            manifest_url=manifest_url,
            manifest_file=manifest_file,
            allow_provider_host=allow_provider_host,
            requested_platform=requested_platform,
        )
    profile_preview = None
    if create_profile_flag:
        profile_preview = _profile_payload(
            name=profile_name,
            target_host=target_host,
            role=node_role or "controller",
            main_port=main_port,
            target_port=target_port,
            control_port=control_port,
            service_port=service_port,
            check_port=check_port,
        )
    readiness = build_readiness_report(
        config=config,
        state=state,
        registry=registry,
        config_path=config_path,
        switch_paths=switch_paths,
        profile_name=profile.name if profile else profile_name,
        adapter_name=adapter_name,
        transport=transport,
        staging_root=switch_paths.staging_root,
        install_root=None,
    )
    actions = {
        "role_initialize": bool(role_value and not config.node.initialized),
        "profile_create_or_update": create_profile_flag,
        "binary_provider_inspect": bool(manifest),
        "binary_download_all": bool(manifest),
        "stage_files": bool(profile_name and adapter_name and transport and node_role == "controller"),
        "export_worker_bundle": bool(bundle_output),
        "import_worker_bundle": bool(bundle_input),
        "backup_before_changes": bool(create_profile_flag or bundle_input or role_value),
    }
    return {
        "ok": True,
        "action": "bootstrap-plan",
        "plan_only": True,
        "profile": profile.name if profile else profile_name,
        "role": node_role,
        "adapter": adapter_name,
        "transport": transport,
        "actions": actions,
        "profile_preview": profile_preview,
        "manifest": manifest,
        "bundle_output": str(bundle_output) if bundle_output else "",
        "bundle_input": str(bundle_input) if bundle_input else "",
        "backup_root": str(backup_root.resolve()) if backup_root else "",
        "readiness": readiness,
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def apply_bootstrap(
    *,
    config: AppConfig,
    state: AppState,
    registry: PortRegistry,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
    switch_paths: SwitchPaths,
    profile_name: str | None,
    adapter_name: str | None,
    transport: str | None,
    role_value: str | None,
    create_profile_flag: bool,
    update_profile_flag: bool,
    target_host: str | None,
    main_port: int | None,
    target_port: int | None,
    control_port: int | None,
    service_port: int | None,
    check_port: int | None,
    manifest_url: str | None,
    manifest_file: Path | None,
    allow_provider_host: str | None,
    bundle_output: Path | None,
    bundle_input: Path | None,
    backup_root: Path | None,
    requested_platform: str | None,
    confirm: str | None,
    force: bool,
    run_version: bool,
) -> dict[str, Any]:
    if confirm != "BOOTSTRAP_APPLY":
        return _failure("Refusing bootstrap apply without --confirm BOOTSTRAP_APPLY")

    node_role = _initialize_or_validate_role(config, role_value, switch_paths.audit_path)
    if create_profile_flag or update_profile_flag:
        require_controller("profile_create", node_role)
    if bundle_input and node_role == "controller":
        require_worker("bundle_import", node_role)
    if bundle_output and node_role == "worker":
        require_controller("bundle_export_worker", node_role)

    existing_profile = _resolve_profile(config, profile_name)
    backup_payload = None
    if _should_backup(config, create_profile_flag, bundle_input, role_value):
        backup_payload = create_backup(
            config=config,
            switch_paths=switch_paths,
            config_path=config_path,
            state_path=state_path,
            registry_path=registry_path,
            audit_path=switch_paths.audit_path,
            profile_name=existing_profile.name if existing_profile else None,
            adapter_name=adapter_name,
            transport=transport,
            install_root=None,
            backup_root=backup_root,
            confirm="BACKUP_CREATE",
        )

    download_payload = None
    if manifest_url or manifest_file:
        download_payload = download_all_binaries(
            manifest_url=manifest_url,
            manifest_file=manifest_file,
            allow_provider_host=allow_provider_host,
            cache_root=switch_paths.work_dir,
            state=state,
            confirm="DOWNLOAD_ALL_BINARIES",
            force=force,
            run_version=run_version,
            audit_path=switch_paths.audit_path,
            requested_platform=requested_platform,
        )
        if not download_payload["ok"]:
            raise ValueError(f"Bootstrap binary preparation failed for adapters {download_payload['failed_adapters']}")

    profile = existing_profile
    profile_created = False
    if create_profile_flag or update_profile_flag:
        profile = _upsert_profile(
            config=config,
            existing=profile,
            name=profile_name,
            target_host=target_host,
            role=node_role,
            main_port=main_port,
            target_port=target_port,
            control_port=control_port,
            service_port=service_port,
            check_port=check_port,
            update=update_profile_flag,
        )
        profile_created = True

    bundle_payload = None
    staged_payload = None
    if bundle_input:
        bundle_payload = _apply_bundle_import(
            config=config,
            state=state,
            registry=registry,
            config_path=config_path,
            state_path=state_path,
            registry_path=registry_path,
            switch_paths=switch_paths,
            bundle_path=bundle_input,
            force=force,
        )
        profile = get_profile(config, bundle_payload["profile"])
    elif profile and adapter_name and transport and node_role == "controller":
        engine = SwitchEngine(config=config, state=state, registry=registry, paths=switch_paths)
        result = engine.switch(profile.name, adapter_name, transport, True)
        config = engine.config
        state = engine.state
        registry = engine.registry
        staged_payload = dict(result.__dict__)
        profile = get_profile(config, profile.name)

    export_payload = None
    if bundle_output:
        if profile is None:
            raise ValueError("Bundle export requires a profile")
        if not adapter_name or not transport:
            raise ValueError("Bundle export requires --adapter and --transport")
        output_path = _validate_output_path(bundle_output)
        bundle = build_worker_bundle(profile, adapter_name, transport, include_staged_paths=True, audit_path=switch_paths.audit_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(bundle, indent=2, sort_keys=True), encoding="utf-8")
        export_payload = {"output_path": str(output_path), "profile": profile.name, "adapter": adapter_name, "transport": transport}

    _save_runtime(config, state, registry, config_path, state_path, registry_path)
    readiness = build_readiness_report(
        config=config,
        state=state,
        registry=registry,
        config_path=config_path,
        switch_paths=switch_paths,
        profile_name=profile.name if profile else profile_name,
        adapter_name=adapter_name,
        transport=transport,
        staging_root=switch_paths.staging_root,
        install_root=None,
    )
    payload = {
        "ok": True,
        "action": "bootstrap-apply",
        "role": node_role,
        "profile": profile.name if profile else profile_name,
        "profile_created_or_updated": profile_created,
        "backup": backup_payload,
        "binary_download_all": download_payload,
        "staged_switch": staged_payload,
        "bundle_import": bundle_payload,
        "bundle_export": export_payload,
        "readiness": readiness,
        "downloads_performed": bool(download_payload and download_payload.get("downloads_performed")),
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }
    write_audit_log("bootstrap-apply", profile.name if profile else "bootstrap", payload, switch_paths.audit_path)
    return payload


def _initialize_or_validate_role(config: AppConfig, role_value: str | None, audit_path: Path) -> str:
    if config.node.initialized:
        if role_value and canonical_role(role_value) != config.node.normalized_role:
            raise ValueError(f"Requested bootstrap role '{canonical_role(role_value)}' does not match initialized node role '{config.node.normalized_role}'")
        return config.node.normalized_role
    if not role_value:
        raise ValueError("Bootstrap requires an initialized role or --role")
    node = build_node_settings(role_value, existing_node_id=config.node.node_id)
    config.node = node
    write_audit_log(
        "init_role",
        "local-node",
        {
            "old_role": "",
            "new_role": node.normalized_role,
            "force": False,
            "role_alias_used": node.role_alias_used,
            "node_id": node.node_id,
        },
        audit_path,
    )
    return node.normalized_role


def _apply_bundle_import(
    *,
    config: AppConfig,
    state: AppState,
    registry: PortRegistry,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
    switch_paths: SwitchPaths,
    bundle_path: Path,
    force: bool,
) -> dict[str, Any]:
    bundle_data = import_bundle(bundle_path)
    imported_profile = _bundle_profile(bundle_data["profile"])
    _validate_profile(imported_profile)
    if config.node.initialized and config.node.normalized_role == "controller" and not force:
        raise PermissionError("bundle import is blocked for controller nodes without --force")
    config.profiles = [item for item in config.profiles if item.name != imported_profile.name]
    config.profiles.append(imported_profile)
    _save_runtime(config, state, registry, config_path, state_path, registry_path)
    staged_files = _stage_bundle_import(imported_profile, bundle_data["adapter"], bundle_data["transport"], switch_paths)
    write_audit_log(
        "bundle-import",
        imported_profile.name,
        {
            "result": "ok",
            "staged_files": staged_files,
            "bundle_type": bundle_data["bundle_type"],
            "adapter": bundle_data["adapter"],
            "transport": bundle_data["transport"],
        },
        switch_paths.audit_path,
    )
    return {
        "profile": imported_profile.name,
        "adapter": bundle_data["adapter"],
        "transport": bundle_data["transport"],
        "staged_files": staged_files,
    }


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


def _bundle_profile(data: dict[str, Any]) -> Profile:
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


def _validate_profile(profile: Profile) -> None:
    if not profile.target_host:
        raise ValueError("Bundle target_host is required")
    for value in [profile.main_port, profile.target_port, profile.ports.control_port, profile.ports.service_port, profile.ports.check_port]:
        if value is None:
            continue
        if value < 1 or value > 65535:
            raise ValueError("Bundle port values must be between 1 and 65535")


def _upsert_profile(
    *,
    config: AppConfig,
    existing: Profile | None,
    name: str | None,
    target_host: str | None,
    role: str,
    main_port: int | None,
    target_port: int | None,
    control_port: int | None,
    service_port: int | None,
    check_port: int | None,
    update: bool,
) -> Profile:
    preview = _profile_payload(
        name=name,
        target_host=target_host,
        role=role,
        main_port=main_port,
        target_port=target_port,
        control_port=control_port,
        service_port=service_port,
        check_port=check_port,
    )
    profile = Profile(
        name=preview["name"],
        main_port=preview["main_port"],
        target_host=preview["target_host"],
        target_port=preview["target_port"],
        role=preview["role"],
        ports=ProfilePorts(
            main_port=preview["main_port"],
            control_port=preview["control_port"],
            service_port=preview["service_port"],
            check_port=preview["check_port"],
        ),
    )
    if existing and not update:
        raise ValueError(f"Profile '{profile.name}' already exists. Use --update-profile to overwrite.")
    config.profiles = [item for item in config.profiles if item.name != profile.name]
    config.profiles.append(profile)
    return profile


def _profile_payload(
    *,
    name: str | None,
    target_host: str | None,
    role: str,
    main_port: int | None,
    target_port: int | None,
    control_port: int | None,
    service_port: int | None,
    check_port: int | None,
) -> dict[str, Any]:
    if not name:
        raise ValueError("Bootstrap profile operations require --profile")
    if not target_host:
        raise ValueError("Bootstrap profile operations require --target-host")
    ports = [main_port, target_port, control_port, service_port, check_port]
    if any(value is None for value in ports):
        raise ValueError("Bootstrap profile operations require --main-port, --target-port, --control-port, --service-port, and --check-port")
    return {
        "name": name,
        "target_host": target_host,
        "role": canonical_role(role),
        "main_port": main_port,
        "target_port": target_port,
        "control_port": control_port,
        "service_port": service_port,
        "check_port": check_port,
    }


def _resolve_profile(config: AppConfig, profile_name: str | None) -> Profile | None:
    if not profile_name:
        return None
    try:
        return get_profile(config, profile_name)
    except KeyError:
        return None


def _resolved_role(config: AppConfig, role_value: str | None) -> str:
    if role_value:
        return canonical_role(role_value)
    if config.node.initialized:
        return config.node.normalized_role
    return ""


def _should_backup(config: AppConfig, create_profile_flag: bool, bundle_input: Path | None, role_value: str | None) -> bool:
    return bool(config.node.initialized or create_profile_flag or bundle_input or role_value)


def _validate_output_path(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError(f"Path traversal blocked for output path: {path!r}")
    return path


def _save_runtime(
    config: AppConfig,
    state: AppState,
    registry: PortRegistry,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
) -> None:
    from .config import save_config
    from .registry import save_registry
    from .state import save_state

    save_config(config, config_path)
    save_state(state, state_path)
    save_registry(registry, registry_path)


def _failure(message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "message": message,
        "downloads_performed": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }
