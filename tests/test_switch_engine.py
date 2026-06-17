import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pilottunnel.adapters.base import AdapterContext, AdapterMetadata
from pilottunnel.config import AppConfig, Profile
from pilottunnel.registry import PortRegistry, RegistryEntry
from pilottunnel.state import AppState, RuntimeRecord
from pilottunnel.switch_engine import SwitchEngine, SwitchPaths


@dataclass
class StubAdapter:
    name: str
    events: list[str]
    healthy: bool = True
    layer: str = "layer4"

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(name=self.name, layer=self.layer, transports=("tcp",))

    def precheck(self, context: AdapterContext) -> tuple[bool, str]:
        self.events.append(f"{self.name}:precheck")
        return True, "ok"

    def install(self, context: AdapterContext) -> dict:
        self.events.append(f"{self.name}:install")
        return {}

    def render_config(self, context: AdapterContext) -> dict:
        self.events.append(f"{self.name}:render")
        return {}

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
    def _engine(self, adapters: dict[str, StubAdapter], state: AppState | None = None) -> SwitchEngine:
        config = AppConfig(
            profiles=[
                Profile(
                    name="turkey-6221",
                    main_port=6221,
                    target_host="127.0.0.1",
                    target_port=6221,
                    active_adapter="backhaul",
                    active_transport="tcp",
                )
            ]
        )
        paths = SwitchPaths(
            lock_dir=Path(tempfile.gettempdir()) / "pilottunnel-locks",
            work_dir=Path(tempfile.gettempdir()) / "pilottunnel-work",
            audit_path=Path(tempfile.gettempdir()) / "pilottunnel-audit.log",
        )
        if state is None:
            state = AppState(
                profiles={
                    "turkey-6221": RuntimeRecord(
                        profile="turkey-6221",
                        active_adapter="backhaul",
                        active_transport="tcp",
                        healthy=True,
                        last_switch_at=(datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat(),
                    )
                }
            )
        return SwitchEngine(
            config=config,
            state=state,
            registry=PortRegistry(
                owners={"6221": RegistryEntry(profile="turkey-6221", adapter="backhaul", transport="tcp")}
            ),
            paths=paths,
            adapter_factory=lambda name: adapters[name],
            now_provider=lambda: datetime.now(timezone.utc),
        )

    def test_only_one_active_owner_per_main_port(self) -> None:
        events: list[str] = []
        adapters = {"backhaul": StubAdapter("backhaul", events), "frp": StubAdapter("frp", events)}
        engine = self._engine(adapters)
        result = engine.switch("turkey-6221", "frp", "tcp", apply_changes=False)
        self.assertTrue(result.ok)
        self.assertEqual(engine.registry.owners["6221"].adapter, "frp")

    def test_old_tunnel_stop_happens_before_new_commit(self) -> None:
        events: list[str] = []
        adapters = {"backhaul": StubAdapter("backhaul", events), "frp": StubAdapter("frp", events)}
        engine = self._engine(adapters)
        result = engine.switch("turkey-6221", "frp", "tcp", apply_changes=False)
        self.assertTrue(result.ok)
        self.assertLess(events.index("backhaul:stop"), events.index("frp:start"))

    def test_rollback_restores_previous_state_on_failed_healthcheck(self) -> None:
        events: list[str] = []
        adapters = {"backhaul": StubAdapter("backhaul", events), "frp": StubAdapter("frp", events, healthy=False)}
        engine = self._engine(adapters)
        result = engine.switch("turkey-6221", "frp", "tcp", apply_changes=False)
        self.assertFalse(result.ok)
        self.assertEqual(engine.registry.owners["6221"].adapter, "backhaul")
        self.assertIn("backhaul:start", events)
