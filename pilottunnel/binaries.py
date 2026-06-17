"""Binary metadata planning, import, and verification without downloads."""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .state import AppState, BinaryRecord


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
    run_version_result: dict | None = None


def binary_catalog(root: Path, state: AppState | None = None) -> dict[str, BinaryPlan]:
    layout = cache_layout(root)
    return {
        "backhaul": BinaryPlan(
            adapter="backhaul",
            binary_name="backhaul",
            supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
            install_status=_status_for("backhaul", state),
            expected_cache_path=str(layout["cache_dir"] / "backhaul"),
            expected_bin_path=str(layout["bin_dir"] / binary_filename("backhaul")),
            source_type="official_release",
            version=_version_for("backhaul", state),
            checksum=_checksum_for("backhaul", state),
            imported_path=_imported_path_for("backhaul", state),
            executable=_executable_for("backhaul", state),
            run_version_result=_run_version_for("backhaul", state),
        ),
        "rathole": BinaryPlan(
            adapter="rathole",
            binary_name="rathole",
            supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
            install_status=_status_for("rathole", state),
            expected_cache_path=str(layout["cache_dir"] / "rathole"),
            expected_bin_path=str(layout["bin_dir"] / binary_filename("rathole")),
            source_type="official_release",
            version=_version_for("rathole", state),
            checksum=_checksum_for("rathole", state),
            imported_path=_imported_path_for("rathole", state),
            executable=_executable_for("rathole", state),
            run_version_result=_run_version_for("rathole", state),
        ),
    }


def list_binary_plans(root: Path, state: AppState | None = None) -> list[dict]:
    return [asdict(plan) for plan in binary_catalog(root, state).values()]


def get_binary_plan(adapter: str, root: Path, state: AppState | None = None) -> dict:
    catalog = binary_catalog(root, state)
    if adapter not in catalog:
        raise KeyError(f"Unknown binary adapter '{adapter}'")
    return asdict(catalog[adapter])


def cache_layout(root: Path) -> dict[str, Path]:
    resolved_root = root.resolve()
    cache_dir = resolved_root / ".var" / "pilottunnel" / "cache" / "binaries"
    bin_dir = resolved_root / ".var" / "pilottunnel" / "bin"
    return {"root": resolved_root, "cache_dir": cache_dir, "bin_dir": bin_dir}


def binary_filename(adapter: str) -> str:
    if platform.system().lower().startswith("win"):
        return f"{adapter}.exe"
    return adapter


def import_binary(
    *,
    adapter: str,
    source: Path,
    version: str,
    cache_root: Path,
    state: AppState,
    sha256_expected: str | None = None,
    force: bool = False,
) -> dict:
    plan = get_binary_plan(adapter, cache_root, state)
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
    executable = os.access(imported_path, os.X_OK)

    record = BinaryRecord(
        adapter=adapter,
        source_filename=source_path.name,
        imported_path=str(imported_path),
        sha256=sha256_actual,
        version=version,
        imported_at=datetime.now(timezone.utc).isoformat(),
        executable=executable,
        platform=platform.platform(),
    )
    state.binaries[adapter] = record
    return {
        "adapter": adapter,
        "imported_path": str(imported_path),
        "cached_source_path": str(cached_source),
        "sha256": sha256_actual,
        "version": version,
        "executable": executable,
        "download_performed": False,
    }


def verify_binary(
    *,
    adapter: str,
    cache_root: Path,
    state: AppState,
    run_version: bool = False,
    timeout_seconds: float = 2.0,
) -> dict:
    plan = get_binary_plan(adapter, cache_root, state)
    record = state.binaries.get(adapter)
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
        "download_performed": False,
        "run_version_result": record.run_version_result,
    }
    if run_version:
        version_result = _run_version(imported_path, timeout_seconds=timeout_seconds)
        record.run_version_result = version_result
        result["run_version_result"] = version_result
    return result


def _run_version(path: Path, *, timeout_seconds: float) -> dict:
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
    except OSError as exc:
        return {
            "ran": False,
            "returncode": None,
            "stdout": "",
            "stderr": _sanitize_output(str(exc)),
            "timed_out": False,
            "warning": "Binary could not be executed safely on this host",
        }


def _sanitize_output(value: str) -> str:
    return value.strip()[:400]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _status_for(adapter: str, state: AppState | None) -> str:
    if state and adapter in state.binaries:
        return "imported"
    return "planned"


def _version_for(adapter: str, state: AppState | None) -> str:
    if state and adapter in state.binaries:
        return state.binaries[adapter].version
    return "planned"


def _checksum_for(adapter: str, state: AppState | None) -> str | None:
    if state and adapter in state.binaries:
        return state.binaries[adapter].sha256
    return "planned"


def _imported_path_for(adapter: str, state: AppState | None) -> str | None:
    if state and adapter in state.binaries:
        return state.binaries[adapter].imported_path
    return None


def _executable_for(adapter: str, state: AppState | None) -> bool | None:
    if state and adapter in state.binaries:
        return state.binaries[adapter].executable
    return None


def _run_version_for(adapter: str, state: AppState | None) -> dict | None:
    if state and adapter in state.binaries:
        return state.binaries[adapter].run_version_result
    return None
