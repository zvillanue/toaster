"""Small filesystem helpers shared across modules."""
from __future__ import annotations

from pathlib import Path


def find_ci(root: Path, rel: str) -> Path | None:
    """Case-insensitive lookup of a relative path (ISO/FAT trees are not case-sensitive)."""
    cur = root
    for part in Path(rel).parts:
        try:
            cur = next(p for p in cur.iterdir() if p.name.lower() == part.lower())
        except (StopIteration, NotADirectoryError, FileNotFoundError):
            return None
    return cur
