"""Find USB drives and refuse targets that could destroy the running system."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .tools import ToolError, require, run

# Mount points that mean "this disk is (part of) the running system".
SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "/etc", "/opt", "/nix", "/gnu")


@dataclass
class Drive:
    path: str
    size: int
    model: str = ""
    vendor: str = ""
    transport: str = ""
    removable: bool = False
    hotplug: bool = False
    read_only: bool = False
    kind: str = "disk"
    mountpoints: list[str] = field(default_factory=list)   # of the disk and all its partitions

    @property
    def is_removable(self) -> bool:
        return self.removable or self.hotplug or self.transport == "usb"

    @property
    def holds_system(self) -> bool:
        return any(m in SYSTEM_MOUNTS or m.startswith(("/boot/", "/home/")) for m in self.mountpoints)

    @property
    def label(self) -> str:
        return " ".join(x for x in (self.vendor, self.model) if x) or "(unknown device)"


def _mounts(node: dict) -> list[str]:
    mps = node.get("mountpoints") or [node.get("mountpoint")]
    found = [m for m in mps if m]
    for child in node.get("children", []) or []:
        found += _mounts(child)
    return found


def parse_lsblk(text: str) -> list[Drive]:
    drives = []
    for node in json.loads(text).get("blockdevices", []):
        if node.get("type") not in ("disk", "loop"):
            continue
        drives.append(Drive(
            path=node.get("path") or f"/dev/{node['name']}",
            size=int(node.get("size") or 0),
            model=(node.get("model") or "").strip(),
            vendor=(node.get("vendor") or "").strip(),
            transport=node.get("tran") or "",
            removable=bool(node.get("rm")),
            hotplug=bool(node.get("hotplug")),
            read_only=bool(node.get("ro")),
            kind=node["type"],
            mountpoints=_mounts(node),
        ))
    return drives


def list_drives(*, include_fixed: bool = False) -> list[Drive]:
    require("lsblk")
    cols = "NAME,PATH,SIZE,RM,HOTPLUG,TRAN,TYPE,MODEL,VENDOR,RO,MOUNTPOINTS"
    drives = parse_lsblk(run(["lsblk", "-J", "-b", "-o", cols]).stdout)
    return [d for d in drives if include_fixed or (d.kind == "disk" and d.is_removable)]


def check_target(drive: Drive, *, allow_fixed: bool = False, needed_bytes: int = 0) -> None:
    """Raise ToolError if writing to ``drive`` is unsafe. ``allow_fixed`` only relaxes the USB check."""
    if drive.holds_system:
        raise ToolError(f"{drive.path} holds the running system ({', '.join(drive.mountpoints)}); refusing")
    if drive.read_only:
        raise ToolError(f"{drive.path} is read-only (check the drive's write-protect switch)")
    if drive.size == 0:
        raise ToolError(f"{drive.path} reports no media")
    if not drive.is_removable and not allow_fixed:
        raise ToolError(f"{drive.path} does not look like a USB/removable drive; pass --allow-fixed to override")
    if needed_bytes and drive.size < needed_bytes:
        raise ToolError(f"{drive.path} is {drive.size / 2**30:.1f} GiB but the image needs "
                        f"about {needed_bytes / 2**30:.1f} GiB")


def find(path: str) -> Drive:
    for d in list_drives(include_fixed=True):
        if d.path == path:
            return d
    raise ToolError(f"{path} is not a whole-disk block device (give /dev/sdX, not a partition)")
