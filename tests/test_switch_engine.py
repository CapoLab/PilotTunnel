import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pilottunnel.adapters.base import AdapterContext, AdapterMetadata
from pilottunnel.config import AppConfig, Profile, ProfilePorts
from pilottunnel.registry import PortRegistry, RegistryEntry
from pilottunnel.state import AppState, RuntimeRecord
from pilottunnel.switch_engine import SwitchEngine, SwitchPaths


@dataclass
class StubAdapter:
    name: str
    events: list[str]
    transports: tuple[str, ...]
    healthy: bool = True
    layer: str = "layer4"

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(name=self.name, layer=self.layer, transports=self.transports)

    def precheck(self, context: AdapterContext) -> tuple[bool, str]:
        self.events.append(f"{self.name}:precheck:{context.transport}")
        if context.transport not in self.transports:
            return False, "unsupported"
        return True, "ok"

    def install(self, context: AdapterContext) -> dict:
        self.events.append(f"{self.name}:install")
        return {}

    def render_config(self, context: AdapterContext) -> dict:
        self.events.append(f"{self.name}:render")
        return {}

    def render_systemd_unit(self, context: AdapterContext) -> dict:
        self.events.append(f"{self.name}:unit")
        return {"unit": {"unit_name": f"svc-{self.name}-{context.transport}-{context.role}"}}

    def service_name(self, context: AdapterContext) -> str:
        return f"svc-{self.name}-{context.transport}-{context.role}"

    def start(self, context: AdapterContext) -> dict:
        self.events.append(f"{self.name}:start")
        return {}

    def stop(self, context: AdapterContext) -> dict:
        self.events.append(f"{self.name}:stop")
        return {}

    def cleanup_runtime(self, context: AdapterContext) -> dict:
        self.events.append(f"{self.name}:cleanup")
        return {}

    def status(self, context: AdapterContext) -> dict:
        return {}

    def healthcheck(self, context: AdapterContext) -> tuple[bool, str]:
        self.events.append(f"{self.name}:healthcheck")
        return self.healthy, "healthy" if self.healthy else "unhealthy"

    def uninstall(self, context: AdapterContext) -> dict:
        return {}


