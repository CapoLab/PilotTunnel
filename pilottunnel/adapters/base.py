"""Base adapter interfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ..config import Profile


@dataclass(frozen=True)
class AdapterMetadata:
    name: str
    layer: str
    transports: tuple[str, ...]
    experimental_transports: tuple[str, ...] = ()
    experimental: bool = False
    supported: bool = True
    notes: str = ""

    def all_transports(self) -> tuple[str, ...]:
        return self.transports + self.experimental_transports


@dataclass
class AdapterContext:
    profile: Profile
    transport: str
    work_dir: Path
    staging_root: Path
    apply_changes: bool = False
    role: str = ""
    remote_stub: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.role:
            self.role = self.profile.role


class BaseAdapter(Protocol):
    def metadata(self) -> AdapterMetadata:
        ...

    def precheck(self, context: AdapterContext) -> tuple[bool, str]:
        ...

    def install(self, context: AdapterContext) -> dict:
        ...

    def render_config(self, context: AdapterContext) -> dict:
        ...

    def render_systemd_unit(self, context: AdapterContext) -> dict:
        ...

    def render_runtime_plan(self, context: AdapterContext, runtime_dir: Path, executable_path: str) -> dict:
        ...

    def start(self, context: AdapterContext) -> dict:
        ...

    def stop(self, context: AdapterContext) -> dict:
        ...

    def cleanup_runtime(self, context: AdapterContext) -> dict:
        ...

    def status(self, context: AdapterContext) -> dict:
        ...

    def healthcheck(self, context: AdapterContext) -> tuple[bool, str]:
        ...

    def uninstall(self, context: AdapterContext) -> dict:
        ...
