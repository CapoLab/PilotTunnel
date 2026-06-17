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

    def precheck(self, context: AdapterContext) -> tuple[bool, str]:
        metadata = self.metadata()
        if context.transport not in metadata.transports:
            return False, f"Transport '{context.transport}' is not supported by adapter '{metadata.name}'"
        return True, "precheck passed"

    def install(self, context: AdapterContext) -> dict:
        return {"action": "install", "mode": "dry-run", "adapter": self.metadata().name}

    def render_config(self, context: AdapterContext) -> dict:
        unit = render_unit_file(
            profile=context.profile_name,
            adapter=self.metadata().name,
            command=(
                f"/usr/bin/env echo Starting {self.metadata().name} "
                f"on {context.main_port} -> {context.target_host}:{context.target_port}"
            ),
            output_dir=context.work_dir,
            apply_changes=context.apply_changes,
        )
        return {"action": "render_config", "unit": asdict(unit), "mode": "apply" if context.apply_changes else "dry-run"}

    def start(self, context: AdapterContext) -> dict:
        return {"action": "start", "mode": "apply" if context.apply_changes else "dry-run"}

    def stop(self, context: AdapterContext) -> dict:
        return {"action": "stop", "mode": "apply" if context.apply_changes else "dry-run"}

    def cleanup_runtime(self, context: AdapterContext) -> dict:
        return {"action": "cleanup_runtime", "mode": "apply" if context.apply_changes else "dry-run"}

    def status(self, context: AdapterContext) -> dict:
        return {"status": "ready", "adapter": self.metadata().name, "profile": context.profile_name}

    def healthcheck(self, context: AdapterContext) -> tuple[bool, str]:
        result = local_only_healthcheck(context.profile_name, self.metadata().name, context.transport)
        return result.ok, result.message

    def uninstall(self, context: AdapterContext) -> dict:
        return {"action": "uninstall", "mode": "apply" if context.apply_changes else "dry-run"}
