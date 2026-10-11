"""Read and modify WIM files through wimlib-imagex."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .tools import ToolError, require, run


@dataclass
class WimImage:
    index: int
    name: str
    description: str = ""
    architecture: str = ""
    version: str = ""        # e.g. "10.0.26100"
    edition_id: str = ""
    total_bytes: int = 0


def _parse_info(text: str) -> list[WimImage]:
    images: list[WimImage] = []
    for block in re.split(r"\n(?=Index:)", text):
        m = re.match(r"Index:\s+(\d+)", block)
        if not m:
            continue

        def field(label: str) -> str:
            f = re.search(rf"^{label}:\s*(.*)$", block, re.M)
            return f.group(1).strip() if f else ""

        ver = ".".join(field(k) for k in ("Major Version", "Minor Version", "Build") if field(k))
        images.append(WimImage(
            index=int(m.group(1)), name=field("Name"), description=field("Description"),
            architecture=field("Architecture"), version=ver, edition_id=field("Edition ID"),
            total_bytes=int(field("Total Bytes") or 0),
        ))
    return images


def images(wim: Path) -> list[WimImage]:
    require("wimlib-imagex")
    return _parse_info(run(["wimlib-imagex", "info", wim]).stdout)


def find_setup_image(wim: Path) -> int:
    """Index of the 'Microsoft Windows Setup' image in boot.wim (normally 2)."""
    imgs = images(wim)
    for img in imgs:
        if "windows setup" in f"{img.name} {img.description}".lower():
            return img.index
    # Unofficial media sometimes ship a single-image boot.wim; fall back to the last image.
    if not imgs:
        raise ToolError(f"{wim} contains no images")
    return imgs[-1].index


def extract(wim: Path, index: int, wim_path: str, dest_dir: Path) -> Path:
    """Extract one file from an image into dest_dir (flat) and return its path."""
    require("wimlib-imagex")
    run(["wimlib-imagex", "extract", wim, str(index), wim_path, f"--dest-dir={dest_dir}",
         "--no-acls", "--nullglob"])
    out = dest_dir / Path(wim_path).name
    if not out.exists():
        raise ToolError(f"'{wim_path}' not found in {wim}[{index}]")
    return out


def _q(path: str | Path) -> str:
    return '"' + str(path).replace('"', '\\"') + '"'


def update(wim: Path, index: int, adds: dict[str, Path]) -> None:
    """Add or replace files in an image: {path inside image: local source file}."""
    require("wimlib-imagex")
    script = "".join(f"add {_q(src)} {_q(dst)}\n" for dst, src in adds.items())
    run(["wimlib-imagex", "update", wim, str(index)], input=script)


def split(wim: Path, dest_swm: Path, part_mib: int = 3800) -> None:
    require("wimlib-imagex")
    run(["wimlib-imagex", "split", wim, dest_swm, str(part_mib)])
