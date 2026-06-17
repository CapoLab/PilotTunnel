"""Read-only real-host install and uninstall planning."""

from __future__ import annotations

import platform
from dataclasses import asdict
from pathlib import Path, PurePosixPath

from .adapters import ADAPTERS
from .adapters.base import AdapterContext
from .binaries import binary_filename, get_binary_plan
from .config import Profile, build_worker_stub, canonical_role, validate_profile_name
from .preflight import run_preflight
from .state import AppState
from .switch_engine import SwitchPaths


def build_install_plan(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str | None,
    paths: SwitchPaths,
    state: AppState,
    install_root: Path | None = None,
) -> dict:
    planned_role = canonical_role(role or profile.role)
    _validate_install_inputs(profile.name, adapter_name, transport, install_root)
    adapter = _adapter_for(adapter_name)
    context = AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=paths.work_dir / profile.name,
        staging_root=paths.staging_root,
        apply_changes=False,
        role=planned_role,
        remote_stub=asdict(build_worker_stub(profile)),
    )
    ok, reason = adapter.precheck(context)
    if not ok:
        raise ValueError(reason)

    rendered_config = adapter.render_config(context)
    rendered_unit = adapter.render_systemd_unit(context)
    service_name = adapter.service_name(context)
    binary_plan = get_binary_plan(adapter_name, paths.work_dir, state)
    destination = _install_destinations(
        profile_name=profile.name,
        adapter_name=adapter_name,
        transport=transport,
        role=planned_role,
        config_name=adapter.config_filename(planned_role),
        service_name=service_name,
        binary_name=binary_filename(adapter_name),
        install_root=install_root,
    )
    source_files = _source_staged_files(
        config_path=Path(rendered_config["config_path"]),
        unit_path=Path(rendered_unit["unit"]["path"]),
    )
    warnings = _plan_warnings(
        source_files=source_files,
        binary_plan=binary_plan,
        preflight=run_preflight(paths.staging_root, profile).to_dict(),
    )
    return {
        "ok": True,
        "action": "install-plan",
        "profile": profile.name,
        "role": planned_role,
        "adapter": adapter_name,
        "transport": transport,
        "source_staged_files": source_files,
        "planned_destination_files": [
            {"kind": "config", "path": destination["config_path"]},
            {"kind": "systemd_unit", "path": destination["unit_path"]},
            {"kind": "binary", "path": destination["binary_path"]},
        ],
        "planned_backups": [
            {"target": destination["config_path"], "backup": _backup_path(destination["config_path"]), "when_exists": True},
            {"target": destination["unit_path"], "backup": _backup_path(destination["unit_path"]), "when_exists": True},
            {"target": destination["binary_path"], "backup": _backup_path(destination["binary_path"]), "when_exists": True},
        ],
        "service_names": [service_name],
        "binary": {
            "adapter": adapter_name,
            "binary_name": binary_plan["binary_name"],
            "imported_binary_exists": binary_plan["install_status"] == "imported",
            "imported_path": binary_plan["imported_path"],
            "install_status": binary_plan["install_status"],
            "planned_destination_path": destination["binary_path"],
        },
        "backup_strategy": "backup existing destinations before overwrite; remove newly-created files on rollback",
        "rollback_steps": [
            f"restore backup if present: {_backup_path(destination['config_path'])} -> {destination['config_path']}",
            f"restore backup if present: {_backup_path(destination['unit_path'])} -> {destination['unit_path']}",
            f"restore backup if present: {_backup_path(destination['binary_path'])} -> {destination['binary_path']}",
            f"remove newly-created file if no prior backup existed: {destination['config_path']}",
            f"remove newly-created file if no prior backup existed: {destination['unit_path']}",
            f"remove newly-created file if no prior backup existed: {destination['binary_path']}",
            f"systemctl daemon-reload after rollback for {service_name}",
        ],
        "safety_checks": [
            "validate profile/adapter/transport/install-root path components",
            "require staged config and unit files before future copy",
            "require imported binary before future real install",
            "require explicit future confirmation before any real apply",
            "block unsupported or experimental transports",
        ],
        "required_privileges": [
            "root/admin privileges required for future real binary and config placement",
            "systemd management privileges required for future enable/start operations",
        ],
        "future_real_apply_commands": [
            f"install -D -m 0644 {rendered_config['config_path']} {destination['config_path']}",
            f"install -D -m 0644 {rendered_unit['unit']['path']} {destination['unit_path']}",
            f"install -D -m 0755 {binary_plan['expected_bin_path']} {destination['binary_path']}",
            "systemctl daemon-reload",
            f"systemctl enable {service_name}",
            f"systemctl start {service_name}",
        ],
        "warnings": warnings,
        "install_root": str(install_root.resolve()) if install_root else None,
        "real_systemd_touched": False,
        "real_firewall_touched": False,
        "service_started": False,
        "plan_only": True,
    }


