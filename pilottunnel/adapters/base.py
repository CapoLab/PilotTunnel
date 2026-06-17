"""Base adapter interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AdapterMetadata:
    name: str
    layer: str
    transports: tuple[str, ...]
    experimental: bool = False
    supported: bool = True
    notes: str = ""


@dataclass
class AdapterContext:
    profile_name: str
    main_port: int
    target_host: str
    target_port: int
    transport: str
    work_dir: Path
    apply_changes: bool = False


class BaseAdapter(Protocol):
    def metadata(self) -> AdapterMetadata:
        ...

    def precheck(self, context: AdapterContext) -> tuple[bool, str]:
        ...

    def install(self, context: AdapterContext) -> dict:
        ...

    def render_config(self, context: AdapterContext) -> dict:
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
