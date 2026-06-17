"""Transactional switch orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .adapters import ADAPTERS
from .adapters.base import AdapterContext, BaseAdapter
from .audit import write_audit_log
from .config import AppConfig, Profile, SUPPORTED_LAYERS, build_worker_stub
from .locks import profile_lock
from .registry import PortRegistry, RegistryEntry
from .state import AppState, RuntimeRecord


@dataclass
class SwitchPaths:
    lock_dir: Path
    work_dir: Path
    audit_path: Path
    staging_root: Path


@dataclass
class SwitchResult:
    ok: bool
    message: str
    actions: list[str] = field(default_factory=list)
    dry_run: bool = True
    rollback_performed: bool = False
    current: dict[str, str] = field(default_factory=dict)
    target: dict[str, str] = field(default_factory=dict)
    generated_service: str = ""
    healthcheck: dict[str, str | bool] = field(default_factory=dict)
    committed: bool = False
    staged_only: bool = False
    real_systemd_touched: bool = False
    real_firewall_touched: bool = False
    generated_files: list[str] = field(default_factory=list)


class SwitchEngine:
    def __init__(
        self,
        *,
        config: AppConfig,
        state: AppState,
        registry: PortRegistry,
        paths: SwitchPaths,
        adapter_factory: Callable[[str], BaseAdapter] | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self.registry = registry
        self.paths = paths
        self.adapter_factory = adapter_factory or (lambda name: ADAPTERS[name]())
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    def _find_profile(self, profile_name: str) -> Profile:
        for profile in self.config.profiles:
            if profile.name == profile_name:
                return profile
        raise KeyError(f"Profile '{profile_name}' not found")

    def _record_for(self, profile_name: str) -> RuntimeRecord:
        return self.state.profiles.setdefault(profile_name, RuntimeRecord(profile=profile_name))

    def _ensure_layer_supported(self, layer: str) -> None:
        if not SUPPORTED_LAYERS.get(layer, False):
            raise ValueError(f"Layer '{layer}' is listed but blocked in v0.1")

    def _ensure_cooldown(self, profile: Profile, record: RuntimeRecord) -> None:
        if not record.last_switch_at:
            return
        last_switch = datetime.fromisoformat(record.last_switch_at)
        if self.now_provider() < last_switch + timedelta(seconds=profile.safety.cooldown_seconds):
            raise ValueError(f"Profile '{profile.name}' is in cooldown")

    def _is_dry_run(self, profile: Profile, apply_changes: bool) -> bool:
        return not apply_changes if not apply_changes else False if profile.safety.dry_run_default else False

    def install(self, profile_name: str, adapter_name: str, transport: str, apply_changes: bool) -> SwitchResult:
        profile = self._find_profile(profile_name)
        adapter = self.adapter_factory(adapter_name)
        self._ensure_layer_supported("layer4")
        context = self._build_context(profile, transport, apply_changes)
        ok, reason = adapter.precheck(context)
        if not ok:
            return SwitchResult(
                False,
                reason,
                dry_run=not apply_changes,
                current={"adapter": profile.active_adapter, "transport": profile.active_transport},
                target={"adapter": adapter_name, "transport": transport},
                staged_only=apply_changes,
            )
        actions = [
            "precheck",
            "install",
            "render_config",
            "render_systemd_unit",
        ]
        install_result = adapter.install(context)
        render_result = adapter.render_config(context)
        unit_result = adapter.render_systemd_unit(context)
        generated_files = [render_result.get("config_path", ""), unit_result["unit"].get("path", "")]
        write_audit_log(
            "install",
            profile_name,
            {
                "from_adapter": profile.active_adapter,
                "from_transport": profile.active_transport,
                "to_adapter": adapter_name,
                "to_transport": transport,
                "dry_run": not apply_changes,
                "staged_only": apply_changes,
                "real_systemd_touched": False,
                "real_firewall_touched": False,
                "result": "prepared",
                "rollback_status": "not-needed",
                "install": install_result,
                "render": render_result,
                "unit": unit_result,
            },
            self.paths.audit_path,
        )
        return SwitchResult(
            True,
            "Adapter prepared",
            actions=actions,
            dry_run=not apply_changes,
            current={"adapter": profile.active_adapter, "transport": profile.active_transport},
            target={"adapter": adapter_name, "transport": transport},
            generated_service=adapter.service_name(context),
            staged_only=apply_changes,
            generated_files=generated_files,
        )

    def switch(self, profile_name: str, adapter_name: str, transport: str, apply_changes: bool) -> SwitchResult:
        profile = self._find_profile(profile_name)
        adapter = self.adapter_factory(adapter_name)
        metadata = adapter.metadata()
        self._ensure_layer_supported(metadata.layer)
        with profile_lock(profile_name, self.paths.lock_dir):
            record = self._record_for(profile_name)
            self._ensure_cooldown(profile, record)
            backup_state = self.state.clone()
            backup_registry = PortRegistry(owners=dict(self.registry.owners))
            record.rollback_snapshot = asdict(record)
            context = self._build_context(profile, transport, apply_changes)
            from_adapter = record.active_adapter
            from_transport = record.active_transport

            ok, reason = adapter.precheck(context)
            if not ok:
                raise_or_result = SwitchResult(
                    False,
                    reason,
                    actions=["precheck"],
                    dry_run=not apply_changes,
                    current={"adapter": from_adapter, "transport": from_transport},
                    target={"adapter": adapter_name, "transport": transport},
                    generated_service=adapter.service_name(context),
                    staged_only=apply_changes,
                )
                self._write_switch_audit(profile.name, from_adapter, from_transport, adapter_name, transport, raise_or_result)
                return raise_or_result

            actions = ["lock", "precheck", "render_config", "render_systemd_unit"]
            rendered_config = adapter.render_config(context)
            rendered_unit = adapter.render_systemd_unit(context)
            generated_files = [rendered_config.get("config_path", ""), rendered_unit["unit"].get("path", "")]
            old_adapter_name = from_adapter
            old_transport = from_transport or transport

            if old_adapter_name:
                old_adapter = self.adapter_factory(old_adapter_name)
                old_context = self._build_context(profile, old_transport, apply_changes)
                old_adapter.stop(old_context)
                old_adapter.cleanup_runtime(old_context)
                self.registry.release(profile.name)
                actions.extend(["stop_old", "cleanup_old"])

            entry = self._registry_entry(profile, adapter_name, transport, adapter.service_name(context))
            self.registry.claim(entry)
            conflicts = self.registry.check_conflicts()
            if conflicts:
                self.state = backup_state
                self.registry = backup_registry
                result = SwitchResult(
                    False,
                    "; ".join(conflicts),
                    actions=actions + ["registry_check"],
                    dry_run=not apply_changes,
                    current={"adapter": from_adapter, "transport": from_transport},
                    target={"adapter": adapter_name, "transport": transport},
                    generated_service=adapter.service_name(context),
                    staged_only=apply_changes,
                    generated_files=generated_files,
                )
                self._write_switch_audit(profile.name, from_adapter, from_transport, adapter_name, transport, result)
                return result

            adapter.start(context)
            actions.extend(["registry_check", "start_new"])
            healthy, message = adapter.healthcheck(context)
            actions.append("healthcheck")
            if not healthy:
                rollback_performed = False
                if profile.safety.rollback_on_failure:
                    self.state = backup_state
                    self.registry = backup_registry
                    rollback_performed = True
                    if old_adapter_name:
                        old_adapter = self.adapter_factory(old_adapter_name)
                        old_context = self._build_context(profile, old_transport, apply_changes)
                        old_adapter.start(old_context)
                result = SwitchResult(
                    False,
                    f"Switch failed healthcheck: {message}",
                    actions=actions + ["rollback"],
                    dry_run=not apply_changes,
                    rollback_performed=rollback_performed,
                    current={"adapter": from_adapter, "transport": from_transport},
                    target={"adapter": adapter_name, "transport": transport},
                    generated_service=adapter.service_name(context),
                    healthcheck={"result": False, "message": message},
                    staged_only=apply_changes,
                    generated_files=generated_files,
                )
                self._write_switch_audit(profile.name, from_adapter, from_transport, adapter_name, transport, result)
                return result

            record.active_adapter = adapter_name
            record.active_transport = transport
            record.active_layer = metadata.layer
            record.service_name = adapter.service_name(context)
            record.role = profile.role
            record.healthy = True
            record.last_error = ""
            record.last_switch_at = self.now_provider().isoformat()
            profile.active_adapter = adapter_name
            profile.active_transport = transport
            profile.active_layer = metadata.layer
            result = SwitchResult(
                True,
                message,
                actions=actions + ["commit"],
                dry_run=not apply_changes,
                current={"adapter": from_adapter, "transport": from_transport},
                target={"adapter": adapter_name, "transport": transport},
                generated_service=adapter.service_name(context),
                healthcheck={"result": True, "message": message},
                committed=True,
                staged_only=apply_changes,
                generated_files=generated_files,
            )
            self._write_switch_audit(profile.name, from_adapter, from_transport, adapter_name, transport, result)
            return result

    def rollback(self, profile_name: str, apply_changes: bool) -> SwitchResult:
        profile = self._find_profile(profile_name)
        with profile_lock(profile_name, self.paths.lock_dir):
            record = self._record_for(profile_name)
            snapshot = record.rollback_snapshot
            if not snapshot:
                return SwitchResult(False, "No rollback snapshot available", dry_run=not apply_changes, committed=False)
            previous_adapter = snapshot.get("active_adapter", "")
            previous_transport = snapshot.get("active_transport", "")
            if not previous_adapter:
                self.registry.release(profile.name)
                record.active_adapter = ""
                record.active_transport = ""
                record.healthy = False
                return SwitchResult(
                    True,
                    "Rolled back to empty state",
                    actions=["rollback"],
                    dry_run=not apply_changes,
                    committed=True,
                    staged_only=apply_changes,
                )
            return self.switch(profile_name, previous_adapter, previous_transport, apply_changes)

    def status(self, profile_name: str) -> dict:
        record = self._record_for(profile_name)
        return asdict(record)

    def healthcheck(self, profile_name: str) -> SwitchResult:
        profile = self._find_profile(profile_name)
        record = self._record_for(profile_name)
        if not record.active_adapter:
            return SwitchResult(False, "No active adapter", dry_run=True)
        adapter = self.adapter_factory(record.active_adapter)
        context = self._build_context(profile, record.active_transport, apply_changes=False)
        healthy, message = adapter.healthcheck(context)
        record.healthy = healthy
        record.last_error = "" if healthy else message
        return SwitchResult(
            healthy,
            message,
            dry_run=not context.apply_changes,
            current={"adapter": record.active_adapter, "transport": record.active_transport},
            target={"adapter": record.active_adapter, "transport": record.active_transport},
            generated_service=record.service_name,
            healthcheck={"result": healthy, "message": message},
            committed=healthy,
        )

    def cleanup(self, profile_name: str, apply_changes: bool, dry_run: bool) -> SwitchResult:
        profile = self._find_profile(profile_name)
        record = self._record_for(profile_name)
        actions = ["cleanup_runtime"]
        if record.active_adapter:
            adapter = self.adapter_factory(record.active_adapter)
            context = self._build_context(profile, record.active_transport or "tcp", apply_changes and not dry_run)
            adapter.cleanup_runtime(context)
        return SwitchResult(
            True,
            "Cleanup prepared",
            actions=actions,
            dry_run=not apply_changes or dry_run,
            staged_only=apply_changes and not dry_run,
        )

    def _build_context(self, profile: Profile, transport: str, apply_changes: bool) -> AdapterContext:
        return AdapterContext(
            profile=profile,
            transport=transport,
            work_dir=self.paths.work_dir / profile.name,
            staging_root=self.paths.staging_root,
            apply_changes=apply_changes,
            role=profile.role,
            remote_stub=asdict(build_worker_stub(profile)),
        )

    def _registry_entry(self, profile: Profile, adapter_name: str, transport: str, service_name: str) -> RegistryEntry:
        return RegistryEntry(
            profile=profile.name,
            main_port=profile.ports.main_port,
            adapter=adapter_name,
            transport=transport,
            role=profile.role,
            owned_ports=profile.ports.owned_ports(),
            owned_services=[service_name],
            owned_firewall_rule_tags=[f"pilottunnel:{profile.name}:{adapter_name}:{transport}"],
            owned_routes=[],
        )

    def _write_switch_audit(
        self,
        profile_name: str,
        from_adapter: str,
        from_transport: str,
        adapter_name: str,
        transport: str,
        result: SwitchResult,
    ) -> None:
        write_audit_log(
            "switch",
            profile_name,
            {
                "from_adapter": from_adapter,
                "from_transport": from_transport,
                "to_adapter": adapter_name,
                "to_transport": transport,
                "dry_run": result.dry_run,
                "staged_only": result.staged_only,
                "real_systemd_touched": result.real_systemd_touched,
                "real_firewall_touched": result.real_firewall_touched,
                "result": "ok" if result.ok else "failed",
                "rollback_status": "performed" if result.rollback_performed else "not-needed",
            },
            self.paths.audit_path,
        )

    def plan(self, profile_name: str, adapter_name: str, transport: str, apply_changes: bool = False) -> dict:
        profile = self._find_profile(profile_name)
        adapter = self.adapter_factory(adapter_name)
        context = self._build_context(profile, transport, apply_changes)
        ok, reason = adapter.precheck(context)
        warnings: list[str] = []
        if not ok:
            warnings.append(reason)
        rendered = adapter.render_config(context)
        unit = adapter.render_systemd_unit(context)
        return {
            "profile": profile.name,
            "role": profile.role,
            "adapter": adapter_name,
            "transport": transport,
            "supported_in_v0_1": ok,
            "warnings": warnings,
            "ports_used": profile.ports.owned_ports(),
            "generated_config_path": rendered.get("config_path", ""),
            "generated_service_path": unit["unit"]["path"],
            "future_apply_commands": [
                f"systemctl daemon-reload",
                f"systemctl enable {unit['unit']['unit_name']}",
                f"systemctl start {unit['unit']['unit_name']}",
            ],
            "staged_only": apply_changes,
            "real_systemd_touched": False,
            "real_firewall_touched": False,
        }