def build_uninstall_plan(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str | None,
    paths: SwitchPaths,
    state: AppState,
    install_root: Path | None = None,
) -> dict:
    planned_role = canonical_role(role or profile.role)
    _validate_install_inputs(profile.name, adapter_name, transport, install_root)
    adapter = _adapter_for(adapter_name)
    context = AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=paths.work_dir / profile.name,
        staging_root=paths.staging_root,
        apply_changes=False,
        role=planned_role,
        remote_stub=asdict(build_worker_stub(profile)),
    )
    ok, reason = adapter.precheck(context)
    if not ok:
        raise ValueError(reason)
    service_name = adapter.service_name(context)
    destination = _install_destinations(
        profile_name=profile.name,
        adapter_name=adapter_name,
        transport=transport,
        role=planned_role,
        config_name=adapter.config_filename(planned_role),
        service_name=service_name,
        binary_name=binary_filename(adapter_name),
        install_root=install_root,
    )
    return {
        "ok": True,
        "action": "uninstall-plan",
        "profile": profile.name,
        "role": planned_role,
        "adapter": adapter_name,
        "transport": transport,
        "services_that_would_be_stopped_disabled": [service_name],
        "files_that_would_be_removed": [
            destination["config_path"],
            destination["unit_path"],
            destination["binary_path"],
        ],
        "planned_backups": [
            {"target": destination["config_path"], "backup": _backup_path(destination["config_path"]), "before_remove": True},
            {"target": destination["unit_path"], "backup": _backup_path(destination["unit_path"]), "before_remove": True},
            {"target": destination["binary_path"], "backup": _backup_path(destination["binary_path"]), "before_remove": True},
        ],
        "registry_state_cleanup_steps": [
            f"remove registry ownership for profile {profile.name}",
            f"clear runtime state for profile {profile.name}",
            f"remove audit references only by explicit future maintenance command for profile {profile.name}",
        ],
        "future_real_apply_commands": [
            f"systemctl stop {service_name}",
            f"systemctl disable {service_name}",
            f"rm -f {destination['unit_path']}",
            f"rm -f {destination['config_path']}",
            f"rm -f {destination['binary_path']}",
            "systemctl daemon-reload",
        ],
        "warnings": _host_warnings(),
        "install_root": str(install_root.resolve()) if install_root else None,
        "real_systemd_touched": False,
        "real_firewall_touched": False,
        "service_stopped": False,
        "plan_only": True,
    }


def _adapter_for(adapter_name: str):
    if adapter_name not in ADAPTERS:
        raise KeyError(f"Unknown adapter '{adapter_name}'")
    return ADAPTERS[adapter_name]()


def _validate_install_inputs(profile_name: str, adapter_name: str, transport: str, install_root: Path | None) -> None:
    validate_profile_name(profile_name)
    for value, label in [(adapter_name, "adapter"), (transport, "transport")]:
        if not value or value in {".", ".."}:
            raise ValueError(f"Invalid {label}: {value!r}")
        if "/" in value or "\\" in value or ".." in value:
            raise ValueError(f"Path traversal blocked for {label}: {value!r}")
    if install_root is not None:
        parts = install_root.as_posix().split("/")
        if any(part == ".." for part in parts):
            raise ValueError(f"Path traversal blocked for install-root: {install_root}")


def _install_destinations(
    *,
    profile_name: str,
    adapter_name: str,
    transport: str,
    role: str,
    config_name: str,
    service_name: str,
    binary_name: str,
    install_root: Path | None,
) -> dict[str, str]:
    if install_root is None:
        config_path = str(PurePosixPath("/etc/pilottunnel/profiles") / profile_name / adapter_name / transport / role / config_name)
        unit_path = str(PurePosixPath("/etc/systemd/system") / service_name)
        binary_path = str(PurePosixPath("/usr/local/bin") / binary_name)
        return {"config_path": config_path, "unit_path": unit_path, "binary_path": binary_path}

    root = install_root.resolve()
    config_path = _under_root(root, Path("etc") / "pilottunnel" / "profiles" / profile_name / adapter_name / transport / role / config_name)
    unit_path = _under_root(root, Path("etc") / "systemd" / "system" / service_name)
    binary_path = _under_root(root, Path("usr") / "local" / "bin" / binary_name)
    return {"config_path": str(config_path), "unit_path": str(unit_path), "binary_path": str(binary_path)}


def _under_root(root: Path, relative: Path) -> Path:
    candidate = (root / relative).resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError(f"Refusing to plan outside install root: {candidate}")
    return candidate


def _source_staged_files(*, config_path: Path, unit_path: Path) -> list[dict]:
    return [
        {"kind": "config", "path": str(config_path), "exists": config_path.exists()},
        {"kind": "systemd_unit", "path": str(unit_path), "exists": unit_path.exists()},
    ]


def _plan_warnings(*, source_files: list[dict], binary_plan: dict, preflight: dict) -> list[str]:
    warnings = list(preflight.get("warnings", []))
    missing = [item["path"] for item in source_files if not item["exists"]]
    if missing:
        warnings.append("Staged files are missing; run staged apply first to materialize config and unit artifacts")
    if binary_plan["install_status"] != "imported":
        warnings.append(f"Imported binary is not available for adapter '{binary_plan['adapter']}'")
    warnings.extend(_host_warnings())
    return warnings


def _host_warnings() -> list[str]:
    warnings: list[str] = []
    if not platform.system().lower().startswith("linux"):
        warnings.append("Host is not Linux; future real systemd apply would require a Linux target")
    return warnings


def _backup_path(target: str) -> str:
    return f"{target}.bak.planned"
