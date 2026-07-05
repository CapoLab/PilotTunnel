from .base import AdapterContext, AdapterMetadata
from .common import DryRunAdapter


class RealmAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="realm", layer="layer4", transports=("tcp",), notes="Dry-run template only in v0.1")

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
        if context.role != "controller":
            raise ValueError("Realm direct Layer 4 baseline only runs on the controller side in candidate smoke mode")
        config_text = self._config_text(context)
        config_path = self._write_runtime_file(context, runtime_dir, config_text, self.config_filename(context.role).replace(".toml", ".txt"))
        real_port = context.remote_stub.get("real_controller_user_facing_port", context.profile.ports.main_port)
        argv = [
            executable_path,
            "--listen",
            f"127.0.0.1:{real_port}",
            "--remote",
            f"{context.worker_address}:{context.remote_stub.get('real_worker_service_port', context.profile.ports.service_port or context.profile.target_port)}",
        ]
        return {
            "config_path": config_path,
            "content": config_text,
            "argv": argv,
            "environment": {},
            "healthcheck_target_summary": {
                "kind": "tcp",
                "host": "127.0.0.1",
                "port": real_port,
            },
        }

    def _config_text(self, context: AdapterContext) -> str:
        real_port = context.remote_stub.get("real_controller_user_facing_port", context.profile.ports.main_port)
        real_worker_port = context.remote_stub.get("real_worker_service_port", context.profile.ports.service_port or context.profile.target_port)
        if context.role == "controller":
            return "\n".join(
                [
                    "[realm]",
                    "mode = direct_l4_baseline",
                    f"listen = 127.0.0.1:{real_port}",
                    f"remote = {context.worker_address}:{real_worker_port}",
                ]
            )
        return "\n".join(
            [
                "[realm]",
                "mode = probe_only",
                f"probe_bind = {context.remote_stub.get('probe_bind_host', '0.0.0.0')}:{real_worker_port}",
            ]
        )
