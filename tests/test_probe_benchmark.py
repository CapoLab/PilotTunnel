import socket
import subprocess
import sys
import time
import unittest

from pilottunnel.probe import probe_roundtrip


class RatholeProbeRegressionTests(unittest.TestCase):
    def test_cli_responder_returns_nonce_exact_match(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
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
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            result = None
            for _ in range(20):
                result = probe_roundtrip(host="127.0.0.1", port=port, timeout=0.25)
                if result.ok:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(result)
            self.assertTrue(result.ok, msg=result.error if result else "no probe result")
            self.assertTrue(result.exact_match)
        finally:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            if process.stderr is not None:
                process.stderr.close()
