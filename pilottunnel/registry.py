"""Public port ownership registry."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import DEFAULT_REGISTRY_PATH


@dataclass
class RegistryEntry:
    profile: str
    adapter: str
    transport: str


@dataclass
class PortRegistry:
    owners: dict[str, RegistryEntry] = field(default_factory=dict)

    def claim(self, main_port: int, profile: str, adapter: str, transport: str) -> None:
        key = str(main_port)
        owner = self.owners.get(key)
        if owner and owner.profile != profile:
            raise ValueError(f"Port {main_port} already owned by profile '{owner.profile}'")
        self.owners[key] = RegistryEntry(profile=profile, adapter=adapter, transport=transport)

    def release(self, main_port: int, profile: str) -> None:
        key = str(main_port)
        owner = self.owners.get(key)
        if owner and owner.profile == profile:
            del self.owners[key]

    def check_conflicts(self) -> list[str]:
        conflicts: list[str] = []
        seen_profiles: dict[str, str] = {}
        for port, entry in self.owners.items():
            if entry.profile in seen_profiles:
                conflicts.append(
                    f"Profile '{entry.profile}' owns multiple main ports: {seen_profiles[entry.profile]} and {port}"
                )
            seen_profiles[entry.profile] = port
        return conflicts


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> PortRegistry:
    if not path.exists():
        return PortRegistry()
    data = json.loads(path.read_text(encoding="utf-8"))
    owners = {port: RegistryEntry(**entry) for port, entry in data.get("owners", {}).items()}
    return PortRegistry(owners=owners)


def save_registry(registry: PortRegistry, path: Path = DEFAULT_REGISTRY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(registry), indent=2, sort_keys=True), encoding="utf-8")
