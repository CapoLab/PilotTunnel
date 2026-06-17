"""Safe systemd unit rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class UnitRenderResult:
    unit_name: str
    path: str
    content: str


def render_unit_file(
    *,
    unit_name: str,
    description: str,
    command: str,
    output_dir: Path,
    apply_changes: bool,
) -> UnitRenderResult:
    path = output_dir / unit_name
    content = "\n".join(
        [
            "[Unit]",
            f"Description={description}",
            "",
            "[Service]",
            "Type=simple",
            f"ExecStart={command}",
            "Restart=always",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    if apply_changes:
        output_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return UnitRenderResult(unit_name=unit_name, path=str(path), content=content)
