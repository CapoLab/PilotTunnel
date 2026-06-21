"""Link setup validation and persistence helpers."""

from __future__ import annotations

import re
from dataclasses import asdict

from .config import AppConfig, LinkProfile, side_label_for_role

SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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
    for existing in config.links:
        if existing.label == target_label:
            preserved_id = existing.id or preserved_id
            replaced = True
            continue
        if existing.label == link.label and not replace_label:
            raise ValueError(f"Link '{link.label}' already exists")
        new_links.append(existing)
    link.id = preserved_id
    new_links.append(link)
    config.links = new_links
    return ("updated" if replaced else "created"), link


def _sync_controller_endpoint_reservations(config: AppConfig) -> None:
    if config.node.normalized_role == "controller":
        config.node.managed_remote_endpoints = [
            {
                "label": link.label,
                "kharej_address": link.kharej_address,
                "status": link.status,
            }
            for link in config.links
        ]
    else:
        config.node.managed_remote_endpoints = []


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
        id=replace_label_value or resolved_label,
        label=resolved_label,
        iran_address=normalized_iran_address,
        iran_main_port=main_port,
        tunnel_port=tunnel_port_value,
        config_port=config_port_value,
        kharej_address=normalized_kharej_address,
        status="configured",
        candidates=[],
    )
    status, stored = _upsert_link(config, link, replace_label=replace_label_value)
    config.node.side_label = side_label_for_role("controller")
    config.node.active_link_label = stored.label
    if not config.node.display_name:
        config.node.display_name = stored.label
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
    normalized_iran_address = validate_link_address(iran_address, "Iran IP / domain")
    tunnel_port_value = validate_link_port(tunnel_port, "Tunnel port")
    config_port_value = validate_link_port(config_port, "Config/service port")
    replace_label_value = validate_link_label(replace_label) if replace_label else None
    resolved_label = validate_link_label(label) if label else suggest_link_label(config, ignore_label=replace_label_value)
    if resolved_label in _existing_labels(config, ignore_label=replace_label_value):
        raise ValueError(f"Link '{resolved_label}' already exists")

    link = LinkProfile(
        id=replace_label_value or resolved_label,
        label=resolved_label,
        iran_address=normalized_iran_address,
        tunnel_port=tunnel_port_value,
        config_port=config_port_value,
        status="configured",
        candidates=[],
    )
    status, stored = _upsert_link(config, link, replace_label=replace_label_value)
    config.node.side_label = side_label_for_role("worker")
    config.node.active_link_label = stored.label
    if not config.node.display_name:
        config.node.display_name = stored.label
    _sync_controller_endpoint_reservations(config)
    return status, stored


def link_payload(link: LinkProfile, *, active_label: str = "") -> dict:
    payload = asdict(link)
    payload["active"] = bool(active_label and link.label == active_label)
    return payload


def link_list_payload(config: AppConfig) -> list[dict]:
    return [link_payload(link, active_label=config.node.active_link_label) for link in config.links]


def current_setup_summary(config: AppConfig) -> dict:
    active_link = get_active_link(config)
    return {
        "side": role_summary_label(config.node.normalized_role) if config.node.normalized_role else "not configured",
        "active_link": asdict(active_link) if active_link else None,
        "link_count": len(config.links),
    }
