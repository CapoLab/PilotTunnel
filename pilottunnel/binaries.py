"""Binary metadata planning without downloads."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


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


def binary_catalog(root: Path) -> dict[str, BinaryPlan]:
    cache_root = root / ".var" / "pilottunnel" / "cache"
    bin_root = root / ".var" / "pilottunnel" / "bin"
    return {
        "backhaul": BinaryPlan(
            adapter="backhaul",
            binary_name="backhaul",
            supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
            install_status="planned",
            expected_cache_path=str(cache_root / "backhaul"),
            expected_bin_path=str(bin_root / "backhaul"),
            source_type="official_release",
            version="planned",
            checksum="planned",
        ),
        "rathole": BinaryPlan(
            adapter="rathole",
            binary_name="rathole",
            supported_platforms=("linux-amd64", "linux-arm64", "windows-amd64"),
            install_status="planned",
            expected_cache_path=str(cache_root / "rathole"),
            expected_bin_path=str(bin_root / "rathole"),
            source_type="official_release",
            version="planned",
            checksum="planned",
        ),
    }


def list_binary_plans(root: Path) -> list[dict]:
    return [asdict(plan) for plan in binary_catalog(root).values()]


def get_binary_plan(adapter: str, root: Path) -> dict:
    catalog = binary_catalog(root)
    if adapter not in catalog:
        raise KeyError(f"Unknown binary adapter '{adapter}'")
    return asdict(catalog[adapter])
