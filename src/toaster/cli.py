"""Command line interface: ``toaster <command>``."""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import __version__, devices, drivers, media, tweaks
from .customize import customize_tree
from .options import BYPASS_CHECKS, PRESETS, Customizations, OptionsError
from .tools import ToolError
from .unattend import build_unattend


def _gib(n: int) -> str:
    return f"{n / 2**30:.1f} GiB"


def _add_customization_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("Windows customization")
    g.add_argument("--profile", metavar="FILE", help="load options from a JSON profile (see --save-profile)")
    g.add_argument("--preset", choices=[*PRESETS, "none"], help="start from a preset (default: minimal)")
    g.add_argument("--bypass", metavar="LIST", help=f"comma-separated hardware checks to bypass: {', '.join(BYPASS_CHECKS)}")
    g.add_argument("--no-bypass", action="store_true", help="do not bypass any hardware check")
    g.add_argument("--local-account", action=argparse.BooleanOptionalAction, default=None,
                   help="skip the Microsoft-account requirement during setup")
    g.add_argument("--username", help="pre-create this local administrator (blank password, change forced at first logon)")
    g.add_argument("--no-data-collection", action="store_true", default=None,
                   help="hide privacy/EULA pages and opt out of the optional privacy toggles")
    g.add_argument("--disable-bitlocker", action="store_true", default=None, help="prevent automatic device encryption")
    g.add_argument("--offline-internal-drives", action="store_true", default=None,
                   help="start with internal disks offline (protects other installs from mistakes)")
    g.add_argument("--timezone", metavar="NAME", help='Windows time-zone name, e.g. "Pacific Standard Time"')
    g.add_argument("--locale", metavar="TAG", help="language/region tag applied to input, system, user and UI, e.g. en-GB")
    g.add_argument("--tweak", action="append", default=[], metavar="ID", help="enable a tweak (repeatable; see `toaster tweaks`)")
    g.add_argument("--offline-debloat", action="store_true", default=None,
                   help="apply tweaks inside install.wim (reaches every new account; slower)")
    g.add_argument("--edition", action="append", type=int, default=[], metavar="N",
                   help="keep only this install image index (repeatable; see `toaster inspect`)")
    g.add_argument("--secureboot-2023", action="store_true", default=None,
                   help="use the 'Windows UEFI CA 2023' signed boot loaders (needs a firmware that trusts them)")
    g.add_argument("--drivers", action="append", default=[], metavar="DIR",
                   help="folder of extracted drivers for Setup and the new system (repeatable)")
    g.add_argument("--silent", action="store_true", default=None,
                   help="answer Setup's language, EULA, edition and account pages (you still choose the disk)")
    g.add_argument("--auto-partition", action="store_true", default=None,
                   help="DANGEROUS: with --silent, also ERASE disk 0 of the target PC and install there (UEFI/GPT)")
    g.add_argument("--install-edition", type=int, metavar="N",
                   help="install image index to deploy; needed with --silent when the image has several editions")
    g.add_argument("--keep-drivers", action="store_true", default=None, help="leave C:\\Drivers after installing them")
    g.add_argument("--save-profile", metavar="FILE", help="write the resulting options to a JSON profile")


def _options_from_args(a: argparse.Namespace) -> Customizations:
    if a.profile:
        opts = Customizations.load(a.profile)
    else:
        opts = PRESETS.get(a.preset or "minimal", lambda: Customizations(bypass_checks=[], local_account=False))()
    if a.no_bypass:
        opts.bypass_checks = []
    elif a.bypass:
        opts.bypass_checks = [x.strip() for x in a.bypass.split(",") if x.strip()]
    for attr in ("local_account", "no_data_collection", "disable_bitlocker", "offline_internal_drives",
                 "username", "timezone", "locale", "offline_debloat", "secureboot_2023", "keep_drivers"):
        value = getattr(a, attr)
        if value is not None:
            setattr(opts, attr, value)
    for flag, attr in (("silent", "silent_install"), ("auto_partition", "auto_partition"),
                       ("install_edition", "install_edition")):
        if getattr(a, flag) is not None:
            setattr(opts, attr, getattr(a, flag))
    opts.tweaks = list(dict.fromkeys([*opts.tweaks, *a.tweak]))
    opts.editions = sorted(set([*opts.editions, *a.edition]))
    opts.drivers = list(dict.fromkeys([*opts.drivers, *(str(Path(d).resolve()) for d in a.drivers)]))
    opts.validate()
    if a.save_profile:
        Path(a.save_profile).write_text(opts.to_json())
        print(f"Saved profile to {a.save_profile}")
    return opts


