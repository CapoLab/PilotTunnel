"""Runtime helpers for safe local TCP port allocation."""

from __future__ import annotations

import socket
from typing import Iterable


AUTO_PORT_KEYS = ("main_port", "target_port", "control_port", "service_port", "check_port")


def allocate_free_tcp_ports(keys: Iterable[str] = AUTO_PORT_KEYS) -> dict[str, int]:
    listeners: list[socket.socket] = []
    allocated: dict[str, int] = {}
    try:
        for key in keys:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listeners.append(listener)
            allocated[key] = listener.getsockname()[1]
        if len(set(allocated.values())) != len(allocated):
            raise RuntimeError("Allocated ports are not unique")
        return allocated
    finally:
        for listener in listeners:
            try:
                listener.close()
            except OSError:
                pass

