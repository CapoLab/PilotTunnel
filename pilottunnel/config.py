"""Configuration models and persistence helpers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

DEFAULT_CONFIG_PATH = Path("/etc/pilottunnel/config.json")
DEFAULT_STATE_PATH = Path("/var/lib/pilottunnel/state.json")
DEFAULT_REGISTRY_PATH = Path("/var/lib/pilottunnel/registry.json")
DEFAULT_AUDIT_PATH = Path("/var/log/pilottunnel/audit.log")
DEFAULT_PROBE_PORT = 27777
DEFAULT_AUX_TEST_PORT = 27778
DEFAULT_RESERVED_TEST_RANGE = range(27777, 27787)

Role = Literal["controller", "worker"]
RuntimeRole = Literal["active", "hot_standby", "config_only", ""]

SUPPORTED_LAYERS = {
    "layer3": False,
    "layer4": True,
    "layer5_6": False,
    "layer7": False,
    "xray_based": False,
    "experimental": False,
}

@dataclass
class NodeSettings:
    node_id: str = ""
    node_role: str = ""
    initialized_at: str = ""
    role_alias_used: str = ""
    normalized_role: str = ""
    side_label: str = ""
    preferred_layer: str = ""
    preferred_layer_selected_at: str = ""
    display_name: str = ""
    install_root: str = ""
    state_directory: str = ""
    work_directory: str = ""
    endpoint_address: str = ""
    notes: str = ""
    active_link_label: str = ""
    managed_remote_endpoints: list[dict[str, Any]] = field(default_factory=list)

    @property
    def initialized(self) -> bool:
        return bool(self.normalized_role)


@dataclass
class Candidate:
    adapter: str
    transport: str
    notes: str = ""


@dataclass
class LinkCandidate:
    adapter: str
    transport: str
    layer: str = "layer4"
    state: str = "config_only"
    selected: bool = False
    first_start_side: str = ""
    runnable: bool = False
    local_role: str = ""
    category: str = ""
    controller_service_name: str = ""
    worker_service_name: str = ""
    controller_config_path: str = ""
    worker_config_path: str = ""
    controller_runtime_config_path: str = ""
    worker_runtime_config_path: str = ""
    controller_service_dir: str = ""
    worker_service_dir: str = ""
    controller_runtime_dir: str = ""
    worker_runtime_dir: str = ""
    controller_unit_path: str = ""
    worker_unit_path: str = ""
    controller_executable: str = ""
    worker_executable: str = ""
    controller_owned_ports: list[int] = field(default_factory=list)
    worker_owned_ports: list[int] = field(default_factory=list)
    controller_command_summary: list[str] = field(default_factory=list)
    worker_command_summary: list[str] = field(default_factory=list)
    controller_environment_summary: dict[str, Any] = field(default_factory=dict)
    worker_environment_summary: dict[str, Any] = field(default_factory=dict)
    healthchecks: list[dict[str, Any]] = field(default_factory=list)
    topology: dict[str, Any] = field(default_factory=dict)
    probe: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    last_result: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
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
class LinkProfile:
    id: str
    label: str
    iran_address: str
    tunnel_port: int
    config_port: int
    iran_main_port: int | None = None
    probe_port: int = DEFAULT_PROBE_PORT
    aux_test_port: int = DEFAULT_AUX_TEST_PORT
    reserved_test_range: list[int] = field(default_factory=lambda: list(DEFAULT_RESERVED_TEST_RANGE))
    kharej_address: str = ""
    status: str = "configured"
    pairing_state: str = ""
    pairing_secret: str = ""
    pairing_version: str = ""
    pairing_issued_at: str = ""
    pairing_checksum: str = ""
    detected_controller_address: str = ""
    detected_worker_address: str = ""
    candidates: list[LinkCandidate] = field(default_factory=list)

    @property
    def controller_address(self) -> str:
        return self.iran_address

    @property
    def worker_address(self) -> str:
        return self.kharej_address

    @property
    def controller_user_facing_port(self) -> int | None:
        return self.iran_main_port

    @property
    def transport_port(self) -> int:
        return self.tunnel_port

    @property
    def worker_service_port(self) -> int:
        return self.config_port

    @property
    def effective_pairing_state(self) -> str:
        if self.pairing_state:
            return self.pairing_state
        if self.pairing_secret and self.detected_worker_address:
            return "paired"
        if self.pairing_secret:
            return "awaiting_worker_import"
        if self.kharej_address and self.iran_main_port is not None:
            return "legacy_manual_controller"
        if self.iran_address:
            return "legacy_manual_worker"
        return "unconfigured"


@dataclass
class BinaryResolutionSettings:
    managed_install_dir: str = ""
    provider_manifest: str = ""
    provider_allow_host: str = ""
    allow_system_path: bool = False
    prefer_managed_install: bool = True


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
    runtime_role: str = ""
    candidates: list[Candidate] = field(default_factory=list)
    ports: ProfilePorts | None = None
    safety: ProfileSafety = field(default_factory=ProfileSafety)

    def __post_init__(self) -> None:
        self.role = canonical_role(self.role)
        self.runtime_role = canonical_runtime_role(self.runtime_role)
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
    binary_resolution: BinaryResolutionSettings = field(default_factory=BinaryResolutionSettings)
    node: NodeSettings = field(default_factory=NodeSettings)
    links: list[LinkProfile] = field(default_factory=list)
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
    if normalized not in {"controller", "worker"}:
        raise ValueError(f"Unsupported role '{value}'")
    return normalized


def canonical_layer(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in SUPPORTED_LAYERS:
        raise ValueError(f"Unknown layer '{value}'")
    return normalized


def canonical_runtime_role(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"", "active", "hot_standby", "config_only"}:
        raise ValueError(f"Unsupported runtime role '{value}'")
    return normalized


def side_label_for_role(role: str) -> str:
    normalized = canonical_role(role)
    return "Iran side" if normalized == "controller" else "Kharej side"


def build_node_settings(role_value: str, existing_node_id: str = "", *, existing_node: NodeSettings | None = None) -> NodeSettings:
    normalized = canonical_role(role_value)
    template = existing_node or NodeSettings()
    return NodeSettings(
        node_id=existing_node_id or f"node-{uuid4().hex[:12]}",
        node_role=normalized,
        initialized_at=datetime.now(timezone.utc).isoformat(),
        role_alias_used=normalized,
        normalized_role=normalized,
        side_label=side_label_for_role(normalized),
        preferred_layer=template.preferred_layer,
        preferred_layer_selected_at=template.preferred_layer_selected_at,
        display_name=template.display_name,
        install_root=template.install_root,
        state_directory=template.state_directory,
        work_directory=template.work_directory,
        endpoint_address=template.endpoint_address,
        notes=template.notes,
        active_link_label=template.active_link_label,
        managed_remote_endpoints=list(template.managed_remote_endpoints),
    )


def validate_profile_name(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("Profile name cannot be empty")
    blocked = {".", ".."}
    if candidate in blocked or "/" in candidate or "\\" in candidate or ".." in candidate:
        raise ValueError(f"Path traversal blocked for profile name: {value!r}")
    return candidate


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
        runtime_role=data.get("runtime_role", ""),
        candidates=candidates,
        ports=ProfilePorts(**ports_data),
        safety=ProfileSafety(
            cooldown_seconds=safety_data.get("cooldown_seconds", 30),
            rollback_on_failure=safety_data.get("rollback_on_failure", True),
            dry_run_default=safety_data.get("dry_run_default", True),
        ),
    )


def _link_from_dict(data: dict[str, Any]) -> LinkProfile:
    controller_address = data.get("iran_address", data.get("controller_address", ""))
    worker_address = data.get("kharej_address", data.get("worker_address", ""))
    user_facing_port = data.get("iran_main_port", data.get("controller_user_facing_port"))
    transport_port = data.get("tunnel_port", data.get("transport_port"))
    service_port = data.get("config_port", data.get("service_port"))
    probe_port = data.get("probe_port", DEFAULT_PROBE_PORT)
    aux_test_port = data.get("aux_test_port", DEFAULT_AUX_TEST_PORT)
    reserved_test_range = data.get("reserved_test_range", list(DEFAULT_RESERVED_TEST_RANGE))
    if not isinstance(reserved_test_range, list) or not reserved_test_range:
        reserved_test_range = list(DEFAULT_RESERVED_TEST_RANGE)
    reserved_test_range = [int(item) for item in reserved_test_range]
    return LinkProfile(
        id=data.get("id") or data["label"],
        label=data["label"],
        iran_address=controller_address,
        iran_main_port=user_facing_port,
        tunnel_port=transport_port,
        config_port=service_port,
        probe_port=int(probe_port),
        aux_test_port=int(aux_test_port),
        reserved_test_range=reserved_test_range,
        kharej_address=worker_address,
        status=data.get("status", "configured"),
        pairing_state=data.get("pairing_state", ""),
        pairing_secret=data.get("pairing_secret", ""),
        pairing_version=data.get("pairing_version", ""),
        pairing_issued_at=data.get("pairing_issued_at", data.get("issued_at", "")),
        pairing_checksum=data.get("pairing_checksum", data.get("checksum", "")),
        detected_controller_address=data.get("detected_controller_address", controller_address),
        detected_worker_address=data.get("detected_worker_address", ""),
        candidates=[LinkCandidate(**item) for item in data.get("candidates", [])],
    )


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    if not path.exists():
        return AppConfig()
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = [_profile_from_dict(item) for item in data.get("profiles", [])]
    links = [_link_from_dict(item) for item in data.get("links", [])]
    return AppConfig(
        controller_role=data.get("controller_role", "controller"),
        worker_role=data.get("worker_role", "worker"),
        pre_armed_configs=data.get("pre_armed_configs", False),
        partition_mode=data.get("partition_mode", False),
        binary_resolution=BinaryResolutionSettings(**(data.get("binary_resolution") or {})),
        node=NodeSettings(**(data.get("node") or {})),
        links=links,
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
