"""Build a synthetic Windows installation tree for integration tests."""
from pathlib import Path

from toaster.tools import run
from .hivefixture import build_minimal_hive

EDITIONS = ("Windows 11 Pro", "Windows 11 Home", "Windows 11 Education")
ONEDRIVE = ("Windows/System32/OneDriveSetup.exe", "Windows/SysWOW64/OneDriveSetup.exe")


def _capture(wim: Path, src: Path, name: str, desc: str, first: bool) -> None:
    verb = "capture" if first else "append"
    run(["wimlib-imagex", verb, src, wim, name, desc, "--no-acls"])


def _windows_image(img: Path, with_hives=("SYSTEM",), onedrive=False, extra=None) -> None:
    cfg = img / "Windows" / "System32" / "config"
    cfg.mkdir(parents=True)
    for hive in with_hives:
        (cfg / hive).write_bytes(build_minimal_hive())
    (img / "marker.txt").write_text("image")
    if onedrive:
        for rel in ONEDRIVE:
            (img / rel).parent.mkdir(parents=True, exist_ok=True)
            (img / rel).write_bytes(b"MZ-onedrive")
    for rel, data in (extra or {}).items():
        (img / rel).parent.mkdir(parents=True, exist_ok=True)
        (img / rel).write_bytes(data)


def make_windows_tree(root: Path, *, rich_install: bool = False, efi_ex: bool = True) -> Path:
    """rich_install: install.wim gets three editions, each with SOFTWARE/SYSTEM/Default-user hives
    and OneDriveSetup.exe, like a real image. efi_ex: the Setup image carries 2023-signed boot files."""
    sources = root / "Sources"          # mixed case on purpose: ISO trees are case-insensitive
    sources.mkdir(parents=True)
    work = root.parent / (root.name + "-wimsrc")

    boot_images = [("Microsoft Windows PE (x64)", {}),
                   ("Microsoft Windows Setup (x64)",
                    {"Windows/Boot/EFI_EX/bootmgfw_EX.efi": b"2023-bootmgfw",
                     "Windows/Boot/EFI_EX/bootmgr_EX.efi": b"2023-bootmgr",
                     "Windows/Boot/Fonts_EX/segoen_slboot_EX.ttf": b"2023-font"} if efi_ex else {})]
    for n, (name, extra) in enumerate(boot_images, 1):
        img = work / f"boot{n}"
        _windows_image(img, extra=extra)
        _capture(sources / "boot.wim", img, name, name, first=(n == 1))

    (sources / "appraiserres.dll").write_bytes(b"MZ-original-appraiser")

    if rich_install:
        for n, name in enumerate(EDITIONS, 1):
            img = work / f"inst{n}"
            _windows_image(img, with_hives=("SYSTEM", "SOFTWARE"), onedrive=True,
                           extra={"Users/Default/NTUSER.DAT": build_minimal_hive(),
                                  "edition.txt": name.encode()})
            _capture(sources / "install.wim", img, name, name, first=(n == 1))
    else:
        inst = work / "inst"
        inst.mkdir()
        (inst / "x.txt").write_text("x")
        run(["wimlib-imagex", "capture", inst, sources / "install.wim", "Windows 11 Pro", "--no-acls"])

    (root / "setup.exe").write_bytes(b"MZ")
    (root / "bootmgr").write_bytes(b"bootmgr-stub")
    (root / "bootmgr.efi").write_bytes(b"2011-bootmgr")
    (root / "efi" / "boot").mkdir(parents=True)
    (root / "efi" / "boot" / "bootx64.efi").write_bytes(b"2011-bootmgfw")
    (root / "efi" / "microsoft" / "boot" / "fonts").mkdir(parents=True)
    (root / "efi" / "microsoft" / "boot" / "fonts" / "segoen_slboot.ttf").write_bytes(b"2011-font")
    return root
