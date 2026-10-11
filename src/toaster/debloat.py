r"""Apply tweaks inside install.wim itself ("offline"), rather than at first boot.

Compared with running ``reg add`` from the answer file this:

* reaches every account created later, because per-user values go into the
  Default User hive (``Users\Default\NTUSER.DAT``) that new profiles copy from;
* needs no commands to run during setup;
* costs time: each selected edition's hives are pulled out of the (multi-GB)
  image, edited with libhivex, and written back.

Provisioned AppX packages are deliberately NOT removed here. Their state also
lives in a database inside the image that cannot be kept consistent from Linux,
so that stays an install-time job (see ``tweaks.no-outlook-teams``).

Note: wimlib cannot restore the original NTFS security descriptors or the
hidden/system attributes on a replaced hive; the file inherits its folder's
permissions, which is what Windows would apply to a new file there anyway.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Callable

from . import regpatch, tweaks, wim
from .tools import ToolError


Entries = dict[str, list[tuple[str, tweaks.RegValue]]]     # hive id -> [(key inside hive, value)]


def plan(tweak_ids: list[str]) -> tuple[Entries, list[str]]:
    """Group selected tweaks into per-hive value lists plus file deletions."""
    by_hive: Entries = {}
    deletes: list[str] = []
    for tid in tweak_ids:
        t = tweaks.get(tid)
        for v in t.values:
            hive_id, key = tweaks.offline_target(v)
            by_hive.setdefault(hive_id, []).append((key, v))
        deletes += [d for d in t.offline_delete if d not in deletes]
    return by_hive, deletes


def apply_to_image(install_wim: Path, index: int, tweak_ids: list[str]) -> list[str]:
    """Patch one image. Returns human-readable descriptions of what changed."""
    by_hive, deletes = plan(tweak_ids)
    done: list[str] = []
    adds: dict[str, Path] = {}
    with tempfile.TemporaryDirectory(prefix="toaster-offline-") as tmp:
        for hive_id, entries in by_hive.items():
            wim_path = tweaks.OFFLINE_HIVES[hive_id]
            dest = Path(tmp) / hive_id
            dest.mkdir()
            try:
                hive_file = wim.extract(install_wim, index, wim_path, dest)
            except ToolError as e:
                raise ToolError(f"image {index}: cannot read {wim_path} ({e})") from None
            with regpatch.Hive(hive_file, write=True) as h:
                for key, v in entries:
                    h.set_value(key, v.name, v.kind, v.data)
            adds[wim_path] = hive_file
            done.append(f"{len(entries)} value(s) in {Path(wim_path).name}")
        wim.update(install_wim, index, adds, deletes)
    if deletes:
        done.append(f"removed {len(deletes)} file(s)")
    return done


def apply_offline(install_wim: Path, tweak_ids: list[str], log: Callable[[str], None]) -> None:
    for img in wim.images(install_wim):
        log(f"  offline tweaks: image {img.index} ({img.name})...")
        apply_to_image(install_wim, img.index, tweak_ids)
