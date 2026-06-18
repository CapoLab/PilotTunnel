"""Backup and restore safety layer for PilotTunnel-owned files."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import ADAPTERS
from .audit import write_audit_log
from .config import AppConfig, Profile, canonical_role, get_profile, validate_profile_name
from .install_plan import REAL_HOST_ROOT
from .state import AppState
from .switch_engine import SwitchPaths


def build_backup_plan(
    *,
    config: AppConfig,
    state: AppState,
    switch_paths: SwitchPaths,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
    audit_path: Path,
    profile_name: str | None,
    adapter_name: str | None,
    transport: str | None,
    install_root: Path | None,
    backup_root: Path | None,
) -> dict[str, Any]:
    profile = _resolve_profile(config, profile_name)
    resolved_role = config.node.normalized_role or (profile.role if profile else "")
    _validate_adapter_transport(adapter_name, transport)
    source_root = _source_root(install_root)
    destination_root = _backup_root(backup_root, switch_paths)
    files, warnings = _collect_sources(
        source_root=source_root,
        destination_root=destination_root,
        config_path=config_path,
        state_path=state_path,
        registry_path=registry_path,
        audit_path=audit_path,
    )
    payload = {
        "ok": True,
        "action": "backup-plan",
        "profile": profile.name if profile else profile_name,
        "adapter": adapter_name,
        "transport": transport,
        "node_role": resolved_role,
        "source_root": str(source_root) if source_root else "",
        "backup_root": str(destination_root),
        "files": [entry for entry in files if entry["exists"]],
        "missing": [entry for entry in files if not entry["exists"]],
        "warnings": warnings,
        "plan_only": True,
        "files_restored": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
    }
    _audit("backup-plan", profile.name if profile else "backup", payload, path=switch_paths.audit_path)
    return payload


def create_backup(
    *,
    config: AppConfig,
    switch_paths: SwitchPaths,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
    audit_path: Path,
    profile_name: str | None,
    adapter_name: str | None,
    transport: str | None,
    install_root: Path | None,
    backup_root: Path | None,
    confirm: str | None,
) -> dict[str, Any]:
    profile = _resolve_profile(config, profile_name)
    resolved_role = config.node.normalized_role or (profile.role if profile else "")
    _validate_adapter_transport(adapter_name, transport)
    destination_root = _backup_root(backup_root, switch_paths)
    attempt = {
        "profile": profile.name if profile else profile_name,
        "adapter": adapter_name,
        "transport": transport,
        "install_root": str(install_root.resolve()) if install_root else "",
        "backup_root": str(destination_root),
        "confirm": confirm or "",
    }
    if confirm != "BACKUP_CREATE":
        payload = {
            "ok": False,
            "message": "Refusing backup create without --confirm BACKUP_CREATE",
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("backup-create", profile.name if profile else "backup", payload, path=switch_paths.audit_path)
        return payload

    source_root = _source_root(install_root)
    files, warnings = _collect_sources(
        source_root=source_root,
        destination_root=destination_root,
        config_path=config_path,
        state_path=state_path,
        registry_path=registry_path,
        audit_path=audit_path,
    )
    existing = [entry for entry in files if entry["exists"]]
    if not existing:
        payload = {
            "ok": False,
            "message": "No PilotTunnel-owned files were found to back up",
            "warnings": warnings,
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("backup-create", profile.name if profile else "backup", payload, path=switch_paths.audit_path)
        return payload

    backup_id = _backup_id("backup")
    backup_dir = _validated_backup_destination(destination_root, backup_id)
    files_dir = backup_dir / "files"
    checksums: dict[str, str] = {}
    stored_entries: list[dict[str, Any]] = []
    files_dir.mkdir(parents=True, exist_ok=True)
    for entry in existing:
        stored_rel = Path(entry["stored_path"])
        target = _ensure_under_root(backup_dir, backup_dir / stored_rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(entry["source_path"]), target, follow_symlinks=False)
        sha256 = _sha256_file(target)
        size = target.stat().st_size
        checksums[str(stored_rel)] = sha256
        stored_entries.append(
            {
                **entry,
                "sha256": sha256,
                "size": size,
                "stored_path": str(stored_rel),
                "mode": _mode_string(target),
            }
        )

    manifest = {
        "backup_id": backup_id,
        "created_at": _timestamp(),
        "profile": profile.name if profile else profile_name,
        "adapter": adapter_name,
        "transport": transport,
        "node_role": resolved_role,
        "platform": platform.system(),
        "source_root": str(source_root) if source_root else "",
        "backup_root": str(destination_root),
        "source_paths": [entry["source_path"] for entry in stored_entries],
        "stored_files": stored_entries,
        "skipped": [entry for entry in files if not entry["exists"]],
        "warnings": warnings,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
    }
    manifest_path = _ensure_under_root(backup_dir, backup_dir / "backup-manifest.json")
    checksums_path = _ensure_under_root(backup_dir, backup_dir / "checksums.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    checksums_path.write_text(json.dumps(checksums, indent=2, sort_keys=True), encoding="utf-8")
    payload = {
        "ok": True,
        "action": "backup-create",
        "backup_id": backup_id,
        "backup_path": str(backup_dir),
        "manifest_path": str(manifest_path),
        "checksums_path": str(checksums_path),
        "profile": profile.name if profile else profile_name,
        "adapter": adapter_name,
        "transport": transport,
        "stored_files_count": len(stored_entries),
        "warnings": warnings,
        "skipped": manifest["skipped"],
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
    }
    _audit("backup-create", profile.name if profile else "backup", payload, path=switch_paths.audit_path)
    return payload


def list_backups(*, switch_paths: SwitchPaths, backup_root: Path | None) -> dict[str, Any]:
    root = _backup_root(backup_root, switch_paths)
    backups = []
    if root.exists():
        for entry in sorted(root.iterdir()):
            manifest_path = entry / "backup-manifest.json"
            if entry.is_dir() and manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                backups.append(
                    {
                        "backup_id": manifest.get("backup_id", entry.name),
                        "created_at": manifest.get("created_at", ""),
                        "profile": manifest.get("profile", ""),
                        "adapter": manifest.get("adapter", ""),
                        "transport": manifest.get("transport", ""),
                        "path": str(entry),
                    }
                )
    return {"ok": True, "action": "backup-list", "backup_root": str(root), "backups": backups}


def inspect_backup(*, switch_paths: SwitchPaths, backup_root: Path | None, backup_id: str) -> dict[str, Any]:
    backup_dir = _backup_dir(_backup_root(backup_root, switch_paths), backup_id)
    manifest = _load_manifest(backup_dir)
    return {"ok": True, "action": "backup-inspect", "backup_id": backup_id, "backup_path": str(backup_dir), "manifest": manifest}


def verify_backup(*, switch_paths: SwitchPaths, backup_root: Path | None, backup_id: str) -> dict[str, Any]:
    backup_dir = _backup_dir(_backup_root(backup_root, switch_paths), backup_id)
    manifest = _load_manifest(backup_dir)
    checksums = _load_checksums(backup_dir)
    missing: list[str] = []
    mismatched: list[str] = []
    for entry in manifest.get("stored_files", []):
        stored_rel = Path(entry["stored_path"])
        stored_file = _ensure_under_root(backup_dir, backup_dir / stored_rel)
        if not stored_file.exists():
            missing.append(str(stored_rel))
            continue
        actual = _sha256_file(stored_file)
        expected = checksums.get(str(stored_rel)) or entry.get("sha256", "")
        if actual != expected:
            mismatched.append(str(stored_rel))
    ok = not missing and not mismatched
    return {
        "ok": ok,
        "action": "backup-verify",
        "backup_id": backup_id,
        "backup_path": str(backup_dir),
        "missing_files": missing,
        "checksum_mismatches": mismatched,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
    }


def build_restore_plan(
    *,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
    audit_path: Path,
    switch_paths: SwitchPaths,
    backup_root: Path | None,
    backup_id: str,
    install_root: Path | None,
) -> dict[str, Any]:
    backup_dir = _backup_dir(_backup_root(backup_root, switch_paths), backup_id)
    manifest = _load_manifest(backup_dir)
    files = _restore_entries(
        manifest=manifest,
        install_root=install_root,
        config_path=config_path,
        state_path=state_path,
        registry_path=registry_path,
        audit_path=audit_path,
    )
    overwrite = [entry for entry in files if Path(entry["target_path"]).exists()]
    payload = {
        "ok": True,
        "action": "restore-plan",
        "backup_id": backup_id,
        "backup_path": str(backup_dir),
        "files": files,
        "overwrite": overwrite,
        "safety_warnings": [
            "Restore will only write files listed in the backup manifest.",
            "Restore requires checksum verification and exact --confirm RESTORE_APPLY.",
            "A pre-restore safety backup will be created before overwriting current files.",
        ],
        "required_confirmation": "RESTORE_APPLY",
        "restore_actions": [f"restore {entry['stored_path']} -> {entry['target_path']}" for entry in files],
        "plan_only": True,
        "files_restored": False,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
    }
    _audit("restore-plan", manifest.get("profile") or "restore", payload, path=switch_paths.audit_path)
    return payload


def apply_restore(
    *,
    config: AppConfig,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
    audit_path: Path,
    switch_paths: SwitchPaths,
    backup_root: Path | None,
    backup_id: str,
    install_root: Path | None,
    confirm: str | None,
) -> dict[str, Any]:
    destination_root = _backup_root(backup_root, switch_paths)
    backup_dir = _backup_dir(destination_root, backup_id)
    attempt = {
        "backup_id": backup_id,
        "backup_root": str(destination_root),
        "install_root": str(install_root.resolve()) if install_root else "",
        "confirm": confirm or "",
    }
    if confirm != "RESTORE_APPLY":
        payload = {
            "ok": False,
            "message": "Refusing restore apply without --confirm RESTORE_APPLY",
            "files_restored": False,
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("restore-apply", "restore", payload, path=switch_paths.audit_path)
        return payload

    verification = verify_backup(switch_paths=switch_paths, backup_root=backup_root, backup_id=backup_id)
    if not verification["ok"]:
        payload = {
            "ok": False,
            "message": "Backup verification failed before restore apply",
            "verification": verification,
            "files_restored": False,
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("restore-apply", "restore", payload, path=switch_paths.audit_path)
        return payload

    manifest = _load_manifest(backup_dir)
    files = _restore_entries(
        manifest=manifest,
        install_root=install_root,
        config_path=config_path,
        state_path=state_path,
        registry_path=registry_path,
        audit_path=audit_path,
    )
    existing_targets = [Path(entry["target_path"]) for entry in files if Path(entry["target_path"]).exists()]
    pre_restore = _create_pre_restore_backup(
        existing_targets=existing_targets,
        destination_root=destination_root,
        install_root=install_root,
        config=config,
        config_path=config_path,
        state_path=state_path,
        registry_path=registry_path,
        audit_path=audit_path,
        manifest=manifest,
    )

    restored: list[str] = []
    created: list[str] = []
    pre_existing = {str(path.resolve()) for path in existing_targets}
    try:
        for entry in files:
            target = Path(entry["target_path"])
            source = _ensure_under_root(backup_dir, backup_dir / entry["stored_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target, follow_symlinks=False)
            if target.resolve() == audit_path.resolve():
                _merge_restored_audit_log(target=target, current_lines=pre_restore.get("audit_log_lines", []))
            restored.append(str(target))
            if str(target.resolve()) not in pre_existing:
                created.append(str(target))
    except OSError as exc:
        _rollback_restore(created=created, pre_restore=pre_restore, backup_root=destination_root, switch_paths=switch_paths)
        payload = {
            "ok": False,
            "message": f"Restore apply failed while writing files: {exc}",
            "pre_restore_backup_id": pre_restore["backup_id"],
            "files_restored": False,
            "real_systemd_touched": False,
            "service_started": False,
            "firewall_touched": False,
            "routes_touched": False,
            "downloads_performed": False,
            **attempt,
        }
        _audit("restore-apply", manifest.get("profile") or "restore", payload, path=switch_paths.audit_path)
        return payload

    payload = {
        "ok": True,
        "action": "restore-apply",
        "backup_id": backup_id,
        "backup_path": str(backup_dir),
        "files_restored": True,
        "restored_files": restored,
        "pre_restore_backup_id": pre_restore["backup_id"],
        "pre_restore_backup_path": pre_restore["backup_path"],
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
    }
    _audit("restore-apply", manifest.get("profile") or "restore", payload, path=switch_paths.audit_path)
    return payload


def _resolve_profile(config: AppConfig, profile_name: str | None) -> Profile | None:
    if not profile_name:
        return None
    return get_profile(config, validate_profile_name(profile_name))


def _validate_adapter_transport(adapter_name: str | None, transport: str | None) -> None:
    if adapter_name is None and transport is not None:
        raise ValueError("Transport requires an adapter context")
    if adapter_name is None:
        return
    _validate_identifier(adapter_name, "adapter")
    adapter_cls = ADAPTERS.get(adapter_name)
    if adapter_cls is None:
        raise KeyError(f"Unknown adapter '{adapter_name}'")
    if transport is None:
        return
    _validate_identifier(transport, "transport")
    metadata = adapter_cls().metadata()
    if transport not in metadata.all_transports():
        raise ValueError(f"Transport '{transport}' is not supported by adapter '{adapter_name}'")
    if transport in metadata.experimental_transports:
        raise ValueError(f"Transport '{transport}' is blocked in v0.1 for adapter '{adapter_name}'")


def _backup_root(backup_root: Path | None, switch_paths: SwitchPaths) -> Path:
    root = (backup_root or (switch_paths.work_dir / ".var" / "pilottunnel" / "backups")).resolve()
    _validate_root(root, "backup-root")
    return root


def _source_root(install_root: Path | None) -> Path:
    root = (install_root or REAL_HOST_ROOT).resolve()
    if str(root) != root.anchor:
        _validate_root(root, "install-root")
    return root


def _collect_sources(
    *,
    source_root: Path,
    destination_root: Path,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
    audit_path: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    entries: list[dict[str, Any]] = []
    backup_destination_resolved = destination_root.resolve()
    rooted_directories = [
        ("install-root", source_root / "etc" / "pilottunnel"),
        ("install-root", source_root / "var" / "lib" / "pilottunnel"),
        ("install-root", source_root / "var" / "backups" / "pilottunnel"),
    ]
    for label, directory in rooted_directories:
        if directory.resolve() == backup_destination_resolved or directory.resolve() in backup_destination_resolved.parents:
            warnings.append(f"Skipping recursive backup source under backup root: {directory}")
            continue
        if not directory.exists():
            entries.append(_missing_install_entry(source_root=source_root, path=directory))
        entries.extend(_collect_directory_files(root_kind=label, source_root=source_root, directory=directory, warnings=warnings))

    systemd_dir = source_root / "etc" / "systemd" / "system"
    if systemd_dir.exists():
        for path in sorted(systemd_dir.glob("pilottunnel-*.service")):
            collected = _collect_single_file(root_kind="install-root", source_root=source_root, path=path, warnings=warnings)
            if collected:
                entries.append(collected)
    else:
        entries.append(_missing_install_entry(source_root=source_root, path=systemd_dir))
        warnings.append(f"Missing PilotTunnel systemd directory: {systemd_dir}")

    for binary_name in ("backhaul", "rathole"):
        binary_path = source_root / "usr" / "local" / "bin" / binary_name
        if binary_path.exists():
            collected = _collect_single_file(root_kind="install-root", source_root=source_root, path=binary_path, warnings=warnings)
            if collected:
                entries.append(collected)
        else:
            entries.append(_missing_install_entry(source_root=source_root, path=binary_path))
            warnings.append(f"Missing PilotTunnel-owned binary candidate: {binary_path}")

    local_files = [
        ("config", config_path),
        ("state", state_path),
        ("registry", registry_path),
        ("audit", audit_path),
    ]
    for root_kind, path in local_files:
        if path.exists():
            entries.append(
                {
                    "kind": root_kind,
                    "source_path": str(path.resolve()),
                    "target_path": str(path.resolve()),
                    "source_root": str(path.parent.resolve()),
                    "relative_restore_path": path.name,
                    "stored_path": str(Path("files") / root_kind / path.name),
                    "exists": True,
                }
            )
        else:
            entries.append(
                {
                    "kind": root_kind,
                    "source_path": str(path.resolve()),
                    "target_path": str(path.resolve()),
                    "source_root": str(path.parent.resolve()),
                    "relative_restore_path": path.name,
                    "stored_path": str(Path("files") / root_kind / path.name),
                    "exists": False,
                }
            )
            warnings.append(f"Missing local PilotTunnel file: {path}")
    return _dedupe_entries(entries), _dedupe_strings(warnings)


def _collect_directory_files(*, root_kind: str, source_root: Path, directory: Path, warnings: list[str]) -> list[dict[str, Any]]:
    if not directory.exists():
        warnings.append(f"Missing PilotTunnel directory: {directory}")
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(directory.rglob("*")):
        if path.is_dir():
            continue
        collected = _collect_single_file(root_kind=root_kind, source_root=source_root, path=path, warnings=warnings)
        if collected:
            files.append(collected)
    return files


def _collect_single_file(*, root_kind: str, source_root: Path, path: Path, warnings: list[str]) -> dict[str, Any] | None:
    resolved = path.resolve()
    if path.is_symlink():
        try:
            if resolved != source_root.resolve() and source_root.resolve() not in resolved.parents:
                warnings.append(f"Skipping symlink escape outside allowed root: {path}")
                return None
        except OSError:
            warnings.append(f"Skipping unreadable symlink: {path}")
            return None
    if resolved != source_root.resolve() and source_root.resolve() not in resolved.parents:
        warnings.append(f"Skipping file outside allowed root: {path}")
        return None
    relative = resolved.relative_to(source_root.resolve())
    return {
        "kind": root_kind,
        "source_path": str(resolved),
        "target_path": str(resolved),
        "source_root": str(source_root.resolve()),
        "relative_restore_path": relative.as_posix(),
        "stored_path": str(Path("files") / root_kind / relative),
        "exists": True,
    }


def _missing_install_entry(*, source_root: Path, path: Path) -> dict[str, Any]:
    resolved_root = source_root.resolve()
    resolved = path.resolve()
    relative = resolved.relative_to(resolved_root)
    return {
        "kind": "install-root",
        "source_path": str(resolved),
        "target_path": str(resolved),
        "source_root": str(resolved_root),
        "relative_restore_path": relative.as_posix(),
        "stored_path": str(Path("files") / "install-root" / relative),
        "exists": False,
    }


def _load_manifest(backup_dir: Path) -> dict[str, Any]:
    manifest_path = _ensure_under_root(backup_dir, backup_dir / "backup-manifest.json")
    if not manifest_path.exists():
        raise ValueError(f"Backup manifest is missing: {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def _load_checksums(backup_dir: Path) -> dict[str, str]:
    checksums_path = _ensure_under_root(backup_dir, backup_dir / "checksums.json")
    if not checksums_path.exists():
        raise ValueError(f"Checksums file is missing: {checksums_path}")
    return json.loads(checksums_path.read_text(encoding="utf-8"))


def _restore_entries(
    *,
    manifest: dict[str, Any],
    install_root: Path | None,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
    audit_path: Path,
) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    local_targets = {
        str(config_path.resolve()),
        str(state_path.resolve()),
        str(registry_path.resolve()),
        str(audit_path.resolve()),
    }
    active_install_root = _source_root(install_root)
    for entry in manifest.get("stored_files", []):
        declared_target = Path(entry["target_path"])
        source_root = entry.get("source_root", "")
        relative = entry.get("relative_restore_path", "")
        if entry["kind"] == "install-root":
            _validate_restore_target(declared_target, active_install_root, local_targets)
            mapped = _ensure_restore_target(active_install_root, Path(relative))
            target_path = str(mapped)
        else:
            resolved = declared_target.resolve()
            if str(resolved) not in local_targets:
                raise ValueError(f"Refusing restore for unknown local target: {resolved}")
            target_path = str(resolved)
        _validate_restore_target(Path(target_path), active_install_root, local_targets)
        files.append(
            {
                "kind": entry["kind"],
                "stored_path": entry["stored_path"],
                "target_path": target_path,
                "source_root": source_root,
                "relative_restore_path": relative,
            }
        )
    return files


def _create_pre_restore_backup(
    *,
    existing_targets: list[Path],
    destination_root: Path,
    install_root: Path | None,
    config: AppConfig,
    config_path: Path,
    state_path: Path,
    registry_path: Path,
    audit_path: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    backup_id = _backup_id("pre-restore")
    backup_dir = _validated_backup_destination(destination_root, backup_id)
    files_dir = backup_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    checksums: dict[str, str] = {}
    entries: list[dict[str, Any]] = []
    install_source_root = _source_root(install_root)
    local_targets = {
        config_path.resolve(): ("config", config_path.resolve().parent, config_path.name),
        state_path.resolve(): ("state", state_path.resolve().parent, state_path.name),
        registry_path.resolve(): ("registry", registry_path.resolve().parent, registry_path.name),
        audit_path.resolve(): ("audit", audit_path.resolve().parent, audit_path.name),
    }
    for target in existing_targets:
        resolved = target.resolve()
        if resolved in local_targets:
            kind, root, relative_name = local_targets[resolved]
            stored_rel = Path("files") / kind / relative_name
            relative_restore_path = relative_name
            source_root = root
        else:
            stored_rel = Path("files") / "install-root" / resolved.relative_to(install_source_root)
            relative_restore_path = resolved.relative_to(install_source_root).as_posix()
            source_root = install_source_root
            kind = "install-root"
        destination = _ensure_under_root(backup_dir, backup_dir / stored_rel)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, destination, follow_symlinks=False)
        sha256 = _sha256_file(destination)
        checksums[str(stored_rel)] = sha256
        entries.append(
            {
                "kind": kind,
                "source_path": str(resolved),
                "target_path": str(resolved),
                "source_root": str(source_root.resolve()),
                "relative_restore_path": relative_restore_path,
                "stored_path": str(stored_rel),
                "sha256": sha256,
                "size": destination.stat().st_size,
                "mode": _mode_string(destination),
            }
        )
    manifest_path = backup_dir / "backup-manifest.json"
    checksums_path = backup_dir / "checksums.json"
    backup_manifest = {
        "backup_id": backup_id,
        "created_at": _timestamp(),
        "profile": manifest.get("profile", ""),
        "adapter": manifest.get("adapter", ""),
        "transport": manifest.get("transport", ""),
        "node_role": config.node.normalized_role,
        "platform": platform.system(),
        "source_root": str(install_source_root) if install_root else "",
        "backup_root": str(destination_root),
        "source_paths": [entry["source_path"] for entry in entries],
        "stored_files": entries,
        "skipped": [],
        "warnings": [],
        "pre_restore_safety_backup": True,
        "real_systemd_touched": False,
        "service_started": False,
        "firewall_touched": False,
        "routes_touched": False,
        "downloads_performed": False,
    }
    manifest_path.write_text(json.dumps(backup_manifest, indent=2, sort_keys=True), encoding="utf-8")
    checksums_path.write_text(json.dumps(checksums, indent=2, sort_keys=True), encoding="utf-8")
    audit_log_lines: list[str] = []
    if audit_path.exists():
        audit_log_lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {"backup_id": backup_id, "backup_path": str(backup_dir), "audit_log_lines": audit_log_lines}


def _rollback_restore(*, created: list[str], pre_restore: dict[str, Any], backup_root: Path, switch_paths: SwitchPaths) -> None:
    for path_str in created:
        path = Path(path_str)
        if path.exists():
            path.unlink()
    backup_id = pre_restore["backup_id"]
    backup_dir = _backup_dir(backup_root, backup_id)
    manifest = _load_manifest(backup_dir)
    for entry in manifest.get("stored_files", []):
        target = Path(entry["target_path"])
        source = _ensure_under_root(backup_dir, backup_dir / entry["stored_path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
    _audit("restore-rollback", manifest.get("profile") or "restore", {"pre_restore_backup_id": backup_id}, path=switch_paths.audit_path)


def _merge_restored_audit_log(*, target: Path, current_lines: list[str]) -> None:
    restored_lines = [line for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]
    merged = list(restored_lines)
    for line in current_lines:
        if line not in merged:
            merged.append(line)
    text = "\n".join(merged)
    if text:
        text += "\n"
    target.write_text(text, encoding="utf-8")


def _backup_dir(root: Path, backup_id: str) -> Path:
    _validate_identifier(backup_id, "backup-id")
    backup_dir = _ensure_under_root(root, root / backup_id)
    if not backup_dir.exists():
        raise ValueError(f"Backup '{backup_id}' not found")
    return backup_dir


def _validated_backup_destination(root: Path, backup_id: str) -> Path:
    _validate_identifier(backup_id, "backup-id")
    destination = _ensure_under_root(root, root / backup_id)
    destination.mkdir(parents=True, exist_ok=False)
    return destination


def _ensure_restore_target(root: Path, relative: Path) -> Path:
    if any(part == ".." for part in relative.parts):
        raise ValueError(f"Path traversal blocked for restore target: {relative}")
    return _ensure_under_root(root, root / relative)


def _validate_restore_target(target: Path, install_root: Path, local_targets: set[str]) -> None:
    resolved = target.resolve()
    if str(resolved) in local_targets:
        return
    allowed_roots = [
        install_root / "etc" / "pilottunnel",
        install_root / "etc" / "systemd" / "system",
        install_root / "usr" / "local" / "bin",
        install_root / "var" / "lib" / "pilottunnel",
        install_root / "var" / "backups" / "pilottunnel",
    ]
    for root in allowed_roots:
        root_resolved = root.resolve()
        if resolved == root_resolved or root_resolved in resolved.parents:
            return
    raise ValueError(f"Refusing restore outside allowed PilotTunnel roots: {resolved}")


def _validate_root(root: Path, label: str) -> None:
    parts = root.parts
    if any(part == ".." for part in parts):
        raise ValueError(f"Path traversal blocked for {label}: {root}")
    if root == Path(root.anchor):
        raise ValueError(f"Refusing dangerous {label}: {root}")


def _validate_identifier(value: str, label: str) -> None:
    if not value or value in {".", ".."}:
        raise ValueError(f"Invalid {label}: {value!r}")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"Path traversal blocked for {label}: {value!r}")


def _ensure_under_root(root: Path, path: Path) -> Path:
    candidate = path.resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        raise ValueError(f"Refusing to operate outside allowed root: {candidate}")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mode_string(path: Path) -> str:
    try:
        return oct(path.stat().st_mode & 0o777)
    except OSError:
        return ""


def _backup_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dedupe_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for entry in entries:
        key = entry["source_path"]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def _dedupe_strings(items: list[str]) -> list[str]:
    deduped: list[str] = []
    for item in items:
        if item and item not in deduped:
            deduped.append(item)
    return deduped


def _audit(action: str, profile: str, details: dict[str, Any], *, path: Path) -> None:
    write_audit_log(action, profile, details, path)
