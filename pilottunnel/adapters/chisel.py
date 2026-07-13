import json

from .base import AdapterContext, AdapterMetadata
from .common import DryRunAdapter


class ChiselAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="chisel", layer="layer4", transports=("tcp",), notes="Dry-run template only in v0.1")

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
        config_text = self._config_text(context)
        config_path = self._write_runtime_file(context, runtime_dir, config_text, self.config_filename(context.role).replace(".toml", ".txt"))
        argv = self._argv(context, executable_path, runtime_dir)
        return {
            "config_path": config_path,
            "content": config_text,
            "argv": argv,
            "environment": self._environment(context),
            "healthcheck_target_summary": {
                "kind": "tcp",
                "host": "127.0.0.1" if context.role == "controller" else context.worker_address or "127.0.0.1",
                "port": context.profile.ports.main_port if context.role == "controller" else context.profile.ports.service_port or context.profile.target_port,
            },
        }

    def _config_text(self, context) -> str:
        transport_port = context.profile.ports.control_port or context.profile.ports.main_port
        user = context.secrets.get("auth_user", "pilot")
        password = context.secrets.get("auth_password", "PAIRING_SECRET_REQUIRED")
        probe_port = int(context.remote_stub.get("probe_port") or context.profile.ports.check_port or 0)
        include_probe = context.remote_stub.get("mode") == "candidate-smoke" and probe_port > 0
        if context.role == "controller":
            lines = [
                "[chisel]",
                "mode = server",
                f"listen_port = {transport_port}",
                "reverse = true",
                f"auth = {user}:{password}",
                f"remote_port = {context.profile.ports.main_port}",
            ]
            if include_probe:
                lines.append(f"probe_remote = 127.0.0.1:{probe_port}")
            return "\n".join(lines)
        return "\n".join(
            [
                "[chisel]",
                "mode = client",
                f"controller = {context.controller_address or context.profile.target_host}:{transport_port}",
                f"auth = {user}:{password}",
                f"reverse_remote = 0.0.0.0:{context.profile.ports.main_port}",
                f"local_target = 127.0.0.1:{context.profile.ports.service_port or context.profile.target_port}",
                *( [f"probe_reverse = 127.0.0.1:{probe_port} -> 127.0.0.1:{probe_port}"] if include_probe else [] ),
            ]
        )

    def _argv(self, context: AdapterContext, executable_path: str, runtime_dir) -> list[str]:
        transport_port = context.profile.ports.control_port or context.profile.ports.main_port
        user = context.secrets.get("auth_user", "pilot")
        password = context.secrets.get("auth_password", "PAIRING_SECRET_REQUIRED")
        if context.role == "controller":
            authfile_path = self._write_runtime_file(
                context,
                runtime_dir,
                json.dumps({f"{user}:{password}": [""]}, indent=2, sort_keys=True),
                "chisel-users.json",
            )
            return [
                executable_path,
                "server",
                "--host",
                "0.0.0.0",
                "--port",
                str(transport_port),
                "--reverse",
                "--authfile",
                authfile_path,
            ]
        controller_address = context.controller_address or context.profile.target_host
        reverse = f"R:0.0.0.0:{context.profile.ports.main_port}:127.0.0.1:{context.profile.ports.service_port or context.profile.target_port}"
        argv = [executable_path, "client", f"{controller_address}:{transport_port}", reverse]
        probe_port = int(context.remote_stub.get("probe_port") or context.profile.ports.check_port or 0)
        if context.remote_stub.get("mode") == "candidate-smoke" and probe_port > 0:
            argv.append(f"R:127.0.0.1:{probe_port}:127.0.0.1:{probe_port}")
        return argv

    def _environment(self, context: AdapterContext) -> dict[str, str]:
        if context.role != "worker":
            return {}
        user = context.secrets.get("auth_user", "pilot")
        password = context.secrets.get("auth_password", "PAIRING_SECRET_REQUIRED")
        return {"AUTH": f"{user}:{password}"}
