import unittest
from pathlib import Path

from pilottunnel.adapters import ADAPTERS
from pilottunnel.adapters.base import AdapterContext
from pilottunnel.config import Profile, ProfilePorts
from testsupport import allocate_tcp_ports


class AdapterMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        ports, listeners = allocate_tcp_ports(5)
        self.main_port, self.target_port, self.control_port, self.service_port, self.check_port = ports
        for listener in listeners:
            listener.close()

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
            main_port=self.main_port,
            target_host="127.0.0.1",
            target_port=self.target_port,
            role="controller",
            ports=ProfilePorts(main_port=self.main_port, control_port=self.control_port, service_port=self.service_port, check_port=self.check_port),
        )
        controller = AdapterContext(profile=profile, transport="tcpmux", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="controller")
        worker = AdapterContext(profile=profile, transport="ws", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="worker")
        controller_render = adapter.render_config(controller)
        worker_render = adapter.render_config(worker)
        self.assertIn("[server]", controller_render["content"])
        self.assertIn('bind_addr = "0.0.0.0:', controller_render["content"])
        self.assertIn('transport = "tcpmux"', controller_render["content"])
        self.assertIn("[client]", worker_render["content"])
        self.assertIn('remote_addr = "127.0.0.1:', worker_render["content"])
        self.assertIn('transport = "ws"', worker_render["content"])

    def test_backhaul_systemd_unit_naming(self) -> None:
        adapter = ADAPTERS["backhaul"]()
        profile = Profile(name="smoke-l4-001", main_port=self.main_port, target_host="127.0.0.1", target_port=self.target_port)
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
            main_port=self.main_port,
            target_host="127.0.0.1",
            target_port=self.target_port,
            role="controller",
            ports=ProfilePorts(main_port=self.main_port, control_port=self.control_port),
        )
        controller = AdapterContext(profile=profile, transport="tcp", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="controller")
        worker = AdapterContext(profile=profile, transport="tcp", work_dir=Path("/tmp"), staging_root=Path("/tmp/staging"), role="worker")
        controller_content = adapter.render_config(controller)["content"]
        worker_content = adapter.render_config(worker)["content"]
        self.assertIn("[server]", controller_content)
        self.assertIn("[server.services.smoke_l4_001]", controller_content)
        self.assertIn('bind_addr = "0.0.0.0:', controller_content)
        self.assertIn("[client]", worker_content)
        self.assertIn("[client.services.smoke_l4_001]", worker_content)
        self.assertIn('local_addr = "127.0.0.1:', worker_content)

    def test_rathole_systemd_unit_naming(self) -> None:
        adapter = ADAPTERS["rathole"]()
        profile = Profile(name="smoke-l4-001", main_port=self.main_port, target_host="127.0.0.1", target_port=self.target_port)
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
