"""Controller-originated pairing workflow helpers."""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
from uuid import uuid4

from .config import (
    AppConfig,
    DEFAULT_AUX_TEST_PORT,
    DEFAULT_PROBE_PORT,
    DEFAULT_RESERVED_TEST_RANGE,
    LinkProfile,
    side_label_for_role,
)
from .network import NetworkDiscoveryResult, detect_local_address

SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PAIRING_PREFIX = "ptlink://v1/"
PAIRING_VERSION = "v1"
MAX_PAIRING_CODE_LENGTH = 4096
MAX_PAIRING_PAYLOAD_BYTES = 3072
REDACTED = "***REDACTED***"


def role_summary_label(role: str) -> str:
    return f"{side_label_for_role(role)} / {role}"


def validate_link_address(value: str, field_label: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field_label} must not be empty")
    if any(char in candidate for char in "\r\n\t"):
        raise ValueError(f"{field_label} contains unsupported whitespace")
    return candidate


def validate_link_port(value: int, field_label: str) -> int:
    if value < 1 or value > 65535:
        raise ValueError(f"{field_label} must be between 1 and 65535")
    return value


def validate_link_label(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("Link label must not be empty")
    if ".." in candidate or "/" in candidate or "\\" in candidate:
        raise ValueError(f"Unsafe link label: {value!r}")
    if not SAFE_LABEL_RE.fullmatch(candidate):
        raise ValueError("Link label may contain only letters, numbers, dashes, and underscores")
    return candidate


def _existing_labels(config: AppConfig, *, ignore_label: str | None = None) -> set[str]:
    return {link.label for link in config.links if link.label != ignore_label}


def suggest_link_label(config: AppConfig, *, prefix: str = "link", ignore_label: str | None = None) -> str:
    existing = _existing_labels(config, ignore_label=ignore_label)
    index = 1
    while True:
        label = f"{prefix}-{index:03d}"
        if label not in existing:
            return label
        index += 1


def _generate_link_id() -> str:
    return f"ptlink-{uuid4().hex[:12]}"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _checksum_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _pairing_payload_without_checksum(link: LinkProfile) -> dict[str, object]:
    return {
        "schema_version": PAIRING_VERSION,
        "link_id": link.id,
        "controller_address": link.controller_address,
        "expected_worker_address": link.worker_address,
        "controller_user_facing_port": link.controller_user_facing_port,
        "transport_port": link.transport_port,
        "worker_service_port": link.worker_service_port,
        "probe_port": link.probe_port,
        "aux_test_port": link.aux_test_port,
        "reserved_test_range": list(link.reserved_test_range),
        "pairing_secret": link.pairing_secret,
        "issued_at": link.pairing_issued_at,
    }


def _ensure_controller_detection(manual_override: str = "") -> NetworkDiscoveryResult:
    discovery = detect_local_address(manual_override=manual_override)
    if discovery.ok:
        return discovery
    raise ValueError(
        "Could not detect the controller address automatically. Use the advanced --controller-address-override option."
    )


def _ensure_worker_detection(manual_override: str = "") -> NetworkDiscoveryResult:
    discovery = detect_local_address(manual_override=manual_override)
    if discovery.ok:
        return discovery
    raise ValueError(
        "Could not detect the worker address automatically. Use the advanced --worker-address-override option."
    )


def get_link(config: AppConfig, label: str) -> LinkProfile:
    for link in config.links:
        if link.label == label:
            return link
    raise KeyError(f"Link '{label}' not found")


def get_active_link(config: AppConfig) -> LinkProfile | None:
    active_label = config.node.active_link_label
    if active_label:
        for link in config.links:
            if link.label == active_label:
                return link
    return config.links[0] if config.links else None


def _upsert_link(config: AppConfig, link: LinkProfile, *, replace_label: str | None = None) -> tuple[str, LinkProfile]:
    target_label = replace_label or link.label
    new_links: list[LinkProfile] = []
    replaced = False
    preserved_id = link.id
    preserved_label = link.label
    for existing in config.links:
        same_label = existing.label == target_label
        same_id = bool(existing.id and link.id) and existing.id == link.id
        if same_label or same_id:
            preserved_id = existing.id or preserved_id
            if same_label or same_id:
                preserved_label = existing.label
            replaced = True
            continue
        if existing.label == link.label and not replace_label and not same_id:
            raise ValueError(f"Link '{link.label}' already exists")
        new_links.append(existing)
    link.id = preserved_id
    link.label = preserved_label
    new_links.append(link)
    config.links = new_links
    return ("updated" if replaced else "created"), link


def _sync_controller_endpoint_reservations(config: AppConfig) -> None:
    if config.node.normalized_role == "controller":
        config.node.managed_remote_endpoints = [
            {
                "id": link.id,
                "label": link.label,
                "worker_address": link.worker_address,
                "pairing_state": link.effective_pairing_state,
                "status": link.status,
            }
            for link in config.links
        ]
    else:
        config.node.managed_remote_endpoints = []


def _effective_local_address(link: LinkProfile, role: str) -> str:
    if role == "controller":
        return link.detected_controller_address or link.controller_address
    if role == "worker":
        return link.detected_worker_address or link.worker_address
    return ""


def _effective_remote_address(link: LinkProfile, role: str) -> str:
    if role == "controller":
        return link.worker_address
    if role == "worker":
        return link.controller_address
    return ""


def _next_action_for_link(link: LinkProfile, role: str) -> str:
    state = link.effective_pairing_state
    if state == "awaiting_worker_import":
        return "Copy this pairing code to the Kharej server and choose Import pairing code."
    if state == "paired":
        if role == "controller":
            return "Worker paired. Continue with candidate generation when you are ready."
        return "Pairing complete. Wait for the controller to continue with candidate generation."
    if state == "paired_address_mismatch":
        return "Pairing completed with an acknowledged worker address mismatch."
    if state == "manual_worker":
        return "Manual worker setup saved. Coordinate the controller-side pairing details next."
    if state == "legacy_manual_controller":
        return "Legacy controller link loaded. Export a fresh pairing code when ready."
    if state == "legacy_manual_worker":
        return "Legacy worker link loaded. Re-import a fresh pairing code when ready."
    return "Complete controller/worker pairing before candidate generation."


def _redacted_candidates(link: LinkProfile) -> list[dict[str, str]]:
    return [asdict(candidate) for candidate in link.candidates]


def link_payload(link: LinkProfile, *, active_label: str = "", role: str = "") -> dict[str, object]:
    return {
        "id": link.id,
        "label": link.label,
        "active": bool(active_label and link.label == active_label),
        "status": link.status,
        "pairing_state": link.effective_pairing_state,
        "controller_address": link.controller_address,
        "worker_address": link.worker_address,
        "controller_user_facing_port": link.controller_user_facing_port,
        "transport_port": link.transport_port,
        "worker_service_port": link.worker_service_port,
        "probe_port": link.probe_port,
        "aux_test_port": link.aux_test_port,
        "reserved_test_range": list(link.reserved_test_range),
        "local_side": role_summary_label(role) if role else "",
        "detected_local_address": _effective_local_address(link, role),
        "remote_address": _effective_remote_address(link, role),
        "detected_controller_address": link.detected_controller_address,
        "detected_worker_address": link.detected_worker_address,
        "pairing_version": link.pairing_version or PAIRING_VERSION,
        "pairing_issued_at": link.pairing_issued_at,
        "pairing_checksum": link.pairing_checksum,
        "pairing_secret_present": bool(link.pairing_secret),
        "next_action": _next_action_for_link(link, role),
        "candidates": _redacted_candidates(link),
    }


def link_list_payload(config: AppConfig) -> list[dict[str, object]]:
    role = config.node.normalized_role or ""
    return [link_payload(link, active_label=config.node.active_link_label, role=role) for link in config.links]


def current_setup_summary(config: AppConfig) -> dict[str, object]:
    active_link = get_active_link(config)
    role = config.node.normalized_role or ""
    return {
        "side": role_summary_label(role) if role else "not configured",
        "active_link": link_payload(active_link, active_label=config.node.active_link_label, role=role) if active_link else None,
        "link_count": len(config.links),
    }


def create_controller_link(
    config: AppConfig,
    *,
    worker_address: str,
    worker_service_port: int,
    transport_port: int,
    controller_user_facing_port: int | None = None,
    controller_address_override: str = "",
    replace_label: str | None = None,
) -> tuple[str, LinkProfile, NetworkDiscoveryResult]:
    discovery = _ensure_controller_detection(controller_address_override)
    normalized_worker_address = validate_link_address(worker_address, "Kharej public IP / domain")
    service_port = validate_link_port(worker_service_port, "Kharej VPN service/config port")
    transport_port_value = validate_link_port(transport_port, "Tunnel transport port")
    user_facing_port = validate_link_port(
        controller_user_facing_port if controller_user_facing_port is not None else service_port,
        "Iran user-facing port",
    )
    replace_label_value = validate_link_label(replace_label) if replace_label else None
    link = LinkProfile(
        id=_generate_link_id(),
        label=suggest_link_label(config, ignore_label=replace_label_value),
        iran_address=discovery.preferred_address,
        iran_main_port=user_facing_port,
        tunnel_port=transport_port_value,
        config_port=service_port,
        probe_port=DEFAULT_PROBE_PORT,
        aux_test_port=DEFAULT_AUX_TEST_PORT,
        reserved_test_range=list(DEFAULT_RESERVED_TEST_RANGE),
        kharej_address=normalized_worker_address,
        status="awaiting_worker_import",
        pairing_state="awaiting_worker_import",
        pairing_secret=secrets.token_urlsafe(24),
        pairing_version=PAIRING_VERSION,
        pairing_issued_at=_now_utc(),
        detected_controller_address=discovery.preferred_address,
        detected_worker_address="",
        candidates=[],
    )
    payload = _pairing_payload_without_checksum(link)
    link.pairing_checksum = _checksum_payload(payload)
    status, stored = _upsert_link(config, link, replace_label=replace_label_value)
    config.node.side_label = side_label_for_role("controller")
    config.node.active_link_label = stored.label
    config.node.endpoint_address = discovery.preferred_address
    _sync_controller_endpoint_reservations(config)
    return status, stored, discovery


def export_pairing_code(link: LinkProfile) -> str:
    if not link.pairing_secret:
        raise ValueError("Link does not have a pairing secret. Create or refresh the controller pairing first.")
    payload = _pairing_payload_without_checksum(link)
    payload["checksum"] = _checksum_payload(payload)
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"{PAIRING_PREFIX}{encoded.rstrip('=')}"


def inspect_pairing_code(pairing_code: str) -> dict[str, object]:
    payload = decode_pairing_code(pairing_code)
    payload["pairing_secret"] = REDACTED
    payload["secret_redacted"] = True
    return payload


def decode_pairing_code(pairing_code: str) -> dict[str, object]:
    candidate = pairing_code.strip()
    if not candidate:
        raise ValueError("Pairing code must not be empty")
    if len(candidate) > MAX_PAIRING_CODE_LENGTH:
        raise ValueError("Pairing code is oversized and was rejected")
    if not candidate.startswith(PAIRING_PREFIX):
        raise ValueError("Unsupported pairing code scheme")
    encoded_payload = candidate[len(PAIRING_PREFIX) :]
    if not encoded_payload:
        raise ValueError("Pairing code payload is missing")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", encoded_payload):
        raise ValueError("Pairing code contains unsafe characters")
    padded = encoded_payload + "=" * ((4 - len(encoded_payload) % 4) % 4)
    try:
        raw_payload = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Pairing code payload is malformed") from exc
    if len(raw_payload) > MAX_PAIRING_PAYLOAD_BYTES:
        raise ValueError("Pairing code payload is oversized and was rejected")
    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Pairing code payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Pairing code payload is not a valid object")

    required_fields = {
        "schema_version",
        "link_id",
        "controller_address",
        "expected_worker_address",
        "controller_user_facing_port",
        "transport_port",
        "worker_service_port",
        "pairing_secret",
        "issued_at",
        "checksum",
    }
    missing = sorted(required_fields - set(payload))
    if missing:
        raise ValueError(f"Pairing code is missing required fields: {', '.join(missing)}")
    if payload["schema_version"] != PAIRING_VERSION:
        raise ValueError(f"Unsupported pairing code version '{payload['schema_version']}'")

    normalized = {
        "schema_version": PAIRING_VERSION,
        "link_id": validate_link_address(str(payload["link_id"]), "Pairing link ID"),
        "controller_address": validate_link_address(str(payload["controller_address"]), "Controller address"),
        "expected_worker_address": validate_link_address(str(payload["expected_worker_address"]), "Expected worker address"),
        "controller_user_facing_port": validate_link_port(int(payload["controller_user_facing_port"]), "Iran user-facing port"),
        "transport_port": validate_link_port(int(payload["transport_port"]), "Tunnel transport port"),
        "worker_service_port": validate_link_port(int(payload["worker_service_port"]), "Kharej service/config port"),
        "probe_port": validate_link_port(int(payload.get("probe_port", DEFAULT_PROBE_PORT)), "Probe port"),
        "aux_test_port": validate_link_port(int(payload.get("aux_test_port", DEFAULT_AUX_TEST_PORT)), "Auxiliary test port"),
        "reserved_test_range": [
            validate_link_port(int(item), "Reserved test port")
            for item in list(payload.get("reserved_test_range") or list(DEFAULT_RESERVED_TEST_RANGE))
        ],
        "pairing_secret": validate_link_address(str(payload["pairing_secret"]), "Pairing secret"),
        "issued_at": validate_link_address(str(payload["issued_at"]), "Issued timestamp"),
        "checksum": validate_link_address(str(payload["checksum"]), "Corruption checksum"),
    }
    computed_checksum = _checksum_payload({key: value for key, value in payload.items() if key != "checksum"})
    if normalized["checksum"] != computed_checksum:
        raise ValueError("Pairing code checksum validation failed")
    return normalized


def import_pairing_code(
    config: AppConfig,
    *,
    pairing_code: str,
    confirm_address_mismatch: bool = False,
    worker_address_override: str = "",
    replace_label: str | None = None,
) -> tuple[str, LinkProfile, NetworkDiscoveryResult, bool]:
    payload = decode_pairing_code(pairing_code)
    discovery = _ensure_worker_detection(worker_address_override)
    expected_worker_address = payload["expected_worker_address"]
    detected_worker_address = discovery.preferred_address
    mismatch = bool(expected_worker_address and detected_worker_address and expected_worker_address != detected_worker_address)
    if mismatch and not confirm_address_mismatch:
        raise ValueError(
            f"Detected worker address '{detected_worker_address}' does not match expected '{expected_worker_address}'. "
            "Re-run with --confirm-address-mismatch to accept the mismatch."
        )

    replace_label_value = validate_link_label(replace_label) if replace_label else None
    link = LinkProfile(
        id=payload["link_id"],
        label=suggest_link_label(config, ignore_label=replace_label_value),
        iran_address=payload["controller_address"],
        iran_main_port=payload["controller_user_facing_port"],
        tunnel_port=payload["transport_port"],
        config_port=payload["worker_service_port"],
        probe_port=payload["probe_port"],
        aux_test_port=payload["aux_test_port"],
        reserved_test_range=list(payload["reserved_test_range"]),
        kharej_address=expected_worker_address,
        status="paired" if not mismatch else "paired_address_mismatch",
        pairing_state="paired" if not mismatch else "paired_address_mismatch",
        pairing_secret=payload["pairing_secret"],
        pairing_version=payload["schema_version"],
        pairing_issued_at=payload["issued_at"],
        pairing_checksum=payload["checksum"],
        detected_controller_address=payload["controller_address"],
        detected_worker_address=detected_worker_address,
        candidates=[],
    )
    status, stored = _upsert_link(config, link, replace_label=replace_label_value)
    config.node.side_label = side_label_for_role("worker")
    config.node.active_link_label = stored.label
    config.node.endpoint_address = detected_worker_address
    _sync_controller_endpoint_reservations(config)
    return status, stored, discovery, mismatch


def setup_worker_manual(
    config: AppConfig,
    *,
    controller_address: str,
    transport_port: int,
    worker_service_port: int,
    worker_address_override: str = "",
    label: str | None = None,
    replace_label: str | None = None,
) -> tuple[str, LinkProfile, NetworkDiscoveryResult]:
    normalized_controller_address = validate_link_address(controller_address, "Iran/controller IP / domain")
    transport_port_value = validate_link_port(transport_port, "Tunnel transport port")
    worker_service_port_value = validate_link_port(worker_service_port, "Local VPN service/config port")
    replace_label_value = validate_link_label(replace_label) if replace_label else None
    resolved_label = validate_link_label(label) if label else suggest_link_label(config, ignore_label=replace_label_value)
    discovery = _ensure_worker_detection(worker_address_override)
    link = LinkProfile(
        id=_generate_link_id(),
        label=resolved_label,
        iran_address=normalized_controller_address,
        iran_main_port=None,
        tunnel_port=transport_port_value,
        config_port=worker_service_port_value,
        probe_port=DEFAULT_PROBE_PORT,
        aux_test_port=DEFAULT_AUX_TEST_PORT,
        reserved_test_range=list(DEFAULT_RESERVED_TEST_RANGE),
        kharej_address=discovery.preferred_address,
        status="manual_worker",
        pairing_state="manual_worker",
        pairing_secret="",
        pairing_version=PAIRING_VERSION,
        pairing_issued_at=_now_utc(),
        pairing_checksum="",
        detected_controller_address=normalized_controller_address,
        detected_worker_address=discovery.preferred_address,
        candidates=[],
    )
    status, stored = _upsert_link(config, link, replace_label=replace_label_value)
    config.node.side_label = side_label_for_role("worker")
    config.node.active_link_label = stored.label
    config.node.endpoint_address = discovery.preferred_address
    _sync_controller_endpoint_reservations(config)
    return status, stored, discovery


def setup_iran_link(
    config: AppConfig,
    *,
    iran_address: str,
    iran_main_port: int,
    tunnel_port: int,
    config_port: int,
    kharej_address: str,
    label: str | None = None,
    replace_label: str | None = None,
) -> tuple[str, LinkProfile]:
    normalized_iran_address = validate_link_address(iran_address, "Iran IP / domain")
    normalized_kharej_address = validate_link_address(kharej_address, "Kharej IP / domain")
    main_port = validate_link_port(iran_main_port, "Main public/user-facing port")
    tunnel_port_value = validate_link_port(tunnel_port, "Tunnel port")
    config_port_value = validate_link_port(config_port, "Config/service port")
    replace_label_value = validate_link_label(replace_label) if replace_label else None
    resolved_label = validate_link_label(label) if label else suggest_link_label(config, ignore_label=replace_label_value)
    if resolved_label in _existing_labels(config, ignore_label=replace_label_value):
        raise ValueError(f"Link '{resolved_label}' already exists")

    link = LinkProfile(
        id=_generate_link_id(),
        label=resolved_label,
        iran_address=normalized_iran_address,
        iran_main_port=main_port,
        tunnel_port=tunnel_port_value,
        config_port=config_port_value,
        probe_port=DEFAULT_PROBE_PORT,
        aux_test_port=DEFAULT_AUX_TEST_PORT,
        reserved_test_range=list(DEFAULT_RESERVED_TEST_RANGE),
        kharej_address=normalized_kharej_address,
        status="legacy_manual_controller",
        pairing_state="legacy_manual_controller",
        detected_controller_address=normalized_iran_address,
        detected_worker_address="",
        candidates=[],
    )
    status, stored = _upsert_link(config, link, replace_label=replace_label_value)
    config.node.side_label = side_label_for_role("controller")
    config.node.active_link_label = stored.label
    config.node.endpoint_address = normalized_iran_address
    _sync_controller_endpoint_reservations(config)
    return status, stored


def setup_kharej_link(
    config: AppConfig,
    *,
    iran_address: str,
    tunnel_port: int,
    config_port: int,
    label: str | None = None,
    replace_label: str | None = None,
) -> tuple[str, LinkProfile]:
    status, link, _ = setup_worker_manual(
        config,
        controller_address=iran_address,
        transport_port=tunnel_port,
        worker_service_port=config_port,
        label=label,
        replace_label=replace_label,
        worker_address_override="",
    )
    return status, link
