"""Swap in the 'Windows UEFI CA 2023' signed boot loaders (see Microsoft KB5025885).

Recent Windows media carry a second set of boot files, signed with the 2023
certificate, inside the Setup image of boot.wim (``Windows\\Boot\\EFI_EX`` and
``Windows\\Boot\\Fonts_EX``). Machines that have revoked the 2011 certificate
will only boot media that uses them; machines that lack the 2023 certificate in
their Secure Boot database will refuse to boot the result. That is why this is
opt-in.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from . import wim
from .fsutil import find_ci

EFI_BOOT_NAMES = ("bootx64.efi", "bootia32.efi", "bootaa64.efi", "bootarm.efi")


def _strip_ex(name: str) -> str:
    return re.sub(r"_EX(?=\.[^.]+$|$)", "", name, flags=re.IGNORECASE)


def apply_2023_bootloaders(tree: Path, boot_wim: Path, setup_index: int) -> tuple[int, list[str]]:
    """Returns (files replaced, warnings)."""
    warnings: list[str] = []
    replaced = 0
    with tempfile.TemporaryDirectory(prefix="toaster-ex-") as tmp:
        tmp_path = Path(tmp)
        wim.extract_tree(boot_wim, setup_index, ["/Windows/Boot/EFI_EX", "/Windows/Boot/Fonts_EX"], tmp_path)
        efi_ex, fonts_ex = tmp_path / "EFI_EX", tmp_path / "Fonts_EX"
        mgfw, mgr = efi_ex / "bootmgfw_EX.efi", efi_ex / "bootmgr_EX.efi"
        if not mgfw.is_file():
            return 0, ["This media has no 2023-signed boot loaders (no Windows\\Boot\\EFI_EX in boot.wim); "
                       "use a newer ISO. Boot files left unchanged."]

        boot_dir = find_ci(tree, "efi/boot")
        target = next((p for n in EFI_BOOT_NAMES if boot_dir and (p := find_ci(boot_dir, n))), None)
        if target is None:
            warnings.append("efi/boot/boot*.efi not found; the fallback boot loader was not replaced")
        else:
            shutil.copyfile(mgfw, target)
            replaced += 1
        root_mgr = find_ci(tree, "bootmgr.efi")
        if mgr.is_file() and root_mgr is not None:
            shutil.copyfile(mgr, root_mgr)
            replaced += 1

        fonts_dest = find_ci(tree, "efi/microsoft/boot/fonts")
        if fonts_ex.is_dir() and fonts_dest is not None:
            for font in sorted(fonts_ex.iterdir()):
                if font.is_file():
                    shutil.copyfile(font, fonts_dest / _strip_ex(font.name))
                    replaced += 1
    return replaced, warnings
