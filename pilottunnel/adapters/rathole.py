from .base import AdapterContext, AdapterMetadata
from .common import DryRunAdapter


class RatholeAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(
        name="rathole",
        layer="layer4",
        transports=("tcp",),
        notes="Dry-run Rathole planning for controller/worker roles in v0.1",
    )

    def render_config(self, context: AdapterContext) -> dict:
        config_text = "\n".join(
            [
                "[rathole]",
                f"role = {context.role}",
                f"transport = {context.transport}",
                f"service_name = {context.profile.name}",
                f"bind_port = {context.profile.ports.main_port}",
                f"target = {context.profile.target_host}:{context.profile.target_port}",
                f"control_port = {context.profile.ports.control_port or context.profile.ports.main_port}",
                f"remote_stub_mode = {context.remote_stub.get('mode', 'local-only')}",
            ]
        )
        config_path = self._write_config_file(context, config_text, self.config_filename(context.role))
        return {
            "action": "render_config",
            "mode": "staged-apply" if context.apply_changes else "dry-run",
            "service_name": self.service_name(context),
            "config_path": config_path,
            "content": config_text,
        }

    def render_runtime_plan(self, context: AdapterContext, runtime_dir, executable_path: str) -> dict:
        config_text = "\n".join(
            [
                "[rathole]",
                f"role = {context.role}",
                f"transport = {context.transport}",
                f"service_name = {context.profile.name}",
                f"bind_port = {context.profile.ports.main_port}",
                f"target = {context.profile.target_host}:{context.profile.target_port}",
                f"control_port = {context.profile.ports.control_port or context.profile.ports.main_port}",
            ]
        )
        config_path = self._write_runtime_file(context, runtime_dir, config_text, self.config_filename(context.role))
        return {
            "config_path": config_path,
            "content": config_text,
            "argv": [executable_path, config_path],
            "environment": {},
            "healthcheck_target_summary": {
                "kind": "tcp",
                "host": context.profile.target_host,
                "port": context.profile.target_port,
            },
        }
