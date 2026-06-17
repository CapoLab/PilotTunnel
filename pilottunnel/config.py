"""Configuration models and persistence helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("/etc/pilottunnel/config.json")
DEFAULT_STATE_PATH = Path("/var/lib/pilottunnel/state.json")
DEFAULT_REGISTRY_PATH = Path("/var/lib/pilottunnel/registry.json")
DEFAULT_AUDIT_PATH = Path("/var/log/pilottunnel/audit.log")

SUPPORTED_LAYERS = {
    "layer3": False,
    "layer4": True,
    "layer5_6": False,
    "layer7": False,
    "xray_based": False,
    "experimental": False,
}


@dataclass
class Candidate:
    adapter: str
    transport: str
    notes: str = ""


@dataclass
class Profile:
    name: str
    main_port: int
    target_host: str
    target_port: int
    active_layer: str = "layer4"
    active_adapter: str = ""
    active_transport: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    cooldown_seconds: int = 30


@dataclass
class AppConfig:
    controller_role: str = "controller"
    worker_role: str = "worker"
    pre_armed_configs: bool = False
    partition_mode: bool = False
    profiles: list[Profile] = field(default_factory=list)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _profile_from_dict(data: dict[str, Any]) -> Profile:
    candidates = [Candidate(**item) for item in data.get("candidates", [])]
    return Profile(
        name=data["name"],
        main_port=data["main_port"],
        target_host=data["target_host"],
        target_port=data["target_port"],
        active_layer=data.get("active_layer", "layer4"),
        active_adapter=data.get("active_adapter", ""),
        active_transport=data.get("active_transport", ""),
        candidates=candidates,
        cooldown_seconds=data.get("cooldown_seconds", 30),
    )


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not path.exists():
        return AppConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = [_profile_from_dict(item) for item in data.get("profiles", [])]
    return AppConfig(
        controller_role=data.get("controller_role", "controller"),
        worker_role=data.get("worker_role", "worker"),
        pre_armed_configs=data.get("pre_armed_configs", False),
        partition_mode=data.get("partition_mode", False),
        profiles=profiles,
    )


def save_config(config: AppConfig, path: Path = DEFAULT_CONFIG_PATH) -> None:
    _ensure_parent(path)
    path.write_text(json.dumps(asdict(config), indent=2, sort_keys=True), encoding="utf-8")


def get_profile(config: AppConfig, name: str) -> Profile:
    for profile in config.profiles:
        if profile.name == name:
            return profile
    raise KeyError(f"Profile '{name}' not found")