class SwitchEngineTests(unittest.TestCase):
    def _engine(self, adapters: dict[str, StubAdapter], state: AppState | None = None) -> tuple[SwitchEngine, Path]:
        temp_dir = Path(tempfile.mkdtemp())
        config = AppConfig(
            profiles=[
                Profile(
                    name="turkey-6221",
                    main_port=6221,
                    target_host="127.0.0.1",
                    target_port=6221,
                    role="controller",
                    active_adapter="backhaul",
                    active_transport="tcp",
                    ports=ProfilePorts(main_port=6221, control_port=7001, service_port=7002, check_port=7003),
                )
            ]
        )
        paths = SwitchPaths(
            lock_dir=temp_dir / "locks",
            work_dir=temp_dir / "work",
            audit_path=temp_dir / "audit.log",
            staging_root=temp_dir / "staging",
        )
        if state is None:
            state = AppState(
                profiles={
                    "turkey-6221": RuntimeRecord(
                        profile="turkey-6221",
                        active_adapter="backhaul",
                        active_transport="tcp",
                        role="controller",
                        healthy=True,
                        last_switch_at=(datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
                    )
                }
            )
        engine = SwitchEngine(
            config=config,
            state=state,
            registry=PortRegistry(
                owners={
                    "turkey-6221": RegistryEntry(
                        profile="turkey-6221",
                        main_port=6221,
                        adapter="backhaul",
                        transport="tcp",
                        role="controller",
                        owned_ports=[6221, 7001, 7002, 7003],
                        owned_services=["svc-backhaul-tcp-controller"],
                    )
                }
            ),
            paths=paths,
            adapter_factory=lambda name: adapters[name],
            now_provider=lambda: datetime.now(timezone.utc),
        )
        return engine, temp_dir

    def test_manual_switch_to_backhaul_tcpmux_dry_run(self) -> None:
        events: list[str] = []
        adapters = {
            "backhaul": StubAdapter("backhaul", events, transports=("tcp", "tcpmux")),
            "rathole": StubAdapter("rathole", events, transports=("tcp",)),
        }
        engine, _ = self._engine(adapters)
        result = engine.switch("turkey-6221", "backhaul", "tcpmux", apply_changes=False)
        self.assertTrue(result.ok)
        self.assertTrue(result.dry_run)
        self.assertEqual(engine.registry.owners["turkey-6221"].transport, "tcpmux")

    def test_manual_switch_to_rathole_tcp_dry_run(self) -> None:
        events: list[str] = []
        adapters = {
            "backhaul": StubAdapter("backhaul", events, transports=("tcp", "tcpmux")),
            "rathole": StubAdapter("rathole", events, transports=("tcp",)),
        }
        engine, _ = self._engine(adapters)
        result = engine.switch("turkey-6221", "rathole", "tcp", apply_changes=False)
        self.assertTrue(result.ok)
        self.assertEqual(engine.registry.owners["turkey-6221"].adapter, "rathole")

    def test_old_tunnel_stop_happens_before_new_commit(self) -> None:
        events: list[str] = []
        adapters = {
            "backhaul": StubAdapter("backhaul", events, transports=("tcp", "tcpmux")),
            "rathole": StubAdapter("rathole", events, transports=("tcp",)),
        }
        engine, _ = self._engine(adapters)
        result = engine.switch("turkey-6221", "rathole", "tcp", apply_changes=False)
        self.assertTrue(result.ok)
        self.assertLess(events.index("backhaul:stop"), events.index("rathole:start"))

    def test_rollback_restores_previous_active_tunnel_on_failed_healthcheck(self) -> None:
        events: list[str] = []
        adapters = {
            "backhaul": StubAdapter("backhaul", events, transports=("tcp", "tcpmux")),
            "rathole": StubAdapter("rathole", events, transports=("tcp",), healthy=False),
        }
        engine, _ = self._engine(adapters)
        result = engine.switch("turkey-6221", "rathole", "tcp", apply_changes=False)
        self.assertFalse(result.ok)
        self.assertTrue(result.rollback_performed)
        self.assertEqual(engine.registry.owners["turkey-6221"].adapter, "backhaul")
        self.assertIn("backhaul:start", events)

    def test_audit_records_dry_run_switch_metadata(self) -> None:
        events: list[str] = []
        adapters = {
            "backhaul": StubAdapter("backhaul", events, transports=("tcp", "tcpmux")),
            "rathole": StubAdapter("rathole", events, transports=("tcp",)),
        }
        engine, temp_dir = self._engine(adapters)
        result = engine.switch("turkey-6221", "rathole", "tcp", apply_changes=False)
        self.assertTrue(result.ok)
        lines = (temp_dir / "audit.log").read_text(encoding="utf-8").splitlines()
        payload = json.loads(lines[-1])
        self.assertTrue(payload["details"]["dry_run"])
        self.assertEqual(payload["details"]["to_adapter"], "rathole")

    def test_switch_transaction_rolls_back_if_staged_healthcheck_fails(self) -> None:
        events: list[str] = []
        adapters = {
            "backhaul": StubAdapter("backhaul", events, transports=("tcp", "tcpmux")),
            "rathole": StubAdapter("rathole", events, transports=("tcp",), healthy=False),
        }
        engine, temp_dir = self._engine(adapters)
        engine.paths = SwitchPaths(
            lock_dir=engine.paths.lock_dir,
            work_dir=engine.paths.work_dir,
            audit_path=engine.paths.audit_path,
            staging_root=temp_dir / "staging",
        )
        result = engine.switch("turkey-6221", "rathole", "tcp", apply_changes=True)
        self.assertFalse(result.ok)
        self.assertTrue(result.rollback_performed)
        self.assertEqual(engine.registry.owners["turkey-6221"].adapter, "backhaul")
