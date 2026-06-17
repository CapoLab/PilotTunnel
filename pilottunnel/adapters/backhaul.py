from .base import AdapterContext, AdapterMetadata
from .common import DryRunAdapter


class BackhaulAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(
        name="backhaul",
        layer="layer4",
        transports=("tcp", "tcpmux", "udp", "ws", "wsmux", "wss", "wssmux", "utcpmux", "uwsmux"),
        experimental_transports=("tcptun", "faketcptun"),
        notes="Dry-run Backhaul planning for controller/worker roles in v0.1",
    )

    def render_config(self, context: AdapterContext) -> dict:
        config_text = "\n".join(
            [
                "[backhaul]",
                f"role = {context.role}",
                f"transport = {context.transport}",
                f"bind_port = {context.profile.ports.main_port}",
                f"target = {context.profile.target_host}:{context.profile.target_port}",
                f"control_port = {context.profile.ports.control_port or context.profile.ports.main_port}",
                f"service_port = {context.profile.ports.service_port or context.profile.target_port}",
                f"check_port = {context.profile.ports.check_port or context.profile.target_port}",
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
