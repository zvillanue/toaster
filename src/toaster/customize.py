"""Apply Customizations to an extracted Windows installation tree.

Order of operations (each step is skipped if its option is off):

1. Hardware-requirement bypass. The LabConfig values are written straight into
   the SYSTEM hive inside boot.wim (image "Microsoft Windows Setup"), which
   keeps them out of the answer file. If that fails, the same values go into
   the answer file's windowsPE pass instead. ``sources/appraiserres.dll`` is
   emptied so in-place upgrades from this media skip the appraisal too.
2. Drivers are staged for Setup (inside boot.wim) and for the new system
   (``sources/$OEM$/$1/Drivers``).
3. install.wim: keep only the requested editions, then apply registry tweaks
   and file removals offline.
4. The answer file is generated for whatever is still left to do and placed
   where Windows Setup will read it (see ``_place_answer_file``).
5. All boot.wim changes are written in ONE update, so the file is rewritten once.
6. Optionally swap in the 2023-signed UEFI boot loaders.
"""
from __future__ import annotations

import dataclasses
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import bootloaders, debloat, drivers, regpatch, wim
from .fsutil import find_ci
from .options import BYPASS_CHECKS, Customizations
from .tools import ToolError
from .unattend import build_unattend

SYSTEM_HIVE_IN_WIM = "/Windows/System32/config/SYSTEM"
WIM_ARCH = {"x86_64": "x64", "x86": "x86", "ARM64": "arm64", "arm64": "arm64"}

__all__ = ["Report", "customize_tree", "find_ci", "SYSTEM_HIVE_IN_WIM"]


@dataclass
class Report:
    actions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    answer_file: str | None = None


def _neutralise_appraiser(root: Path, report: Report) -> None:
    dll = find_ci(root, "sources/appraiserres.dll")
    if dll is None:
        return          # newer media may not ship it; Setup then has nothing to enforce from it
    dll.rename(dll.with_name("appraiserres.bak"))
    dll.touch()         # must exist and be empty, or Setup extracts its own copy
    report.actions.append("Replaced sources/appraiserres.dll with an empty file (original kept as appraiserres.bak)")


def _prepare_bypass_hive(boot_wim: Path, index: int, checks: list[str], workdir: Path, report: Report) -> Path | None:
    """Return a patched copy of boot.wim's SYSTEM hive, or None (with a warning) if that is not possible."""
    try:
        workdir.mkdir(parents=True, exist_ok=True)
        hive = wim.extract(boot_wim, index, SYSTEM_HIVE_IN_WIM, workdir)
        regpatch.set_dwords(hive, "Setup\\LabConfig", {BYPASS_CHECKS[c]: 1 for c in checks})
    except ToolError as e:
        report.warnings.append(f"Could not patch boot.wim registry ({e}); using the answer file instead")
        return None
    names = ", ".join(BYPASS_CHECKS[c] for c in checks)
    report.actions.append(f"Wrote HKLM\\SYSTEM\\Setup\\LabConfig [{names}] into boot.wim image {index}")
    return hive


# --- install.wim ----------------------------------------------------------------

def _ensure_wim(install: Path, log: Callable[[str], None]) -> Path:
    """Offline edits need a read/write WIM; convert a solid install.esd first."""
    if install.suffix.lower() != ".esd":
        return install
    log("  converting install.esd to install.wim (this takes a while)...")
    out = install.with_suffix(".wim")
    wim.export(install, list(range(1, wim.image_count(install) + 1)), out, compress="LZX")
    install.unlink()
    return out


def _trim_editions(install: Path, keep: list[int], report: Report) -> None:
    present = {i.index: i for i in wim.images(install)}
    missing = [k for k in keep if k not in present]
    if missing:
        listing = ", ".join(f"{i.index}={i.name}" for i in present.values())
        raise ToolError(f"edition index {missing} not in install image (available: {listing})")
    out = install.with_name(install.stem + ".trim" + install.suffix)
    wim.export(install, sorted(set(keep)), out)
    install.unlink()
    out.rename(install)
    report.actions.append("Kept editions: " + ", ".join(present[k].name for k in sorted(set(keep))))


def _install_operations(root: Path, opts: Customizations, report: Report, log: Callable[[str], None]) -> bool:
    """Edition trimming and offline tweaks. Returns True if offline tweaks were applied to every image."""
    install = find_ci(root, "sources/install.wim") or find_ci(root, "sources/install.esd")
    if install is None:
        report.warnings.append("No install.wim/esd found; edition selection and offline tweaks skipped")
        return False
    if opts.editions or opts.offline_debloat:
        install = _ensure_wim(install, log)
    if opts.editions:
        log("Selecting editions...")
        _trim_editions(install, opts.editions, report)
    if not opts.offline_debloat:
        return False
    if not opts.tweaks:
        return False
    log("Applying tweaks inside install.wim...")
    try:
        debloat.apply_offline(install, opts.tweaks, log)
    except ToolError as e:
        report.warnings.append(f"Offline tweaks failed ({e}); falling back to first-boot commands")
        return False
    report.actions.append(f"Applied {len(opts.tweaks)} tweak(s) inside install.wim "
                          "(registry values in each image's hives; applies to all new accounts)")
    return True


