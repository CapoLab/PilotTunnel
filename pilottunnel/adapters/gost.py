from .base import AdapterContext, AdapterMetadata
from .common import DryRunAdapter


class GostAdapter(DryRunAdapter):
    ADAPTER_METADATA = AdapterMetadata(name="gost", layer="layer4", transports=("tcp", "udp"), notes="Dry-run template only in v0.1")

    def render_config(self, context: AdapterContext) -> dict:
        config_text = self._config_text(context)
        config_path = self._write_config_file(context, config_text, self.config_filename(context.role).replace(".toml", ".yaml"))
        return {
            "action": "render_config",
            "mode": "staged-apply" if context.apply_changes else "dry-run",
            "service_name": self.service_name(context),
            "config_path": config_path,
            "content": config_text,
        }

    def render_runtime_plan(self, context: AdapterContext, runtime_dir, executable_path: str) -> dict:
        config_text = self._config_text(context)
        config_path = self._write_runtime_file(
            context,
            runtime_dir,
            config_text,
            self.config_filename(context.role).replace(".toml", ".yaml"),
        )
        real_port = context.remote_stub.get("real_controller_user_facing_port", context.profile.ports.main_port)
        return {
            "config_path": config_path,
            "content": config_text,
            "argv": [executable_path, "-C", config_path],
            "environment": {},
            "healthcheck_target_summary": {
                "kind": "tcp",
                "host": "127.0.0.1",
                "port": real_port,
            },
        }

    def _config_text(self, context: AdapterContext) -> str:
        real_controller_port = context.remote_stub.get("real_controller_user_facing_port", context.profile.ports.main_port)
        real_worker_port = context.remote_stub.get("real_worker_service_port", context.profile.ports.service_port or context.profile.target_port)
        tunnel_id = context.remote_stub.get("gost_tunnel_id", "")
        service_host = context.remote_stub.get("gost_service_host", "service.local")
        probe_host = context.remote_stub.get("gost_probe_host", "probe.local")
        probe_port = int(context.remote_stub.get("probe_port") or context.profile.ports.check_port or 0)
        include_probe = context.remote_stub.get("mode") == "candidate-smoke" and probe_port > 0
        transport_port = context.profile.ports.control_port or context.profile.ports.main_port
        controller_address = context.controller_address or context.profile.target_host
        if context.role == "controller":
            lines = [
                "services:",
                "- name: tunnel-server",
                f"  addr: :{transport_port}",
                "  handler:",
                "    type: tunnel",
                "    metadata:",
                "      tunnel.direct: true",
                "  listener:",
                "    type: tcp",
                "- name: service-visitor",
                f"  addr: :{real_controller_port}",
                "  handler:",
                "    type: tcp",
                "    chain: chain-0",
                "  listener:",
                "    type: tcp",
                "  forwarder:",
                "    nodes:",
                "    - name: service",
                f"      addr: {service_host}",
            ]
            if include_probe:
                lines.extend(
                    [
                        "- name: probe-visitor",
                        f"  addr: 127.0.0.1:{probe_port}",
                        "  handler:",
                        "    type: tcp",
                        "    chain: chain-0",
                        "  listener:",
                        "    type: tcp",
                        "  forwarder:",
                        "    nodes:",
                        "    - name: probe",
                        f"      addr: {probe_host}",
                    ]
                )
            lines.extend(
                [
                "chains:",
                "- name: chain-0",
                "  hops:",
                "  - name: hop-0",
                "    nodes:",
                "    - name: node-0",
                f"      addr: :{transport_port}",
                "      connector:",
                "        type: tunnel",
                "        metadata:",
                f"          tunnel.id: {tunnel_id}",
                "      dialer:",
                "        type: tcp",
                ]
            )
            return "\n".join(lines)
        lines = [
                "services:",
                "- name: probe-client",
                "  addr: :0",
                "  handler:",
                "    type: rtcp",
                "  listener:",
                "    type: rtcp",
                "    chain: chain-0",
                "  forwarder:",
                "    nodes:",
                "    - name: service",
                f"      addr: 127.0.0.1:{real_worker_port}",
                "      filter:",
                f"        host: {service_host}",
        ]
        if include_probe:
            lines.extend(
                [
                    "    - name: probe",
                    f"      addr: 127.0.0.1:{probe_port}",
                    "      filter:",
                    f"        host: {probe_host}",
                ]
            )
        lines.extend(
            [
                "chains:",
                "- name: chain-0",
                "  hops:",
                "  - name: hop-0",
                "    nodes:",
                "    - name: node-0",
                f"      addr: {controller_address}:{transport_port}",
                "      connector:",
                "        type: tunnel",
                "        metadata:",
                f"          tunnel.id: {tunnel_id}",
                "      dialer:",
                "        type: tcp",
            ]
        )
        return "\n".join(lines)
