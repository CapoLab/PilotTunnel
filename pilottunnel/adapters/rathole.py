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
        return {
            "config_path": config_path,
            "content": config_text,
            "argv": [executable_path, config_path],
            "environment": {},
            "healthcheck_target_summary": {
                "kind": "tcp",
                "host": "127.0.0.1" if context.role == "controller" else context.worker_address or "127.0.0.1",
                "port": context.profile.ports.main_port if context.role == "controller" else context.profile.ports.service_port or context.profile.target_port,
            },
        }

    def _config_text(self, context: AdapterContext) -> str:
        token = context.secrets.get("shared_token", "PAIRING_SECRET_REQUIRED")
        service_name = context.profile.name.replace("-", "_")
        probe_service_name = f"{service_name}_probe"
        transport_port = context.profile.ports.control_port or context.profile.ports.main_port
        probe_port = int(context.remote_stub.get("probe_port") or context.profile.ports.check_port or 0)
        include_probe = context.remote_stub.get("mode") == "candidate-smoke" and probe_port > 0
        if context.role == "controller":
            lines = [
                "[server]",
                f'bind_addr = "0.0.0.0:{transport_port}"',
                f'default_token = "{token}"',
                "",
                f"[server.services.{service_name}]",
                f'bind_addr = "0.0.0.0:{context.profile.ports.main_port}"',
            ]
            if include_probe:
                lines.extend(
                    [
                        "",
                        f"[server.services.{probe_service_name}]",
                        f'bind_addr = "127.0.0.1:{probe_port}"',
                    ]
                )
            return "\n".join(lines)
        controller_address = context.controller_address or context.profile.target_host
        worker_target_port = context.profile.ports.service_port or context.profile.target_port
        lines = [
            "[client]",
            f'remote_addr = "{controller_address}:{transport_port}"',
            f'default_token = "{token}"',
            "",
            f"[client.services.{service_name}]",
            f'local_addr = "127.0.0.1:{worker_target_port}"',
        ]
        if include_probe:
            lines.extend(
                [
                    "",
                    f"[client.services.{probe_service_name}]",
                    f'local_addr = "127.0.0.1:{probe_port}"',
                ]
            )
        return "\n".join(lines)
