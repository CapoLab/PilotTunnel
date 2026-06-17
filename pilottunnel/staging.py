"""Safe staging helpers for generated config and unit files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StagingLayout:
    root: Path
    configs_root: Path
    systemd_root: Path


def resolve_staging_root(root: Path, profile: str) -> StagingLayout:
    _validate_component(profile, "profile")
    resolved_root = root.resolve()
    return StagingLayout(
        root=resolved_root,
        configs_root=resolved_root / "configs",
        systemd_root=resolved_root / "systemd",
    )


def safe_profile_dir(layout: StagingLayout, profile: str, adapter: str, transport: str, role: str) -> Path:
    for value, label in [
        (profile, "profile"),
        (adapter, "adapter"),
        (transport, "transport"),
        (role, "role"),
    ]:
        _validate_component(value, label)
    path = layout.configs_root / profile / adapter / transport / role
    _ensure_under_root(layout.root, path)
    return path


def safe_systemd_path(layout: StagingLayout, unit_name: str) -> Path:
    _validate_component(unit_name, "unit_name")
    path = layout.systemd_root / unit_name
    _ensure_under_root(layout.root, path)
    return path


def _validate_component(value: str, label: str) -> None:
    if not value or value in {".", ".."}:
        raise ValueError(f"Invalid {label}: {value!r}")
    if any(part in {"..", ""} for part in value.split("/")) or any(part in {"..", ""} for part in value.split("\\")):
        raise ValueError(f"Path traversal blocked for {label}: {value!r}")


def _ensure_under_root(root: Path, path: Path) -> None:
    resolved = path.resolve()
    if root not in resolved.parents and resolved != root:
        raise ValueError(f"Refusing to write outside staging root: {resolved}")
