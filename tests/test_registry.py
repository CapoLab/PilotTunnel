import unittest

from pilottunnel.registry import PortRegistry, RegistryEntry
from testsupport import allocate_tcp_ports


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        ports, listeners = allocate_tcp_ports(4)
        self.main_port, self.alt_port, self.control_port, self.service_port = ports
        for listener in listeners:
            listener.close()

    def test_detects_port_conflicts(self) -> None:
        registry = PortRegistry()
        registry.claim(
            RegistryEntry(
                profile="smoke-l4-001",
                main_port=self.main_port,
                adapter="backhaul",
                transport="tcp",
                role="controller",
                owned_ports=[self.main_port, self.control_port, self.service_port, self.alt_port],
                owned_services=["svc-a"],
            )
        )
        with self.assertRaises(ValueError):
            registry.claim(
                RegistryEntry(
                    profile="smoke-l4-002",
                    main_port=self.alt_port,
                    adapter="rathole",
                    transport="tcp",
                    role="worker",
                    owned_ports=[self.alt_port, self.service_port],
                    owned_services=["svc-b"],
                )
            )

    def test_detects_same_main_port_used_by_two_profiles(self) -> None:
        registry = PortRegistry(
            owners={
                "smoke-l4-001": RegistryEntry(
                    profile="smoke-l4-001",
                    main_port=self.main_port,
                    adapter="backhaul",
                    transport="tcp",
                    role="controller",
                    owned_ports=[self.main_port],
                    owned_services=["svc-a"],
                ),
                "smoke-l4-002": RegistryEntry(
                    profile="smoke-l4-002",
                    main_port=self.main_port,
                    adapter="rathole",
                    transport="tcp",
                    role="worker",
                    owned_ports=[self.alt_port],
                    owned_services=["svc-b"],
                ),
            }
        )
        self.assertTrue(registry.check_conflicts())

    def test_detects_unsupported_transport_selected(self) -> None:
        registry = PortRegistry(
            owners={
                "smoke-l4-001": RegistryEntry(
                    profile="smoke-l4-001",
                    main_port=self.main_port,
                    adapter="backhaul",
                    transport="tcptun",
                    role="controller",
                    owned_ports=[self.main_port],
                    owned_services=["svc-a"],
                )
            }
        )
        self.assertTrue(any("Unsupported transport selected" in item for item in registry.check_conflicts()))
