"""Stage a Windows ISO, then partition, format and populate a USB drive."""
from __future__ import annotations

import hashlib
import os
import random
import re
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator, Protocol

from . import bootcode, wim
from .devices import Drive, check_target
from .fsutil import find_ci
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


# --- partitioning ---------------------------------------------------------------

MBR_CODE_SIZE = bootcode.MBR_CODE_SIZE
ALIGN_SECTORS = 2048            # 1 MiB with 512-byte sectors; what every modern tool uses
MBR_FAT32_LBA = 0x0C


class Layout:
    """Where the single FAT32 partition lives on the disk."""
    def __init__(self, start: int, sectors: int, sector_size: int, node: str | None) -> None:
        self.start, self.sectors, self.sector_size, self.node = start, sectors, sector_size, node

    @property
    def offset(self) -> int:
        return self.start * self.sector_size

    @property
    def size(self) -> int:
        return self.sectors * self.sector_size


def partition_path(device: str, number: int = 1) -> str:
    return f"{device}p{number}" if device[-1].isdigit() else f"{device}{number}"


def _sector_size(drive: Drive) -> int:
    if drive.is_image:
        return 512
    return int(run(["blockdev", "--getss", drive.path]).stdout.strip() or 512)


def mbr_table(disk_sectors: int, start: int, signature: int) -> bytes:
    """The 70 bytes from offset 440: disk signature, reserved word, one active FAT32 partition, boot magic."""
    entry = struct.pack("<B3sB3sII", 0x80, b"\xfe\xff\xff", MBR_FAT32_LBA, b"\xfe\xff\xff",
                        start, min(disk_sectors - start, 0xFFFFFFFF))
    return struct.pack("<IH", signature, 0) + entry + bytes(48) + b"\x55\xaa"


def _partition(drive: Drive, scheme: str, label: str) -> Layout:
    ss = _sector_size(drive)
    total = drive.size // ss
    require("sgdisk")
    run(["sgdisk", "--zap-all", drive.path])      # removes GPT and MBR structures (and the backup GPT)
    start = ALIGN_SECTORS if ss == 512 else 256
    if scheme == "gpt":
        # 0700 = Microsoft basic data, what Windows' own tools use for FAT32 install media.
        run(["sgdisk", f"--new=1:{start}:0", "--typecode=1:0700", f"--change-name=1:{label}", drive.path])
        info = run(["sgdisk", "-i", "1", drive.path]).stdout
        first = int(re.search(r"First sector: (\d+)", info).group(1))
        last = int(re.search(r"Last sector: (\d+)", info).group(1))
        sectors = last - first + 1
    else:
        with open(drive.path, "r+b") as f:
            f.seek(MBR_CODE_SIZE)
            f.write(mbr_table(total, start, random.getrandbits(32) or 1))
        sectors = total - start
    node = None
    if not drive.is_image:
        run(["blockdev", "--rereadpt", drive.path], check=False)
        node = partition_path(drive.path)
        _wait_for(node)
    return Layout(start, sectors, ss, node)



def _wait_for(path: str, timeout: float = 15.0) -> None:
    if shutil.which("udevadm"):         # absent on minimal systems; we just poll for the node then
        run(["udevadm", "settle"], check=False)
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(path):
            return
        time.sleep(0.2)
    raise ToolError(f"partition {path} did not appear after partitioning")


