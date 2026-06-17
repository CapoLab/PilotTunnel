"""Public port ownership registry."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import DEFAULT_REGISTRY_PATH


@dataclass
class RegistryEntry:
    profile: str
    main_port: int
    adapter: str
    transport: str
    role: str
    owned_ports: list[int] = field(default_factory=list)
    owned_services: list[str] = field(default_factory=list)
    owned_firewall_rule_tags: list[str] = field(default_factory=list)
    owned_routes: list[str] = field(default_factory=list)


@dataclass
class PortRegistry:
    owners: dict[str, RegistryEntry] = field(default_factory=dict)

    def claim(self, entry: RegistryEntry) -> None:
        self.validate_entry(entry)
        key = entry.profile
        for other_profile, owner in self.owners.items():
            if other_profile == key:
                continue
            if owner.main_port == entry.main_port:
                raise ValueError(f"Main port {entry.main_port} already owned by profile '{owner.profile}'")
            conflict_ports = sorted(set(owner.owned_ports) & set(entry.owned_ports))
            if conflict_ports:
                raise ValueError(
                    f"Profile '{entry.profile}' conflicts with '{owner.profile}' on owned ports {conflict_ports}"
                )
        self.owners[key] = entry

    def release(self, profile: str) -> None:
        self.owners.pop(profile, None)

    def validate_entry(self, entry: RegistryEntry) -> None:
        if entry.transport not in {"tcp", "tcpmux", "udp", "ws", "wsmux", "wss", "wssmux"}:
            raise ValueError(f"Unsupported transport selected: {entry.transport}")
        if entry.profile in self.owners and self.owners[entry.profile].main_port != entry.main_port:
            raise ValueError(f"Duplicate active owner for same profile '{entry.profile}'")

    def check_conflicts(self) -> list[str]:
        conflicts: list[str] = []
        seen_main_ports: dict[int, str] = {}
        for profile, entry in self.owners.items():
            current = seen_main_ports.get(entry.main_port)
            if current and current != profile:
                conflicts.append(f"Main port {entry.main_port} is owned by both '{current}' and '{profile}'")
            seen_main_ports[entry.main_port] = profile
            if len(entry.owned_ports) != len(set(entry.owned_ports)):
                conflicts.append(f"Profile '{profile}' has duplicate owned port declarations")
            try:
                self.validate_entry(entry)
            except ValueError as exc:
                conflicts.append(str(exc))
        profiles = list(self.owners.items())
        for index, (profile, entry) in enumerate(profiles):
            for other_profile, other_entry in profiles[index + 1 :]:
                overlap = sorted(set(entry.owned_ports) & set(other_entry.owned_ports))
                if overlap:
                    conflicts.append(
                        f"Profiles '{profile}' and '{other_profile}' conflict on owned ports {overlap}"
                    )
        return conflicts


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> PortRegistry:
    if not path.exists():
        return PortRegistry()
    data = json.loads(path.read_text(encoding="utf-8"))
    owners = {profile: RegistryEntry(**entry) for profile, entry in data.get("owners", {}).items()}
    return PortRegistry(owners=owners)


def save_registry(registry: PortRegistry, path: Path = DEFAULT_REGISTRY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(registry), indent=2, sort_keys=True), encoding="utf-8")
