"""Shared safe adapter behavior."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ..healthcheck import local_only_healthcheck
from ..staging import resolve_staging_root, safe_profile_dir, safe_systemd_path
from ..systemd import render_unit_file
from .base import AdapterContext, AdapterMetadata


class DryRunAdapter:
    ADAPTER_METADATA: AdapterMetadata

    def metadata(self) -> AdapterMetadata:
        return self.ADAPTER_METADATA

    def _supports_transport(self, transport: str) -> bool:
        return transport in self.metadata().all_transports()

    def _blocked_transport(self, transport: str) -> bool:
        return transport in self.metadata().experimental_transports

    def service_name(self, context: AdapterContext) -> str:
        return (
            f"pilottunnel-{context.profile.name}-{self.metadata().name}-"
            f"{context.transport}-{context.role}.service"
        )

    def precheck(self, context: AdapterContext) -> tuple[bool, str]:
        metadata = self.metadata()
        if not self._supports_transport(context.transport):
            return False, f"Transport '{context.transport}' is not supported by adapter '{metadata.name}'"
        if self._blocked_transport(context.transport):
            return False, f"Transport '{context.transport}' is blocked in v0.1 for adapter '{metadata.name}'"
        return True, "precheck passed"

    def install(self, context: AdapterContext) -> dict:
        return {
            "action": "install",
            "mode": "apply" if context.apply_changes else "dry-run",
            "plan": [f"prepare {self.metadata().name} assets for {context.role}"],
        }

    def render_config(self, context: AdapterContext) -> dict:
        staging = resolve_staging_root(context.staging_root, context.profile.name)
        config_dir = safe_profile_dir(staging, context.profile.name, self.metadata().name, context.transport, context.role)
        config_name = self.config_filename(context.role)
        config_path = config_dir / config_name
        if context.apply_changes:
            config_dir.mkdir(parents=True, exist_ok=True)
        return {
            "action": "render_config",
            "mode": "staged-apply" if context.apply_changes else "dry-run",
            "profile": context.profile.name,
            "role": context.role,
            "transport": context.transport,
            "target": f"{context.profile.target_host}:{context.profile.target_port}",
            "config_path": str(config_path),
        }

    def render_systemd_unit(self, context: AdapterContext) -> dict:
        staging = resolve_staging_root(context.staging_root, context.profile.name)
        systemd_dir = staging.systemd_root
        unit = render_unit_file(
            unit_name=self.service_name(context),
            description=f"PilotTunnel {context.profile.name} {self.metadata().name} {context.transport} {context.role}",
            command=f"/usr/bin/env echo Starting {self.metadata().name} {context.transport} {context.role}",
            output_dir=systemd_dir,
            apply_changes=context.apply_changes,
        )
        safe_systemd_path(staging, unit.unit_name)
        return {"action": "render_systemd_unit", "mode": "staged-apply" if context.apply_changes else "dry-run", "unit": asdict(unit)}

    def start(self, context: AdapterContext) -> dict:
        return {
            "action": "start",
            "mode": "staged-apply" if context.apply_changes else "dry-run",
            "service": self.service_name(context),
            "plan": [f"start {self.service_name(context)}"],
        }

    def stop(self, context: AdapterContext) -> dict:
        return {
            "action": "stop",
            "mode": "staged-apply" if context.apply_changes else "dry-run",
            "service": self.service_name(context),
            "plan": [f"stop {self.service_name(context)}"],
        }

    def cleanup_runtime(self, context: AdapterContext) -> dict:
        return {
            "action": "cleanup_runtime",
            "mode": "staged-apply" if context.apply_changes else "dry-run",
            "plan": [f"cleanup owned runtime for {self.service_name(context)}"],
        }

    def status(self, context: AdapterContext) -> dict:
        return {"status": "ready", "adapter": self.metadata().name, "profile": context.profile.name, "role": context.role}

    def healthcheck(self, context: AdapterContext) -> tuple[bool, str]:
        result = local_only_healthcheck(context.profile.name, self.metadata().name, context.transport)
        return result.ok, result.message

    def uninstall(self, context: AdapterContext) -> dict:
        return {"action": "uninstall", "mode": "staged-apply" if context.apply_changes else "dry-run"}

    def config_filename(self, role: str) -> str:
        return f"{self.metadata().name}-{role}.toml"

    def _write_config_file(self, context: AdapterContext, content: str, config_name: str) -> str:
        staging = resolve_staging_root(context.staging_root, context.profile.name)
        config_dir = safe_profile_dir(staging, context.profile.name, self.metadata().name, context.transport, context.role)
        config_path = config_dir / config_name
        if context.apply_changes:
            config_dir.mkdir(parents=True, exist_ok=True)
            config_path.write_text(content, encoding="utf-8")
        return str(config_path)
