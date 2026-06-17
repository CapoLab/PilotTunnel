"""Read-only local TCP healthcheck helpers."""

from __future__ import annotations

import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from .config import Profile

DEFAULT_TIMEOUT_SECONDS = 2.0


@dataclass
class HealthcheckResult:
    ok: bool
    message: str = ""
    mode: str = "local-only"


@dataclass
class TcpCheckResult:
    ok: bool
    host: str
    port: int
    timeout: float
    latency_ms: float | None
    error: str
    checked_at: str
    role: str
    profile: str
    label: str

    def to_dict(self) -> dict:
        return asdict(self)


def local_only_healthcheck(profile: str, adapter: str, transport: str) -> HealthcheckResult:
    return HealthcheckResult(
        ok=True,
        message=f"Local-only healthcheck passed for profile={profile} adapter={adapter} transport={transport}",
    )


def tcp_healthcheck(
    *,
    host: str,
    port: int,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    role: str,
    profile: str,
    label: str = "endpoint",
) -> TcpCheckResult:
    _validate_host_port(host, port)
    if timeout <= 0:
        raise ValueError("timeout must be greater than 0")
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            latency_ms = round((time.perf_counter() - start) * 1000, 3)
            return TcpCheckResult(
                ok=True,
                host=host,
                port=port,
                timeout=timeout,
                latency_ms=latency_ms,
                error="",
                checked_at=_checked_at(),
                role=role,
                profile=profile,
                label=label,
            )
    except OSError as exc:
        return TcpCheckResult(
            ok=False,
            host=host,
            port=port,
            timeout=timeout,
            latency_ms=None,
            error=str(exc),
            checked_at=_checked_at(),
            role=role,
            profile=profile,
            label=label,
        )


def run_profile_healthchecks(
    *,
    profile: Profile,
    node_role: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    include_all: bool = False,
    role_aware: bool = False,
) -> list[dict]:
    checks = _profile_endpoints(profile=profile, node_role=node_role, include_all=include_all, role_aware=role_aware)
    results: list[dict] = []
    for label, host, port in checks:
        results.append(
            tcp_healthcheck(
                host=host,
                port=port,
                timeout=timeout,
                role=node_role,
                profile=profile.name,
                label=label,
            ).to_dict()
        )
    return results


def build_profile_healthcheck_plan(
    *,
    profile: Profile,
    node_role: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    include_all: bool = False,
    role_aware: bool = False,
) -> list[dict]:
    checks = _profile_endpoints(profile=profile, node_role=node_role, include_all=include_all, role_aware=role_aware)
    return [
        {
            "label": label,
            "host": host,
            "port": port,
            "timeout": timeout,
            "role": node_role,
            "profile": profile.name,
        }
        for label, host, port in checks
    ]


def summarize_healthchecks(results: list[dict], *, profile: str, role: str) -> dict:
    return {
        "ok": all(item["ok"] for item in results) if results else False,
        "profile": profile,
        "role": role,
        "results": results,
    }


def _profile_endpoints(*, profile: Profile, node_role: str, include_all: bool, role_aware: bool) -> list[tuple[str, str, int]]:
    controller = [
        ("target", profile.target_host, profile.target_port),
        ("main_port", "127.0.0.1", profile.ports.main_port),
    ]
    if profile.ports.service_port:
        controller.append(("service_port", "127.0.0.1", profile.ports.service_port))
    if profile.ports.check_port:
        controller.append(("check_port", "127.0.0.1", profile.ports.check_port))

    worker = [
        ("worker_target_port", "127.0.0.1", profile.target_port),
    ]
    if profile.ports.service_port:
        worker.append(("worker_service_port", "127.0.0.1", profile.ports.service_port))
    if profile.ports.check_port:
        worker.append(("worker_check_port", "127.0.0.1", profile.ports.check_port))
    if profile.target_host:
        worker.append(("controller_endpoint", profile.target_host, profile.target_port))

    if include_all or not role_aware:
        candidates = controller + worker
    elif node_role == "worker":
        candidates = worker
    else:
        candidates = controller

    deduped: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()
    for label, host, port in candidates:
        key = (label, host, port)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((label, host, port))
    return deduped


def _validate_host_port(host: str, port: int) -> None:
    if not host or host.strip() != host:
        raise ValueError("host must be a non-empty value")
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()
