"""Port mapping parser for tunnel forwarding plans."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    host: str | None
    port: int


@dataclass(frozen=True)
class PortMapping:
    listen_start: int
    listen_end: int
    target: Endpoint | None = None

    @property
    def is_range(self) -> bool:
        return self.listen_start != self.listen_end


def parse_port_mapping(value: str) -> PortMapping:
    raw = value.strip()
    if not raw:
        raise ValueError("Port mapping cannot be empty")

    left, sep, right = raw.partition("=")
    if not sep and ":" in left and "-" in left.split(":", 1)[0]:
        range_part, target_part = left.split(":", 1)
        listen_start, listen_end = _parse_listen(range_part.strip())
        return PortMapping(listen_start=listen_start, listen_end=listen_end, target=_parse_target(target_part.strip()))
    listen_start, listen_end = _parse_listen(left.strip())
    if not sep:
        if listen_start != listen_end:
            return PortMapping(listen_start=listen_start, listen_end=listen_end, target=None)
        return PortMapping(listen_start=listen_start, listen_end=listen_end, target=None)

    target = _parse_target(right.strip())
    if listen_start == listen_end and target.port == listen_start and target.host is None:
        return PortMapping(listen_start=listen_start, listen_end=listen_end, target=None)
    return PortMapping(listen_start=listen_start, listen_end=listen_end, target=target)


def _parse_listen(value: str) -> tuple[int, int]:
    host: str | None = None
    port_part = value
    if ":" in value:
        host, port_part = value.rsplit(":", 1)
        host = host.strip()
        if not host:
            raise ValueError("Host prefix cannot be empty")
    if "-" in port_part:
        start_raw, end_raw = port_part.split("-", 1)
        start = _parse_port(start_raw)
        end = _parse_port(end_raw)
        if start >= end:
            raise ValueError("Port range start must be lower than end")
    else:
        start = end = _parse_port(port_part)
    if host is not None and start != end:
        raise ValueError("Host-specific listen address does not support ranges in v0.1")
    return start, end


def _parse_target(value: str) -> Endpoint:
    host: str | None = None
    port_raw = value
    if ":" in value:
        host, port_raw = value.rsplit(":", 1)
        host = host.strip()
        if not host:
            raise ValueError("Target host cannot be empty")
    return Endpoint(host=host, port=_parse_port(port_raw))


def _parse_port(value: str) -> int:
    if not value.isdigit():
        raise ValueError(f"Invalid port '{value}'")
    port = int(value)
    if port < 1 or port > 65535:
        raise ValueError(f"Port '{value}' is out of range")
    return port
