"""Paired-link candidate preparation and real smoke workflows."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .adapters import ADAPTERS
from .adapters.base import AdapterContext
from .audit import redact_secrets
from .binary_install import resolve_binary_reference
from .config import AppConfig, LinkCandidate, LinkProfile, Profile, ProfilePorts, ProfileSafety
from .links import get_active_link, get_link
from .probe import DEFAULT_PROBE_TIMEOUT_SECONDS, probe_roundtrip
from .state import AppState
from .switch_engine import SwitchPaths
from .systemd import render_unit_file
from .systemd_control import (
    RELOAD_CONFIRM_TOKEN,
    START_CONFIRM_TOKEN,
    STOP_CONFIRM_TOKEN,
    apply_reload,
    apply_start,
    apply_stop,
    inspect_managed_status,
)

CANDIDATE_TRANSPORTS = {
    "backhaul": "tcpmux",
    "rathole": "tcp",
    "frp": "tcp",
    "gost": "tcp",
    "chisel": "tcp",
    "realm": "tcp",
    "bore": "tcp",
}
CANDIDATE_CATEGORIES = {
    "backhaul": "two_sided_tunnel",
    "rathole": "two_sided_tunnel",
    "frp": "two_sided_tunnel",
    "gost": "two_sided_tunnel",
    "chisel": "two_sided_tunnel",
    "realm": "direct_l4_baseline",
    "bore": "two_sided_tunnel",
}
CANDIDATE_STATES = {
    "config_only",
    "prepared",
    "starting",
    "running",
    "test_passed",
    "test_failed",
    "stopped",
    "selected",
}
SYSTEMD_TARGET_DIR = Path("/etc/systemd/system")
PROBE_MARKER = "pilotunnel-probe"
BORE_CONTROL_PORT = 7835


def prepare_all_candidates(
    *,
    config: AppConfig,
    state: AppState,
    paths: SwitchPaths,
    requested_platform: str | None = None,
    link_label: str | None = None,
) -> dict[str, Any]:
    role = _require_role(config)
    link = _target_link(config, link_label)
    _validate_link(link)
    existing = {item.adapter: item for item in link.candidates}
    candidates: list[LinkCandidate] = []
    warnings: list[str] = []
    blockers: list[str] = []

    for adapter_name, transport in CANDIDATE_TRANSPORTS.items():
        candidate = existing.get(adapter_name) or LinkCandidate(adapter=adapter_name, transport=transport)
        _reset_candidate(candidate, adapter_name=adapter_name, transport=transport, local_role=role)
        probe = _probe_plan(link, adapter_name)
        topology = _build_topology(link, adapter_name, transport, probe)
        local_warnings: list[str] = []
        local_blockers: list[str] = []
        local_notes: list[str] = []
        stage_result = _stage_candidate(
            config=config,
            state=state,
            paths=paths,
            link=link,
            candidate=candidate,
            topology=topology,
            probe=probe,
            requested_platform=requested_platform,
            warnings=local_warnings,
            blockers=local_blockers,
            notes=local_notes,
        )
        candidate.category = topology["category"]
        candidate.first_start_side = topology["first_start_side"]
        candidate.topology = topology
        candidate.probe = probe
        candidate.runnable = stage_result["runnable"]
        candidate.state = "prepared" if candidate.runnable else "config_only"
        candidate.warnings = sorted(set(filter(None, local_warnings)))
        candidate.blockers = sorted(set(filter(None, local_blockers)))
        candidate.notes = " ".join(part.strip() for part in local_notes if part.strip())
        warnings.extend(candidate.warnings)
        blockers.extend(candidate.blockers)
        candidates.append(candidate)

    link.candidates = candidates
    return {
        "ok": True,
        "action": "candidate-prepare-all",
        "link_id": link.id,
        "link_label": link.label,
        "role": role,
        "pairing_state": link.effective_pairing_state,
        "candidates": [_candidate_payload(item) for item in candidates],
        "warnings": sorted(set(filter(None, warnings))),
        "blockers": sorted(set(filter(None, blockers))),
        "next_instruction": _prepare_next_instruction(role),
        "real_systemd_touched": False,
        "services_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def plan_candidate(
    *,
    config: AppConfig,
    state: AppState,
    paths: SwitchPaths,
    adapter_name: str,
    link_label: str | None = None,
) -> dict[str, Any]:
    del state, paths
    role = _require_role(config)
    link = _target_link(config, link_label)
    candidate = _candidate_for(link, adapter_name)
    payload = _candidate_payload(candidate)
    payload["current_server_role"] = role
    payload["current_server_owned_ports"] = _owned_ports_for_role(candidate, role)
    payload["current_server_dependency_ports"] = list(_side_plan(candidate, role).get("dependency_ports") or [])
    payload["start_first"] = candidate.first_start_side or candidate.topology.get("first_start_side", "")
    payload["next_instruction"] = _plan_next_instruction(role, candidate)
    payload["real_systemd_touched"] = False
    payload["services_started"] = False
    payload["firewall_touched"] = False
    payload["routes_touched"] = False
    return {
        "ok": True,
        "action": "candidate-plan",
        "link_id": link.id,
        "link_label": link.label,
        "role": role,
        "candidate": payload,
        "warnings": list(candidate.warnings),
        "blockers": list(candidate.blockers),
    }


def start_candidate(
    *,
    config: AppConfig,
    state: AppState,
    paths: SwitchPaths,
    adapter_name: str,
    link_label: str | None = None,
    requested_platform: str | None = None,
) -> dict[str, Any]:
    del requested_platform
    role = _require_role(config)
    link = _target_link(config, link_label)
    candidate = _candidate_for(link, adapter_name)
    side_plan = _side_plan(candidate, role)
    if not candidate.runnable or not side_plan["services"]:
        return _failure(
            "candidate-start",
            f"Adapter '{candidate.adapter}' is prepared for comparison only and cannot start on the {role} side",
            link=link,
            candidate=candidate,
            role=role,
        )

    active = state.active_link_candidates.get(link.id) or {}
    if active and active.get("state") in {"starting", "running"} and active.get("adapter") != candidate.adapter:
        return _failure(
            "candidate-start",
            f"Candidate '{active.get('adapter')}' is already active for link '{link.label}'",
            link=link,
            candidate=candidate,
            role=role,
        )
    if active and active.get("state") == "running" and active.get("adapter") == candidate.adapter:
        return {
            "ok": True,
            "action": "candidate-start",
            "message": "Candidate is already running on this server",
            "link_id": link.id,
            "link_label": link.label,
            "role": role,
            "candidate": _candidate_payload(candidate),
            "next_instruction": _start_next_instruction(role, candidate),
            "real_systemd_touched": False,
            "services_started": True,
            "firewall_touched": False,
            "routes_touched": False,
        }

    occupied = _occupied_ports(side_plan["owned_ports"])
    if occupied:
        _mark_candidate(link, candidate.adapter, "selected")
        return {
            "ok": False,
            "action": "candidate-start",
            "message": "Candidate start blocked because required local ports are already occupied",
            "link_id": link.id,
            "link_label": link.label,
            "role": role,
            "candidate": _candidate_payload(candidate),
            "occupied_ports": occupied,
            "next_instruction": "Stop the conflicting process manually or choose another candidate. PilotTunnel did not modify it.",
            "real_systemd_touched": False,
            "services_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }

    _mark_candidate(link, candidate.adapter, "starting")
    state.active_link_candidates[link.id] = {
        "adapter": candidate.adapter,
        "transport": candidate.transport,
        "category": candidate.category,
        "role": role,
        "state": "starting",
        "services": [
            {
                "service_name": service["service_name"],
                "service_dir": service["service_dir"],
                "runtime_dir": service["runtime_dir"],
                "kind": service["kind"],
            }
            for service in side_plan["services"]
        ],
        "updated_at": _now_utc(),
    }

    install_payload = _install_candidate_service_units(
        services=side_plan["services"],
        summary_name=f"pilottunnel-candidate-install-summary-{candidate.adapter}-{role}.json",
    )
    if not install_payload.get("ok"):
        _mark_candidate(link, candidate.adapter, "test_failed")
        state.active_link_candidates.pop(link.id, None)
        return {
            "ok": False,
            "action": "candidate-start",
            "message": install_payload.get("message", "Service installation failed"),
            "link_id": link.id,
            "link_label": link.label,
            "role": role,
            "candidate": _candidate_payload(candidate),
            "install": install_payload,
            "next_instruction": "Review the staged unit failure and re-run candidate prepare-all before retrying.",
            "real_systemd_touched": False,
            "services_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }

    reload_payload = apply_reload(target_dir=SYSTEMD_TARGET_DIR, confirm=RELOAD_CONFIRM_TOKEN, audit_path=paths.audit_path)
    if not reload_payload.get("ok"):
        _rollback_service_install(install_payload, audit_path=paths.audit_path)
        _mark_candidate(link, candidate.adapter, "test_failed")
        state.active_link_candidates.pop(link.id, None)
        return {
            "ok": False,
            "action": "candidate-start",
            "message": reload_payload.get("errors", ["systemd daemon-reload failed"])[0],
            "link_id": link.id,
            "link_label": link.label,
            "role": role,
            "candidate": _candidate_payload(candidate),
            "install": install_payload,
            "daemon_reload": reload_payload,
            "rolled_back": True,
            "next_instruction": "Review the daemon-reload failure. PilotTunnel restored the staged unit installation snapshot.",
            "real_systemd_touched": True,
            "services_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }

    started: list[dict[str, Any]] = []
    start_results: list[dict[str, Any]] = []
    for service in side_plan["services"]:
        start_payload = apply_start(
            service_dir=Path(service["service_dir"]),
            service_name=service["service_name"],
            confirm=START_CONFIRM_TOKEN,
            audit_path=paths.audit_path,
        )
        start_results.append(start_payload)
        if not start_payload.get("ok"):
            for started_service in reversed(started):
                apply_stop(
                    service_dir=Path(started_service["service_dir"]),
                    service_name=started_service["service_name"],
                    confirm=STOP_CONFIRM_TOKEN,
                    audit_path=paths.audit_path,
                )
            _rollback_service_install(install_payload, audit_path=paths.audit_path)
            _mark_candidate(link, candidate.adapter, "test_failed")
            state.active_link_candidates.pop(link.id, None)
            return {
                "ok": False,
                "action": "candidate-start",
                "message": start_payload.get("errors", ["systemd start failed"])[0],
                "link_id": link.id,
                "link_label": link.label,
                "role": role,
                "candidate": _candidate_payload(candidate),
                "install": install_payload,
                "daemon_reload": reload_payload,
                "start": start_results,
                "rolled_back": True,
                "next_instruction": "Inspect the staged unit logs, then retry the candidate after resolving the startup blocker.",
                "real_systemd_touched": True,
                "services_started": False,
                "firewall_touched": False,
                "routes_touched": False,
            }
        started.append(service)

    status_payload = inspect_managed_status(
        service_dir=Path(side_plan["service_dir"]),
        service_name=None,
        audit_path=paths.audit_path,
    )
    status_services = {item.get("service_name", ""): item for item in status_payload.get("services", [])}
    inactive_services = [
        service["service_name"]
        for service in side_plan["services"]
        if status_services.get(service["service_name"], {}).get("active_state") != "active"
    ]
    if not status_payload.get("ok") or inactive_services:
        for started_service in reversed(started):
            apply_stop(
                service_dir=Path(started_service["service_dir"]),
                service_name=started_service["service_name"],
                confirm=STOP_CONFIRM_TOKEN,
                audit_path=paths.audit_path,
            )
        _rollback_service_install(install_payload, audit_path=paths.audit_path)
        _mark_candidate(link, candidate.adapter, "test_failed")
        state.active_link_candidates.pop(link.id, None)
        return {
            "ok": False,
            "action": "candidate-start",
            "message": (
                status_payload.get("errors", ["Candidate start verification failed because systemd reported inactive service state"])[0]
                if not status_payload.get("ok")
                else f"Candidate start verification failed because systemd reported inactive service state for: {', '.join(sorted(inactive_services))}"
            ),
            "link_id": link.id,
            "link_label": link.label,
            "role": role,
            "candidate": _candidate_payload(candidate),
            "install": install_payload,
            "daemon_reload": reload_payload,
            "start": start_results,
            "systemd_status": status_payload,
            "rolled_back": True,
            "next_instruction": "Review the systemd status output, then retry the candidate after the service reports active.",
            "real_systemd_touched": True,
            "services_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }

    _mark_candidate(link, candidate.adapter, "running")
    state.active_link_candidates[link.id]["state"] = "running"
    state.active_link_candidates[link.id]["updated_at"] = _now_utc()
    return {
        "ok": True,
        "action": "candidate-start",
        "message": "Candidate service started",
        "link_id": link.id,
        "link_label": link.label,
        "role": role,
        "candidate": _candidate_payload(candidate),
        "install": install_payload,
        "daemon_reload": reload_payload,
        "start": start_results,
        "systemd_status": status_payload,
        "next_instruction": _start_next_instruction(role, candidate),
        "real_systemd_touched": True,
        "services_started": True,
        "firewall_touched": False,
        "routes_touched": False,
    }


def stop_candidate(
    *,
    config: AppConfig,
    state: AppState,
    paths: SwitchPaths,
    adapter_name: str | None = None,
    link_label: str | None = None,
) -> dict[str, Any]:
    del adapter_name
    role = _require_role(config)
    link = _target_link(config, link_label)
    active = state.active_link_candidates.get(link.id) or {}
    active_adapter = str(active.get("adapter") or "")
    if not active_adapter:
        return {
            "ok": False,
            "action": "candidate-stop",
            "message": "No active candidate is running on this server for the selected link",
            "link_id": link.id,
            "link_label": link.label,
            "role": role,
            "real_systemd_touched": False,
            "services_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }
    candidate = _candidate_for(link, active_adapter)
    services = list(active.get("services") or [])
    stop_results: list[dict[str, Any]] = []
    errors: list[str] = []
    for service in reversed(services):
        payload = apply_stop(
            service_dir=Path(service["service_dir"]),
            service_name=service["service_name"],
            confirm=STOP_CONFIRM_TOKEN,
            audit_path=paths.audit_path,
        )
        stop_results.append(payload)
        if not payload.get("ok"):
            errors.extend(payload.get("errors", []) or [payload.get("message", "Candidate stop failed")])
    if errors:
        return {
            "ok": False,
            "action": "candidate-stop",
            "message": errors[0],
            "link_id": link.id,
            "link_label": link.label,
            "role": role,
            "candidate": _candidate_payload(candidate),
            "stop": stop_results,
            "real_systemd_touched": True,
            "services_started": False,
            "firewall_touched": False,
            "routes_touched": False,
        }

    runtime_dirs = {service.get("runtime_dir", "") for service in services if service.get("runtime_dir")}
    service_dirs = {service.get("service_dir", "") for service in services if service.get("service_dir")}
    for runtime_dir in runtime_dirs:
        _safe_remove_tree(Path(runtime_dir), paths.work_dir)
    for service_dir in service_dirs:
        _safe_remove_tree(Path(service_dir), paths.work_dir)

    _mark_candidate(link, candidate.adapter, "stopped")
    state.active_link_candidates.pop(link.id, None)
    return {
        "ok": True,
        "action": "candidate-stop",
        "message": "Candidate service stopped and runtime files cleaned",
        "link_id": link.id,
        "link_label": link.label,
        "role": role,
        "candidate": _candidate_payload(candidate),
        "stop": stop_results,
        "next_instruction": _stop_next_instruction(role),
        "real_systemd_touched": True,
        "services_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def smoke_test_candidate(
    *,
    config: AppConfig,
    state: AppState,
    paths: SwitchPaths,
    adapter_name: str,
    attempts: int = 3,
    timeout: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    link_label: str | None = None,
) -> dict[str, Any]:
    role = _require_role(config)
    if role != "controller":
        raise ValueError("Real candidate smoke tests must be run from the controller side")
    link = _target_link(config, link_label)
    candidate = _candidate_for(link, adapter_name)
    side_plan = _side_plan(candidate, role)
    runtime_status = _candidate_runtime_systemd_status(audit_path=paths.audit_path, candidate=candidate, role=role)
    active = state.active_link_candidates.get(link.id) or {}
    runtime_is_active = runtime_status.get("state") == "active"
    if runtime_is_active:
        reconciled = {
            "adapter": candidate.adapter,
            "transport": candidate.transport,
            "category": candidate.category,
            "role": role,
            "state": "running",
            "services": list(side_plan.get("services") or active.get("services") or []),
            "updated_at": _now_utc(),
        }
        state.active_link_candidates[link.id] = reconciled
        _mark_candidate(link, candidate.adapter, "running")
        active = reconciled
    if active.get("adapter") != candidate.adapter or active.get("state") != "running":
        return _failure(
            "candidate-smoke-test",
            _candidate_runtime_blocker(candidate, runtime_status),
            link=link,
            candidate=candidate,
            role=role,
        )
    real_port = int(candidate.topology.get("ports", {}).get("controller_user_facing_port") or candidate.probe.get("port") or 0)
    probe_port = int(candidate.probe.get("port") or 0)
    if real_port < 1:
        return _failure(
            "candidate-smoke-test",
            "Candidate real service mapping is missing. Re-run candidate prepare-all first.",
            link=link,
            candidate=candidate,
            role=role,
        )
    if attempts < 1:
        attempts = 1
    attempt_results: list[dict[str, Any]] = []
    success_count = 0
    failure_count = 0
    connect_latencies: list[float] = []
    roundtrip_latencies: list[float] = []
    errors: list[str] = []
    for _index in range(attempts):
        result = probe_roundtrip(host="127.0.0.1", port=real_port, timeout=timeout).to_dict()
        attempt_results.append(result)
        if result["ok"]:
            success_count += 1
            if result["connect_latency_ms"] is not None:
                connect_latencies.append(float(result["connect_latency_ms"]))
            if result["roundtrip_latency_ms"] is not None:
                roundtrip_latencies.append(float(result["roundtrip_latency_ms"]))
        else:
            failure_count += 1
            if result["error"]:
                errors.append(result["error"])
    real_pass = bool(attempt_results) and failure_count == 0 and success_count == len(attempt_results)
    _mark_candidate(link, candidate.adapter, "test_passed" if real_pass else "test_failed")
    summary = {
        "checked_at": _now_utc(),
        "adapter": candidate.adapter,
        "category": candidate.category,
        "smoke_mode": "real_service" if real_port else "probe_only",
        "attempt_count": attempts,
        "timeout": timeout,
        "controller_role": candidate.topology.get("controller_process_role", ""),
        "worker_role": candidate.topology.get("worker_process_role", ""),
        "persisted_state": candidate.state,
        "runtime_systemd_state": runtime_status["state"],
        "runtime_systemd_status": runtime_status["status"],
        "tested_ports": {
            "real_service_port": real_port,
            "probe_port": probe_port,
            "transport_port": link.transport_port,
            "controller_user_facing_port": link.controller_user_facing_port,
            "worker_service_port": link.worker_service_port,
        },
        "success_count": success_count,
        "failure_count": failure_count,
        "average_connect_latency_ms": round(sum(connect_latencies) / len(connect_latencies), 3) if connect_latencies else None,
        "average_roundtrip_latency_ms": round(sum(roundtrip_latencies) / len(roundtrip_latencies), 3) if roundtrip_latencies else None,
        "real_pass": real_pass,
        "results": attempt_results,
        "errors": sorted(set(filter(None, errors))),
    }
    if probe_port >= 1:
        summary["probe_roundtrip"] = probe_roundtrip(host="127.0.0.1", port=probe_port, timeout=timeout).to_dict()
    candidate.last_result = summary
    candidate.history.append(summary)
    if real_pass and link.pairing_state == "awaiting_worker_import":
        link.pairing_state = "paired"
        link.status = "paired"
    return {
        "ok": real_pass,
        "action": "candidate-smoke-test",
        "message": "Real end-to-end candidate smoke test passed" if real_pass else "Real end-to-end candidate smoke test failed",
        "link_id": link.id,
        "link_label": link.label,
        "role": role,
        "candidate": _candidate_payload(candidate),
        "result": summary,
        "runtime_systemd_state": runtime_status["state"],
        "persisted_state": candidate.state,
        "pairing_state": link.effective_pairing_state,
        "next_instruction": _smoke_next_instruction(candidate, real_pass),
        "real_systemd_touched": False,
        "services_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def candidate_results(
    *,
    config: AppConfig,
    paths: SwitchPaths,
    link_label: str | None = None,
) -> dict[str, Any]:
    role = _require_role(config)
    link = _target_link(config, link_label)
    candidate_payloads: list[dict[str, Any]] = []
    for candidate in link.candidates:
        payload = _candidate_payload(candidate)
        runtime = _candidate_runtime_systemd_status(audit_path=paths.audit_path, candidate=candidate, role=role, allow_missing=True)
        payload["persisted_state"] = candidate.state
        payload["runtime_systemd_state"] = runtime["state"]
        payload["runtime_systemd_ok"] = runtime["ok"]
        candidate_payloads.append(payload)
    return {
        "ok": True,
        "action": "candidate-results",
        "link_id": link.id,
        "link_label": link.label,
        "role": role,
        "pairing_state": link.effective_pairing_state,
        "candidates": candidate_payloads,
        "real_systemd_touched": False,
        "services_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def _reset_candidate(candidate: LinkCandidate, *, adapter_name: str, transport: str, local_role: str) -> None:
    candidate.adapter = adapter_name
    candidate.transport = transport
    candidate.state = "config_only"
    candidate.selected = False
    candidate.first_start_side = ""
    candidate.runnable = False
    candidate.local_role = local_role
    candidate.category = ""
    candidate.controller_service_name = ""
    candidate.worker_service_name = ""
    candidate.controller_config_path = ""
    candidate.worker_config_path = ""
    candidate.controller_runtime_config_path = ""
    candidate.worker_runtime_config_path = ""
    candidate.controller_service_dir = ""
    candidate.worker_service_dir = ""
    candidate.controller_runtime_dir = ""
    candidate.worker_runtime_dir = ""
    candidate.controller_unit_path = ""
    candidate.worker_unit_path = ""
    candidate.controller_executable = ""
    candidate.worker_executable = ""
    candidate.controller_owned_ports = []
    candidate.worker_owned_ports = []
    candidate.controller_command_summary = []
    candidate.worker_command_summary = []
    candidate.controller_environment_summary = {}
    candidate.worker_environment_summary = {}
    candidate.healthchecks = []
    candidate.topology = {}
    candidate.probe = {}
    candidate.warnings = []
    candidate.blockers = []
    candidate.notes = ""


def _stage_candidate(
    *,
    config: AppConfig,
    state: AppState,
    paths: SwitchPaths,
    link: LinkProfile,
    candidate: LinkCandidate,
    topology: dict[str, Any],
    probe: dict[str, Any],
    requested_platform: str | None,
    warnings: list[str],
    blockers: list[str],
    notes: list[str],
) -> dict[str, Any]:
    adapter = ADAPTERS[candidate.adapter]()
    side_payloads: dict[str, dict[str, Any]] = {}
    runnable = True
    for side_name in ("controller", "worker"):
        side_payload = _stage_side(
            config=config,
            state=state,
            paths=paths,
            link=link,
            adapter_name=candidate.adapter,
            transport=candidate.transport,
            adapter=adapter,
            side_name=side_name,
            topology=topology,
            probe=probe,
            requested_platform=requested_platform,
            warnings=warnings,
            blockers=blockers,
        )
        side_payloads[side_name] = side_payload
        runnable = runnable and side_payload["runnable"]

    topology["sides"] = {name: payload["side"] for name, payload in side_payloads.items()}
    ports = topology.get("ports", {})
    candidate.healthchecks = [
        {
            "label": "controller_user_facing_port",
            "host": "127.0.0.1",
            "port": ports.get("controller_user_facing_port", probe["port"]),
            "mode": "real",
        },
        {
            "label": "worker_service_port",
            "host": probe["worker_bind_host"],
            "port": ports.get("worker_service_port", probe["port"]),
            "mode": "real",
        },
        {
            "label": "probe_responder",
            "host": probe["worker_bind_host"],
            "port": probe["port"],
            "mode": "probe",
        },
    ]

    controller_side = side_payloads["controller"]["side"]
    worker_side = side_payloads["worker"]["side"]
    candidate.controller_service_name = controller_side["primary_service_name"]
    candidate.worker_service_name = worker_side["primary_service_name"]
    candidate.controller_config_path = controller_side["config_path"]
    candidate.worker_config_path = worker_side["config_path"]
    candidate.controller_runtime_config_path = controller_side["runtime_config_path"]
    candidate.worker_runtime_config_path = worker_side["runtime_config_path"]
    candidate.controller_service_dir = controller_side["service_dir"]
    candidate.worker_service_dir = worker_side["service_dir"]
    candidate.controller_runtime_dir = controller_side["runtime_dir"]
    candidate.worker_runtime_dir = worker_side["runtime_dir"]
    candidate.controller_unit_path = controller_side["unit_path"]
    candidate.worker_unit_path = worker_side["unit_path"]
    candidate.controller_executable = controller_side["executable"]
    candidate.worker_executable = worker_side["executable"]
    candidate.controller_owned_ports = controller_side["owned_ports"]
    candidate.worker_owned_ports = worker_side["owned_ports"]
    candidate.controller_command_summary = controller_side["command_summary"]
    candidate.worker_command_summary = worker_side["command_summary"]
    candidate.controller_environment_summary = controller_side["environment_summary"]
    candidate.worker_environment_summary = worker_side["environment_summary"]

    if topology["category"] == "direct_l4_baseline":
        notes.append("Realm is prepared as a genuine direct Layer 4 baseline only; it is not treated as a two-sided tunnel.")
    if candidate.adapter == "bore":
        notes.append("Bore uses its fixed control port 7835 automatically while the paired transport port remains unchanged for other adapters.")
        warnings.append("Bore requires local TCP 7835 availability on the controller side and may require external provider/firewall reachability.")
    return {"runnable": runnable and not blockers}


def _stage_side(
    *,
    config: AppConfig,
    state: AppState,
    paths: SwitchPaths,
    link: LinkProfile,
    adapter_name: str,
    transport: str,
    adapter: Any,
    side_name: str,
    topology: dict[str, Any],
    probe: dict[str, Any],
    requested_platform: str | None,
    warnings: list[str],
    blockers: list[str],
) -> dict[str, Any]:
    side_template = topology["sides"][side_name]
    profile = _candidate_profile(link, side_name, probe, topology)
    context = _candidate_context(
        profile=profile,
        link=link,
        adapter_name=adapter_name,
        transport=transport,
        role=side_name,
        paths=paths,
        topology=topology,
        probe=probe,
    )
    ok, reason = adapter.precheck(context)
    if not ok:
        blockers.append(reason)
        return {"runnable": False, "side": _empty_side_payload(side_name, side_template)}

    try:
        rendered = adapter.render_config(context)
    except (KeyError, OSError, ValueError) as exc:
        blockers.append(str(exc))
        return {"runnable": False, "side": _empty_side_payload(side_name, side_template)}
    runtime_config_path = ""
    service_dir = str(_role_service_dir(paths, link, adapter_name, side_name))
    runtime_dir = str(_role_runtime_dir(paths, link, adapter_name, side_name))
    unit_path = ""
    executable = ""
    command_summary: list[str] = []
    environment_summary: dict[str, Any] = {}
    services: list[dict[str, Any]] = []
    primary_service_name = ""
    primary_unit_path = ""
    runnable = True

    if side_template["adapter_enabled"]:
        if adapter_name == "bore" and side_name == "controller" and config.node.normalized_role == "controller":
            occupied = _occupied_ports([BORE_CONTROL_PORT])
            if occupied:
                runnable = False
                owner = occupied[0].get("owner") or "owner unavailable"
                blockers.append(f"Bore fixed control port {BORE_CONTROL_PORT} is already in use on this controller ({owner})")
        try:
            binary_resolution = resolve_binary_reference(
                adapter=adapter_name,
                component=_runtime_component_for_side(adapter_name, side_name),
                config=config,
                state=state,
                requested_platform=requested_platform,
            )
        except (KeyError, ValueError) as exc:
            binary_resolution = {"ok": False, "message": str(exc), "path": ""}
        if not binary_resolution.get("ok"):
            runnable = False
            blockers.append(binary_resolution.get("message", f"Binary resolution failed for adapter '{adapter_name}'"))
        else:
            executable = binary_resolution["path"]
            try:
                runtime = adapter.render_runtime_plan(context, Path(runtime_dir), executable)
            except (KeyError, OSError, ValueError) as exc:
                runnable = False
                blockers.append(str(exc))
                runtime = None
            if runtime is not None:
                runtime_config_path = runtime["config_path"]
                command_summary = _redacted_command_summary(runtime.get("argv", []))
                environment_summary = redact_secrets(runtime.get("environment", {}))
                unit = render_unit_file(
                    unit_name=adapter.service_name(context),
                    description=f"PilotTunnel {link.label} {adapter_name} {transport} {side_name}",
                    command=shlex.join(runtime.get("argv", [])),
                    output_dir=Path(service_dir),
                    apply_changes=True,
                    environment=runtime.get("environment", {}),
                )
                unit_path = unit.path
                primary_unit_path = unit.path
                primary_service_name = adapter.service_name(context)
                services.append(
                    {
                        "kind": "adapter",
                        "service_name": adapter.service_name(context),
                        "service_dir": service_dir,
                        "runtime_dir": runtime_dir,
                        "unit_path": unit.path,
                    }
                )

    if side_name == "worker":
        probe_service = _render_probe_service(
            paths=paths,
            link=link,
            adapter_name=adapter_name,
            probe=probe,
        )
        services.insert(0, probe_service)
        if not primary_service_name:
            primary_service_name = probe_service["service_name"]
            primary_unit_path = probe_service["unit_path"]
        if probe["port"] not in side_template["owned_ports"]:
            side_template["owned_ports"] = sorted(set(side_template["owned_ports"] + [probe["port"]]))

    return {
        "runnable": runnable and bool(services),
        "side": {
            "role": side_name,
            "process_role": side_template["process_role"],
            "config_path": rendered.get("config_path", ""),
            "runtime_config_path": runtime_config_path,
            "service_dir": service_dir,
            "runtime_dir": runtime_dir,
            "unit_path": primary_unit_path or unit_path,
            "primary_service_name": primary_service_name,
            "services": services,
            "dependency_ports": list(side_template.get("dependency_ports") or []),
            "executable": executable,
            "owned_ports": sorted(set(side_template["owned_ports"])),
            "listens_on": list(side_template["listens_on"]),
            "connects_to": list(side_template["connects_to"]),
            "command_summary": command_summary,
            "environment_summary": environment_summary,
        },
    }


def _candidate_ports(link: LinkProfile, adapter_name: str, probe: dict[str, Any]) -> dict[str, int]:
    probe_port = int(probe["port"])
    controller_user_facing_port = int(link.controller_user_facing_port or probe_port)
    worker_service_port = int(link.worker_service_port or probe_port)
    transport_port = int(link.transport_port)
    effective_transport_port = BORE_CONTROL_PORT if adapter_name == "bore" else transport_port
    return {
        "probe_port": probe_port,
        "controller_user_facing_port": controller_user_facing_port,
        "worker_service_port": worker_service_port,
        "transport_port": transport_port,
        "effective_transport_port": effective_transport_port,
    }


def _candidate_profile(link: LinkProfile, role: str, probe: dict[str, Any], topology: dict[str, Any]) -> Profile:
    ports = topology.get("ports") or _candidate_ports(link, topology.get("adapter", ""), probe)
    controller_user_facing_port = int(ports["controller_user_facing_port"])
    worker_service_port = int(ports["worker_service_port"])
    transport_port = int(ports["transport_port"])
    probe_port = int(ports["probe_port"])
    target_host = link.worker_address if role == "controller" else link.controller_address
    target_port = worker_service_port if role == "controller" else transport_port
    return Profile(
        name=link.label,
        main_port=controller_user_facing_port if role == "controller" else worker_service_port,
        target_host=target_host,
        target_port=target_port,
        role=role,
        active_layer="layer4",
        ports=ProfilePorts(
            main_port=controller_user_facing_port if role == "controller" else worker_service_port,
            control_port=transport_port,
            service_port=worker_service_port,
            check_port=probe_port,
        ),
        safety=ProfileSafety(cooldown_seconds=0, rollback_on_failure=True, dry_run_default=False),
    )


def _candidate_context(
    *,
    profile: Profile,
    link: LinkProfile,
    adapter_name: str,
    transport: str,
    role: str,
    paths: SwitchPaths,
    topology: dict[str, Any],
    probe: dict[str, Any],
) -> AdapterContext:
    return AdapterContext(
        profile=profile,
        transport=transport,
        work_dir=paths.work_dir / "candidates" / link.id / adapter_name / role / "work",
        staging_root=paths.staging_root / "candidates" / link.label,
        apply_changes=True,
        role=role,
        remote_stub={
            "mode": "candidate-smoke",
            "category": topology["category"],
            "probe_port": probe["port"],
            "probe_bind_host": probe["worker_bind_host"],
            "real_controller_user_facing_port": topology.get("ports", {}).get("controller_user_facing_port", link.controller_user_facing_port or 0),
            "real_worker_service_port": topology.get("ports", {}).get("worker_service_port", link.worker_service_port or 0),
            "real_transport_port": topology.get("ports", {}).get("effective_transport_port", link.transport_port),
            "gost_tunnel_id": topology.get("gost_tunnel_id", ""),
            "gost_probe_host": topology.get("gost_probe_host", ""),
            "bore_control_port": topology.get("bore_control_port", BORE_CONTROL_PORT),
            "paired_transport_port": link.transport_port,
            "paired_user_facing_port": link.controller_user_facing_port or 0,
            "paired_service_port": link.worker_service_port,
        },
        link_id=link.id,
        link_label=link.label,
        controller_address=link.controller_address,
        worker_address=link.worker_address,
        secrets=_derived_secrets(link, adapter_name),
    )


def _build_topology(link: LinkProfile, adapter_name: str, transport: str, probe: dict[str, Any]) -> dict[str, Any]:
    probe_port = int(probe["port"])
    ports = _candidate_ports(link, adapter_name, probe)
    controller_user_facing_port = ports["controller_user_facing_port"]
    worker_service_port = ports["worker_service_port"]
    transport_port = ports["transport_port"]
    effective_transport_port = ports["effective_transport_port"]
    real_service_path = f"127.0.0.1:{controller_user_facing_port} -> {adapter_name} -> 127.0.0.1:{worker_service_port}"
    category = CANDIDATE_CATEGORIES[adapter_name]
    if category == "direct_l4_baseline":
        return {
            "adapter": adapter_name,
            "transport": transport,
            "category": category,
            "ports": ports,
            "real_service_path": real_service_path,
            "controller_process_role": "direct_forwarder",
            "worker_process_role": "probe_responder_only",
            "listening_side": "worker_probe_responder",
            "connecting_side": "controller_direct_forwarder",
            "first_start_side": "worker",
            "supports_runtime": True,
            "gost_tunnel_id": "",
            "gost_probe_host": "",
            "bore_control_port": 0,
            "sides": {
                "controller": {
                    "process_role": "direct_forwarder",
                    "adapter_enabled": True,
                    "owned_ports": [controller_user_facing_port],
                    "dependency_ports": [worker_service_port],
                    "listens_on": [f"127.0.0.1:{controller_user_facing_port}"],
                    "connects_to": [f"{link.worker_address}:{worker_service_port}"],
                },
                "worker": {
                    "process_role": "probe_responder_only",
                    "adapter_enabled": False,
                    "owned_ports": [probe_port],
                    "dependency_ports": [worker_service_port],
                    "listens_on": [f"{probe['worker_bind_host']}:{worker_service_port}", f"{probe['worker_bind_host']}:{probe_port}"],
                    "connects_to": [],
                },
            },
        }

    process_roles = {
        "backhaul": ("server", "client"),
        "rathole": ("server", "client"),
        "frp": ("server", "client"),
        "gost": ("visitor+tunnel-server", "tunnel-client"),
        "chisel": ("server", "client"),
        "bore": ("server", "local-client"),
    }
    controller_role, worker_role = process_roles[adapter_name]
    listening_side = "controller_transport_listener"
    connecting_side = "worker_tunnel_connector"
    if adapter_name == "bore":
        controller_listens = [f"0.0.0.0:{effective_transport_port}", f"0.0.0.0:{controller_user_facing_port}"]
    else:
        controller_listens = [f"0.0.0.0:{effective_transport_port}", f"0.0.0.0:{controller_user_facing_port}"]
    return {
        "adapter": adapter_name,
        "transport": transport,
        "category": category,
        "ports": ports,
        "real_service_path": real_service_path,
        "controller_process_role": controller_role,
        "worker_process_role": worker_role,
        "listening_side": listening_side,
        "connecting_side": connecting_side,
        "first_start_side": "controller",
        "supports_runtime": True,
        "gost_tunnel_id": _uuid_from_hmac(link, adapter_name, "gost-tunnel-id") if adapter_name == "gost" else "",
        "gost_probe_host": f"probe-{link.id[-6:]}-{adapter_name}.local" if adapter_name == "gost" else "",
        "bore_control_port": BORE_CONTROL_PORT if adapter_name == "bore" else 0,
        "effective_transport_port": effective_transport_port,
        "sides": {
            "controller": {
                "process_role": controller_role,
                "adapter_enabled": True,
                "owned_ports": [effective_transport_port, controller_user_facing_port],
                "dependency_ports": [worker_service_port],
                "listens_on": controller_listens,
                "connects_to": [],
            },
            "worker": {
                "process_role": worker_role,
                "adapter_enabled": True,
                "owned_ports": [probe_port],
                "dependency_ports": [worker_service_port],
                "listens_on": [f"{probe['worker_bind_host']}:{worker_service_port}", f"{probe['worker_bind_host']}:{probe_port}"],
                "connects_to": [f"{link.controller_address}:{effective_transport_port}"],
            },
        },
    }


def _probe_plan(link: LinkProfile, adapter_name: str) -> dict[str, Any]:
    reserved = {
        int(value)
        for value in [
            link.transport_port,
            link.worker_service_port,
            link.controller_user_facing_port,
        ]
        if value
    }
    seed = hashlib.sha256(f"{link.id}:{adapter_name}:probe".encode("utf-8")).digest()
    candidate = 43000 + (int.from_bytes(seed[:2], "big") % 18000)
    while candidate in reserved:
        candidate += 1
        if candidate > 62000:
            candidate = 43000
    worker_bind_host = "0.0.0.0" if adapter_name == "realm" else "127.0.0.1"
    return {
        "port": candidate,
        "worker_bind_host": worker_bind_host,
        "controller_connect_host": "127.0.0.1",
        "path": f"127.0.0.1:{candidate} -> {adapter_name} -> {worker_bind_host}:{candidate}",
    }


def _runtime_component_for_side(adapter_name: str, side_name: str) -> str | None:
    if adapter_name != "frp":
        return None
    return "frps" if side_name == "controller" else "frpc"


def _render_probe_service(
    *,
    paths: SwitchPaths,
    link: LinkProfile,
    adapter_name: str,
    probe: dict[str, Any],
) -> dict[str, Any]:
    runtime_dir = _role_runtime_dir(paths, link, f"{adapter_name}-probe", "worker")
    service_dir = _role_service_dir(paths, link, f"{adapter_name}-probe", "worker")
    service_name = f"pilottunnel-{link.label}-{adapter_name}-{PROBE_MARKER}-worker.service"
    argv = [
        sys.executable,
        "-m",
        "pilottunnel.probe",
        "responder",
        "--bind-host",
        probe["worker_bind_host"],
        "--port",
        str(probe["port"]),
    ]
    unit = render_unit_file(
        unit_name=service_name,
        description=f"PilotTunnel {link.label} {adapter_name} worker probe responder",
        command=shlex.join(argv),
        output_dir=service_dir,
        apply_changes=True,
    )
    return {
        "kind": "probe",
        "service_name": service_name,
        "service_dir": str(service_dir),
        "runtime_dir": str(runtime_dir),
        "unit_path": unit.path,
    }


def _install_candidate_service_units(*, services: list[dict[str, Any]], summary_name: str) -> dict[str, Any]:
    installed_services: list[dict[str, Any]] = []
    for service in services:
        service_name = str(service["service_name"])
        if not service_name.endswith(".service") or "/" in service_name or "\\" in service_name or ".." in service_name:
            return _install_failure(f"Unsafe managed service name: {service_name}", installed_services, summary_name)
        staged_unit = Path(service["unit_path"]).resolve()
        if not staged_unit.exists():
            return _install_failure(f"Prepared staged service unit is missing: {staged_unit}", installed_services, summary_name)
        if staged_unit.is_symlink():
            return _install_failure(f"Symlink escape blocked for staged service unit: {staged_unit}", installed_services, summary_name)
        content = staged_unit.read_text(encoding="utf-8")
        if "# Managed-by: PilotTunnel" not in content:
            return _install_failure(f"Prepared service unit is not marked as PilotTunnel-owned: {staged_unit}", installed_services, summary_name)
        try:
            _validate_parent_chain(SYSTEMD_TARGET_DIR)
            target_path = (SYSTEMD_TARGET_DIR / service_name).resolve()
            _validate_parent_chain(target_path.parent)
        except ValueError as exc:
            return _install_failure(str(exc), installed_services, summary_name)
        backup_path = Path()
        if target_path.exists() and target_path.is_symlink():
            return _install_failure(f"Symlink escape blocked for target service unit: {target_path}", installed_services, summary_name)
        if target_path.exists():
            backup_root = (SYSTEMD_TARGET_DIR / ".pilottunnel-candidate-backups").resolve()
            backup_root.mkdir(parents=True, exist_ok=True)
            try:
                _validate_parent_chain(backup_root)
            except ValueError as exc:
                return _install_failure(str(exc), installed_services, summary_name)
            backup_path = backup_root / f"{service_name}.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.bak"
            shutil.copy2(target_path, backup_path)
        target_path.write_text(content, encoding="utf-8")
        if os.name != "nt":
            target_path.chmod(0o644)
        installed_services.append(
            {
                "service_name": service_name,
                "staged_unit_path": str(staged_unit),
                "target_unit_path": str(target_path),
                "backup_path": str(backup_path) if backup_path else "",
                "kind": service["kind"],
            }
        )
    payload = {
        "ok": True,
        "action": "candidate-service-install",
        "message": "Prepared candidate service unit installed",
        "target_dir": str(SYSTEMD_TARGET_DIR),
        "summary_file": str((SYSTEMD_TARGET_DIR / summary_name).resolve()),
        "services": installed_services,
    }
    Path(payload["summary_file"]).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return payload


def _install_failure(message: str, services: list[dict[str, Any]], summary_name: str) -> dict[str, Any]:
    return {
        "ok": False,
        "action": "candidate-service-install",
        "message": message,
        "services": services,
        "target_dir": str(SYSTEMD_TARGET_DIR),
        "summary_file": str((SYSTEMD_TARGET_DIR / summary_name).resolve()),
    }


def _validate_parent_chain(path: Path) -> None:
    current = path
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError(f"Symlink escape blocked for path: {current}")
        if current.parent == current:
            return
        current = current.parent


def _rollback_service_install(payload: dict[str, Any], *, audit_path: Path) -> None:
    services = payload.get("services") or []
    summary_file = payload.get("summary_file")
    for item in services:
        target_unit_path = item.get("target_unit_path")
        backup_path = item.get("backup_path")
        if not target_unit_path:
            continue
        target = Path(target_unit_path)
        if backup_path:
            backup = Path(backup_path)
            if backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
                continue
        if target.exists():
            target.unlink()
    if summary_file:
        summary_path = Path(summary_file)
        if summary_path.exists():
            summary_path.unlink()
    apply_reload(target_dir=SYSTEMD_TARGET_DIR, confirm=RELOAD_CONFIRM_TOKEN, audit_path=audit_path)


def _derived_secrets(link: LinkProfile, adapter_name: str) -> dict[str, str]:
    shared = _derive_hmac(link.pairing_secret, link.id, adapter_name, "shared-token")
    return {
        "shared_token": shared,
        "auth_user": f"pt-{link.id[-6:]}",
        "auth_password": _derive_hmac(link.pairing_secret, link.id, adapter_name, "auth-password")[:32],
    }


def _derive_hmac(pairing_secret: str, link_id: str, adapter_name: str, purpose: str) -> str:
    payload = f"{link_id}:{adapter_name}:{purpose}".encode("utf-8")
    return hmac.new(pairing_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _uuid_from_hmac(link: LinkProfile, adapter_name: str, purpose: str) -> str:
    raw = _derive_hmac(link.pairing_secret, link.id, adapter_name, purpose)
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def _target_link(config: AppConfig, link_label: str | None) -> LinkProfile:
    if link_label:
        return get_link(config, link_label)
    link = get_active_link(config)
    if link is None:
        raise ValueError("No active paired link is configured")
    return link


def _candidate_for(link: LinkProfile, adapter_name: str) -> LinkCandidate:
    for candidate in link.candidates:
        if candidate.adapter == adapter_name:
            return candidate
    raise ValueError(f"Candidate '{adapter_name}' has not been prepared for link '{link.label}'")


def _candidate_service_name(candidate: LinkCandidate, role: str) -> str:
    if role == "controller":
        return candidate.controller_service_name
    if role == "worker":
        return candidate.worker_service_name
    return ""


def _candidate_runtime_systemd_status(*, audit_path: Path, candidate: LinkCandidate, role: str, allow_missing: bool = False) -> dict[str, Any]:
    service_name = _candidate_service_name(candidate, role)
    if not service_name:
        return {
            "ok": False,
            "state": "unknown",
            "service_name": "",
            "status": {"ok": False, "services": [], "warnings": [], "errors": ["Candidate service name is unavailable"]},
        }
    try:
        payload = inspect_managed_status(
            service_dir=SYSTEMD_TARGET_DIR,
            service_name=service_name,
            audit_path=audit_path,
        )
    except ValueError as exc:
        if allow_missing:
            return {
                "ok": False,
                "state": "missing",
                "service_name": service_name,
                "status": {"ok": False, "services": [], "warnings": [], "errors": [str(exc)]},
            }
        raise

    services = payload.get("services", [])
    service_entry = next((item for item in services if item.get("service_name") == service_name), {})
    active_state = str(service_entry.get("active_state") or "").strip()
    sub_state = str(service_entry.get("sub_state") or "").strip()
    ok = payload.get("ok", False) and active_state == "active"
    if allow_missing and not service_entry:
        return {
            "ok": False,
            "state": "missing",
            "service_name": service_name,
            "status": payload,
        }
    runtime_state = "active" if ok and sub_state in {"running", "exited", "listening"} else active_state or ("inactive" if service_entry else "missing")
    return {
        "ok": ok,
        "state": runtime_state,
        "service_name": service_name,
        "service_entry": service_entry,
        "status": payload,
    }


def _candidate_runtime_blocker(candidate: LinkCandidate, runtime_status: dict[str, Any]) -> str:
    state = runtime_status.get("state") or "missing"
    service_name = runtime_status.get("service_name") or _candidate_service_name(candidate, "controller") or candidate.adapter
    if state == "active":
        return f"Candidate '{candidate.adapter}' is not currently running on this controller"
    if state == "missing":
        return f"Candidate '{candidate.adapter}' service '{service_name}' is not installed or not discoverable on this controller"
    return f"Candidate '{candidate.adapter}' is not currently running on this controller (systemd state: {state})"


def _require_role(config: AppConfig) -> str:
    role = config.node.normalized_role
    if not role:
        raise ValueError("Node role is not initialized")
    return role


def _validate_link(link: LinkProfile) -> None:
    if not link.controller_address:
        raise ValueError("Paired link is missing the controller address")
    if not link.worker_address:
        raise ValueError("Paired link is missing the worker address")
    if not link.transport_port:
        raise ValueError("Paired link is missing the tunnel transport port")
    if not link.worker_service_port:
        raise ValueError("Paired link is missing the worker service port")
    if not link.controller_user_facing_port:
        raise ValueError("Paired link is missing the controller user-facing port")
    if not link.pairing_secret:
        raise ValueError("Paired link is missing the pairing secret")


def _candidate_payload(candidate: LinkCandidate) -> dict[str, Any]:
    return {
        "adapter": candidate.adapter,
        "transport": candidate.transport,
        "state": candidate.state,
        "selected": candidate.selected,
        "runnable": candidate.runnable,
        "category": candidate.category,
        "first_start_side": candidate.first_start_side,
        "controller_service_name": candidate.controller_service_name,
        "worker_service_name": candidate.worker_service_name,
        "controller_config_path": candidate.controller_config_path,
        "worker_config_path": candidate.worker_config_path,
        "controller_runtime_config_path": candidate.controller_runtime_config_path,
        "worker_runtime_config_path": candidate.worker_runtime_config_path,
        "controller_service_dir": candidate.controller_service_dir,
        "worker_service_dir": candidate.worker_service_dir,
        "controller_runtime_dir": candidate.controller_runtime_dir,
        "worker_runtime_dir": candidate.worker_runtime_dir,
        "controller_unit_path": candidate.controller_unit_path,
        "worker_unit_path": candidate.worker_unit_path,
        "controller_executable": candidate.controller_executable,
        "worker_executable": candidate.worker_executable,
        "controller_owned_ports": list(candidate.controller_owned_ports),
        "worker_owned_ports": list(candidate.worker_owned_ports),
        "controller_dependency_ports": list(candidate.topology.get("sides", {}).get("controller", {}).get("dependency_ports", [])),
        "worker_dependency_ports": list(candidate.topology.get("sides", {}).get("worker", {}).get("dependency_ports", [])),
        "controller_command_summary": list(candidate.controller_command_summary),
        "worker_command_summary": list(candidate.worker_command_summary),
        "controller_environment_summary": redact_secrets(candidate.controller_environment_summary),
        "worker_environment_summary": redact_secrets(candidate.worker_environment_summary),
        "healthchecks": list(candidate.healthchecks),
        "topology": redact_secrets(candidate.topology),
        "probe": redact_secrets(candidate.probe),
        "warnings": list(candidate.warnings),
        "blockers": list(candidate.blockers),
        "last_result": redact_secrets(candidate.last_result),
        "history_count": len(candidate.history),
        "notes": candidate.notes,
    }


def _side_plan(candidate: LinkCandidate, role: str) -> dict[str, Any]:
    topology = candidate.topology or {}
    sides = topology.get("sides") or {}
    if role not in sides:
        raise ValueError(f"Candidate '{candidate.adapter}' is missing prepared side data for role '{role}'")
    return sides[role]


def _owned_ports_for_role(candidate: LinkCandidate, role: str) -> list[int]:
    return list(_side_plan(candidate, role).get("owned_ports") or [])


def _mark_candidate(link: LinkProfile, adapter_name: str, state_name: str) -> None:
    if state_name not in CANDIDATE_STATES:
        raise ValueError(f"Unsupported candidate state '{state_name}'")
    for candidate in link.candidates:
        candidate.selected = candidate.adapter == adapter_name
        if candidate.adapter == adapter_name:
            candidate.state = state_name


def _prepare_next_instruction(role: str) -> str:
    if role == "worker":
        return "Next: open Candidate Testing and start one candidate when the controller tells you which adapter to test."
    return "Next: review a candidate plan, start the controller side first when required, then coordinate the matching worker-side start."


def _plan_next_instruction(role: str, candidate: LinkCandidate) -> str:
    first_side = candidate.first_start_side or candidate.topology.get("first_start_side", "")
    if role == first_side:
        return f"Start this adapter on the {role} server first, then continue on the other server."
    return f"Wait for the {first_side} server to start '{candidate.adapter}' first, then continue here."


def _start_next_instruction(role: str, candidate: LinkCandidate) -> str:
    adapter = candidate.adapter
    first_side = candidate.first_start_side or candidate.topology.get("first_start_side", "")
    if role == "controller":
        if first_side == "controller":
            return (
                f"Next: on the worker server open Candidate Testing, choose Start selected candidate, and start '{adapter}'. "
                f"After the worker side is running, return here and choose Run real smoke test."
            )
        return f"Next: once the worker side is already running for '{adapter}', choose Run real smoke test on this controller."
    if first_side == "worker":
        return (
            f"Next: on the controller server open Candidate Testing, choose Start selected candidate for '{adapter}', "
            f"then run the real smoke test there."
        )
    return (
        f"Next: return to the controller server, start '{adapter}' there if needed, then use Run real smoke test on the controller."
    )


def _stop_next_instruction(role: str) -> str:
    if role == "worker":
        return "Next: tell the controller that this worker-side candidate is stopped, then move to the next adapter."
    return "Next: stop the worker-side candidate too before moving to the next adapter."


def _smoke_next_instruction(candidate: LinkCandidate, real_pass: bool) -> str:
    if real_pass:
        return f"Real smoke test passed for '{candidate.adapter}'. Stop both sides before moving to the next candidate."
    return "Real smoke test did not pass. Review both sides, then stop or retry the candidate without touching unrelated services."


def _failure(action: str, message: str, *, link: LinkProfile, candidate: LinkCandidate, role: str) -> dict[str, Any]:
    return {
        "ok": False,
        "action": action,
        "message": message,
        "link_id": link.id,
        "link_label": link.label,
        "role": role,
        "candidate": _candidate_payload(candidate),
        "real_systemd_touched": False,
        "services_started": False,
        "firewall_touched": False,
        "routes_touched": False,
    }


def _role_runtime_dir(paths: SwitchPaths, link: LinkProfile, adapter_name: str, role: str) -> Path:
    target = (paths.work_dir / "candidate-runtime" / link.id / adapter_name / role).resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _role_service_dir(paths: SwitchPaths, link: LinkProfile, adapter_name: str, role: str) -> Path:
    target = (paths.work_dir / "candidate-services" / link.id / adapter_name / role).resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _redacted_command_summary(argv: list[str]) -> list[str]:
    return [str(item) for item in redact_secrets(list(argv))]


def _occupied_ports(ports: list[int]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if not ports or os.name == "nt" or not shutil.which("ss"):
        return entries
    import subprocess

    for port in sorted(set(ports)):
        command = ["ss", "-ltnp", f"sport = :{port}"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=2, check=False)
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if len(lines) <= 1:
            continue
        entries.append({"port": port, "owner": " ".join(lines[1:3])[:300]})
    return entries


def _safe_remove_tree(path: Path, base: Path) -> None:
    resolved = path.resolve()
    base_resolved = base.resolve()
    if base_resolved not in resolved.parents:
        raise ValueError(f"Refusing to remove runtime path outside work dir: {resolved}")
    current = resolved
    while True:
        if current.exists() and current.is_symlink():
            raise ValueError(f"Symlink escape blocked for candidate cleanup path: {current}")
        if current.parent == current or current == base_resolved:
            break
        current = current.parent
    if resolved.exists():
        shutil.rmtree(resolved)


def _empty_side_payload(side_name: str, side_template: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": side_name,
        "process_role": side_template["process_role"],
        "config_path": "",
        "runtime_config_path": "",
        "service_dir": "",
        "runtime_dir": "",
        "unit_path": "",
        "primary_service_name": "",
        "services": [],
        "executable": "",
        "owned_ports": list(side_template["owned_ports"]),
        "dependency_ports": list(side_template.get("dependency_ports") or []),
        "listens_on": list(side_template["listens_on"]),
        "connects_to": list(side_template["connects_to"]),
        "command_summary": [],
        "environment_summary": {},
    }


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()
