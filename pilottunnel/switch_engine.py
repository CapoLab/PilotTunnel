"""Transactional switch orchestration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .adapters import ADAPTERS
from .adapters.base import AdapterContext, BaseAdapter
from .audit import write_audit_log
from .config import AppConfig, Profile, SUPPORTED_LAYERS
from .locks import profile_lock
from .registry import PortRegistry
from .state import AppState, RuntimeRecord


@dataclass
class SwitchPaths:
    lock_dir: Path
    work_dir: Path
    audit_path: Path


@dataclass
class SwitchResult:
    ok: bool
    message: str
    actions: list[str] = field(default_factory=list)


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
        if self.now_provider() < last_switch + timedelta(seconds=profile.cooldown_seconds):
            raise ValueError(f"Profile '{profile.name}' is in cooldown")

    def install(self, profile_name: str, adapter_name: str, transport: str, apply_changes: bool) -> SwitchResult:
        profile = self._find_profile(profile_name)
        adapter = self.adapter_factory(adapter_name)
        self._ensure_layer_supported("layer4")
        context = self._build_context(profile, transport, apply_changes)
        ok, reason = adapter.precheck(context)
        if not ok:
            return SwitchResult(False, reason)
        install_result = adapter.install(context)
        render_result = adapter.render_config(context)
        write_audit_log(
            "install",
            profile_name,
            {"adapter": adapter_name, "transport": transport, "install": install_result, "render": render_result},
            self.paths.audit_path,
        )
        return SwitchResult(True, "Adapter prepared", actions=["install", "render_config"])

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

            ok, reason = adapter.precheck(context)
            if not ok:
                return SwitchResult(False, reason)

            adapter.render_config(context)
            old_adapter_name = record.active_adapter
            if old_adapter_name:
                old_adapter = self.adapter_factory(old_adapter_name)
                old_context = self._build_context(profile, record.active_transport or transport, apply_changes)
                old_adapter.stop(old_context)
                old_adapter.cleanup_runtime(old_context)
                self.registry.release(profile.main_port, profile.name)

            self.registry.claim(profile.main_port, profile.name, adapter_name, transport)
            adapter.start(context)
            healthy, message = adapter.healthcheck(context)
            if not healthy:
                self.state = backup_state
                self.registry = backup_registry
                if old_adapter_name:
                    old_adapter = self.adapter_factory(old_adapter_name)
                    old_context = self._build_context(profile, record.active_transport or transport, apply_changes)
                    old_adapter.start(old_context)
                write_audit_log(
                    "switch_failed",
                    profile_name,
                    {"adapter": adapter_name, "transport": transport, "reason": message},
                    self.paths.audit_path,
                )
                return SwitchResult(False, f"Switch failed healthcheck: {message}", actions=["rollback"])

            record.active_adapter = adapter_name
            record.active_transport = transport
            record.active_layer = metadata.layer
            record.service_name = f"pilottunnel-{profile.name}-{adapter_name}.service"
            record.healthy = True
            record.last_error = ""
            record.last_switch_at = self.now_provider().isoformat()
            profile.active_adapter = adapter_name
            profile.active_transport = transport
            profile.active_layer = metadata.layer
            write_audit_log(
                "switch",
                profile_name,
                {"adapter": adapter_name, "transport": transport, "message": message},
                self.paths.audit_path,
            )
            return SwitchResult(True, message, actions=["switch"])

    def rollback(self, profile_name: str, apply_changes: bool) -> SwitchResult:
        profile = self._find_profile(profile_name)
        with profile_lock(profile_name, self.paths.lock_dir):
            record = self._record_for(profile_name)
            snapshot = record.rollback_snapshot
            if not snapshot:
                return SwitchResult(False, "No rollback snapshot available")
            previous_adapter = snapshot.get("active_adapter", "")
            previous_transport = snapshot.get("active_transport", "")
            if not previous_adapter:
                self.registry.release(profile.main_port, profile.name)
                record.active_adapter = ""
                record.active_transport = ""
                record.healthy = False
                return SwitchResult(True, "Rolled back to empty state", actions=["rollback"])
            return self.switch(profile_name, previous_adapter, previous_transport, apply_changes)

    def status(self, profile_name: str) -> dict:
        record = self._record_for(profile_name)
        return asdict(record)

    def healthcheck(self, profile_name: str) -> SwitchResult:
        profile = self._find_profile(profile_name)
        record = self._record_for(profile_name)
        if not record.active_adapter:
            return SwitchResult(False, "No active adapter")
        adapter = self.adapter_factory(record.active_adapter)
        context = self._build_context(profile, record.active_transport, apply_changes=False)
        healthy, message = adapter.healthcheck(context)
        record.healthy = healthy
        record.last_error = "" if healthy else message
        return SwitchResult(healthy, message)

    def cleanup(self, profile_name: str, apply_changes: bool, dry_run: bool) -> SwitchResult:
        profile = self._find_profile(profile_name)
        record = self._record_for(profile_name)
        actions = ["cleanup_runtime"]
        if record.active_adapter:
            adapter = self.adapter_factory(record.active_adapter)
            context = self._build_context(profile, record.active_transport or "tcp", apply_changes and not dry_run)
            adapter.cleanup_runtime(context)
        return SwitchResult(True, "Cleanup prepared", actions=actions)

    def _build_context(self, profile: Profile, transport: str, apply_changes: bool) -> AdapterContext:
        return AdapterContext(
            profile_name=profile.name,
            main_port=profile.main_port,
            target_host=profile.target_host,
            target_port=profile.target_port,
            transport=transport,
            work_dir=self.paths.work_dir / profile.name,
            apply_changes=apply_changes,
        )
