import unittest
import socket
import tempfile
import threading
import time
from pathlib import Path

from pilottunnel.probe import build_benchmark_message, parse_benchmark_message, probe_roundtrip, run_echo_responder
from pilottunnel.candidates import benchmark_readiness
from pilottunnel.config import LinkCandidate


class BenchmarkProbeProtocolTests(unittest.TestCase):
    def test_authenticated_report_round_trip(self) -> None:
        secret = b"test-secret"
        message = build_benchmark_message(action="report", payload={"ok": True}, secret=secret)
        action, payload = parse_benchmark_message(message=message, secret=secret)
        self.assertEqual(action, "report")
        self.assertEqual(payload, {"ok": True})

    def test_rejects_tampered_or_unsupported_messages(self) -> None:
        secret = b"test-secret"
        message = build_benchmark_message(action="probe", payload={}, secret=secret)
        with self.assertRaises(ValueError):
            parse_benchmark_message(message=message[:-1] + b"x", secret=secret)
        with self.assertRaises(ValueError):
            build_benchmark_message(action="shell", payload={}, secret=secret)

    def test_secret_file_responder_requires_authenticated_probe(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_file = Path(temp_dir) / "probe.secret"
            secret_file.write_bytes(b"test-secret")
            thread = threading.Thread(
                target=run_echo_responder,
                kwargs={"bind_host": "127.0.0.1", "port": port, "accept_timeout": 0.05, "secret_file": str(secret_file)},
                daemon=True,
            )
            thread.start()
            time.sleep(0.05)
            result = probe_roundtrip(host="127.0.0.1", port=port, timeout=1.0, secret=b"test-secret")
            self.assertTrue(result.ok)
            rejected = probe_roundtrip(host="127.0.0.1", port=port, timeout=0.2, secret=b"wrong-secret")
            self.assertFalse(rejected.ok)

    def test_rathole_readiness_requires_a_runnable_two_sided_path(self) -> None:
        candidate = LinkCandidate(
            adapter="rathole",
            transport="tcp",
            runnable=True,
            topology={"category": "two_sided_tunnel", "real_service_path": "controller -> worker"},
            probe={"path": "controller probe -> worker probe"},
        )
        readiness = benchmark_readiness(candidate)
        self.assertTrue(readiness["benchmark_capable"])
        self.assertEqual(readiness["authentication_status"], "authenticated_hmac_probe")
