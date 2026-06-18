"""Binary metadata planning, import, and verification."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .state import AppState, BinaryRecord


@dataclass(frozen=True)
class BinarySpec:
    adapter: str
    binary_name: str
    supported_platforms: tuple[str, ...]
    coverage: str
    source_type: str
    system_command: str = ""
    notes: str = ""


@dataclass(frozen=True)
class BinaryPlan:
    adapter: str
    binary_name: str
    supported_platforms: tuple[str, ...]
    install_status: str
    expected_cache_path: str
    expected_bin_path: str
    source_type: str
    version: str
    checksum: str | None = None
    download_performed: bool = False
    imported_path: str | None = None
    executable: bool | None = None
    run_version_result: dict[str, Any] | None = None
    coverage: str = ""
    system_command: str = ""
    system_command_available: bool = False
    provider_host: str = ""
    source_provider: str = ""
    downloaded_at: str = ""
    notes: str = ""


BINARY_SPECS: dict[str, BinarySpec] = {
    "backhaul": BinarySpec("backhaul", "backhaul", ("linux-amd64", "linux-arm64", "windows-amd64"), "provider_required", "official_release"),
    "rathole": BinarySpec("rathole", "rathole", ("linux-amd64", "linux-arm64", "windows-amd64"), "provider_required", "official_release"),
    "frp": BinarySpec("frp", "frpc", ("linux-amd64", "linux-arm64", "windows-amd64"), "provider_required", "official_release"),
    "gost": BinarySpec("gost", "gost", ("linux-amd64", "linux-arm64", "windows-amd64"), "provider_required", "official_release"),
    "chisel": BinarySpec("chisel", "chisel", ("linux-amd64", "linux-arm64", "windows-amd64"), "provider_required", "official_release"),
    "realm": BinarySpec("realm", "realm", ("linux-amd64", "linux-arm64", "windows-amd64"), "provider_required", "official_release"),
    "bore": BinarySpec("bore", "bore", ("linux-amd64", "linux-arm64", "windows-amd64"), "provider_required", "official_release"),
    "wstunnel": BinarySpec("wstunnel", "wstunnel", ("linux-amd64", "linux-arm64", "windows-amd64"), "listed_only", "official_release", notes="Layer7 catalog entry only; not part of the v0.1 provider workflow"),
    "udp2raw": BinarySpec("udp2raw", "udp2raw", ("linux-amd64", "linux-arm64"), "listed_only", "official_release", notes="Experimental catalog entry only; not part of the v0.1 provider workflow"),
    "ssh_reverse": BinarySpec("ssh_reverse", "ssh", ("linux-amd64", "linux-arm64", "windows-amd64"), "system_dependency", "system_dependency", system_command="ssh"),
}


def all_binary_adapters() -> tuple[str, ...]:
    return tuple(BINARY_SPECS)


def provider_required_adapters() -> tuple[str, ...]:
    return tuple(adapter for adapter, spec in BINARY_SPECS.items() if spec.coverage == "provider_required")


def supported_platforms() -> set[str]:
    platforms: set[str] = set()
    for spec in BINARY_SPECS.values():
        platforms.update(spec.supported_platforms)
    return platforms


def current_platform_id() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        arch = "amd64"
    elif machine in {"aarch64", "arm64"}:
        arch = "arm64"
    else:
        raise ValueError(f"Unsupported platform architecture '{platform.machine()}'")
    if system.startswith("linux"):
        return f"linux-{arch}"
    if system.startswith("windows"):
        return f"windows-{arch}"
    raise ValueError(f"Unsupported platform '{platform.system()}'")


def binary_spec(adapter: str) -> BinarySpec:
    try:
        return BINARY_SPECS[adapter]
    except KeyError as exc:
        raise KeyError(f"Unknown binary adapter '{adapter}'") from exc


def binary_catalog(root: Path, state: AppState | None = None) -> dict[str, BinaryPlan]:
    layout = cache_layout(root)
    catalog: dict[str, BinaryPlan] = {}
    for adapter, spec in BINARY_SPECS.items():
        record = state.binaries.get(adapter) if state else None
        status = _status_for(spec, record)
        imported_path = record.imported_path if record else str(layout["bin_dir"] / binary_filename(adapter))
        catalog[adapter] = BinaryPlan(
            adapter=adapter,
            binary_name=spec.binary_name,
            supported_platforms=spec.supported_platforms,
            install_status=status,
            expected_cache_path=str(layout["cache_dir"] / adapter / spec.binary_name),
            expected_bin_path=_expected_bin_path(spec, layout),
            source_type=record.source_type if record else spec.source_type,
            version=record.version if record else _planned_version(spec),
            checksum=record.sha256 if record else None,
            download_performed=bool(record and record.downloaded_at),
            imported_path=record.imported_path if record else (imported_path if spec.coverage == "provider_required" else None),
            executable=record.executable if record else None,
            run_version_result=record.run_version_result if record and record.run_version_result else None,
            coverage=spec.coverage,
            system_command=spec.system_command,
            system_command_available=bool(spec.system_command and shutil.which(spec.system_command)),
            provider_host=record.provider_host if record else "",
            source_provider=record.source_provider if record else "",
            downloaded_at=record.downloaded_at if record else "",
            notes=spec.notes,
        )
    return catalog


def list_binary_plans(root: Path, state: AppState | None = None) -> list[dict[str, Any]]:
    return [asdict(plan) for plan in binary_catalog(root, state).values()]


def get_binary_plan(adapter: str, root: Path, state: AppState | None = None) -> dict[str, Any]:
    catalog = binary_catalog(root, state)
    if adapter not in catalog:
        raise KeyError(f"Unknown binary adapter '{adapter}'")
    return asdict(catalog[adapter])


def cache_layout(root: Path) -> dict[str, Path]:
    resolved_root = root.resolve()
    cache_dir = resolved_root / ".var" / "pilottunnel" / "cache" / "binaries"
    downloads_dir = resolved_root / ".var" / "pilottunnel" / "cache" / "downloads"
    bin_dir = resolved_root / ".var" / "pilottunnel" / "bin"
    return {"root": resolved_root, "cache_dir": cache_dir, "downloads_dir": downloads_dir, "bin_dir": bin_dir}


def binary_filename(adapter: str) -> str:
    spec = binary_spec(adapter)
    filename = spec.binary_name
    if platform.system().lower().startswith("win") and not filename.endswith(".exe"):
        return f"{filename}.exe"
    return filename


def import_binary(
    *,
    adapter: str,
    source: Path,
    version: str,
    cache_root: Path,
    state: AppState,
    sha256_expected: str | None = None,
    force: bool = False,
    source_type: str = "user_supplied",
    source_provider: str = "",
    provider_host: str = "",
    downloaded_at: str = "",
) -> dict[str, Any]:
    spec = binary_spec(adapter)
    if spec.coverage != "provider_required":
        raise ValueError(f"Adapter '{adapter}' uses '{spec.coverage}' and does not support binary import")
    if ".." in source.as_posix().split("/"):
        raise ValueError(f"Path traversal blocked for source path: {source}")
    source_path = source.resolve()
    if not source_path.exists():
        raise ValueError(f"Source file does not exist: {source}")
    if source_path.is_dir():
        raise ValueError(f"Source path must be a file, not a directory: {source}")

    layout = cache_layout(cache_root)
    cache_dir = layout["cache_dir"] / adapter
    bin_dir = layout["bin_dir"]
    cache_dir.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    imported_path = (bin_dir / binary_filename(adapter)).resolve()
    if layout["root"] not in imported_path.parents:
        raise ValueError(f"Refusing to write outside cache root: {imported_path}")
    if imported_path.exists() and not force:
        raise ValueError(f"Imported binary already exists for adapter '{adapter}'. Use --force to overwrite.")

    sha256_actual = _sha256_file(source_path)
    if sha256_expected and sha256_expected.lower() != sha256_actual.lower():
        raise ValueError("Provided sha256 does not match imported file")

    cached_source = (cache_dir / source_path.name).resolve()
    if layout["root"] not in cached_source.parents:
        raise ValueError(f"Refusing to write outside cache root: {cached_source}")
    shutil.copy2(source_path, cached_source)
    shutil.copy2(source_path, imported_path)
    if not platform.system().lower().startswith("win"):
        imported_path.chmod(imported_path.stat().st_mode | 0o755)
    executable = os.access(imported_path, os.X_OK)

    record = BinaryRecord(
        adapter=adapter,
        source_filename=source_path.name,
        imported_path=str(imported_path),
        sha256=sha256_actual,
        version=version,
        imported_at=datetime.now(timezone.utc).isoformat(),
        executable=executable,
        platform=current_platform_id(),
        source_type=source_type,
        source_provider=source_provider,
        provider_host=provider_host,
        downloaded_at=downloaded_at,
    )
    state.binaries[adapter] = record
    return {
        "adapter": adapter,
        "imported_path": str(imported_path),
        "cached_source_path": str(cached_source),
        "sha256": sha256_actual,
        "version": version,
        "executable": executable,
        "download_performed": source_type == "provider",
        "source_type": source_type,
        "source_provider": source_provider,
        "provider_host": provider_host,
        "downloaded_at": downloaded_at,
    }


def verify_binary(
    *,
    adapter: str,
    cache_root: Path,
    state: AppState,
    run_version: bool = False,
    timeout_seconds: float = 2.0,
) -> dict[str, Any]:
    plan = get_binary_plan(adapter, cache_root, state)
    record = state.binaries.get(adapter)
    spec = binary_spec(adapter)
    if spec.coverage == "system_dependency":
        command_path = shutil.which(spec.system_command) or spec.system_command
        return {
            "adapter": adapter,
            "imported_path": "",
            "sha256": "",
            "version": "system",
            "platform": current_platform_id(),
            "executable": bool(command_path),
            "status": plan["install_status"],
            "download_performed": False,
            "run_version_result": {},
            "system_command": spec.system_command,
            "system_command_available": bool(shutil.which(spec.system_command)),
        }
    if record is None:
        raise ValueError(f"No imported binary found for adapter '{adapter}'")
    imported_path = Path(record.imported_path)
    result = {
        "adapter": adapter,
        "imported_path": record.imported_path,
        "sha256": record.sha256,
        "version": record.version,
        "platform": record.platform,
        "executable": record.executable,
        "status": plan["install_status"],
        "download_performed": bool(record.downloaded_at),
        "run_version_result": record.run_version_result,
        "source_type": record.source_type,
        "source_provider": record.source_provider,
        "provider_host": record.provider_host,
        "downloaded_at": record.downloaded_at,
    }
    if run_version:
        version_result = _run_version(imported_path, timeout_seconds=timeout_seconds)
        record.run_version_result = version_result
        result["run_version_result"] = version_result
    return result


def _run_version(path: Path, *, timeout_seconds: float) -> dict[str, Any]:
    command = [str(path), "--version"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        return {
            "ran": True,
            "returncode": completed.returncode,
            "stdout": _sanitize_output(completed.stdout),
            "stderr": _sanitize_output(completed.stderr),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ran": True,
            "returncode": None,
            "stdout": _sanitize_output(exc.stdout or ""),
            "stderr": _sanitize_output(exc.stderr or ""),
            "timed_out": True,
        }
    except (OSError, TimeoutError) as exc:
        return {
            "ran": False,
            "returncode": None,
            "stdout": "",
            "stderr": _sanitize_output(str(exc)),
            "timed_out": False,
            "warning": "Binary could not be executed safely on this host",
        }


def _status_for(spec: BinarySpec, record: BinaryRecord | None) -> str:
    if record is not None:
        return "imported"
    if spec.coverage == "system_dependency":
        return "system_dependency"
    if spec.coverage == "template_only":
        return "template_only"
    if spec.coverage == "listed_only":
        return "listed_only"
    return "missing"


def _expected_bin_path(spec: BinarySpec, layout: dict[str, Path]) -> str:
    if spec.coverage == "system_dependency":
        return shutil.which(spec.system_command) or spec.system_command
    filename = spec.binary_name
    if platform.system().lower().startswith("win") and not filename.endswith(".exe"):
        filename = f"{filename}.exe"
    return str(layout["bin_dir"] / filename)


def _planned_version(spec: BinarySpec) -> str:
    if spec.coverage == "system_dependency":
        return "system"
    return "planned"


def _sanitize_output(value: str) -> str:
    return value.strip()[:400]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
