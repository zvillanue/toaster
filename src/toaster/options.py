"""User-facing customization options and their JSON profile format."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import tweaks

# LabConfig values Windows Setup honours, keyed by the short name users pass.
BYPASS_CHECKS = {
    "tpm": "BypassTPMCheck",
    "secureboot": "BypassSecureBootCheck",
    "ram": "BypassRAMCheck",
    "cpu": "BypassCPUCheck",
    "storage": "BypassStorageCheck",
    "disk": "BypassDiskCheck",
}
DEFAULT_BYPASS = ("tpm", "secureboot", "ram")

# Names Windows refuses (or mangles) for the first local account.
RESERVED_ACCOUNT_NAMES = {
    n.lower() for n in (
        "Administrator", "Guest", "DefaultAccount", "WDAGUtilityAccount", "HelpAssistant",
        "KRBTGT", "Local", "NONE", "SYSTEM", "Administrateur", "Administrador", "Administratör",
        "Järjestelmänvalvoja", "Rendszergazda", "Администратор",
    )
}
_INVALID_ACCOUNT_CHARS = re.compile(r'[\x00-\x1f"/\\\[\]:;|=,+*?<>@]')
MAX_ACCOUNT_LENGTH = 20


class OptionsError(ValueError):
    pass


def sanitize_username(name: str) -> str:
    """Return a Windows-legal local account name, or raise OptionsError."""
    cleaned = _INVALID_ACCOUNT_CHARS.sub("_", name.strip()).rstrip(".")
    if not cleaned:
        raise OptionsError("username is empty")
    if len(cleaned) > MAX_ACCOUNT_LENGTH:
        raise OptionsError(f"username longer than {MAX_ACCOUNT_LENGTH} characters")
    if cleaned.lower() in RESERVED_ACCOUNT_NAMES:
        raise OptionsError(f"'{cleaned}' is reserved by Windows and cannot be used")
    return cleaned


@dataclass
class Customizations:
    # Windows Setup
    bypass_checks: list[str] = field(default_factory=lambda: list(DEFAULT_BYPASS))
    # OOBE
    local_account: bool = True            # skip the Microsoft-account requirement
    username: str | None = None           # pre-create this local admin (blank password, must change at logon)
    no_data_collection: bool = False      # hide EULA/privacy pages, opt out of the privacy toggles
    disable_bitlocker: bool = False       # prevent automatic device encryption
    offline_internal_drives: bool = False  # SAN policy 4: internal disks start offline
    timezone: str | None = None           # Windows time-zone name, e.g. "Pacific Standard Time"
    locale: str | None = None             # BCP-47 tag applied to input/system/user/UI, e.g. "en-GB"
    # Post-install tweaks (ids from toaster.tweaks)
    tweaks: list[str] = field(default_factory=list)

    def validate(self) -> None:
        for check in self.bypass_checks:
            if check not in BYPASS_CHECKS:
                raise OptionsError(f"unknown bypass check '{check}' (choose from {', '.join(BYPASS_CHECKS)})")
        for tweak_id in self.tweaks:
            try:
                tweaks.get(tweak_id)
            except ValueError as e:
                raise OptionsError(str(e)) from None
        if self.username is not None:
            self.username = sanitize_username(self.username)

    # --- profiles -------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2) + "\n"

    @classmethod
    def from_dict(cls, data: dict) -> "Customizations":
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise OptionsError(f"unknown profile keys: {', '.join(sorted(unknown))}")
        opts = cls(**data)
        opts.validate()
        return opts

    @classmethod
    def load(cls, path: str | Path) -> "Customizations":
        try:
            return cls.from_dict(json.loads(Path(path).read_text()))
        except json.JSONDecodeError as e:
            raise OptionsError(f"{path}: invalid JSON ({e})") from None


PRESETS = {
    # What most people reinstalling Windows 11 on unsupported or just unloved hardware want.
    "minimal": lambda: Customizations(),
    "debloat": lambda: Customizations(no_data_collection=True, disable_bitlocker=True,
                                      tweaks=tweaks.debloat_preset()),
}
