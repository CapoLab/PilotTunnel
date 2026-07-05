from .base import AdapterContext, AdapterMetadata
from .common import DryRunAdapter


class BoreAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="bore", layer="layer4", transports=("tcp",), notes="Dry-run template only in v0.1")

    def render_config(self, context: AdapterContext) -> dict:
        config_text = self._config_text(context)
        config_path = self._write_config_file(context, config_text, self.config_filename(context.role).replace(".toml", ".txt"))
        return {
            "action": "render_config",
            "mode": "staged-apply" if context.apply_changes else "dry-run",
            "service_name": self.service_name(context),
            "config_path": config_path,
            "content": config_text,
        }

    def render_runtime_plan(self, context: AdapterContext, runtime_dir, executable_path: str) -> dict:
        control_port = int(context.remote_stub.get("bore_control_port", 7835))
        real_controller_port = int(context.remote_stub.get("real_controller_user_facing_port", context.profile.ports.main_port))
        real_worker_port = int(context.remote_stub.get("real_worker_service_port", context.profile.ports.service_port or context.profile.target_port))
        config_text = self._config_text(context)
        config_path = self._write_runtime_file(context, runtime_dir, config_text, self.config_filename(context.role).replace(".toml", ".txt"))
        environment = {"BORE_SECRET": context.secrets.get("shared_token", "PAIRING_SECRET_REQUIRED")}
        if context.role == "controller":
            argv = [
                executable_path,
                "server",
                "--bind-addr",
                "0.0.0.0",
                "--bind-tunnels",
                "0.0.0.0",
                "--min-port",
                str(real_controller_port),
                "--max-port",
                str(real_controller_port),
            ]
        else:
            controller_address = context.controller_address or context.profile.target_host
            argv = [
                executable_path,
                "local",
                str(real_worker_port),
                "--local-host",
                "127.0.0.1",
                "--to",
                f"{controller_address}:{control_port}",
                "--port",
                str(real_controller_port),
            ]
        return {
            "config_path": config_path,
            "content": config_text,
            "argv": argv,
            "environment": environment,
            "healthcheck_target_summary": {
                "kind": "tcp",
                "host": "127.0.0.1",
                "port": real_controller_port,
            },
            "effective_transport_port": control_port,
        }

    def _config_text(self, context: AdapterContext) -> str:
        real_controller_port = context.remote_stub.get("real_controller_user_facing_port", context.profile.ports.main_port)
        real_worker_port = context.remote_stub.get("real_worker_service_port", context.profile.ports.service_port or context.profile.target_port)
        if context.role == "controller":
            return "\n".join(
                [
                    "[bore]",
                    "mode = server",
                    f"control_port = {context.remote_stub.get('bore_control_port', 7835)}",
                    f"probe_port = {real_controller_port}",
                ]
            )
        return "\n".join(
            [
                "[bore]",
                "mode = local",
                f"controller = {context.controller_address or context.profile.target_host}:{context.remote_stub.get('bore_control_port', 7835)}",
                f"probe_port = {real_worker_port}",
            ]
        )
