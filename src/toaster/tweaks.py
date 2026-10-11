"""Catalog of post-install tweaks that make a fresh Windows 11 less intrusive.

Each tweak is plain data: registry values and/or shell commands. The unattend
generator decides *when* they run (machine-wide values during the specialize
pass, per-user values at first logon), so adding a tweak never means touching
the generator.

All keys are documented Windows policies or well-known per-user settings.
Policies marked ``edition_note`` are only honoured by some Windows editions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

HKLM = "HKLM"
HKCU = "HKCU"

_POWERSHELL = "PowerShell -NonInteractive -WindowStyle Hidden -Command"


@dataclass(frozen=True)
class RegValue:
    hive: str          # HKLM (machine-wide) or HKCU (first user to log on)
    key: str           # path below the hive
    name: str          # value name; "" means the key's (Default) value
    data: int | str
    kind: str = "REG_DWORD"

    def command(self) -> str:
        parts = [f'reg add "{self.hive}\\{self.key}"']
        parts.append(f'/v "{self.name}"' if self.name else "/ve")
        parts.append(f"/t {self.kind}")
        parts.append(f'/d "{self.data}"' if isinstance(self.data, str) else f"/d {self.data}")
        parts.append("/f")
        return " ".join(parts)


@dataclass(frozen=True)
class Tweak:
    id: str
    title: str
    category: str
    values: tuple[RegValue, ...] = ()
    machine_commands: tuple[str, ...] = ()   # run during specialize (as SYSTEM)
    edition_note: str = ""
    in_debloat_preset: bool = True
    tags: tuple[str, ...] = field(default=(), compare=False)


def _appx_removal(pattern: str) -> tuple[str, str]:
    """Remove a provisioned package (future users) and installed copies."""
    return (
        f'{_POWERSHELL} "Get-AppxProvisionedPackage -Online | Where-Object '
        f"{{$_.PackageName -like '{pattern}'}} | Remove-AppxProvisionedPackage -Online\"",
        f'{_POWERSHELL} "Get-AppxPackage -AllUsers {pattern} | Remove-AppxPackage -AllUsers"',
    )


_POLICIES = "Software\\Policies\\Microsoft"
_ADVANCED = "Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced"
_CDM = "Software\\Microsoft\\Windows\\CurrentVersion\\ContentDeliveryManager"

TWEAKS: tuple[Tweak, ...] = (
    Tweak("no-onedrive", "Disable OneDrive file sync and remove its installer", "Bloat",
          values=(RegValue(HKLM, f"{_POLICIES}\\Windows\\OneDrive", "DisableFileSyncNGSC", 1),),
          machine_commands=(
              f'{_POWERSHELL} "Remove-Item -Path $env:SystemRoot\\System32\\OneDriveSetup.exe '
              f'-Force -Confirm:$false -ErrorAction SilentlyContinue; Remove-Item -Path '
              f'$env:SystemRoot\\SysWOW64\\OneDriveSetup.exe -Force -Confirm:$false -ErrorAction SilentlyContinue"',
          )),
    Tweak("no-outlook-teams", "Remove the new Outlook and consumer Teams packages", "Bloat",
          machine_commands=(*_appx_removal("*Outlook*"), *_appx_removal("*Teams*")),
          values=(RegValue(HKLM, "Software\\Microsoft\\Windows\\CurrentVersion\\Communications",
                           "ConfigureChatAutoInstall", 0),)),
    Tweak("no-consumer-content", "Stop sponsored apps and cloud-delivered suggestions", "Bloat",
          values=(RegValue(HKLM, f"{_POLICIES}\\Windows\\CloudContent", "DisableWindowsConsumerFeatures", 1),
                  RegValue(HKLM, f"{_POLICIES}\\Windows\\CloudContent", "DisableCloudOptimizedContent", 1))),
    Tweak("no-start-ads", "Disable Start menu suggestions and Bing web search", "Bloat",
          values=(RegValue(HKCU, _CDM, "SystemPaneSuggestionsEnabled", 0),
                  RegValue(HKCU, _CDM, "SubscribedContent-338388Enabled", 0),
                  RegValue(HKCU, _CDM, "SubscribedContent-338389Enabled", 0),
                  RegValue(HKCU, "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "BingSearchEnabled", 0))),
    Tweak("no-advertising-id", "Turn off the advertising ID", "Privacy",
          values=(RegValue(HKLM, f"{_POLICIES}\\Windows\\AdvertisingInfo", "DisabledByGroupPolicy", 1),)),
    Tweak("no-telemetry", "Set diagnostic data to the minimum the edition allows", "Privacy",
          values=(RegValue(HKLM, f"{_POLICIES}\\Windows\\DataCollection", "AllowTelemetry", 0),),
          edition_note="Home and Pro treat 0 as 1 (Required); only Enterprise/Education honour 0."),
    Tweak("no-copilot", "Disable Copilot and hide its taskbar button", "AI",
          values=(RegValue(HKLM, f"{_POLICIES}\\Windows\\WindowsCopilot", "TurnOffWindowsCopilot", 1),
                  RegValue(HKCU, _ADVANCED, "ShowCopilotButton", 0))),
    Tweak("no-recall", "Turn off Windows Recall snapshots", "AI",
          values=(RegValue(HKLM, f"{_POLICIES}\\Windows\\WindowsAI", "DisableAIDataAnalysis", 1),),
          edition_note="Only has an effect on builds that ship Recall (Copilot+ PCs)."),
    Tweak("no-widgets-news", "Disable Widgets and News and Interests", "Bloat",
          values=(RegValue(HKLM, f"{_POLICIES}\\Dsh", "AllowNewsAndInterests", 0),
                  RegValue(HKLM, f"{_POLICIES}\\Windows\\Windows Feeds", "EnableFeeds", 0))),
    Tweak("no-edge-first-run", "Skip Edge's first-run experience", "Bloat",
          values=(RegValue(HKLM, f"{_POLICIES}\\Edge", "HideFirstRunExperience", 1),)),
    Tweak("no-fast-startup", "Disable Fast Startup so shutdown is a real shutdown", "Behaviour",
          values=(RegValue(HKLM, "System\\CurrentControlSet\\Control\\Session Manager\\Power",
                           "HiberbootEnabled", 0),)),
    Tweak("search-icon", "Shrink taskbar search to an icon", "UI",
          values=(RegValue(HKCU, "Software\\Microsoft\\Windows\\CurrentVersion\\Search", "SearchboxTaskbarMode", 1),)),
    Tweak("classic-context-menu", "Restore the full right-click menu", "UI", in_debloat_preset=False,
          values=(RegValue(HKCU, "Software\\Classes\\CLSID\\{86ca1aa0-34aa-4e8b-a509-50c905bae2a2}\\InprocServer32",
                           "", "", "REG_SZ"),)),
    Tweak("taskbar-left", "Align the taskbar to the left", "UI", in_debloat_preset=False,
          values=(RegValue(HKCU, _ADVANCED, "TaskbarAl", 0),)),
    Tweak("show-file-extensions", "Show file extensions and hidden files", "UI", in_debloat_preset=False,
          values=(RegValue(HKCU, _ADVANCED, "HideFileExt", 0),
                  RegValue(HKCU, _ADVANCED, "Hidden", 1))),
)

_BY_ID = {t.id: t for t in TWEAKS}


def get(tweak_id: str) -> Tweak:
    try:
        return _BY_ID[tweak_id]
    except KeyError:
        raise ValueError(f"unknown tweak '{tweak_id}' (see `toaster tweaks`)") from None


def debloat_preset() -> list[str]:
    return [t.id for t in TWEAKS if t.in_debloat_preset]
