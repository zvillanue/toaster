"""Thin wrapper around the external programs toaster drives."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# binary -> Debian/Ubuntu package that provides it (for the error message)
PACKAGES = {
    "wimlib-imagex": "wimtools",
    "hivexsh": "libhivex-bin",
    "hivexget": "libhivex-bin",
    "7z": "p7zip-full",
    "sgdisk": "gdisk",
    "mkfs.vfat": "dosfstools",
    "lsblk": "util-linux",
    "syslinux": "syslinux syslinux-common",
    "mcopy": "mtools",
    "mmd": "mtools",
}


class ToolError(RuntimeError):
    pass


def require(*names: str) -> None:
    missing = [n for n in names if shutil.which(n) is None]
    if missing:
        pkgs = sorted({PACKAGES.get(n, n) for n in missing})
        raise ToolError(f"missing required tool(s): {', '.join(missing)}\n"
                        f"  Debian/Ubuntu: sudo apt install {' '.join(pkgs)}")


def run(cmd: list[str | Path], *, input: str | None = None, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run([str(c) for c in cmd], input=input, capture_output=True, text=True, env=env)
    except FileNotFoundError:
        raise ToolError(f"{cmd[0]}: command not found") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise ToolError(f"{Path(str(cmd[0])).name} failed (exit {proc.returncode}): {detail}")
    return proc
