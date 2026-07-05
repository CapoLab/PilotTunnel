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
        config_text = self._config_text(context)
        config_path = self._write_config_file(context, config_text, self.config_filename(context.role))
        return {
            "action": "render_config",
            "mode": "staged-apply" if context.apply_changes else "dry-run",
            "service_name": self.service_name(context),
            "config_path": config_path,
            "content": config_text,
        }

    def render_runtime_plan(self, context: AdapterContext, runtime_dir, executable_path: str) -> dict:
        config_text = self._config_text(context)
        config_path = self._write_runtime_file(context, runtime_dir, config_text, self.config_filename(context.role))
        real_port = context.remote_stub.get("real_controller_user_facing_port", context.profile.ports.main_port)
        return {
            "config_path": config_path,
            "content": config_text,
            "argv": [executable_path, "-c", config_path],
            "environment": {},
            "healthcheck_target_summary": {
                "kind": "tcp",
                "host": "127.0.0.1",
                "port": real_port,
            },
        }

    def _config_text(self, context: AdapterContext) -> str:
        token = context.secrets.get("shared_token", "PAIRING_SECRET_REQUIRED")
        transport_port = context.profile.ports.control_port or context.profile.ports.main_port
        real_controller_port = context.remote_stub.get("real_controller_user_facing_port", context.profile.ports.main_port)
        real_worker_port = context.remote_stub.get("real_worker_service_port", context.profile.ports.service_port or context.profile.target_port)
        if context.role == "controller":
            return "\n".join(
                [
                    "[server]",
                    f'bind_addr = "0.0.0.0:{transport_port}"',
                    f'transport = "{context.transport}"',
                    f'token = "{token}"',
                    "log_level = \"info\"",
                    "ports = [",
                    f'  "{real_controller_port}=127.0.0.1:{real_worker_port}"',
                    "]",
                ]
            )
        controller_address = context.controller_address or context.profile.target_host
        return "\n".join(
            [
                "[client]",
                f'remote_addr = "{controller_address}:{transport_port}"',
                f'transport = "{context.transport}"',
                f'token = "{token}"',
                "log_level = \"info\"",
            ]
        )
