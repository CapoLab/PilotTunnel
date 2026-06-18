import unittest

from pilottunnel.registry import PortRegistry, RegistryEntry


class RegistryTests(unittest.TestCase):
    def test_detects_port_conflicts(self) -> None:
        registry = PortRegistry()
        registry.claim(
            RegistryEntry(
                profile="smoke-l4-001",
                main_port=38080,
                adapter="backhaul",
                transport="tcp",
                role="controller",
                owned_ports=[38080, 39081, 39082, 39083],
                owned_services=["svc-a"],
            )
        )
        with self.assertRaises(ValueError):
            registry.claim(
                RegistryEntry(
                    profile="smoke-l4-002",
                    main_port=7443,
                    adapter="rathole",
                    transport="tcp",
                    role="worker",
                    owned_ports=[7443, 39082],
                    owned_services=["svc-b"],
                )
            )

    def test_detects_same_main_port_used_by_two_profiles(self) -> None:
        registry = PortRegistry(
            owners={
                "smoke-l4-001": RegistryEntry(
                    profile="smoke-l4-001",
                    main_port=38080,
                    adapter="backhaul",
                    transport="tcp",
                    role="controller",
                    owned_ports=[38080],
                    owned_services=["svc-a"],
                ),
                "smoke-l4-002": RegistryEntry(
                    profile="smoke-l4-002",
                    main_port=38080,
                    adapter="rathole",
                    transport="tcp",
                    role="worker",
                    owned_ports=[7443],
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
                    main_port=38080,
                    adapter="backhaul",
                    transport="tcptun",
                    role="controller",
                    owned_ports=[38080],
                    owned_services=["svc-a"],
                )
            }
        )
        self.assertTrue(any("Unsupported transport selected" in item for item in registry.check_conflicts()))
