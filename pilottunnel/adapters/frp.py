from .base import AdapterMetadata
from .common import DryRunAdapter


class FrpAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="frp", layer="layer4", transports=("tcp", "udp"), notes="Dry-run template only in v0.1")

    def render_config(self, context):
        config_text = "\n".join(
            [
                "[common]",
                f"server_addr = {context.profile.target_host}",
                f"server_port = {context.profile.ports.control_port or context.profile.ports.main_port}",
                "",
                f"[{context.profile.name}]",
                f"type = {context.transport}",
                "local_ip = 127.0.0.1",
                f"local_port = {context.profile.target_port}",
                f"remote_port = {context.profile.ports.main_port}",
                f"role = {context.role}",
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

    def render_runtime_plan(self, context, runtime_dir, executable_path: str) -> dict:
        config_text = "\n".join(
            [
                "[common]",
                f"server_addr = {context.profile.target_host}",
                f"server_port = {context.profile.ports.control_port or context.profile.ports.main_port}",
                "",
                f"[{context.profile.name}]",
                f"type = {context.transport}",
                "local_ip = 127.0.0.1",
                f"local_port = {context.profile.target_port}",
                f"remote_port = {context.profile.ports.main_port}",
                f"role = {context.role}",
            ]
        )
        config_path = self._write_runtime_file(context, runtime_dir, config_text, self.config_filename(context.role).replace(".toml", ".ini"))
        return {
            "config_path": config_path,
            "content": config_text,
            "argv": [executable_path, "-c", config_path],
            "environment": {},
            "healthcheck_target_summary": {
                "kind": "tcp",
                "host": context.profile.target_host,
                "port": context.profile.target_port,
            },
        }
