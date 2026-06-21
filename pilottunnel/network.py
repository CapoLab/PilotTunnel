"""Structured network discovery helpers for local controller/worker pairing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import ipaddress
import os
import socket
from urllib.error import URLError
from urllib.request import urlopen

DEFAULT_PUBLIC_IP_ENDPOINTS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
)


@dataclass
class NetworkDiscoveryResult:
    preferred_address: str = ""
    default_route_address: str = ""
    hostname_addresses: list[str] = field(default_factory=list)
    public_address: str = ""
    detection_method: str = "unavailable"
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.preferred_address)

    def to_payload(self) -> dict[str, object]:
        return asdict(self)


def _is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        candidate = value.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return ordered


def _discover_default_route_address() -> tuple[str, str]:
    attempts = (
        (socket.AF_INET, ("8.8.8.8", 80)),
        (socket.AF_INET, ("1.1.1.1", 80)),
    )
    last_error = ""
    for family, remote in attempts:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as probe:
                probe.settimeout(1.0)
                probe.connect(remote)
                return probe.getsockname()[0], "default_route"
        except OSError as exc:
            last_error = str(exc)
    return "", last_error


def _discover_hostname_addresses() -> list[str]:
    candidates: list[str] = []
    hostnames = [socket.gethostname()]
    fqdn = socket.getfqdn()
    if fqdn and fqdn not in hostnames:
        hostnames.append(fqdn)
    for hostname in hostnames:
        try:
            for _family, _socktype, _proto, _canonname, sockaddr in socket.getaddrinfo(hostname, None):
                if not sockaddr:
                    continue
                address = sockaddr[0]
                if ":" in address:
                    continue
                candidates.append(address)
        except OSError:
            continue
    return _dedupe(candidates)


def _discover_public_address(*, timeout_seconds: float = 1.5, endpoints: tuple[str, ...] = DEFAULT_PUBLIC_IP_ENDPOINTS) -> tuple[str, str]:
    for endpoint in endpoints:
        try:
            with urlopen(endpoint, timeout=timeout_seconds) as response:
                candidate = response.read(128).decode("utf-8", errors="replace").strip()
        except (OSError, URLError):
            continue
        if _is_public_address(candidate):
            return candidate, endpoint
    return "", ""


def detect_local_address(
    *,
    manual_override: str = "",
    allow_public_lookup: bool = False,
    public_lookup_timeout_seconds: float = 1.5,
) -> NetworkDiscoveryResult:
    env_override = os.environ.get("PILOTTUNNEL_LOCAL_ADDRESS_OVERRIDE", "").strip()
    if env_override and not manual_override.strip():
        manual_override = env_override
    if manual_override.strip():
        return NetworkDiscoveryResult(
            preferred_address=manual_override.strip(),
            detection_method="manual_override",
        )

    result = NetworkDiscoveryResult()
    default_route_address, route_meta = _discover_default_route_address()
    hostname_addresses = _discover_hostname_addresses()
    result.default_route_address = default_route_address
    result.hostname_addresses = hostname_addresses

    if default_route_address:
        result.preferred_address = default_route_address
        result.detection_method = "default_route"
        if _is_public_address(default_route_address):
            return result

    if allow_public_lookup:
        public_address, endpoint = _discover_public_address(timeout_seconds=public_lookup_timeout_seconds)
        if public_address:
            result.public_address = public_address
            result.preferred_address = public_address
            result.detection_method = f"public_lookup:{endpoint}"
            return result
        result.warnings.append("Public address discovery did not return a globally routable address.")

    if default_route_address:
        result.preferred_address = default_route_address
        result.detection_method = "default_route_private"
    elif hostname_addresses:
        result.preferred_address = hostname_addresses[0]
        result.detection_method = "hostname"
    else:
        result.detection_method = "unavailable"

    if not result.preferred_address:
        detail = route_meta or "No route-based or hostname address could be detected."
        result.warnings.append(detail)
    elif not _is_public_address(result.preferred_address):
        result.warnings.append(
            "Detected address is not globally routable. Use an advanced manual override if the remote server cannot reach it."
        )
    return result
