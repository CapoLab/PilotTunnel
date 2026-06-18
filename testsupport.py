"""Shared test helpers for dynamic ports and local HTTP fixtures."""

from __future__ import annotations

import socket
import threading
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator


def allocate_tcp_ports(count: int) -> tuple[list[int], list[socket.socket]]:
    listeners: list[socket.socket] = []
    ports: list[int] = []
    for _ in range(count):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listeners.append(listener)
        ports.append(listener.getsockname()[1])
    return ports, listeners


@contextmanager
def static_http_server(root: Path) -> Iterator[str]:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
