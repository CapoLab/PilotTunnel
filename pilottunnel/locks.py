"""Profile-scoped file locking."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - exercised on non-Linux dev hosts
    fcntl = None


@contextmanager
def profile_lock(profile: str, lock_dir: Path) -> Iterator[Path]:
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{profile}.lock"
    with path.open("w", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield path
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