def _describe(opts: Customizations) -> list[str]:
    lines = []
    if opts.bypass_checks:
        lines.append("Bypass hardware checks: " + ", ".join(opts.bypass_checks))
    if opts.local_account:
        lines.append("Skip Microsoft-account requirement")
    if opts.username:
        lines.append(f"Create local administrator '{opts.username}' (blank password, must change at first logon)")
    if opts.no_data_collection:
        lines.append("Opt out of privacy toggles / hide EULA page")
    if opts.disable_bitlocker:
        lines.append("Prevent automatic device encryption")
    if opts.offline_internal_drives:
        lines.append("Internal drives start offline")
    if opts.timezone:
        lines.append(f"Time zone: {opts.timezone}")
    if opts.locale:
        lines.append(f"Locale: {opts.locale}")
    lines += [f"Tweak: {tweaks.get(t).title}" for t in opts.tweaks]
    if opts.tweaks and opts.offline_debloat:
        lines.append("Tweaks are applied inside install.wim (all new accounts)")
    if opts.editions:
        lines.append("Keep only install image(s): " + ", ".join(map(str, opts.editions)))
    if opts.secureboot_2023:
        lines.append("2023-signed UEFI boot loaders")
    if opts.silent_install:
        lines.append("Silent install: language, EULA, edition and account pages are skipped"
                     + (f" (edition {opts.install_edition})" if opts.install_edition else ""))
    if opts.auto_partition:
        lines.append("!! AUTO-PARTITION: the PC you install on will have DISK 0 ERASED without further questions")
    lines += [f"Drivers from {d}" for d in opts.drivers]
    return lines or ["(no customization)"]


# --- commands ---------------------------------------------------------------

def cmd_drives(a: argparse.Namespace) -> int:
    found = devices.list_drives(include_fixed=a.all)
    if not found:
        print("No removable drives found." + ("" if a.all else " (use --all to list every disk)"))
        return 0
    for d in found:
        flags = []
        if d.holds_system:
            flags.append("SYSTEM DISK")
        if d.mountpoints:
            flags.append("mounted: " + ", ".join(d.mountpoints))
        print(f"{d.path:<14} {_gib(d.size):>10}  {d.label}" + (f"  [{'; '.join(flags)}]" if flags else ""))
    return 0


def cmd_tweaks(a: argparse.Namespace) -> int:
    for t in tweaks.TWEAKS:
        star = "*" if t.in_debloat_preset else " "
        print(f"{star} {t.id:<22} [{t.category}] {t.title}")
        if t.edition_note:
            print(f"    note: {t.edition_note}")
    print("\n* = included in the 'debloat' preset")
    return 0


def cmd_unattend(a: argparse.Namespace) -> int:
    opts = _options_from_args(a)
    pe_paths = drivers.stage_drivers(opts.drivers, Path("/nonexistent"), copy=False).pe_paths if opts.drivers else []
    result = build_unattend(opts, a.arch, pe_bypass=True, pe_driver_paths=pe_paths, os_drivers=bool(opts.drivers),
                           install_index=opts.install_edition)
    if result is None:
        print("Nothing to write: no options selected.", file=sys.stderr)
        return 1
    if a.output:
        Path(a.output).write_text(result.xml, encoding="utf-8")
        print(f"Wrote {a.output}")
    else:
        sys.stdout.write(result.xml)
    return 0


def cmd_inspect(a: argparse.Namespace) -> int:
    info = media.inspect_windows_media(Path(a.source))
    print(f"Install image: {info['install_image']} ({_gib(info['install_bytes'])})")
    if info["needs_split_for_fat32"]:
        print("  larger than 4 GiB: will be split into .swm parts for a FAT32 drive")
    for e in info["editions"]:
        print(f"  [{e['index']}] {e['name']}  {e['arch']}  {e['version']}")
    print(f"boot.wim: {'present' if info['boot_wim'] else 'MISSING'}"
          + (f" (Setup image index {info['setup_image_index']})" if info["setup_image_index"] else ""))
    return 0


