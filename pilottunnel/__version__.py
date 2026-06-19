"""Canonical PilotTunnel release metadata."""

from __future__ import annotations

from typing import Any

PROJECT_NAME = "PilotTunnel"
VERSION = "0.1.1-dev"
RELEASE_PHASE = "post-v0.1-dev"
SAFETY_NOTES = [
    "No automatic failover or auto-switch is included in the current development stage.",
    "No background monitoring daemon is included in the current development stage.",
    "Real deployment still requires explicit operator confirmation.",
    "The Linux installer workflow is intended for non-production smoke testing first.",
]

SUPPORTED_SCOPE = [
    "CLI only",
    "Config-file driven",
    "Layer 4 TCP only",
    "Selected adapters only",
    "One active tunnel",
    "Up to two hot-standby tunnels",
    "Config-only for remaining tunnels",
    "Guarded manual switch with rollback support",
]

KNOWN_LIMITATIONS = [
    "No automatic failover",
    "No background monitor",
    "No UI",
    "Real deployment requires operator confirmation",
    "Production rollout should begin with a non-production smoke test",
]

__version__ = VERSION


def version_payload() -> dict[str, Any]:
    return {
        "project": PROJECT_NAME,
        "version": VERSION,
        "release_phase": RELEASE_PHASE,
        "supported_scope": list(SUPPORTED_SCOPE),
        "safety_notes": list(SAFETY_NOTES),
        "known_limitations": list(KNOWN_LIMITATIONS),
    }
