"""Local end-to-end simulation for controller/worker workflows."""

from __future__ import annotations

import io
import json
import shutil
import socket
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .audit import write_audit_log
from .config import validate_profile_name


@dataclass
class SimulationRoots:
    run_root: Path
    controller_root: Path
    worker_root: Path
    bundle_root: Path
    audit_path: Path


def run_e2e_simulation(
    *,
    profile: str,
    adapter: str,
    transport: str,
    base_root: Path | None = None,
    keep_files: bool = False,
) -> dict[str, Any]:
    profile_name = validate_profile_name(profile)
    roots = _prepare_roots(profile_name, base_root)
    ports, listeners = _allocate_test_ports()
    try:
        controller = _controller_paths(roots.controller_root)
        worker = _worker_paths(roots.worker_root)
        bundle_path = roots.bundle_root / f"{profile_name}-worker.json"
        bundle_path.parent.mkdir(parents=True, exist_ok=True)

        controller_init = _run_cli(
            controller,
            "init",
            "--role",
            "controller",
        )
        _ensure_ok(controller_init, "controller init")
        _run_profile_create(controller, profile_name, "127.0.0.1", adapter, transport, ports)

        export_result = _run_cli(
            controller,
            "bundle",
            "export-worker",
            "--profile",
            profile_name,
            "--adapter",
            adapter,
            "--transport",
            transport,
            "--output",
            str(bundle_path),
        )
        _ensure_ok(export_result, "bundle export-worker")

        worker_init = _run_cli(worker, "init", "--role", "worker")
        _ensure_ok(worker_init, "worker init")
        inspect_result = _run_cli(worker, "bundle", "inspect", "--input", str(bundle_path))
        _ensure_ok(inspect_result, "bundle inspect")
        import_result = _run_cli(
            worker,
            "bundle",
            "import",
            "--input",
            str(bundle_path),
            "--staging-root",
            str(worker.staging_root),
            "--confirm",
            "IMPORT",
        )
        _ensure_ok(import_result, "bundle import")

        worker_switch_guard = _run_cli(worker, "switch", "--profile", profile_name, "--adapter", adapter, "--transport", transport)

        controller_switch = _run_cli(
            controller,
            "--apply",
            "switch",
            "--profile",
            profile_name,
            "--adapter",
            adapter,
            "--transport",
            transport,
        )
        _ensure_ok(controller_switch, "controller staged apply")

        controller_install = _run_cli(
            controller,
            "install",
            "plan",
            "--profile",
            profile_name,
            "--adapter",
            adapter,
            "--transport",
            transport,
            "--install-root",
            str(controller.install_root),
        )
        _ensure_ok(controller_install, "controller install plan")

        controller_service = _run_cli(
            controller,
            "service",
            "plan",
            "--profile",
            profile_name,
            "--adapter",
            adapter,
            "--transport",
            transport,
            "--action",
            "start",
        )
        _ensure_ok(controller_service, "controller service plan")

        worker_install = _run_cli(
            worker,
            "install",
            "plan",
            "--profile",
            profile_name,
            "--adapter",
            adapter,
            "--transport",
            transport,
            "--install-root",
            str(worker.install_root),
        )
        _ensure_ok(worker_install, "worker install plan")

        worker_service = _run_cli(
            worker,
            "service",
            "plan",
            "--profile",
            profile_name,
            "--adapter",
            adapter,
            "--transport",
            transport,
            "--action",
            "start",
        )
        _ensure_ok(worker_service, "worker service plan")

        with _loopback_servers(listeners):
            controller_health = _run_cli(
                controller,
                "healthcheck",
                "--profile",
                profile_name,
                "--all",
                "--role-aware",
            )
            _ensure_ok(controller_health, "controller healthcheck")
            worker_health = _run_cli(
                worker,
                "healthcheck",
                "--profile",
                profile_name,
                "--all",
                "--role-aware",
            )
            _ensure_ok(worker_health, "worker healthcheck")

        controller_summary = json.loads(controller_health.stdout)
        worker_summary = json.loads(worker_health.stdout)
        inspect_summary = json.loads(inspect_result.stdout)
        bundle_export_summary = json.loads(export_result.stdout)
        bundle_import_summary = json.loads(import_result.stdout)
        controller_switch_summary = json.loads(controller_switch.stdout)
        controller_install_summary = json.loads(controller_install.stdout)
        controller_service_summary = json.loads(controller_service.stdout)
        worker_install_summary = json.loads(worker_install.stdout)
        worker_service_summary = json.loads(worker_service.stdout)

        result = {
            "ok": True,
            "profile": profile_name,
            "adapter": adapter,
            "transport": transport,
            "controller_root": str(roots.controller_root),
            "worker_root": str(roots.worker_root),
            "bundle_path": str(bundle_path),
            "staged_files_count": _count_files(controller.staging_root) + _count_files(worker.staging_root),
            "install_plan_count": len(controller_install_summary.get("planned_destination_files", []))
            + len(worker_install_summary.get("planned_destination_files", [])),
            "service_plan_count": len(controller_service_summary.get("plan_steps", [])) + len(worker_service_summary.get("plan_steps", [])),
            "healthcheck_summary": {
                "controller": controller_summary,
                "worker": worker_summary,
                "ok": controller_summary.get("ok", False) and worker_summary.get("ok", False),
            },
            "warnings": _collect_warnings(
                bundle_export_summary,
                inspect_summary,
                bundle_import_summary,
                controller_switch_summary,
                controller_install_summary,
                worker_install_summary,
                controller_summary,
                worker_summary,
            ),
            "real_systemd_touched": False,
            "real_firewall_touched": False,
            "routes_touched": False,
            "services_started": False,
            "downloads_performed": False,
            "worker_controller_switch_attempt": {
                "ok": worker_switch_guard.code == 0,
                "stdout": worker_switch_guard.stdout,
                "stderr": worker_switch_guard.stderr,
            },
            "no_system_changes": True,
        }
        write_audit_log(
            "simulate-e2e",
            profile_name,
            {
                "adapter": adapter,
                "transport": transport,
                "ok": True,
                "bundle_path": str(bundle_path),
                "controller_root": str(roots.controller_root),
                "worker_root": str(roots.worker_root),
                "staged_files_count": result["staged_files_count"],
                "install_plan_count": result["install_plan_count"],
                "service_plan_count": result["service_plan_count"],
            },
            roots.audit_path,
        )
        return result
    except Exception as exc:
        write_audit_log(
            "simulate-e2e",
            profile_name,
            {"adapter": adapter, "transport": transport, "ok": False, "message": str(exc)},
            roots.audit_path,
        )
        return {
            "ok": False,
            "profile": profile_name,
            "adapter": adapter,
            "transport": transport,
            "controller_root": str(roots.controller_root),
            "worker_root": str(roots.worker_root),
            "bundle_path": str(roots.bundle_root / f"{profile_name}-worker.json"),
            "staged_files_count": 0,
            "install_plan_count": 0,
            "service_plan_count": 0,
            "healthcheck_summary": {"ok": False, "controller": {}, "worker": {}},
            "warnings": [str(exc)],
            "real_systemd_touched": False,
            "real_firewall_touched": False,
            "routes_touched": False,
            "services_started": False,
            "downloads_performed": False,
            "no_system_changes": True,
        }
    finally:
        for listener in listeners:
            try:
                listener.close()
            except OSError:
                pass
        if not keep_files:
            shutil.rmtree(roots.run_root, ignore_errors=True)


