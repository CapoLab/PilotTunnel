from pathlib import Path

from .base import AdapterContext, AdapterMetadata
from .common import DryRunAdapter


class FrpAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="frp", layer="layer4", transports=("tcp", "udp"), notes="Dry-run template only in v0.1")

    def runtime_service_specs(self, role: str) -> tuple[dict[str, str], ...]:
        """Declare runtime components; future probe visitors add a second controller spec."""
        return (
            ({"name": "frps", "component": "frps"}, {"name": "frpc-visitor", "component": "frpc"})
            if role == "controller"
            else ({"name": "frpc", "component": "frpc"},)
        )

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
        runtime_role = context.remote_stub.get("frp_runtime_role", "")
        if context.role == "controller" and runtime_role != "frpc-visitor":
            return "\n".join(
                [
                    f"bindPort = {transport_port}",
                    "transport.tcpMux = false",
                    "auth.method = \"token\"",
                    f"auth.token = \"{token}\"",
                ]
            )
        controller_address = "127.0.0.1" if runtime_role == "frpc-visitor" else context.controller_address or context.profile.target_host
        probe_port = int(context.remote_stub.get("probe_port") or context.profile.ports.check_port or 0)
        if runtime_role == "frpc-visitor":
            return "\n".join([
                f'serverAddr = "{controller_address}"', f"serverPort = {transport_port}",
                "auth.method = \"token\"", f"auth.token = \"{token}\"", "",
                "[[visitors]]", f'name = "{context.profile.name}-probe-visitor"', 'type = "stcp"',
                f'serverName = "{context.profile.name}-probe"', f'secretKey = "{token}"',
                'bindAddr = "127.0.0.1"', f"bindPort = {probe_port}",
            ])
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
                "",
                "[[proxies]]",
                f'name = "{context.profile.name}-probe"',
                'type = "stcp"',
                f'secretKey = "{token}"',
                'localIP = "127.0.0.1"',
                f"localPort = {probe_port}",
            ]
        )

    def _runtime_command(self, context: AdapterContext, executable_path: str, config_path: str) -> list[str]:
        executable = Path(executable_path)
        if context.role == "controller" and context.remote_stub.get("frp_runtime_role") != "frpc-visitor":
            if executable.name.lower() not in {"frps", "frps.exe"}:
                raise ValueError(f"Expected frps for controller runtime, got '{executable.name}'")
            return [executable_path, "-c", config_path]
        if executable.name.lower() not in {"frpc", "frpc.exe"}:
            raise ValueError(f"Expected frpc for worker runtime, got '{executable.name}'")
        return [executable_path, "-c", config_path]

    def _config_filename(self, context: AdapterContext) -> str:
        suffix = context.remote_stub.get("frp_runtime_role", context.role)
        return f"{self.metadata().name}-{suffix}.toml"
