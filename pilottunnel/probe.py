"""PilotTunnel-owned echo probe helpers for real candidate smoke tests."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import socket
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

DEFAULT_PROBE_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_PAYLOAD_BYTES = 8192
PROBE_MAGIC = b"PTPROBE1:"
BENCHMARK_MAGIC = b"PTBENCH1:"
BENCHMARK_ACTIONS = frozenset({"probe", "report", "finalize"})


def build_benchmark_message(*, action: str, payload: dict, secret: bytes) -> bytes:
    """Create a narrow authenticated benchmark message, never a command channel."""
    if action not in BENCHMARK_ACTIONS:
        raise ValueError("Unsupported benchmark probe action")
    if not secret:
        raise ValueError("Benchmark probe secret is required")
    body = json.dumps({"action": action, "payload": payload}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest().encode("ascii")
    return BENCHMARK_MAGIC + signature + b":" + body


def parse_benchmark_message(*, message: bytes, secret: bytes) -> tuple[str, dict]:
    """Validate a bounded benchmark-only request without executing anything."""
    if not secret or not message.startswith(BENCHMARK_MAGIC):
        raise ValueError("Invalid benchmark probe message")
    try:
        signature, body = message[len(BENCHMARK_MAGIC):].split(b":", 1)
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest().encode("ascii")
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed benchmark probe message") from exc
    if not hmac.compare_digest(signature, expected):
        raise ValueError("Benchmark probe authentication failed")
    action = decoded.get("action")
    payload = decoded.get("payload")
    if action not in BENCHMARK_ACTIONS or not isinstance(payload, dict):
        raise ValueError("Invalid benchmark probe action")
    return action, payload


@dataclass
class ProbeAttemptResult:
    ok: bool
    host: str
    port: int
    timeout: float
    connect_latency_ms: float | None
    roundtrip_latency_ms: float | None
    error: str
    checked_at: str
    bytes_sent: int
    bytes_received: int
    exact_match: bool

    def to_dict(self) -> dict:
        return asdict(self)


def run_echo_responder(
    *,
    bind_host: str,
    port: int,
    accept_timeout: float = 1.0,
    io_timeout: float = 5.0,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    secret_file: str | None = None,
) -> None:
    _validate_endpoint(bind_host, port)
    if accept_timeout <= 0 or io_timeout <= 0:
        raise ValueError("Probe responder timeouts must be greater than 0")
    if max_payload_bytes < len(PROBE_MAGIC) + 1:
        raise ValueError("Probe responder max payload must be large enough for a nonce")

    secret = _load_secret_file(secret_file) if secret_file else b""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((bind_host, port))
        listener.listen()
        listener.settimeout(accept_timeout)
        while True:
            try:
                conn, _addr = listener.accept()
            except socket.timeout:
                continue
            thread = threading.Thread(
                target=_handle_connection,
                args=(conn, io_timeout, max_payload_bytes, secret),
                daemon=True,
            )
            thread.start()


def probe_roundtrip(*, host: str, port: int, timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS, secret: bytes | None = None) -> ProbeAttemptResult:
    _validate_endpoint(host, port)
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0")
    nonce = PROBE_MAGIC + secrets.token_bytes(32)
    expected = build_benchmark_message(action="probe", payload={"nonce": nonce.hex()}, secret=secret) if secret else nonce
    started = time.perf_counter()
    connect_latency_ms: float | None = None
    try:
        with socket.create_connection((host, port), timeout=timeout) as conn:
            connect_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            conn.settimeout(timeout)
            conn.sendall(expected)
            if not secret:
                conn.shutdown(socket.SHUT_WR)
            received = _recv_all(conn, DEFAULT_MAX_PAYLOAD_BYTES)
            roundtrip_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            exact_match = received == expected
            if not exact_match:
                if received.startswith(PROBE_MAGIC):
                    error = "Probe response nonce mismatch"
                elif not received:
                    error = "Probe response was empty"
                else:
                    error = "Probe response did not match the expected PilotTunnel payload"
                return ProbeAttemptResult(
                    ok=False,
                    host=host,
                    port=port,
                    timeout=timeout,
                    connect_latency_ms=connect_latency_ms,
                    roundtrip_latency_ms=roundtrip_latency_ms,
                    error=error,
                    checked_at=_now_utc(),
                    bytes_sent=len(expected),
                    bytes_received=len(received),
                    exact_match=False,
                )
            return ProbeAttemptResult(
                ok=True,
                host=host,
                port=port,
                timeout=timeout,
                connect_latency_ms=connect_latency_ms,
                roundtrip_latency_ms=roundtrip_latency_ms,
                error="",
                checked_at=_now_utc(),
                bytes_sent=len(expected),
                bytes_received=len(received),
                exact_match=True,
            )
    except TimeoutError:
        return ProbeAttemptResult(
            ok=False,
            host=host,
            port=port,
            timeout=timeout,
            connect_latency_ms=connect_latency_ms,
            roundtrip_latency_ms=None,
            error="Probe timed out",
            checked_at=_now_utc(),
            bytes_sent=len(expected),
            bytes_received=0,
            exact_match=False,
        )
    except OSError as exc:
        return ProbeAttemptResult(
            ok=False,
            host=host,
            port=port,
            timeout=timeout,
            connect_latency_ms=connect_latency_ms,
            roundtrip_latency_ms=None,
            error=str(exc),
            checked_at=_now_utc(),
            bytes_sent=len(expected),
            bytes_received=0,
            exact_match=False,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pilottunnel.probe")
    subparsers = parser.add_subparsers(dest="command", required=True)

    responder = subparsers.add_parser("responder")
    responder.add_argument("--bind-host", required=True)
    responder.add_argument("--port", type=int, required=True)
    responder.add_argument("--accept-timeout", type=float, default=1.0)
    responder.add_argument("--io-timeout", type=float, default=5.0)
    responder.add_argument("--max-payload-bytes", type=int, default=DEFAULT_MAX_PAYLOAD_BYTES)
    responder.add_argument("--secret-file")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "responder":
        run_echo_responder(
            bind_host=args.bind_host,
            port=args.port,
            accept_timeout=args.accept_timeout,
            io_timeout=args.io_timeout,
            max_payload_bytes=args.max_payload_bytes,
            secret_file=args.secret_file,
        )
        return 0
    parser.error("Unhandled command")
    return 2


def _handle_connection(conn: socket.socket, io_timeout: float, max_payload_bytes: int, secret: bytes) -> None:
    with conn:
        conn.settimeout(io_timeout)
        # Benchmark frames are bounded single messages. Reading one frame avoids
        # waiting for a peer half-close before sending the authenticated reply.
        data = conn.recv(max_payload_bytes) if secret else _recv_all(conn, max_payload_bytes)
        if not data:
            return
        if len(data) > max_payload_bytes:
            return
        try:
            if secret:
                parse_benchmark_message(message=data, secret=secret)
                # These actions only acknowledge benchmark data flow; they never
                # select or execute host commands.
                conn.sendall(data)
            else:
                conn.sendall(data)
        except (OSError, ValueError):
            return


def _recv_all(conn: socket.socket, max_payload_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_payload_bytes:
            return b""
        chunks.append(chunk)


def _validate_endpoint(host: str, port: int) -> None:
    if not host or host.strip() != host:
        raise ValueError("host must be a non-empty value")
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")


def _load_secret_file(secret_file: str) -> bytes:
    path = Path(secret_file)
    if not path.is_file() or path.is_symlink():
        raise ValueError("Benchmark probe secret file is unavailable")
    secret = path.read_bytes().strip()
    if not secret:
        raise ValueError("Benchmark probe secret file is empty")
    return secret


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
