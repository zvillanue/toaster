"""Stage a Windows ISO, then partition, format and populate a USB drive."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Callable

from . import wim
from .customize import find_ci
from .devices import Drive, check_target
from .tools import ToolError, require, run

FAT32_MAX_FILE = 4 * 2**30 - 1
Progress = Callable[[str, int, int], None]      # (phase, done, total)


def _noop(phase: str, done: int, total: int) -> None:
    pass


# --- staging ----------------------------------------------------------------

def extract_iso(iso: Path, dest: Path, only: list[str] | None = None) -> None:
    """Extract an ISO (ISO9660/UDF) into ``dest`` without mounting it."""
    require("7z")
    dest.mkdir(parents=True, exist_ok=True)
    run(["7z", "x", "-y", f"-o{dest}", iso, *(only or [])])


def tree_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


def find_install_image(tree: Path) -> Path | None:
    for name in ("sources/install.wim", "sources/install.esd", "sources/install.swm"):
        p = find_ci(tree, name)
        if p is not None:
            return p
    return None


def split_oversized_install_wim(tree: Path, part_mib: int = 3800) -> Path | None:
    """FAT32 cannot hold files >= 4 GiB. Split install.wim into install.swm parts that Setup reads natively."""
    image = find_install_image(tree)
    if image is None or image.stat().st_size <= FAT32_MAX_FILE:
        return None
    if image.suffix.lower() != ".wim":
        raise ToolError(f"{image.name} is larger than 4 GiB and cannot be split; "
                        "convert it to .wim with `wimlib-imagex export` first")
    swm = image.with_suffix(".swm")
    wim.split(image, swm, part_mib)
    image.unlink()
    return swm


def inspect_windows_media(source: Path) -> dict:
    """Summarise an ISO or extracted tree: editions, build, whether it will need splitting."""
    with tempfile.TemporaryDirectory(prefix="toaster-inspect-") as tmp:
        tree = source
        if source.is_file():
            tree = Path(tmp)
            extract_iso(source, tree, ["sources/install.wim", "sources/install.esd", "sources/install.swm",
                                       "sources/boot.wim", "sources/Boot.wim"])
        install, boot = find_install_image(tree), find_ci(tree, "sources/boot.wim")
        if install is None:
            raise ToolError("no sources/install.wim|esd|swm found: this does not look like Windows installation media")
        editions = wim.images(install)
        return {
            "install_image": install.name,
            "install_bytes": install.stat().st_size,
            "needs_split_for_fat32": install.stat().st_size > FAT32_MAX_FILE,
            "editions": [{"index": e.index, "name": e.name, "arch": e.architecture, "version": e.version}
                         for e in editions],
            "boot_wim": boot is not None,
            "setup_image_index": wim.find_setup_image(boot) if boot else None,
        }


# --- device writing -----------------------------------------------------------

def partition_path(device: str, number: int = 1) -> str:
    return f"{device}p{number}" if device[-1].isdigit() else f"{device}{number}"


def _wait_for(path: str, timeout: float = 15.0) -> None:
    if shutil.which("udevadm"):         # absent on minimal systems; we just poll for the node then
        run(["udevadm", "settle"], check=False)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(path):
            return
        time.sleep(0.2)
    raise ToolError(f"partition {path} did not appear after partitioning")


def prepare_device(drive: Drive, label: str) -> str:
    """Wipe the drive, create one GPT/FAT32 partition and return its path."""
    require("sgdisk", "mkfs.vfat")
    for mp in sorted(drive.mountpoints, key=len, reverse=True):
        run(["umount", mp])
    run(["sgdisk", "--zap-all", drive.path])
    # 0700 = Microsoft basic data, what Windows' own tools use for FAT32 install media.
    run(["sgdisk", "--new=1:0:0", "--typecode=1:0700", f"--change-name=1:{label}", drive.path])
    run(["blockdev", "--rereadpt", drive.path], check=False)
    part = partition_path(drive.path)
    _wait_for(part)
    run(["mkfs.vfat", "-F", "32", "-n", label, part])
    return part


def _copy_tree(src: Path, dst: Path, total: int, progress: Progress) -> None:
    done = 0
    for path in sorted(src.rglob("*")):
        target = dst / path.relative_to(src)
        if path.is_dir():
            target.mkdir(exist_ok=True)
            continue
        with open(path, "rb") as r, open(target, "wb") as w:
            while chunk := r.read(4 * 2**20):
                w.write(chunk)
                done += len(chunk)
                progress("copy", done, total)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(4 * 2**20):
            h.update(chunk)
    return h.hexdigest()


def _verify_tree(src: Path, dst: Path, progress: Progress) -> list[str]:
    files = [p for p in sorted(src.rglob("*")) if p.is_file()]
    total, done, bad = sum(p.stat().st_size for p in files), 0, []
    for p in files:
        q = dst / p.relative_to(src)
        if not q.is_file() or q.stat().st_size != p.stat().st_size or _sha256(p) != _sha256(q):
            bad.append(str(p.relative_to(src)))
        done += p.stat().st_size
        progress("verify", done, total)
    return bad


def write_tree_to_device(tree: Path, drive: Drive, *, label: str = "WINSTALL", allow_fixed: bool = False,
                         verify: bool = False, progress: Progress = _noop) -> None:
    """Destroy ``drive`` and replace its contents with ``tree`` on a fresh FAT32 partition. Needs root."""
    if os.geteuid() != 0:
        raise ToolError("writing to a device requires root (try sudo)")
    if not re.fullmatch(r"[A-Za-z0-9_ -]{1,11}", label):
        raise ToolError("label must be 1-11 characters: letters, digits, space, '_' or '-'")
    total = tree_size(tree)
    check_target(drive, allow_fixed=allow_fixed, needed_bytes=total)
    part = prepare_device(drive, label.upper())
    with tempfile.TemporaryDirectory(prefix="toaster-mnt-") as mnt:
        mnt_path = Path(mnt)
        run(["mount", "-t", "vfat", "-o", "utf8,shortname=mixed", part, mnt])
        try:
            _copy_tree(tree, mnt_path, total, progress)
            progress("sync", 0, 1)
            run(["sync"])
        finally:
            run(["umount", mnt], check=False)
        if verify:
            run(["mount", "-t", "vfat", "-o", "ro", part, mnt])
            try:
                bad = _verify_tree(tree, mnt_path, progress)
            finally:
                run(["umount", mnt], check=False)
            if bad:
                raise ToolError(f"verification failed for {len(bad)} file(s): {', '.join(bad[:5])}")
    progress("done", total, total)


def clean_workdir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
