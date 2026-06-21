"""Audit logging with secret redaction."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_AUDIT_PATH

SECRET_KEYS = {
    "secret",
    "password",
    "token",
    "private_key",
    "apikey",
    "api_key",
    "pairing_secret",
    "pairing_code",
}


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("***REDACTED***" if str(key).lower() in SECRET_KEYS else redact_secrets(inner))
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    return value


def write_audit_log(action: str, profile: str, details: dict[str, Any], path: Path = DEFAULT_AUDIT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "profile": profile,
        "action": action,
        "details": redact_secrets(details),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")
