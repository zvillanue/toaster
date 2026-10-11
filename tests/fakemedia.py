"""Build a synthetic Windows installation tree for integration tests."""
from pathlib import Path

from toaster.tools import run
from .hivefixture import build_minimal_hive


def _capture(wim: Path, src: Path, name: str, desc: str, first: bool) -> None:
    verb = "capture" if first else "append"
    run(["wimlib-imagex", verb, src, wim, name, desc, "--no-acls"])


def make_windows_tree(root: Path) -> Path:
    sources = root / "Sources"          # mixed case on purpose: ISO trees are case-insensitive
    sources.mkdir(parents=True)
    work = root.parent / (root.name + "-wimsrc")
    for n, (name, desc) in enumerate([("Microsoft Windows PE (x64)", "Microsoft Windows PE (x64)"),
                                      ("Microsoft Windows Setup (x64)", "Microsoft Windows Setup (x64)")], 1):
        img = work / f"img{n}"
        cfg = img / "Windows" / "System32" / "config"
        cfg.mkdir(parents=True)
        (cfg / "SYSTEM").write_bytes(build_minimal_hive())
        (img / "marker.txt").write_text(f"image {n}")
        _capture(sources / "boot.wim", img, name, desc, first=(n == 1))
    (sources / "appraiserres.dll").write_bytes(b"MZ-original-appraiser")
    inst = work / "inst"
    inst.mkdir()
    (inst / "x.txt").write_text("x")
    run(["wimlib-imagex", "capture", inst, sources / "install.wim", "Windows 11 Pro", "--no-acls"])
    (root / "setup.exe").write_bytes(b"MZ")
    return root