def _format(drive: Drive, layout: Layout, label: str) -> None:
    require("mkfs.vfat")
    base = ["mkfs.vfat", "-F", "32", "-n", label, "-h", str(layout.start)]
    if drive.is_image:
        # Format inside the file at the partition's offset; the block count is in 1 KiB units.
        run([*base, "--offset", str(layout.start), drive.path, str(layout.size // 1024)])
    else:
        run([*base, layout.node])


# --- volumes: ways of putting files on the formatted partition ------------------------

Progress = Callable[[str, int, int], None]      # (phase, done, total)
CHUNK = 4 * 2**20


def _noop(phase: str, done: int, total: int) -> None:
    pass


class Volume(Protocol):
    def mkdir(self, rel: str) -> None: ...
    def write(self, src: Path | bytes, rel: str, on_bytes: Callable[[int], None]) -> None: ...
    def read(self, rel: str) -> Iterator[bytes]: ...


class MountedVolume:
    """A kernel-mounted FAT volume (real block devices; the kernel copes with automounters)."""
    def __init__(self, root: Path) -> None:
        self.root = root

    def mkdir(self, rel: str) -> None:
        (self.root / rel).mkdir(exist_ok=True)

    def write(self, src: Path | bytes, rel: str, on_bytes: Callable[[int], None]) -> None:
        with open(self.root / rel, "wb") as w:
            for chunk in _chunks(src):
                w.write(chunk)
                on_bytes(len(chunk))

    def read(self, rel: str) -> Iterator[bytes]:
        try:
            yield from _chunks(self.root / rel)
        except OSError as e:               # same contract as MtoolsVolume: unreadable -> ToolError
            raise ToolError(f"cannot read {rel}: {e.strerror}") from None


class MtoolsVolume:
    """A FAT volume inside a disk image, accessed without mounting (no root needed)."""
    def __init__(self, image: str, offset: int) -> None:
        require("mcopy", "mmd")
        self.spec = f"{image}@@{offset}"
        self.env = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}

    def mkdir(self, rel: str) -> None:
        run(["mmd", "-i", self.spec, f"::{rel}"], env=self.env)

    def write(self, src: Path | bytes, rel: str, on_bytes: Callable[[int], None]) -> None:
        with subprocess.Popen(["mcopy", "-i", self.spec, "-", f"::{rel}"], stdin=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.env) as proc:
            try:
                for chunk in _chunks(src):
                    proc.stdin.write(chunk)
                    on_bytes(len(chunk))
            except BrokenPipeError:
                pass                       # mcopy exited early; its exit status and stderr explain why
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            err = proc.stderr.read().decode(errors="replace").strip()
            if proc.wait() != 0:
                raise ToolError(f"mcopy failed for {rel}: {err}")

    def read(self, rel: str) -> Iterator[bytes]:
        with subprocess.Popen(["mcopy", "-i", self.spec, f"::{rel}", "-"], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.env) as proc:
            while chunk := proc.stdout.read(CHUNK):
                yield chunk
            err = proc.stderr.read().decode(errors="replace").strip()
            if proc.wait() != 0:
                raise ToolError(f"mcopy could not read {rel}: {err}")


def _chunks(src: Path | bytes) -> Iterator[bytes]:
    if isinstance(src, bytes):
        yield src
        return
    with open(src, "rb") as f:
        while chunk := f.read(CHUNK):
            yield chunk


def _entries(tree: Path) -> list[Path]:
    return sorted(tree.rglob("*"))       # parents sort before their children


def copy_tree(tree: Path, vol: Volume, progress: Progress = _noop, extra: dict[str, bytes] | None = None) -> None:
    total = tree_size(tree) + sum(len(b) for b in (extra or {}).values())
    done = 0

    def advance(n: int) -> None:
        nonlocal done
        done += n
        progress("copy", done, total)

    for path in _entries(tree):
        rel = path.relative_to(tree).as_posix()
        if path.is_dir():
            vol.mkdir(rel)
        else:
            vol.write(path, rel, advance)
    for name, data in (extra or {}).items():
        vol.write(data, name, advance)


def _sha256(chunks: Iterator[bytes]) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c)
    return h.hexdigest()


def verify_tree(tree: Path, vol: Volume, progress: Progress = _noop,
                extra: dict[str, bytes] | None = None) -> list[str]:
    """Compare every file against what is on the volume. Returns relative paths that differ."""
    files = [(p.relative_to(tree).as_posix(), p) for p in _entries(tree) if p.is_file()]
    files += [(n, d) for n, d in (extra or {}).items()]
    total = sum((p.stat().st_size if isinstance(p, Path) else len(p)) for _, p in files) or 1
    done, bad = 0, []
    for rel, src in files:
        try:
            ok = _sha256(_chunks(src)) == _sha256(vol.read(rel))
        except ToolError:
            ok = False
        if not ok:
            bad.append(rel)
        done += src.stat().st_size if isinstance(src, Path) else len(src)
        progress("verify", done, total)
    return bad


# --- the writer ----------------------------------------------------------------------------

SCHEMES = {"uefi": "gpt", "bios": "mbr"}


def write_tree_to_device(tree: Path, drive: Drive, *, label: str = "WINSTALL", boot: str = "uefi",
                         allow_fixed: bool = False, verify: bool = False, progress: Progress = _noop) -> None:
    """Destroy ``drive`` and replace its contents with ``tree`` on a fresh FAT32 partition.

    boot="uefi": GPT. boot="bios": MBR with an active partition and syslinux boot code; such a drive
    boots on legacy BIOS *and* on UEFI firmware (the FAT32 partition still holds the EFI loader).
    Real block devices need root. Disk images (regular files) do not.
    """
    if boot not in SCHEMES:
        raise ToolError(f"unknown boot mode '{boot}' (choose uefi or bios)")
    if not drive.is_image and os.geteuid() != 0:
        raise ToolError("writing to a device requires root (try sudo)")
    if not re.fullmatch(r"[A-Za-z0-9_ -]{1,11}", label):
        raise ToolError("label must be 1-11 characters: letters, digits, space, '_' or '-'")
    if boot == "bios":
        bootcode.check_available()
        if find_ci(tree, "bootmgr") is None:
            raise ToolError("this media has no 'bootmgr' file, so legacy BIOS boot is not possible")
    extra = bootcode.module_files() if boot == "bios" else {}
    total = tree_size(tree) + sum(len(b) for b in extra.values())
    check_target(drive, allow_fixed=allow_fixed, needed_bytes=total + total // 20)

    label = label.upper()
    layout = _partition(drive, SCHEMES[boot], label)
    _format(drive, layout, label)
    if boot == "bios":
        bootcode.write_mbr_code(drive.path)
        bootcode.install_boot_sector(drive.path if drive.is_image else layout.node,
                                     layout.offset if drive.is_image else None)

    if drive.is_image:
        _populate(MtoolsVolume(drive.path, layout.offset), tree, extra, verify, progress)
    else:
        for mp in sorted(drive.mountpoints, key=len, reverse=True):
            run(["umount", mp], check=False)
        with tempfile.TemporaryDirectory(prefix="toaster-mnt-") as mnt:
            run(["mount", "-t", "vfat", "-o", "utf8,shortname=mixed", layout.node, mnt])
            try:
                _populate(MountedVolume(Path(mnt)), tree, extra, False, progress)
                progress("sync", 0, 1)
                run(["sync"])
            finally:
                run(["umount", mnt], check=False)
            if verify:
                run(["mount", "-t", "vfat", "-o", "ro", layout.node, mnt])
                try:
                    _check(verify_tree(tree, MountedVolume(Path(mnt)), progress, extra))
                finally:
                    run(["umount", mnt], check=False)
    progress("done", total, total)


def _populate(vol: Volume, tree: Path, extra: dict[str, bytes], verify: bool, progress: Progress) -> None:
    copy_tree(tree, vol, progress, extra)
    if verify:
        _check(verify_tree(tree, vol, progress, extra))


def _check(bad: list[str]) -> None:
    if bad:
        raise ToolError(f"verification failed for {len(bad)} file(s): {', '.join(bad[:5])}")


def clean_workdir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
