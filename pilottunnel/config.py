"""Configuration models and persistence helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

DEFAULT_CONFIG_PATH = Path("/etc/pilottunnel/config.json")
DEFAULT_STATE_PATH = Path("/var/lib/pilottunnel/state.json")
DEFAULT_REGISTRY_PATH = Path("/var/lib/pilottunnel/registry.json")
DEFAULT_AUDIT_PATH = Path("/var/log/pilottunnel/audit.log")

Role = Literal["controller", "iran", "worker", "foreign"]

SUPPORTED_LAYERS = {
    "layer3": False,
    "layer4": True,
    "layer5_6": False,
    "layer7": False,
    "xray_based": False,
    "experimental": False,
}

ROLE_ALIASES = {
    "controller": "controller",
    "iran": "controller",
    "worker": "worker",
    "foreign": "worker",
}


@dataclass
class Candidate:
    adapter: str
    transport: str
    notes: str = ""


@dataclass
class ProfilePorts:
    main_port: int
    control_port: int | None = None
    service_port: int | None = None
    check_port: int | None = None

    def owned_ports(self) -> list[int]:
        values = [self.main_port, self.control_port, self.service_port, self.check_port]
        return [value for value in values if value is not None]


@dataclass
class ProfileSafety:
    cooldown_seconds: int = 30
    rollback_on_failure: bool = True
    dry_run_default: bool = True


@dataclass
class Profile:
    name: str
    main_port: int
    target_host: str
    target_port: int
    role: str = "controller"
    active_layer: str = "layer4"
    active_adapter: str = ""
    active_transport: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    ports: ProfilePorts | None = None
    safety: ProfileSafety = field(default_factory=ProfileSafety)

    def __post_init__(self) -> None:
        self.role = canonical_role(self.role)
        if self.ports is None:
            self.ports = ProfilePorts(main_port=self.main_port)
        self.main_port = self.ports.main_port

    @property
    def cooldown_seconds(self) -> int:
        return self.safety.cooldown_seconds


@dataclass
class AppConfig:
    controller_role: str = "controller"
    worker_role: str = "worker"
    pre_armed_configs: bool = False
    partition_mode: bool = False
    profiles: list[Profile] = field(default_factory=list)


@dataclass
class RemoteWorkerStub:
    profile: str
    role: str
    mode: str = "local-only"
    reachable: bool = False
    notes: str = "Remote coordination is stubbed in v0.1"


def canonical_role(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in ROLE_ALIASES:
        raise ValueError(f"Unsupported role '{value}'")
    return ROLE_ALIASES[normalized]


def build_worker_stub(profile: Profile) -> RemoteWorkerStub:
    return RemoteWorkerStub(profile=profile.name, role=profile.role)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _profile_from_dict(data: dict[str, Any]) -> Profile:
    candidates = [Candidate(**item) for item in data.get("candidates", [])]
    ports_data = data.get("ports") or {"main_port": data["main_port"]}
    safety_data = data.get("safety") or {"cooldown_seconds": data.get("cooldown_seconds", 30)}
    return Profile(
        name=data["name"],
        main_port=data["main_port"],
        target_host=data["target_host"],
        target_port=data["target_port"],
        role=data.get("role", "controller"),
        active_layer=data.get("active_layer", "layer4"),
        active_adapter=data.get("active_adapter", ""),
        active_transport=data.get("active_transport", ""),
        candidates=candidates,
        ports=ProfilePorts(**ports_data),
        safety=ProfileSafety(
            cooldown_seconds=safety_data.get("cooldown_seconds", 30),
            rollback_on_failure=safety_data.get("rollback_on_failure", True),
            dry_run_default=safety_data.get("dry_run_default", True),
        ),
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