@dataclass(frozen=True)
class _CliPaths:
    config: Path
    state: Path
    registry: Path
    audit_log: Path
    lock_dir: Path
    work_dir: Path
    staging_root: Path
    cache_root: Path
    install_root: Path


@dataclass
class _RunResult:
    code: int
    stdout: str
    stderr: str


def _prepare_roots(profile: str, base_root: Path | None) -> SimulationRoots:
    if base_root is not None:
        if ".." in base_root.parts:
            raise ValueError(f"Path traversal blocked for base-root: {base_root!r}")
        root = base_root.resolve() / _run_id(profile)
    else:
        root = Path.cwd() / ".var" / "pilottunnel" / "simulations" / _run_id(profile)
    controller_root = root / "controller"
    worker_root = root / "worker"
    bundle_root = root / "bundle"
    audit_path = root / "audit.log"
    for path in [controller_root, worker_root, bundle_root]:
        path.mkdir(parents=True, exist_ok=True)
    return SimulationRoots(
        run_root=root,
        controller_root=controller_root,
        worker_root=worker_root,
        bundle_root=bundle_root,
        audit_path=audit_path,
    )


def _run_id(profile: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{profile}-{stamp}-{uuid4().hex[:8]}"


def _controller_paths(root: Path) -> _CliPaths:
    return _paths_for_root(root)


def _worker_paths(root: Path) -> _CliPaths:
    return _paths_for_root(root)


def _paths_for_root(root: Path) -> _CliPaths:
    config = root / "config.json"
    state = root / "state.json"
    registry = root / "registry.json"
    audit_log = root / "audit.log"
    lock_dir = root / "locks"
    work_dir = root / "work"
    staging_root = root / "staging"
    cache_root = root / "cache"
    install_root = root / "install-root"
    return _CliPaths(
        config=config,
        state=state,
        registry=registry,
        audit_log=audit_log,
        lock_dir=lock_dir,
        work_dir=work_dir,
        staging_root=staging_root,
        cache_root=cache_root,
        install_root=install_root,
    )


def _run_cli(paths: _CliPaths, *args: str) -> _RunResult:
    from . import cli as cli_module

    argv = [
        "--config",
        str(paths.config),
        "--state",
        str(paths.state),
        "--registry",
        str(paths.registry),
        "--audit-log",
        str(paths.audit_log),
        "--lock-dir",
        str(paths.lock_dir),
        "--work-dir",
        str(paths.work_dir),
        "--staging-root",
        str(paths.staging_root),
        "--cache-root",
        str(paths.cache_root),
        *args,
    ]
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli_module.main(argv)
    return _RunResult(code=code, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def _run_profile_create(paths: _CliPaths, profile: str, target_host: str, adapter: str, transport: str, ports: dict[str, int]) -> None:
    result = _run_cli(
        paths,
        "profile",
        "create",
        "--name",
        profile,
        "--main-port",
        str(ports["main_port"]),
        "--target-host",
        target_host,
        "--target-port",
        str(ports["target_port"]),
        "--role",
        "controller",
        "--control-port",
        str(ports["control_port"]),
        "--service-port",
        str(ports["service_port"]),
        "--check-port",
        str(ports["check_port"]),
        "--candidate",
        f"{adapter}:{transport}",
    )
    _ensure_ok(result, "profile create")


def _allocate_test_ports() -> tuple[dict[str, int], list[socket.socket]]:
    listeners: list[socket.socket] = []
    ports: dict[str, int] = {}
    for key in ("main_port", "target_port", "control_port", "service_port", "check_port"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        listeners.append(sock)
        ports[key] = sock.getsockname()[1]
    return ports, listeners


class _LoopbackServerGroup:
    def __init__(self, listeners: list[socket.socket]) -> None:
        self.listeners = listeners
        self.threads: list[threading.Thread] = []
        self._running = False

    def __enter__(self) -> "_LoopbackServerGroup":
        self._running = True
        for sock in self.listeners:
            thread = threading.Thread(target=self._serve, args=(sock,), daemon=True)
            thread.start()
            self.threads.append(thread)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._running = False
        for sock in self.listeners:
            try:
                sock.close()
            except OSError:
                pass

    def _serve(self, sock: socket.socket) -> None:
        while self._running:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            try:
                conn.close()
            except OSError:
                pass


def _loopback_servers(listeners: list[socket.socket]) -> _LoopbackServerGroup:
    return _LoopbackServerGroup(listeners)


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for item in path.rglob("*") if item.is_file())


def _collect_warnings(*payloads: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for payload in payloads:
        if isinstance(payload, dict):
            warnings.extend([str(item) for item in payload.get("warnings", []) if item])
            healthcheck = payload.get("healthcheck_summary")
            if isinstance(healthcheck, dict):
                warnings.extend([str(item) for item in healthcheck.get("warnings", []) if item])
    deduped: list[str] = []
    seen: set[str] = set()
    for warning in warnings:
        if warning in seen:
            continue
        seen.add(warning)
        deduped.append(warning)
    return deduped


def _ensure_ok(result: _RunResult, step: str) -> None:
    if result.code == 0:
        return
    message = result.stdout.strip() or result.stderr.strip() or f"{step} failed"
    raise ValueError(f"{step} failed: {message}")
