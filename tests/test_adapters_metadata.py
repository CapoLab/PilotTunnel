import unittest
from pathlib import Path

from pilottunnel.adapters import ADAPTERS
from pilottunnel.adapters.base import AdapterContext
from pilottunnel.config import Profile, ProfilePorts


class AdapterMetadataTests(unittest.TestCase):
    def test_all_adapters_expose_metadata(self) -> None:
        expected = {
            "backhaul",
            "rathole",
            "frp",
            "gost",
            "chisel",
            "realm",
            "wstunnel",
            "bore",
            "ssh_reverse",
            "udp2raw",
        }
        self.assertEqual(set(ADAPTERS), expected)
        for name, adapter_cls in ADAPTERS.items():
            meta = adapter_cls().metadata()
            self.assertEqual(meta.name, name)
            self.assertTrue(meta.all_transports())

    def test_backhaul_config_rendering_for_controller_and_worker(self) -> None:
        adapter = ADAPTERS["backhaul"]()
        profile = Profile(
            name="smoke-l4-001",
            main_port=38080,
            target_host="127.0.0.1",
            target_port=39080,
            role="controller",
            ports=ProfilePorts(main_port=38080, control_port=39081, service_port=39082, check_port=39083),
        )
        controller = AdapterContext(profile=profile, transport="tcpmux", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="controller")
        worker = AdapterContext(profile=profile, transport="ws", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="worker")
        controller_render = adapter.render_config(controller)
        worker_render = adapter.render_config(worker)
        self.assertIn("role = controller", controller_render["content"])
        self.assertIn("transport = tcpmux", controller_render["content"])
        self.assertIn("role = worker", worker_render["content"])
        self.assertIn("transport = ws", worker_render["content"])

    def test_backhaul_systemd_unit_naming(self) -> None:
        adapter = ADAPTERS["backhaul"]()
        profile = Profile(name="smoke-l4-001", main_port=38080, target_host="127.0.0.1", target_port=39080)
        controller = AdapterContext(profile=profile, transport="tcp", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="controller")
        worker = AdapterContext(profile=profile, transport="tcp", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="worker")
        self.assertEqual(
            adapter.render_systemd_unit(controller)["unit"]["unit_name"],
            "pilottunnel-smoke-l4-001-backhaul-tcp-controller.service",
        )
        self.assertEqual(
            adapter.render_systemd_unit(worker)["unit"]["unit_name"],
            "pilottunnel-smoke-l4-001-backhaul-tcp-worker.service",
        )

    def test_rathole_config_rendering_for_controller_and_worker(self) -> None:
        adapter = ADAPTERS["rathole"]()
        profile = Profile(
            name="smoke-l4-001",
            main_port=38080,
            target_host="127.0.0.1",
            target_port=39080,
            role="controller",
            ports=ProfilePorts(main_port=38080, control_port=39081),
        )
        controller = AdapterContext(profile=profile, transport="tcp", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="controller")
        worker = AdapterContext(profile=profile, transport="tcp", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="worker")
        self.assertIn("role = controller", adapter.render_config(controller)["content"])
        self.assertIn("role = worker", adapter.render_config(worker)["content"])

    def test_rathole_systemd_unit_naming(self) -> None:
        adapter = ADAPTERS["rathole"]()
        profile = Profile(name="smoke-l4-001", main_port=38080, target_host="127.0.0.1", target_port=39080)
        controller = AdapterContext(profile=profile, transport="tcp", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="controller")
        worker = AdapterContext(profile=profile, transport="tcp", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="worker")
        self.assertEqual(
            adapter.render_systemd_unit(controller)["unit"]["unit_name"],
            "pilottunnel-smoke-l4-001-rathole-tcp-controller.service",
        )
        self.assertEqual(
            adapter.render_systemd_unit(worker)["unit"]["unit_name"],
            "pilottunnel-smoke-l4-001-rathole-tcp-worker.service",
        )
