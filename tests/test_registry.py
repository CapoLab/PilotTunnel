import unittest

from pilottunnel.registry import PortRegistry


class RegistryTests(unittest.TestCase):
    def test_detects_port_conflicts(self) -> None:
        registry = PortRegistry()
        registry.claim(6221, "turkey-6221", "backhaul", "tcp")
        with self.assertRaises(ValueError):
            registry.claim(6221, "germany-6221", "frp", "tcp")

    def test_detects_profile_multi_port_conflict(self) -> None:
        registry = PortRegistry()
        registry.claim(6221, "turkey-6221", "backhaul", "tcp")
        registry.claim(7443, "turkey-6221", "backhaul", "tcp")
        self.assertTrue(registry.check_conflicts())
