"""Stage extra drivers so Windows Setup can see hardware it has no driver for.

Windows' own driver database (what DISM maintains) cannot be reproduced safely
from Linux, so toaster uses the documented answer-file route instead:

* Setup (WinPE): the drivers are copied into boot.wim's Setup image at
  ``X:\\Drivers`` and listed under ``Microsoft-Windows-PnpCustomizationsWinPE``,
  so storage/network devices appear before the disk-selection screen.
* Installed system: the same files are placed in ``sources\\$OEM$\\$1\\Drivers``
  (Setup copies ``$1`` to the system drive) and installed with ``pnputil``
  during the specialize pass.

Drivers must be extracted (``.inf`` + ``.sys`` + ``.cat``) and signed.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .tools import ToolError

PE_ROOT = "X:\\Drivers"


@dataclass
class DriverPlan:
    staging: Path                                  # local folder that becomes \Drivers
    pe_paths: list[str] = field(default_factory=list)   # X:\Drivers\... one per folder holding an .inf
    inf_count: int = 0
    warnings: list[str] = field(default_factory=list)


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "drivers"


def stage_drivers(sources: list[str], staging: Path, *, copy: bool = True) -> DriverPlan:
    """Copy each source directory into ``staging/<n>-<name>`` and describe the result.

    With ``copy=False`` nothing is written; only the plan (paths, warnings) is computed.
    """
    plan = DriverPlan(staging=staging)
    if copy:
        staging.mkdir(parents=True, exist_ok=True)
    for n, src_str in enumerate(sources, 1):
        src = Path(src_str).expanduser()
        if not src.is_dir():
            raise ToolError(f"driver folder not found: {src}")
        infs = [p for p in src.rglob("*") if p.is_file() and p.suffix.lower() == ".inf"]
        if not infs:
            raise ToolError(f"{src} contains no .inf files; extract the driver package first")
        dest = staging / f"{n}-{_safe_name(src.name)}"
        if copy:
            shutil.copytree(src, dest)
        for inf in infs:
            rel_dir = (dest / inf.relative_to(src)).parent
            path = PE_ROOT + "\\" + "\\".join(rel_dir.relative_to(staging).parts)
            if path not in plan.pe_paths:
                plan.pe_paths.append(path)
            if not any(c.suffix.lower() == ".cat" for c in inf.parent.iterdir()):
                plan.warnings.append(f"{inf.relative_to(src)}: no .cat file next to it; "
                                     "unsigned drivers are rejected by 64-bit Windows")
        plan.inf_count += len(infs)
    return plan
