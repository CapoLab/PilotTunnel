from pathlib import Path

from .base import AdapterContext, AdapterMetadata
from .common import DryRunAdapter


class FrpAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="frp", layer="layer4", transports=("tcp", "udp"), notes="Dry-run template only in v0.1")

    def render_config(self, context: AdapterContext) -> dict:
        config_text = self._config_text(context)
        config_path = self._write_config_file(context, config_text, self._config_filename(context))
        return {
            "action": "render_config",
            "mode": "staged-apply" if context.apply_changes else "dry-run",
            "service_name": self.service_name(context),
            "config_path": config_path,
            "content": config_text,
        }

    def render_runtime_plan(self, context: AdapterContext, runtime_dir, executable_path: str) -> dict:
        config_text = self._config_text(context)
        config_path = self._write_runtime_file(context, runtime_dir, config_text, self._config_filename(context))
        command = self._runtime_command(context, executable_path, config_path)
        real_port = context.remote_stub.get("real_controller_user_facing_port", context.profile.ports.main_port)
        return {
            "config_path": config_path,
            "content": config_text,
            "argv": command,
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
                    f"bindPort = {transport_port}",
                    "transport.tcpMux = false",
                    "auth.method = \"token\"",
                    f"auth.token = \"{token}\"",
                ]
            )
        controller_address = context.controller_address or context.profile.target_host
        return "\n".join(
            [
                f'serverAddr = "{controller_address}"',
                f"serverPort = {transport_port}",
                "auth.method = \"token\"",
                f"auth.token = \"{token}\"",
                "",
                "[[proxies]]",
                f'name = "{context.profile.name}"',
                'type = "tcp"',
                'localIP = "127.0.0.1"',
                f"localPort = {real_worker_port}",
                f"remotePort = {real_controller_port}",
            ]
        )

    def _runtime_command(self, context: AdapterContext, executable_path: str, config_path: str) -> list[str]:
        executable = Path(executable_path)
        if context.role == "controller":
            if executable.name.lower() not in {"frps", "frps.exe"}:
                raise ValueError(f"Expected frps for controller runtime, got '{executable.name}'")
            return [executable_path, "-c", config_path]
        if executable.name.lower() not in {"frpc", "frpc.exe"}:
            raise ValueError(f"Expected frpc for worker runtime, got '{executable.name}'")
        return [executable_path, "-c", config_path]

    def _config_filename(self, context: AdapterContext) -> str:
        return f"{self.metadata().name}-{context.role}.toml"
