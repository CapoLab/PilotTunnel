"""Audit logging with secret redaction."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_AUDIT_PATH

SECRET_KEYS = {
    "auth",
    "auth_token",
    "bearer_token",
    "bore_secret",
    "passcode",
    "passwd",
    "passphrase",
    "secret",
    "password",
    "token",
    "private",
    "private_key",
    "apikey",
    "api_key",
    "pairing_secret",
    "pairing_code",
}
SECRET_KEY_SUFFIXES = {
    "auth_key",
    "secret_key",
    "token_key",
}


def _is_secret_key(value: Any) -> bool:
    normalized = str(value).strip().lower().replace("-", "_")
    if normalized in SECRET_KEYS:
        return True
    return normalized.endswith(tuple(SECRET_KEY_SUFFIXES))


def _redact_secret_string(value: str) -> str:
    stripped = value.strip()
    upper_value = stripped.upper()
    if stripped.startswith("ptlink://"):
        return "***REDACTED***"
    if stripped.startswith("-----BEGIN ") and "PRIVATE KEY-----" in upper_value:
        return "***REDACTED***"
    if any(token in upper_value for token in ("AUTH=", "SECRET=", "TOKEN=", "PASSWORD=", "PRIVATE_KEY=", "BORE_SECRET=")):
        return "***REDACTED***"
    return value


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("***REDACTED***" if _is_secret_key(key) else redact_secrets(inner))
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_secret_string(value)
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
