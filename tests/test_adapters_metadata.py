import unittest

from pilottunnel.adapters import ADAPTERS


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
            self.assertTrue(meta.transports)