def _prepare(source: Path, opts: Customizations, tree: Path) -> None:
    """Extract ``source`` into the (empty or missing) directory ``tree`` and customize it there."""
    tree.parent.mkdir(parents=True, exist_ok=True)
    need = source.stat().st_size * 13 // 10 if source.is_file() else media.tree_size(source) * 11 // 10
    free = shutil.disk_usage(tree.parent).free
    if free < need:
        raise ToolError(f"not enough free space in {tree.parent}: need about {_gib(need)}, have {_gib(free)} "
                        "(use --workdir to pick another location)")
    print(f"Extracting {source.name}...")
    if source.is_dir():
        shutil.copytree(source, tree, dirs_exist_ok=True)
    else:
        media.extract_iso(source, tree)
    if media.find_install_image(tree) is None:
        raise ToolError(f"{source.name} does not look like Windows installation media "
                        "(no sources/install.wim, .esd or .swm)")
    report = customize_tree(tree, opts, log=print)
    for action in report.actions:
        print(f"  + {action}")
    for warning in report.warnings:
        print(f"  ! {warning}", file=sys.stderr)
    swm = media.split_oversized_install_wim(tree)
    if swm:
        print(f"  + install.wim exceeds the FAT32 4 GiB limit; split into {swm.name} parts")


def cmd_prepare(a: argparse.Namespace) -> int:
    opts = _options_from_args(a)
    out = Path(a.out)
    if out.exists() and any(out.iterdir()):
        raise ToolError(f"{out} is not empty")
    _prepare(Path(a.source), opts, out)
    print(f"\nCustomized files are in {out}")
    return 0


def _progress(phase: str, done: int, total: int) -> None:
    pct = 100 * done // max(total, 1)
    sys.stderr.write(f"\r  {phase:<7} {pct:3d}%  {_gib(done)} / {_gib(total)}   ")
    if phase in ("done",) or done >= total:
        sys.stderr.write("\n")
    sys.stderr.flush()


def parse_size(text: str) -> int:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?)(?:i?B)?", text.strip(), re.IGNORECASE)
    if not m:
        raise ToolError(f"cannot understand size '{text}' (examples: 8G, 512M)")
    return int(float(m.group(1)) * 1024 ** "BKMGT".index(m.group(2).upper() or "B"))


def _resolve_target(a: argparse.Namespace) -> tuple[devices.Drive, bool]:
    """The drive to write to, and whether it already existed (so overwriting needs confirmation)."""
    size = parse_size(a.image_size) if a.image_size else None
    path = Path(a.device)
    existed = path.exists()
    if size or path.is_file():
        return devices.image_drive(a.device, size), existed
    return devices.find(a.device), True


def _machine_progress(phase: str, done: int, total: int) -> None:
    """One tab-separated line per update, for front ends that drive toaster as a subprocess."""
    print(f"PROGRESS\t{phase}\t{done}\t{total}", flush=True)


def _progress_for(a: argparse.Namespace):
    return _machine_progress if getattr(a, "machine_progress", False) else _progress


def cmd_gui(a: argparse.Namespace) -> int:
    try:
        from . import gui
    except ImportError as e:        # tkinter is a separate package on most Linux distributions
        raise ToolError(f"the graphical interface needs tkinter ({e})\n  Debian/Ubuntu: sudo apt install python3-tk") from None
    return gui.main()


def cmd_flash(a: argparse.Namespace) -> int:
    """Write an already-prepared folder (from `toaster prepare`) to a drive or disk image."""
    tree = Path(a.tree)
    if not tree.is_dir():
        raise ToolError(f"{tree} is not a folder (create one with `toaster prepare`)")
    drive, existed = _resolve_target(a)
    devices.check_target(drive, allow_fixed=a.allow_fixed, needed_bytes=media.tree_size(tree))
    if existed and not a.yes:
        typed = input(f"This replaces everything in {drive.path}. Type it again to confirm: ").strip()
        if typed != drive.path:
            print("Aborted; nothing was written.")
            return 1
    media.write_tree_to_device(tree, drive, label=a.label, boot=a.boot, allow_fixed=a.allow_fixed,
                               verify=a.verify, progress=_progress_for(a))
    print(f"Done. {drive.path} is ready to boot.")
    return 0


