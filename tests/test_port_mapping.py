import unittest

from pilottunnel.port_mapping import parse_port_mapping
from testsupport import allocate_tcp_ports


class PortMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        ports, listeners = allocate_tcp_ports(2)
        self.example_port, self.second_port = ports
        for listener in listeners:
            listener.close()

    def test_valid_examples(self) -> None:
        listen_range_end = self.example_port + 1 if self.example_port < 65535 else self.example_port
        examples = [
            str(self.example_port),
            f"{self.example_port}={self.second_port}",
            f"{self.example_port}-{listen_range_end}:{self.second_port}",
            f"{self.example_port}=1.1.1.1:{self.second_port}",
            f"127.0.0.2:{self.example_port}={self.second_port}",
        ]
        for value in examples:
            with self.subTest(value=value):
                mapping = parse_port_mapping(value)
                self.assertGreaterEqual(mapping.listen_start, 1)

    def test_invalid_examples(self) -> None:
        invalid_range = f"{self.example_port + 1}-{self.example_port}" if self.example_port < 65535 else f"{self.example_port}-{self.example_port - 1}"
        examples = [
            "",
            "abc",
            invalid_range,
            f"127.0.0.2:{self.example_port + 1}-{self.example_port}={self.second_port}",
            f"{self.example_port}=",
            str(65536 + (self.example_port % 1000)),
        ]
        for value in examples:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_port_mapping(value)
