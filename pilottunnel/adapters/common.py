"""Shared safe adapter behavior."""

from __future__ import annotations

from dataclasses import asdict

from ..healthcheck import local_only_healthcheck
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
        return {
            "action": "render_config",
            "mode": "apply" if context.apply_changes else "dry-run",
            "profile": context.profile.name,
            "role": context.role,
            "transport": context.transport,
            "target": f"{context.profile.target_host}:{context.profile.target_port}",
        }

    def render_systemd_unit(self, context: AdapterContext) -> dict:
        unit = render_unit_file(
            unit_name=self.service_name(context),
            description=f"PilotTunnel {context.profile.name} {self.metadata().name} {context.transport} {context.role}",
            command=f"/usr/bin/env echo Starting {self.metadata().name} {context.transport} {context.role}",
            output_dir=context.work_dir,
            apply_changes=context.apply_changes,
        )
        return {"action": "render_systemd_unit", "mode": "apply" if context.apply_changes else "dry-run", "unit": asdict(unit)}

    def start(self, context: AdapterContext) -> dict:
        return {
            "action": "start",
            "mode": "apply" if context.apply_changes else "dry-run",
            "service": self.service_name(context),
            "plan": [f"start {self.service_name(context)}"],
        }

    def stop(self, context: AdapterContext) -> dict:
        return {
            "action": "stop",
            "mode": "apply" if context.apply_changes else "dry-run",
            "service": self.service_name(context),
            "plan": [f"stop {self.service_name(context)}"],
        }

    def cleanup_runtime(self, context: AdapterContext) -> dict:
        return {
            "action": "cleanup_runtime",
            "mode": "apply" if context.apply_changes else "dry-run",
            "plan": [f"cleanup owned runtime for {self.service_name(context)}"],
        }

    def status(self, context: AdapterContext) -> dict:
        return {"status": "ready", "adapter": self.metadata().name, "profile": context.profile.name, "role": context.role}

    def healthcheck(self, context: AdapterContext) -> tuple[bool, str]:
        result = local_only_healthcheck(context.profile.name, self.metadata().name, context.transport)
        return result.ok, result.message

    def uninstall(self, context: AdapterContext) -> dict:
        return {"action": "uninstall", "mode": "apply" if context.apply_changes else "dry-run"}
