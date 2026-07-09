"""Read-only host preflight checks."""

from __future__ import annotations

import os
import platform
import shutil
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import LinkProfile, Profile


@dataclass
class CommandAvailability:
    name: str
    found: bool
    required_for_real_apply: bool
    path: str | None = None


@dataclass
class HostPreflightResult:
    host: dict
    commands: list[dict]
    staging_root: str
    staging_writable: bool
    systemd_available: bool
    port_availability: dict[int, bool] = field(default_factory=dict)
    test_port_availability: dict[int, bool] = field(default_factory=dict)
    suggested_test_ports: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_to_stage: bool = True
    safe_to_real_apply: bool = False
    staged_only: bool = True
    real_systemd_touched: bool = False
    real_firewall_touched: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


COMMANDS = {
    "ss": False,
    "systemctl": True,
    "ip": True,
    "iptables": True,
    "nft": True,
    "curl": False,
    "tar": False,
    "unzip": False,
}


def run_preflight(
    staging_root: Path,
    profile: Profile | None = None,
    *,
    link: LinkProfile | None = None,
    command_lookup=None,
    platform_name: str | None = None,
    probe_write: bool = True,
) -> HostPreflightResult:
    lookup = command_lookup or shutil.which
    system_name = (platform_name or platform.system()).lower()
    is_windows = system_name.startswith("win")
    is_linux = system_name.startswith("linux")

    commands: list[CommandAvailability] = []
    warnings: list[str] = []
    for command, required in COMMANDS.items():
        path = lookup(command)
        commands.append(CommandAvailability(name=command, found=bool(path), required_for_real_apply=required, path=path))
        if required and not path:
            warnings.append(f"Command '{command}' is missing for future real apply planning")

    systemd_available = any(item.name == "systemctl" and item.found for item in commands) and is_linux
    if is_linux and not systemd_available:
        warnings.append("systemd does not appear available on this host")
    if is_windows:
        warnings.append("Windows host detected; real apply remains unsupported in v0.1")

    staging_writable = _check_staging_writable(staging_root) if probe_write else _check_staging_writable_readonly(staging_root)
    if not staging_writable:
        warnings.append(f"Staging root is not writable: {staging_root}")

    port_availability: dict[int, bool] = {}
    if profile is not None:
        for port in profile.ports.owned_ports():
            port_availability[port] = _port_available(port)
            if not port_availability[port]:
                warnings.append(f"Port {port} does not appear available")

    test_port_availability: dict[int, bool] = {}
    suggested_test_ports: list[int] = []
    if link is not None:
        checked_ports = []
        for port in [link.probe_port, link.aux_test_port]:
            if port not in checked_ports:
                checked_ports.append(port)
        for port in checked_ports:
            available = _port_available(port)
            test_port_availability[port] = available
        if not test_port_availability.get(link.probe_port, True):
            warnings.append(
                f"Probe/test port {link.probe_port} is already in use. Use aux_test_port {link.aux_test_port} or choose a custom probe port."
            )
        if not test_port_availability.get(link.aux_test_port, True):
            warnings.append(
                f"Auxiliary test port {link.aux_test_port} is already in use. Choose a custom probe port or another reserved test port."
            )
        suggested_test_ports = [
            int(port)
            for port in link.reserved_test_range
            if int(port) not in checked_ports and _port_available(int(port))
        ]

    host = {
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "os": system_name,
        "is_windows": is_windows,
        "is_linux": is_linux,
        "admin_or_root": _is_admin_or_root(is_windows),
    }
    return HostPreflightResult(
        host=host,
        commands=[asdict(item) for item in commands],
        staging_root=str(staging_root),
        staging_writable=staging_writable,
        systemd_available=systemd_available,
        port_availability=port_availability,
        test_port_availability=test_port_availability,
        suggested_test_ports=suggested_test_ports,
        warnings=warnings,
        safe_to_stage=staging_writable,
        safe_to_real_apply=False,
    )


def _check_staging_writable(staging_root: Path) -> bool:
    try:
        staging_root.mkdir(parents=True, exist_ok=True)
        probe = staging_root / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _check_staging_writable_readonly(staging_root: Path) -> bool:
    candidate = staging_root
    while not candidate.exists():
        if candidate.parent == candidate:
            break
        candidate = candidate.parent
    try:
        return os.access(candidate, os.W_OK)
    except OSError:
        return False


def _is_admin_or_root(is_windows: bool) -> bool:
    if is_windows:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:
        return False
    return geteuid() == 0


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True
