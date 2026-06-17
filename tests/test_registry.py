import unittest

from pilottunnel.registry import PortRegistry, RegistryEntry


class RegistryTests(unittest.TestCase):
    def test_detects_port_conflicts(self) -> None:
        registry = PortRegistry()
        registry.claim(
            RegistryEntry(
                profile="turkey-6221",
                main_port=6221,
                adapter="backhaul",
                transport="tcp",
                role="controller",
                owned_ports=[6221, 7001, 7002, 7003],
                owned_services=["svc-a"],
            )
        )
        with self.assertRaises(ValueError):
            registry.claim(
                RegistryEntry(
                    profile="germany-6221",
                    main_port=7443,
                    adapter="rathole",
                    transport="tcp",
                    role="worker",
                    owned_ports=[7443, 7002],
                    owned_services=["svc-b"],
                )
            )

    def test_detects_same_main_port_used_by_two_profiles(self) -> None:
        registry = PortRegistry(
            owners={
                "turkey-6221": RegistryEntry(
                    profile="turkey-6221",
                    main_port=6221,
                    adapter="backhaul",
                    transport="tcp",
                    role="controller",
                    owned_ports=[6221],
                    owned_services=["svc-a"],
                ),
                "germany-6221": RegistryEntry(
                    profile="germany-6221",
                    main_port=6221,
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
                "turkey-6221": RegistryEntry(
                    profile="turkey-6221",
                    main_port=6221,
                    adapter="backhaul",
                    transport="tcptun",
                    role="controller",
                    owned_ports=[6221],
                    owned_services=["svc-a"],
                )
            }
        )
        self.assertTrue(any("Unsupported transport selected" in item for item in registry.check_conflicts()))
