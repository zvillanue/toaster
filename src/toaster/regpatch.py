"""Edit offline Windows registry hives with hivex."""
from __future__ import annotations

from pathlib import Path

from .tools import ToolError, require, run


def _key_exists(hive: Path, key: str) -> bool:
    return run(["hivexget", hive, "\\" + key], check=False).returncode == 0


def _existing_dwords(hive: Path, key: str) -> dict[str, int]:
    """DWORD values already in ``key`` (empty if it does not exist).

    Raises ToolError if the key holds any other value type: hivexsh can only
    replace a key's values wholesale, so we could not preserve them.
    """
    if not _key_exists(hive, key):
        return {}
    script = "".join(f"cd {part}\n" for part in key.split("\\")) + "lsval\n"
    existing: dict[str, int] = {}
    for line in run(["hivexsh", hive], input=script).stdout.splitlines():
        name, _, rest = line.partition("=")
        if not rest.startswith("dword:"):
            raise ToolError(f"{key} holds a non-DWORD value ({line.strip()}); refusing to overwrite it")
        existing[name.strip().strip('"')] = int(rest[6:], 16)
    return existing


def set_dwords(hive: Path, key: str, values: dict[str, int]) -> None:
    """Create ``key`` (a backslash path below the hive root) and set DWORD values in it."""
    require("hivexsh", "hivexget")
    lines: list[str] = []
    walked: list[str] = []
    for part in key.split("\\"):
        walked.append(part)
        if not _key_exists(hive, "\\".join(walked)):
            lines.append(f"add {part}")
        lines.append(f"cd {part}")
    # hivexsh's `setval N` *replaces* the key's whole value set with N values, so merge
    # in what is already there and write everything in one call.
    merged = {**_existing_dwords(hive, key), **values}
    lines.append(f"setval {len(merged)}")
    for name, value in merged.items():
        lines += [name, f"dword:{value}"]
    lines.append("commit")
    run(["hivexsh", "-w", hive], input="\n".join(lines) + "\n")


def get_value(hive: Path, key: str, name: str) -> str:
    require("hivexget")
    return run(["hivexget", hive, "\\" + key, name]).stdout.strip()
