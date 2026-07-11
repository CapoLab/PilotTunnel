import unittest
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from pathlib import Path

from pilottunnel.probe import _encode_frame, _recv_frame, build_benchmark_message, parse_benchmark_message, probe_roundtrip, run_echo_responder
from pilottunnel.candidates import _benchmark_probe_secret, _candidate_runtime_config_status, _candidate_runtime_fingerprint, benchmark_readiness
from pilottunnel.config import LinkCandidate, LinkProfile


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
            raw = probe_roundtrip(host="127.0.0.1", port=port, timeout=0.2)
            self.assertFalse(raw.ok)
            rejected = probe_roundtrip(host="127.0.0.1", port=port, timeout=0.2, secret=b"wrong-secret")
            self.assertFalse(rejected.ok)

    def test_cli_responder_accepts_production_derived_secret(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        link = LinkProfile(
            id="ptlink-test-001",
            label="link-test-001",
            iran_address="controller.example.invalid",
            kharej_address="worker.example.invalid",
            tunnel_port=42001,
            config_port=42002,
            pairing_secret="shared-test-pairing-secret",
        )
        secret = _benchmark_probe_secret(link, "rathole")
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_file = Path(temp_dir) / "benchmark-probe.secret"
            secret_file.write_bytes(secret)
            if sys.platform != "win32":
                secret_file.chmod(0o600)
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "pilottunnel.probe",
                    "responder",
                    "--bind-host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--accept-timeout",
                    "0.05",
                    "--secret-file",
                    str(secret_file),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                result = None
                for _ in range(20):
                    result = probe_roundtrip(host="127.0.0.1", port=port, timeout=0.25, secret=secret)
                    if result.ok:
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(result)
                self.assertTrue(result.ok, msg=result.error if result else "no probe result")
                self.assertTrue(result.exact_match)
                self.assertLess(result.roundtrip_latency_ms or float("inf"), 250.0)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                if process.stderr is not None:
                    process.stderr.close()

    def test_secret_file_responder_accepts_fragmented_authenticated_frame(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        secret = b"fragmented-secret"
        with tempfile.TemporaryDirectory() as temp_dir:
            secret_file = Path(temp_dir) / "probe.secret"
            secret_file.write_bytes(secret)
            thread = threading.Thread(
                target=run_echo_responder,
                kwargs={"bind_host": "127.0.0.1", "port": port, "accept_timeout": 0.05, "secret_file": str(secret_file)},
                daemon=True,
            )
            thread.start()
            time.sleep(0.05)
            message = build_benchmark_message(action="probe", payload={"nonce": "fragmented"}, secret=secret)
            frame = _encode_frame(message, 8192)
            with socket.create_connection(("127.0.0.1", port), timeout=1.0) as conn:
                conn.settimeout(1.0)
                for byte in frame:
                    conn.sendall(bytes([byte]))
                response = _recv_frame(conn, 8192)
            self.assertEqual(response, message)
            self.assertEqual(parse_benchmark_message(message=response, secret=secret)[0], "probe")

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

    def test_legacy_rathole_probe_unit_without_secret_file_is_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = root / "rathole.toml"
            staged = root / "probe.service"
            target_dir = root / "systemd"
            target_dir.mkdir()
            runtime.write_text("[client]\n", encoding="utf-8")
            staged.write_text("ExecStart=python -m pilottunnel.probe responder --secret-file /safe/secret\n", encoding="utf-8")
            service_name = "pilottunnel-link-rathole-pilotunnel-probe-worker.service"
            (target_dir / service_name).write_text("ExecStart=python -m pilottunnel.probe responder\n", encoding="utf-8")
            candidate = LinkCandidate(
                adapter="rathole",
                transport="tcp",
                topology={"sides": {"worker": {"runtime_config_path": str(runtime), "services": [{"kind": "probe", "service_name": service_name, "unit_path": str(staged)}]}}},
            )
            active = {"runtime_fingerprint": _candidate_runtime_fingerprint(candidate, "worker")}
            with patch("pilottunnel.candidates.SYSTEMD_TARGET_DIR", target_dir):
                status = _candidate_runtime_config_status(candidate=candidate, role="worker", active_state=active)
            self.assertEqual(status["status"], "drifted")
            self.assertIn("missing the required benchmark secret file", " ".join(status["reasons"]))