def _resolve_install_index(root: Path, opts: Customizations, report: Report) -> int | None:
    """Index of the image a silent install deploys, in the install image *as it is now* (after trimming)."""
    if not opts.silent_install:
        return None
    install = (find_ci(root, "sources/install.wim") or find_ci(root, "sources/install.esd")
               or find_ci(root, "sources/install.swm"))
    if install is None:
        report.warnings.append("No install image found; Setup will ask which edition to install")
        return None
    present = wim.images(install)
    if opts.install_edition is not None:
        kept = sorted(set(opts.editions))
        index = kept.index(opts.install_edition) + 1 if kept else opts.install_edition   # trimming renumbers
        if index > len(present):
            raise ToolError(f"edition {opts.install_edition} does not exist (the image has {len(present)})")
        return index
    if len(present) == 1:
        return 1
    listing = ", ".join(f"{i.index}={i.name}" for i in present)
    raise ToolError("a silent install needs to know which edition to deploy: pass --install-edition N "
                    f"(or keep a single one with --edition N). Available: {listing}")


# --- main entry -----------------------------------------------------------------------

def customize_tree(root: Path, opts: Customizations, *, log: Callable[[str], None] = lambda m: None) -> Report:
    opts.validate()
    report = Report()
    boot_wim = find_ci(root, "sources/boot.wim")
    setup_index, arch, setup_language = 0, "x64", "en-US"

    if boot_wim is not None:
        setup_index = wim.find_setup_image(boot_wim)
        info = next((i for i in wim.images(boot_wim) if i.index == setup_index), None)
        if info and info.architecture in WIM_ARCH:
            arch = WIM_ARCH[info.architecture]
        if info and info.default_language:
            setup_language = info.default_language
    else:
        report.warnings.append("sources/boot.wim not found; is this a Windows installation image?")

    with tempfile.TemporaryDirectory(prefix="toaster-") as tmp:
        work = Path(tmp)
        boot_adds: dict[str, Path] = {}

        # 1. hardware-requirement bypass
        bypass_in_registry = False
        if opts.bypass_checks:
            log("Applying hardware-requirement bypass...")
            _neutralise_appraiser(root, report)
            if boot_wim is not None:
                hive = _prepare_bypass_hive(boot_wim, setup_index, opts.bypass_checks, work / "hive", report)
                if hive is not None:
                    boot_adds[SYSTEM_HIVE_IN_WIM] = hive
                    bypass_in_registry = True

        # 2. drivers
        pe_paths: list[str] = []
        if opts.drivers:
            log("Staging drivers...")
            plan = drivers.stage_drivers(opts.drivers, work / "Drivers")
            report.warnings += plan.warnings
            oem = (find_ci(root, "sources") or root / "sources") / "$OEM$" / "$1" / "Drivers"
            oem.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(plan.staging, oem, dirs_exist_ok=True)
            report.actions.append(f"Staged {plan.inf_count} driver package(s) in {oem.relative_to(root).as_posix()} "
                                  "(installed into the new system with pnputil)")
            if boot_wim is not None:
                boot_adds["/Drivers"] = plan.staging
                pe_paths = plan.pe_paths
                report.actions.append(f"Added drivers to boot.wim image {setup_index} so Setup can use them")
            else:
                report.warnings.append("No boot.wim: drivers will not be available inside Windows Setup")

        # 3. install.wim
        offline_done = _install_operations(root, opts, report, log)

        # 4. answer file
        effective = dataclasses.replace(opts, offline_debloat=offline_done)
        install_index = _resolve_install_index(root, opts, report)
        if opts.silent_install:
            report.actions.append(
                f"Silent install: skips the language, EULA, edition and account pages "
                f"(edition index {install_index}, language {opts.locale or setup_language})"
                if install_index else "Silent install: skips the language, EULA and account pages")
            if opts.auto_partition:
                report.actions.append("AUTO-PARTITION: Setup will ERASE disk 0 of the target PC and install there "
                                      "(only if the install drive is disk 1; otherwise the disk page is shown)")
        answer = build_unattend(effective, arch, pe_bypass=not bypass_in_registry,
                                pe_driver_paths=pe_paths, os_drivers=bool(opts.drivers),
                                install_index=install_index, setup_language=setup_language)
        if answer is None:
            report.actions.append("No answer file needed")
        else:
            log("Writing answer file...")
            _place_answer_file(root, answer.xml, answer.needs_pe_pass, boot_wim, boot_adds, work, report)

        # 5. one boot.wim rewrite for everything queued above
        if boot_adds and boot_wim is not None:
            log("Updating boot.wim...")
            wim.update(boot_wim, setup_index, boot_adds)

    # 6. 2023 boot loaders
    if opts.secureboot_2023:
        if boot_wim is None:
            report.warnings.append("2023 boot loaders need boot.wim; skipped")
        else:
            log("Replacing UEFI boot loaders with 2023-signed versions...")
            n, warns = bootloaders.apply_2023_bootloaders(root, boot_wim, setup_index)
            report.warnings += warns
            if n:
                report.actions.append(f"Replaced {n} boot file(s) with 'Windows UEFI CA 2023' signed versions; "
                                      "the firmware must trust that certificate to boot this media")
    return report


def _place_answer_file(root: Path, xml: str, needs_pe: bool, boot_wim: Path | None,
                       boot_adds: dict[str, Path], work: Path, report: Report) -> None:
    if needs_pe:
        # windowsPE settings are only read from Autounattend.xml at the root of boot.wim's
        # Setup image; Setup then carries it into %WINDIR%\Panther for the later passes.
        if boot_wim is None:
            report.warnings.append("Cannot apply Windows PE settings without boot.wim; they were dropped")
            return
        src = work / "Autounattend.xml"
        src.write_text(xml, encoding="utf-8")
        boot_adds["/Autounattend.xml"] = src
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
