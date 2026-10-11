"""Apply Customizations to an extracted Windows installation tree.

What happens, in order:

1. Hardware-requirement bypass. The LabConfig values are written directly into
   the SYSTEM hive inside boot.wim (image "Microsoft Windows Setup"), which
   keeps them out of the answer file. If that fails for any reason the same
   values go into the answer file's windowsPE pass instead. Separately,
   ``sources/appraiserres.dll`` is replaced with an empty file so in-place
   upgrades started from this media skip the compatibility appraisal too.
2. The answer file is generated for what is left to do and placed where
   Windows Setup will actually read it (see ``_place_answer_file``).
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import regpatch, wim
from .options import BYPASS_CHECKS, Customizations
from .tools import ToolError
from .unattend import build_unattend

SYSTEM_HIVE_IN_WIM = "/Windows/System32/config/SYSTEM"
WIM_ARCH = {"x86_64": "x64", "x86": "x86", "ARM64": "arm64", "arm64": "arm64"}


@dataclass
class Report:
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    answer_file: str | None = None


def find_ci(root: Path, rel: str) -> Path | None:
    """Case-insensitive lookup of a relative path (ISO/FAT trees are not case-sensitive)."""
    cur = root
    for part in Path(rel).parts:
        try:
            match = next(p for p in cur.iterdir() if p.name.lower() == part.lower())
        except (StopIteration, NotADirectoryError, FileNotFoundError):
            return None
        cur = match
    return cur


def _neutralise_appraiser(root: Path, report: Report) -> None:
    dll = find_ci(root, "sources/appraiserres.dll")
    if dll is None:
        return          # newer media may not ship it; Setup then has nothing to enforce from it
    dll.rename(dll.with_name("appraiserres.bak"))
    dll.touch()         # must exist and be empty, or Setup extracts its own copy
    report.actions.append("Replaced sources/appraiserres.dll with an empty file (original kept as appraiserres.bak)")


def _patch_boot_wim_registry(boot_wim: Path, index: int, checks: list[str], report: Report) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="toaster-") as tmp:
            hive = wim.extract(boot_wim, index, SYSTEM_HIVE_IN_WIM, Path(tmp))
            regpatch.set_dwords(hive, "Setup\\LabConfig", {BYPASS_CHECKS[c]: 1 for c in checks})
            wim.update(boot_wim, index, {SYSTEM_HIVE_IN_WIM: hive})
    except ToolError as e:
        report.warnings.append(f"Could not patch boot.wim registry ({e}); using the answer file instead")
        return False
    names = ", ".join(BYPASS_CHECKS[c] for c in checks)
    report.actions.append(f"Wrote HKLM\\SYSTEM\\Setup\\LabConfig [{names}] into boot.wim image {index}")
    return True


def customize_tree(root: Path, opts: Customizations, *, log: Callable[[str], None] = lambda m: None) -> Report:
    opts.validate()
    report = Report()
    boot_wim = find_ci(root, "sources/boot.wim")
    setup_index = 0
    arch = "x64"

    if boot_wim is not None:
        setup_index = wim.find_setup_image(boot_wim)
        info = next((i for i in wim.images(boot_wim) if i.index == setup_index), None)
        if info and info.architecture in WIM_ARCH:
            arch = WIM_ARCH[info.architecture]
    else:
        report.warnings.append("sources/boot.wim not found; is this a Windows installation image?")

    bypass_in_registry = False
    if opts.bypass_checks:
        log("Applying hardware-requirement bypass...")
        _neutralise_appraiser(root, report)
        if boot_wim is not None:
            bypass_in_registry = _patch_boot_wim_registry(boot_wim, setup_index, opts.bypass_checks, report)

    answer = build_unattend(opts, arch, pe_bypass=not bypass_in_registry)
    if answer is None:
        report.actions.append("No answer file needed")
        return report

    log("Writing answer file...")
    _place_answer_file(root, boot_wim, setup_index, answer.xml, answer.needs_pe_pass, report)
    return report


def _place_answer_file(root: Path, boot_wim: Path | None, index: int, xml: str,
                       needs_pe: bool, report: Report) -> None:
    if needs_pe:
        # windowsPE settings are only read from Autounattend.xml at the root of boot.wim's
        # Setup image; Setup then carries it into %WINDIR%\Panther for the later passes.
        if boot_wim is None:
            report.warnings.append("Cannot apply Windows PE settings without boot.wim; they were dropped")
            return
        with tempfile.TemporaryDirectory(prefix="toaster-") as tmp:
            src = Path(tmp) / "Autounattend.xml"
            src.write_text(xml, encoding="utf-8")
            wim.update(boot_wim, index, {"/Autounattend.xml": src})
        report.answer_file = "boot.wim:/Autounattend.xml"
    else:
        # Without a windowsPE pass Setup ignores a root Autounattend.xml for later passes, but it
        # copies sources\$OEM$\$$\* into %WINDIR%, so Panther\unattend.xml gets picked up.
        sources = find_ci(root, "sources") or (root / "sources")
        target = sources / "$OEM$" / "$$" / "Panther"
        target.mkdir(parents=True, exist_ok=True)
        (target / "unattend.xml").write_text(xml, encoding="utf-8")
        report.answer_file = "sources/$OEM$/$$/Panther/unattend.xml"
    report.actions.append(f"Installed answer file at {report.answer_file}")
