import unittest

from pilottunnel.port_mapping import parse_port_mapping


class PortMappingTests(unittest.TestCase):
    def test_valid_examples(self) -> None:
        examples = [
            "443",
            "4000=5000",
            "443-600:39080",
            "443=1.1.1.1:39080",
            "127.0.0.2:443=39080",
        ]
        for value in examples:
            with self.subTest(value=value):
                mapping = parse_port_mapping(value)
                self.assertGreaterEqual(mapping.listen_start, 1)

    def test_invalid_examples(self) -> None:
        examples = [
            "",
            "abc",
            "443-400",
            "127.0.0.2:443-600=39080",
            "443=",
            "70000",
        ]
        for value in examples:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_port_mapping(value)
