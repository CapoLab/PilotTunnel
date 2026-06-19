"""Runtime state persistence."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import DEFAULT_STATE_PATH


@dataclass
class RuntimeRecord:
    profile: str
    active_adapter: str = ""
    active_transport: str = ""
    active_layer: str = "layer4"
    service_name: str = ""
    role: str = "controller"
    healthy: bool = False
    last_error: str = ""
    last_switch_at: str = ""
    rollback_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass
class BinaryRecord:
    adapter: str
    source_filename: str
    imported_path: str
    sha256: str
    version: str
    imported_at: str
    executable: bool
    platform: str
    source_type: str = "user_supplied"
    source_provider: str = ""
    provider_host: str = ""
    downloaded_at: str = ""
    run_version_result: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppState:
    profiles: dict[str, RuntimeRecord] = field(default_factory=dict)
    binaries: dict[str, BinaryRecord] = field(default_factory=dict)
    manual_active_tunnel: str = ""
    manual_previous_tunnel: str = ""
    last_manual_switch: dict[str, Any] = field(default_factory=dict)

    def clone(self) -> "AppState":
        return copy.deepcopy(self)


def load_state(path: Path = DEFAULT_STATE_PATH) -> AppState:
    if not path.exists():
        return AppState()
    data = json.loads(path.read_text(encoding="utf-8"))
    profiles = {name: RuntimeRecord(**payload) for name, payload in data.get("profiles", {}).items()}
    binaries = {name: BinaryRecord(**payload) for name, payload in data.get("binaries", {}).items()}
    return AppState(
        profiles=profiles,
        binaries=binaries,
        manual_active_tunnel=data.get("manual_active_tunnel", ""),
        manual_previous_tunnel=data.get("manual_previous_tunnel", ""),
        last_manual_switch=data.get("last_manual_switch") or {},
    )


def save_state(state: AppState, path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True), encoding="utf-8")
