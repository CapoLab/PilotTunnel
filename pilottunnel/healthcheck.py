"""Healthcheck helpers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HealthcheckResult:
    ok: bool
    message: str = ""
    mode: str = "local-only"


def local_only_healthcheck(profile: str, adapter: str, transport: str) -> HealthcheckResult:
    return HealthcheckResult(
        ok=True,
        message=f"Local-only healthcheck passed for profile={profile} adapter={adapter} transport={transport}",
    )
