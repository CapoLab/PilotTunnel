"""Two-sided controller/worker bundle support."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import ADAPTERS
from .adapters.base import AdapterContext
from .audit import write_audit_log
from .config import Candidate, Profile, ProfilePorts, ProfileSafety, build_worker_stub, validate_profile_name
from .healthcheck import build_profile_healthcheck_plan

SCHEMA_VERSION = 1
BUNDLE_TYPE = "worker_prepare"
CONTROLLER_ROLE = "controller"
WORKER_ROLE = "worker"


def build_worker_bundle(
    profile: Profile,
    adapter_name: str,
    transport: str,
    *,
    include_staged_paths: bool = False,
    audit_path: Path | None = None,
) -> dict[str, Any]:
    validate_profile_name(profile.name)
    adapter = _adapter_for(adapter_name)
    worker_profile = _worker_profile(profile)
    worker_profile.active_adapter = adapter_name
    worker_profile.active_transport = transport
    context = _worker_context(worker_profile, transport)
    ok, reason = adapter.precheck(context)
    if not ok:
        raise ValueError(reason)

    service_name = adapter.service_name(context)
    config_filename = adapter.config_filename(WORKER_ROLE)
    bundle = {
        "schema_version": SCHEMA_VERSION,
        "bundle_type": BUNDLE_TYPE,
        "created_at": _checked_at(),
        "source_role": profile.role,
        "controller_role": CONTROLLER_ROLE,
        "worker_role": WORKER_ROLE,
        "profile": asdict(worker_profile),
        "adapter": adapter_name,
        "transport": transport,
        "ports": _bundle_ports(worker_profile),
        "service_names": {"worker": service_name},
        "config_filenames": {"worker": config_filename},
        "healthcheck_expectations": build_profile_healthcheck_plan(
            profile=worker_profile,
            node_role=WORKER_ROLE,
            include_all=False,
            role_aware=True,
        ),
        "warnings": _bundle_warnings(worker_profile, adapter_name, transport),
        "no_system_changes": True,
    }
    if include_staged_paths:
        bundle["staged_paths"] = _expected_staged_paths(worker_profile.name, adapter_name, transport, config_filename, service_name)
    else:
        bundle["expected_paths"] = _expected_staged_paths(worker_profile.name, adapter_name, transport, config_filename, service_name)
    _audit(
        "bundle-export-worker",
        worker_profile.name,
        {
            "adapter": adapter_name,
            "transport": transport,
            "schema_version": SCHEMA_VERSION,
            "bundle_type": BUNDLE_TYPE,
            "no_system_changes": True,
        },
        path=audit_path,
    )
    return bundle


def validate_bundle(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise ValueError("Bundle must be a JSON object")
    if bundle.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Missing or unsupported schema_version")
    if bundle.get("bundle_type") != BUNDLE_TYPE:
        raise ValueError(f"Unknown bundle_type '{bundle.get('bundle_type')}'")
    if bundle.get("no_system_changes") is not True:
        raise ValueError("Bundles must declare no_system_changes=true")
    if any(bundle.get(key) for key in ("system_changes", "real_system_changes", "modifies_system", "touches_system")):
        raise ValueError("Bundles that modify system state are not supported")

    profile_data = bundle.get("profile")
    if not isinstance(profile_data, dict):
        raise ValueError("Bundle profile must be present")
    profile = _profile_from_bundle(profile_data)

    adapter_name = bundle.get("adapter")
    transport = bundle.get("transport")
    adapter = _adapter_for(adapter_name)
    context = _worker_context(profile, transport)
    ok, reason = adapter.precheck(context)
    if not ok:
        raise ValueError(reason)

    controller_role = bundle.get("controller_role")
    worker_role = bundle.get("worker_role")
    if controller_role != CONTROLLER_ROLE or worker_role != WORKER_ROLE:
        raise ValueError("Bundle role markers are invalid")

    required_keys = ("ports", "service_names", "config_filenames", "healthcheck_expectations", "warnings")
    for key in required_keys:
        if key not in bundle:
            raise ValueError(f"Missing required bundle field: {key}")

    _validate_ports_dict(bundle.get("ports") or {})
    _validate_profile(profile)
    return {
        **bundle,
        "profile": asdict(profile),
        "adapter": adapter_name,
        "transport": transport,
        "validated_at": _checked_at(),
    }


def import_bundle(bundle_path: Path | str) -> dict[str, Any]:
    path = Path(bundle_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid JSON bundle") from exc
    validated = validate_bundle(data)
    validated["bundle_path"] = str(path)
    return validated


def inspect_bundle(bundle_path: Path | str) -> dict[str, Any]:
    bundle = import_bundle(bundle_path)
    return {
        "ok": True,
        "schema_version": bundle["schema_version"],
        "bundle_type": bundle["bundle_type"],
        "created_at": bundle["created_at"],
        "profile": bundle["profile"]["name"],
        "adapter": bundle["adapter"],
        "transport": bundle["transport"],
        "expected_role": bundle["profile"]["role"],
        "ports": bundle["ports"],
        "service_names": bundle["service_names"],
        "config_filenames": bundle["config_filenames"],
        "healthcheck_expectations": bundle["healthcheck_expectations"],
        "warnings": bundle.get("warnings", []),
        "no_changes_made": True,
    }


def _adapter_for(adapter_name: str):
    if adapter_name not in ADAPTERS:
        raise KeyError(f"Unknown adapter '{adapter_name}'")
    return ADAPTERS[adapter_name]()


def _worker_profile(profile: Profile) -> Profile:
    ports = ProfilePorts(
        main_port=profile.ports.main_port,
        control_port=profile.ports.control_port,
        service_port=profile.ports.service_port,
        check_port=profile.ports.check_port,
    )
    return Profile(
        name=profile.name,
        main_port=profile.main_port,
        target_host=profile.target_host,
        target_port=profile.target_port,
        role=WORKER_ROLE,
        active_layer=profile.active_layer,
        active_adapter=profile.active_adapter,
        active_transport=profile.active_transport,
        candidates=[Candidate(adapter=item.adapter, transport=item.transport, notes=item.notes) for item in profile.candidates],
        ports=ports,
        safety=ProfileSafety(
            cooldown_seconds=profile.safety.cooldown_seconds,
            rollback_on_failure=profile.safety.rollback_on_failure,
            dry_run_default=profile.safety.dry_run_default,
        ),
    )


def _worker_context(profile: Profile, transport: str) -> AdapterContext:
    return AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=Path("."),
        staging_root=Path("."),
        apply_changes=False,
        role=WORKER_ROLE,
        remote_stub=asdict(build_worker_stub(profile)),
    )


def _profile_from_bundle(profile_data: dict[str, Any]) -> Profile:
    name = validate_profile_name(profile_data.get("name", ""))
    role = profile_data.get("role", WORKER_ROLE)
    if role != WORKER_ROLE:
        raise ValueError("Bundle profile must target the worker role")
    ports_data = profile_data.get("ports") or {}
    ports = ProfilePorts(
        main_port=profile_data.get("main_port", ports_data.get("main_port")),
        control_port=ports_data.get("control_port"),
        service_port=ports_data.get("service_port"),
        check_port=ports_data.get("check_port"),
    )
    _validate_ports_dict({
        "main_port": ports.main_port,
        "control_port": ports.control_port,
        "service_port": ports.service_port,
        "check_port": ports.check_port,
    })
    safety_data = profile_data.get("safety") or {}
    return Profile(
        name=name,
        main_port=ports.main_port,
        target_host=profile_data.get("target_host", ""),
        target_port=profile_data.get("target_port"),
        role=role,
        active_layer=profile_data.get("active_layer", "layer4"),
        active_adapter=profile_data.get("active_adapter", ""),
        active_transport=profile_data.get("active_transport", ""),
        candidates=[Candidate(**item) for item in profile_data.get("candidates", [])],
        ports=ports,
        safety=ProfileSafety(
            cooldown_seconds=safety_data.get("cooldown_seconds", 30),
            rollback_on_failure=safety_data.get("rollback_on_failure", True),
            dry_run_default=safety_data.get("dry_run_default", True),
        ),
    )


def _validate_profile(profile: Profile) -> None:
    if not profile.target_host:
        raise ValueError("Bundle target_host is required")
    if profile.main_port is None:
        raise ValueError("Bundle main_port is required")
    if profile.target_port is None:
        raise ValueError("Bundle target_port is required")


def _validate_ports_dict(ports: dict[str, Any]) -> None:
    for label, value in ports.items():
        if value is None:
            continue
        if not isinstance(value, int):
            raise ValueError(f"{label} must be an integer")
        if value < 1 or value > 65535:
            raise ValueError(f"{label} must be between 1 and 65535")


def _bundle_ports(profile: Profile) -> dict[str, int | None]:
    return {
        "main_port": profile.ports.main_port,
        "target_port": profile.target_port,
        "service_port": profile.ports.service_port,
        "check_port": profile.ports.check_port,
        "control_port": profile.ports.control_port,
    }


def _expected_staged_paths(profile_name: str, adapter_name: str, transport: str, config_filename: str, service_name: str) -> dict[str, str]:
    return {
        "config": f"configs/{profile_name}/{adapter_name}/{transport}/worker/{config_filename}",
        "systemd_unit": f"systemd/{service_name}",
    }


def _bundle_warnings(profile: Profile, adapter_name: str, transport: str) -> list[str]:
    warnings = [f"Bundle is worker-preparation only for profile '{profile.name}'"]
    if profile.role != WORKER_ROLE:
        warnings.append(f"Source profile role '{profile.role}' will be normalized to worker")
    warnings.append(f"No system changes are performed for adapter '{adapter_name}' transport '{transport}'")
    return warnings


def _audit(action: str, profile: str, details: dict[str, Any], path: Path | None = None) -> None:
    if path is None:
        write_audit_log(action, profile, details)
    else:
        write_audit_log(action, profile, details, path)


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()
