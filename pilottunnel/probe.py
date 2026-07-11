"""PilotTunnel-owned echo probe helpers for real candidate smoke tests."""

from __future__ import annotations

import argparse
import secrets
import socket
import struct
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

DEFAULT_PROBE_TIMEOUT_SECONDS = 2.0
DEFAULT_MAX_PAYLOAD_BYTES = 8192
PROBE_MAGIC = b"PTPROBE1:"
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
) -> None:
    _validate_endpoint(bind_host, port)
    if accept_timeout <= 0 or io_timeout <= 0:
        raise ValueError("Probe responder timeouts must be greater than 0")
    if max_payload_bytes < len(PROBE_MAGIC) + 1:
        raise ValueError("Probe responder max payload must be large enough for a nonce")

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
                args=(conn, io_timeout, max_payload_bytes),
                daemon=True,
            )
            thread.start()


def probe_roundtrip(*, host: str, port: int, timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS) -> ProbeAttemptResult:
    _validate_endpoint(host, port)
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0")
    nonce = PROBE_MAGIC + secrets.token_bytes(32)
    expected = nonce
    started = time.perf_counter()
    connect_latency_ms: float | None = None
    try:
        with socket.create_connection((host, port), timeout=timeout) as conn:
            connect_latency_ms = round((time.perf_counter() - started) * 1000, 3)
            conn.settimeout(timeout)
            conn.sendall(_encode_probe_frame(expected, DEFAULT_MAX_PAYLOAD_BYTES))
            received = _recv_probe_frame(conn, DEFAULT_MAX_PAYLOAD_BYTES)
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
    except (OSError, ValueError) as exc:
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
        )
        return 0
    parser.error("Unhandled command")
    return 2


def _handle_connection(conn: socket.socket, io_timeout: float, max_payload_bytes: int) -> None:
    with conn:
        conn.settimeout(io_timeout)
        try:
            data = _recv_probe_frame(conn, max_payload_bytes)
        except (OSError, ValueError):
            return
        if not data:
            return
        if len(data) > max_payload_bytes:
            return
        try:
            conn.sendall(_encode_probe_frame(data, max_payload_bytes))
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


def _encode_probe_frame(payload: bytes, max_payload_bytes: int) -> bytes:
    if not payload or len(payload) > max_payload_bytes:
        raise ValueError("Probe payload exceeds the limit")
    return struct.pack("!I", len(payload)) + payload


def _recv_probe_frame(conn: socket.socket, max_payload_bytes: int) -> bytes:
    header = _recv_exact(conn, 4)
    size = struct.unpack("!I", header)[0]
    if size < 1 or size > max_payload_bytes:
        raise ValueError("Invalid probe frame length")
    return _recv_exact(conn, size)


def _recv_exact(conn: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = conn.recv(remaining)
        if not chunk:
            raise ValueError("Probe frame ended before completion")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _validate_endpoint(host: str, port: int) -> None:
    if not host or host.strip() != host:
        raise ValueError("host must be a non-empty value")
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
