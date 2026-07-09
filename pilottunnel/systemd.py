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
    environment: dict[str, str] | None = None,
    working_directory: str | Path | None = None,
) -> UnitRenderResult:
    path = output_dir / unit_name
    service_lines = [
        "# Managed-by: PilotTunnel",
        "[Unit]",
        f"Description={description}",
        "",
        "[Service]",
        "Type=simple",
    ]
    for key, value in sorted((environment or {}).items()):
        safe_key = key.strip()
        if not safe_key or any(char.isspace() for char in safe_key):
            raise ValueError(f"Invalid systemd environment variable name: {key!r}")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        service_lines.append(f'Environment="{safe_key}={escaped}"')
    if working_directory is not None:
        safe_working_directory = str(working_directory).strip()
        if not safe_working_directory:
            raise ValueError("WorkingDirectory must not be empty")
        service_lines.append(f"WorkingDirectory={safe_working_directory}")
    service_lines.extend(
        [
            f"ExecStart={command}",
            "Restart=always",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )
    content = "\n".join(service_lines)
    if apply_changes:
        output_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return UnitRenderResult(unit_name=unit_name, path=str(path), content=content)