def cmd_write(a: argparse.Namespace) -> int:
    opts = _options_from_args(a)
    if opts.auto_partition and a.boot == "bios":
        raise ToolError("--auto-partition lays the target disk out for UEFI (GPT) and cannot be combined "
                        "with --boot bios; use --boot uefi")
    opts.media_label = a.label.upper()
    opts.validate()
    drive, existed = _resolve_target(a)
    source = Path(a.source)
    devices.check_target(drive, allow_fixed=a.allow_fixed)      # fail fast, before the slow part
    with tempfile.TemporaryDirectory(prefix="toaster-", dir=a.workdir) as work:
        tree = Path(work) / "tree"
        _prepare(source, opts, tree)
        devices.check_target(drive, allow_fixed=a.allow_fixed, needed_bytes=media.tree_size(tree))
        what = "disk image" if drive.is_image else "ERASE"
        print(f"\nAbout to write ({what}) {drive.path}: {drive.label}, {_gib(drive.size)}, "
              f"{'legacy BIOS + UEFI (MBR)' if a.boot == 'bios' else 'UEFI (GPT)'}")
        print("Windows media options:")
        for line in _describe(opts):
            print(f"  - {line}")
        if opts.auto_partition and not a.yes:
            print("\n*** AUTO-PARTITION WARNING ***\n"
                  "Booting this drive will ERASE DISK 0 of that PC and install Windows, with no further prompt\n"
                  "if Setup finds this drive as disk 1. Disconnect any disk you care about first, and try it\n"
                  "in a virtual machine before using it on real hardware.")
            if input("Type 'erase disk 0' to accept: ").strip().lower() != "erase disk 0":
                print("Aborted; nothing was written.")
                return 1
        if existed and not a.yes:
            typed = input(f"\nThis replaces everything in {drive.path}. Type it again to confirm: ").strip()
            if typed != drive.path:
                print("Aborted; nothing was written.")
                return 1
        media.write_tree_to_device(tree, drive, label=a.label, boot=a.boot, allow_fixed=a.allow_fixed,
                                   verify=a.verify, progress=_progress_for(a))
    modes = "legacy BIOS and UEFI" if a.boot == "bios" else "UEFI"
    print(f"Done. {drive.path} is ready to boot ({modes}).")
    return 0


def _add_target_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("--boot", choices=["uefi", "bios"], default="uefi",
                   help="uefi: GPT (default). bios: MBR with syslinux boot code; boots legacy BIOS and UEFI")
    s.add_argument("--image-size", metavar="SIZE", help="treat DEVICE as a disk image file, creating it at this size (e.g. 8G)")
    s.add_argument("--label", default="WINSTALL")
    s.add_argument("--verify", action="store_true", help="re-read the drive and compare checksums")
    s.add_argument("--allow-fixed", action="store_true", help="permit a non-removable disk (still never the system disk)")
    s.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    s.add_argument("--machine-progress", action="store_true", help=argparse.SUPPRESS)   # for the GUI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="toaster", description="Create Windows 11 install USB drives that respect you.")
    p.add_argument("--version", action="version", version=f"toaster {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("drives", help="list USB drives"); s.add_argument("--all", action="store_true", help="include non-removable disks")
    s.set_defaults(func=cmd_drives)

    s = sub.add_parser("tweaks", help="list available post-install tweaks"); s.set_defaults(func=cmd_tweaks)

    s = sub.add_parser("unattend", help="print or save the generated answer file")
    s.add_argument("-o", "--output"); s.add_argument("--arch", default="x64", choices=["x86", "x64", "arm64"])
    _add_customization_args(s); s.set_defaults(func=cmd_unattend)

    s = sub.add_parser("inspect", help="show editions and layout of a Windows ISO or extracted folder")
    s.add_argument("source"); s.set_defaults(func=cmd_inspect)

    s = sub.add_parser("prepare", help="extract and customize an ISO into a folder (no USB needed)")
    s.add_argument("source", help="Windows ISO or extracted folder"); s.add_argument("--out", required=True)
    _add_customization_args(s); s.set_defaults(func=cmd_prepare)

    s = sub.add_parser("gui", help="open the graphical interface"); s.set_defaults(func=cmd_gui)

    s = sub.add_parser("flash", help="write a folder made by `prepare` to a USB drive or disk image (USB needs root)")
    s.add_argument("tree", help="folder created by `toaster prepare`")
    s.add_argument("device", help="whole disk (/dev/sdb) or a disk image file")
    _add_target_args(s)
    s.set_defaults(func=cmd_flash)

    s = sub.add_parser("write", help="customize an ISO and write it to a USB drive or disk image (USB needs root)")
    s.add_argument("source", help="Windows ISO or extracted folder"); s.add_argument("device", help="whole disk (/dev/sdb) or a disk image file")
    _add_target_args(s)
    s.add_argument("--workdir", help="where to stage files (needs ~1.3x the ISO size)")
    _add_customization_args(s); s.set_defaults(func=cmd_write)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ToolError, OptionsError) as e:
        print(f"toaster: error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
