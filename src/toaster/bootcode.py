"""Legacy BIOS boot for Windows media, using the syslinux project's tools.

BIOS firmware runs the MBR, which starts the active partition's boot sector.
Both come from syslinux (an external tool; nothing from it is bundled with
toaster). Syslinux's ``chain.c32`` then loads Windows' ``bootmgr`` the way
NTLDR-style loaders expect (``ntldr=/bootmgr``), which hands over to Windows
Boot Manager exactly as the stock Windows boot sector would.
"""
from __future__ import annotations

import os
from pathlib import Path

from .tools import ToolError, require, run

# Where distributions install syslinux's binary assets.
_MBR_DIRS = ("/usr/lib/syslinux/mbr", "/usr/lib/syslinux/bios", "/usr/share/syslinux",
             "/usr/lib/SYSLINUX", "/usr/lib/syslinux")
_MODULE_DIRS = ("/usr/lib/syslinux/modules/bios", "/usr/share/syslinux", "/usr/lib/syslinux/bios",
                "/usr/lib/syslinux", "/usr/lib/SYSLINUX")
MODULES = ("chain.c32", "libcom32.c32", "libutil.c32")
MBR_CODE_SIZE = 440      # bytes before the disk signature and partition table

SYSLINUX_CFG = """\
DEFAULT windows
PROMPT 0
TIMEOUT 0
LABEL windows
  COM32 chain.c32
  APPEND ntldr=/bootmgr
"""

HINT = "Debian/Ubuntu: sudo apt install syslinux syslinux-common mtools"


def _find(name: str, dirs: tuple[str, ...]) -> Path:
    for d in dirs:
        p = Path(d) / name
        if p.is_file():
            return p
    raise ToolError(f"syslinux file '{name}' not found\n  {HINT}")


def check_available() -> None:
    require("syslinux", "mcopy")
    _find("mbr.bin", _MBR_DIRS)
    for m in MODULES:
        _find(m, _MODULE_DIRS)


def mbr_code() -> bytes:
    code = _find("mbr.bin", _MBR_DIRS).read_bytes()
    if len(code) != MBR_CODE_SIZE:
        raise ToolError(f"unexpected syslinux mbr.bin size ({len(code)} bytes, expected {MBR_CODE_SIZE})")
    return code


def module_files() -> dict[str, bytes]:
    """Files to copy into the root of the boot partition: name -> content."""
    files = {m: _find(m, _MODULE_DIRS).read_bytes() for m in MODULES}
    files["syslinux.cfg"] = SYSLINUX_CFG.encode()
    return files


def write_mbr_code(disk: str | Path) -> None:
    """Install the boot code in the first 440 bytes, leaving the signature and partition table alone."""
    with open(disk, "r+b") as f:
        f.write(mbr_code())


def install_boot_sector(target: str | Path, offset_bytes: int | None = None) -> None:
    """Write syslinux's partition boot sector and ldlinux.sys into an (unmounted) FAT volume."""
    cmd: list[str | Path] = ["syslinux"]
    if offset_bytes:
        cmd += ["--offset", str(offset_bytes)]
    cmd += ["--install", target]
    env = {**os.environ, "MTOOLS_SKIP_CHECK": "1"}
    run(cmd, env=env)
