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
        return {
            "action": "render_config",
            "mode": "apply" if context.apply_changes else "dry-run",
            "service_name": self.service_name(context),
            "config_path": str(context.work_dir / f"{self.service_name(context)}.toml"),
            "content": config_text,
        }
