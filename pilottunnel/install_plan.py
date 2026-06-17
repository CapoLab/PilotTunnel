"""Read-only planning plus controlled install-root apply helpers."""

from __future__ import annotations

import json
import platform
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from .adapters import ADAPTERS
from .adapters.base import AdapterContext
from .audit import write_audit_log
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
    context = _context(profile, transport, planned_role, paths)
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
    context = _context(profile, transport, planned_role, paths)
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


def apply_install(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str | None,
    paths: SwitchPaths,
    state: AppState,
    install_root: Path | None,
    confirm: str | None,
    dry_run: bool,
) -> dict:
    profile_name = profile.name
    attempt = {
        "adapter": adapter_name,
        "transport": transport,
        "install_root": str(install_root.resolve()) if install_root else None,
        "confirm": bool(confirm),
        "dry_run": dry_run,
    }
    if confirm != "APPLY":
        _audit("install-apply", profile_name, {**attempt, "result": "failed", "reason": "missing confirm APPLY"}, path=paths.audit_path)
        return _failure("install-apply", "Refusing to write files without --confirm APPLY")
    if install_root is None:
        _audit("install-apply", profile_name, {**attempt, "result": "failed", "reason": "missing install-root"}, path=paths.audit_path)
        return _failure("install-apply", "Refusing to write files without --install-root")

    try:
        plan = build_install_plan(
            profile=profile,
            adapter_name=adapter_name,
            transport=transport,
            role=role,
            paths=paths,
            state=state,
            install_root=install_root,
        )
        root = _validated_apply_root(install_root)
        source_map = {item["kind"]: item for item in plan["source_staged_files"]}
        missing = [item["path"] for item in plan["source_staged_files"] if not item["exists"]]
        if missing:
            raise ValueError("Refusing apply because staged files are missing")
        if not plan["binary"]["imported_binary_exists"] or not plan["binary"]["imported_path"]:
            raise ValueError(f"Refusing apply because imported binary is missing for adapter '{adapter_name}'")

        copied_files: list[dict[str, Any]] = []
        backups_created: list[dict[str, str]] = []
        skipped_files: list[dict[str, str]] = []
        destination_map = {item["kind"]: item["path"] for item in plan["planned_destination_files"]}
        for kind in ("config", "systemd_unit"):
            copied, backup = _copy_with_backup(
                source=Path(source_map[kind]["path"]),
                destination=Path(destination_map[kind]),
                install_root=root,
                dry_run=dry_run,
            )
            copied_files.append(copied)
            if backup:
                backups_created.append(backup)
        copied, backup = _copy_with_backup(
            source=Path(plan["binary"]["imported_path"]),
            destination=Path(destination_map["binary"]),
            install_root=root,
            dry_run=dry_run,
        )
        copied_files.append(copied)
        if backup:
            backups_created.append(backup)

        manifest = {
            "profile": profile_name,
            "adapter": adapter_name,
            "transport": transport,
            "role": plan["role"],
            "copied_files": copied_files,
            "backups_created": backups_created,
            "skipped_files": skipped_files,
            "timestamp": _timestamp(),
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
        manifest_path = _manifest_path(root, profile_name, adapter_name, transport)
        if not dry_run:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        _audit(
            "install-apply",
            profile_name,
            {
                **attempt,
                "result": "dry-run" if dry_run else "success",
                "manifest": str(manifest_path),
                "copied_files": copied_files,
                "backups_created": backups_created,
            },
            path=paths.audit_path,
        )
        return {
            "ok": True,
            "action": "install-apply",
            "profile": profile_name,
            "adapter": adapter_name,
            "transport": transport,
            "role": plan["role"],
            "install_root": str(root),
            "copied_files": copied_files,
            "backups_created": backups_created,
            "skipped_files": skipped_files,
            "manifest_path": str(manifest_path),
            "dry_run": dry_run,
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
    except (KeyError, ValueError) as exc:
        _audit("install-apply", profile_name, {**attempt, "result": "failed", "reason": str(exc)}, path=paths.audit_path)
        return _failure("install-apply", str(exc))


def rollback_install(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    paths: SwitchPaths,
    install_root: Path | None,
    confirm: str | None,
) -> dict:
    profile_name = profile.name
    attempt = {
        "adapter": adapter_name,
        "transport": transport,
        "install_root": str(install_root.resolve()) if install_root else None,
        "confirm": bool(confirm),
    }
    if confirm != "ROLLBACK":
        _audit("install-rollback", profile_name, {**attempt, "result": "failed", "reason": "missing confirm ROLLBACK"}, path=paths.audit_path)
        return _failure("install-rollback", "Refusing rollback without --confirm ROLLBACK")
    if install_root is None:
        _audit("install-rollback", profile_name, {**attempt, "result": "failed", "reason": "missing install-root"}, path=paths.audit_path)
        return _failure("install-rollback", "Refusing rollback without --install-root")

    try:
        root = _validated_apply_root(install_root)
        manifest_path = _manifest_path(root, profile_name, adapter_name, transport)
        if not manifest_path.exists():
            raise ValueError("No apply manifest found for rollback")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        restored_files: list[dict[str, str]] = []
        removed_files: list[str] = []
        for item in manifest.get("copied_files", []):
            destination = _ensure_under_root(root, Path(item["destination"]))
            if item.get("newly_created"):
                if destination.exists():
                    destination.unlink()
                    removed_files.append(str(destination))
                continue
            backup_path = item.get("backup_path")
            if backup_path:
                backup = _ensure_under_root(root, Path(backup_path))
                if backup.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(backup, destination)
                    restored_files.append({"destination": str(destination), "restored_from": str(backup)})
        manifest_path.unlink()
        _audit(
            "install-rollback",
            profile_name,
            {**attempt, "result": "success", "restored_files": restored_files, "removed_files": removed_files},
            path=paths.audit_path,
        )
        return {
            "ok": True,
            "action": "install-rollback",
            "profile": profile_name,
            "adapter": adapter_name,
            "transport": transport,
            "install_root": str(root),
            "restored_files": restored_files,
            "removed_files": removed_files,
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
    except ValueError as exc:
        _audit("install-rollback", profile_name, {**attempt, "result": "failed", "reason": str(exc)}, path=paths.audit_path)
        return _failure("install-rollback", str(exc))


def apply_uninstall(
    *,
    profile: Profile,
    adapter_name: str,
    transport: str,
    role: str | None,
    paths: SwitchPaths,
    state: AppState,
    install_root: Path | None,
    confirm: str | None,
) -> dict:
    profile_name = profile.name
    attempt = {
        "adapter": adapter_name,
        "transport": transport,
        "install_root": str(install_root.resolve()) if install_root else None,
        "confirm": bool(confirm),
    }
    if confirm != "UNINSTALL":
        _audit("uninstall-apply", profile_name, {**attempt, "result": "failed", "reason": "missing confirm UNINSTALL"}, path=paths.audit_path)
        return _failure("uninstall-apply", "Refusing uninstall without --confirm UNINSTALL")
    if install_root is None:
        _audit("uninstall-apply", profile_name, {**attempt, "result": "failed", "reason": "missing install-root"}, path=paths.audit_path)
        return _failure("uninstall-apply", "Refusing uninstall without --install-root")

    try:
        plan = build_uninstall_plan(
            profile=profile,
            adapter_name=adapter_name,
            transport=transport,
            role=role,
            paths=paths,
            state=state,
            install_root=install_root,
        )
        root = _validated_apply_root(install_root)
        manifest_path = _manifest_path(root, profile_name, adapter_name, transport)
        if not manifest_path.exists():
            raise ValueError("No apply manifest found; refusing to remove files that are not recorded as PilotTunnel-owned")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        owned_paths = {item["destination"] for item in manifest.get("copied_files", [])}
        backups_created: list[dict[str, str]] = []
        removed_files: list[str] = []
        for target in plan["files_that_would_be_removed"]:
            if target not in owned_paths:
                continue
            destination = _ensure_under_root(root, Path(target))
            if destination.exists():
                backup_path = _ensure_under_root(root, Path(_backup_path(str(destination))))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, backup_path)
                backups_created.append({"target": str(destination), "backup": str(backup_path)})
                destination.unlink()
                removed_files.append(str(destination))
        _audit(
            "uninstall-apply",
            profile_name,
            {**attempt, "result": "success", "removed_files": removed_files, "backups_created": backups_created},
            path=paths.audit_path,
        )
        return {
            "ok": True,
            "action": "uninstall-apply",
            "profile": profile_name,
            "adapter": adapter_name,
            "transport": transport,
            "install_root": str(root),
            "removed_files": removed_files,
            "backups_created": backups_created,
            "real_systemd_touched": False,
            "service_stopped": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
    except (KeyError, ValueError) as exc:
        _audit("uninstall-apply", profile_name, {**attempt, "result": "failed", "reason": str(exc)}, path=paths.audit_path)
        return _failure("uninstall-apply", str(exc))


def _context(profile: Profile, transport: str, role: str, paths: SwitchPaths) -> AdapterContext:
    return AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=paths.work_dir / profile.name,
        staging_root=paths.staging_root,
        apply_changes=False,
        role=role,
        remote_stub=asdict(build_worker_stub(profile)),
    )


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


def _copy_with_backup(*, source: Path, destination: Path, install_root: Path, dry_run: bool) -> tuple[dict[str, Any], dict[str, str] | None]:
    src = source.resolve()
    dest = _ensure_under_root(install_root, destination)
    backup_path = _ensure_under_root(install_root, Path(_backup_path(str(dest))))
    existed = dest.exists()
    backup = None
    if existed:
        backup = {"target": str(dest), "backup": str(backup_path)}
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if existed:
            shutil.copy2(dest, backup_path)
        shutil.copy2(src, dest)
    copied = {
        "source": str(src),
        "destination": str(dest),
        "backup_path": str(backup_path) if existed else "",
        "newly_created": not existed,
    }
    return copied, backup


def _source_staged_files(*, config_path: Path, unit_path: Path) -> list[dict]:
    return [
        {"kind": "config", "path": str(config_path), "exists": config_path.exists()},
        {"kind": "systemd_unit", "path": str(unit_path), "exists": unit_path.exists()},
    ]


def _plan_warnings(*, source_files: list[dict], binary_plan: dict, preflight: dict) -> list[str]:
    warnings = list(preflight.get("warnings", []))
    if any(not item["exists"] for item in source_files):
        warnings.append("Staged files are missing; run staged apply first to materialize config and unit artifacts")
    if binary_plan["install_status"] != "imported":
        warnings.append(f"Imported binary is not available for adapter '{binary_plan['adapter']}'")
    warnings.extend(_host_warnings())
    return warnings


def _validated_apply_root(install_root: Path) -> Path:
    root = install_root.resolve()
    _guard_dangerous_root(root)
    return root


def _guard_dangerous_root(root: Path) -> None:
    if not str(root):
        raise ValueError("Invalid install-root")
    if root == Path(root.anchor):
        raise ValueError(f"Refusing dangerous install-root: {root}")


def _ensure_under_root(root: Path, path: Path) -> Path:
    candidate = path.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"Refusing to operate outside install root: {candidate}")
    return candidate


def _under_root(root: Path, relative: Path) -> Path:
    return _ensure_under_root(root, root / relative)


def _manifest_path(root: Path, profile: str, adapter: str, transport: str) -> Path:
    filename = f"{profile}-{adapter}-{transport}.json"
    return _under_root(root, Path("var") / "lib" / "pilottunnel" / "apply-manifests" / filename)


def _host_warnings() -> list[str]:
    warnings: list[str] = []
    if not platform.system().lower().startswith("linux"):
        warnings.append("Host is not Linux; future real systemd apply would require a Linux target")
    return warnings


def _backup_path(target: str) -> str:
    return f"{target}.bak.planned"


def _audit(action: str, profile: str, details: dict[str, Any], path: Path | None = None) -> None:
    if path is None:
        write_audit_log(action, profile, details)
        return
    write_audit_log(action, profile, details, path)


def _failure(action: str, message: str) -> dict:
    return {"ok": False, "action": action, "message": message}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
