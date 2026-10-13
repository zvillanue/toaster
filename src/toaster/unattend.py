"""Generate a Windows answer file (unattend.xml) from Customizations.

Pass layout, chosen so the answer file stays as small and quiet as possible:

* windowsPE      - LabConfig bypass values, *only* when they could not be written
                   straight into boot.wim's registry hive (see customize.py).
* specialize     - OOBE network bypass and every machine-wide tweak.
* oobeSystem     - OOBE page behaviour, local account, locale, BitLocker and
                   first-logon commands for per-user tweaks.
* offlineServicing - SAN policy.
"""
from __future__ import annotations

import base64
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass

from . import tweaks
from .options import BYPASS_CHECKS, Customizations

NS = "urn:schemas-microsoft-com:unattend"
WCM = "http://schemas.microsoft.com/WMIConfig/2002/State"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
ET.register_namespace("", NS)
ET.register_namespace("wcm", WCM)
ET.register_namespace("xsi", XSI)

ARCH_NAMES = {"x86": "x86", "x64": "amd64", "amd64": "amd64", "arm64": "arm64"}


@dataclass
class Unattend:
    xml: str
    needs_pe_pass: bool   # True -> must ship as Autounattend.xml inside boot.wim


def _q(tag: str) -> str:
    return f"{{{NS}}}{tag}"


def _add(parent: ET.Element, tag: str, text: str | None = None) -> ET.Element:
    el = ET.SubElement(parent, _q(tag))
    if text is not None:
        el.text = text
    return el


def _settings(root: ET.Element, pass_name: str) -> ET.Element:
    return ET.SubElement(root, _q("settings"), {"pass": pass_name})


def _action(el: ET.Element, action: str = "add") -> ET.Element:
    el.set(f"{{{WCM}}}action", action)
    return el


def _component(settings: ET.Element, name: str, arch: str) -> ET.Element:
    return ET.SubElement(settings, _q("component"), {
        "name": name, "processorArchitecture": arch, "language": "neutral",
        "publicKeyToken": "31bf3856ad364e35", "versionScope": "nonSxS",
    })


def empty_password_blob() -> str:
    """Unattend's encoding of a blank password: base64(UTF-16LE(password + "Password"))."""
    return base64.b64encode("Password".encode("utf-16-le")).decode("ascii")


def _commands(parent: ET.Element, wrapper: str, item: str, field: str, cmds: list[str]) -> None:
    if not cmds:
        return
    block = _add(parent, wrapper)
    for order, cmd in enumerate(cmds, 1):
        entry = _action(_add(block, item))
        _add(entry, "Order", str(order))
        _add(entry, field, cmd)


def _disk_and_image(comp: ET.Element, opts: Customizations, install_index: int | None) -> None:
    """Disk layout (auto_partition only) and which image to deploy, inside Microsoft-Windows-Setup.

    Without auto_partition nothing here names a disk, so Setup still shows its "Where do you want to
    install Windows?" page and the user chooses; only the edition page is skipped.
    """
    if opts.auto_partition:
        dc = _add(comp, "DiskConfiguration")
        _add(dc, "WillShowUI", "OnError")
        if opts.disable_bitlocker:
            _add(dc, "DisableEncryptedDiskProvisioning", "true")

        # SAFEGUARD. Relabel partition 1 of disk 1, which is the install drive when the PC has
        # exactly one internal disk (disk 0) plus this drive. If disk 1 or its partition 1 does not
        # exist (for example a missing storage driver leaves the install drive as the only disk),
        # this fails, and WillShowUI=OnError then shows the normal disk page instead of erasing
        # anything. It must stay before the disk that gets wiped.
        guard = _action(_add(dc, "Disk"), "modify")
        _add(guard, "DiskID", "1")
        mods = _add(guard, "ModifyPartitions")
        mod = _action(_add(mods, "ModifyPartition"), "modify")
        _add(mod, "Order", "1")
        _add(mod, "PartitionID", "1")
        _add(mod, "Label", opts.media_label.upper())

        disk = _action(_add(dc, "Disk"))
        _add(disk, "DiskID", "0")
        _add(disk, "WillWipeDisk", "true")
        creates = _add(disk, "CreatePartitions")
        for order, (kind, size) in enumerate((("EFI", "260"), ("MSR", "16"), ("Primary", None)), 1):
            part = _action(_add(creates, "CreatePartition"))
            _add(part, "Order", str(order))
            _add(part, "Type", kind)
            if size:
                _add(part, "Size", size)
            else:
                _add(part, "Extend", "true")
        mods = _add(disk, "ModifyPartitions")
        for order, (pid, label, fs, letter) in enumerate(((1, "System", "FAT32", None),
                                                          (3, "Windows", "NTFS", "C")), 1):
            mod = _action(_add(mods, "ModifyPartition"))
            _add(mod, "Order", str(order))
            _add(mod, "PartitionID", str(pid))
            _add(mod, "Label", label)
            if letter:
                _add(mod, "Letter", letter)
            _add(mod, "Format", fs)

    image = _add(_add(comp, "ImageInstall"), "OSImage")
    _add(image, "WillShowUI", "OnError" if opts.auto_partition else "Always")
    if install_index is not None:
        meta = _action(_add(_add(image, "InstallFrom"), "MetaData"))
        _add(meta, "Key", "/IMAGE/INDEX")
        _add(meta, "Value", str(install_index))
    if opts.auto_partition:
        to = _add(image, "InstallTo")
        _add(to, "DiskID", "0")
        _add(to, "PartitionID", "3")


def build_unattend(opts: Customizations, arch: str = "x64", *, pe_bypass: bool = True,
                   pe_driver_paths: Sequence[str] = (), os_drivers: bool = False,
                   install_index: int | None = None, setup_language: str = "en-US") -> Unattend | None:
    """Return the answer file for ``opts``, or None if nothing needs one.

    ``pe_bypass`` says whether the LabConfig values still need to be applied by
    the answer file. Pass False when they were already written to boot.wim.
    ``pe_driver_paths`` are folders (inside the Setup image, e.g. ``X:\\Drivers\\vmd``) that
    Windows PE should load drivers from; ``os_drivers`` installs the staged
    ``%SystemDrive%\\Drivers`` tree into the new system with pnputil.
    ``install_index`` is the install image to deploy and ``setup_language`` the language Setup runs in;
    both are only used for silent installs.
    """
    opts.validate()
    arch = ARCH_NAMES[arch]
    root = ET.Element(_q("unattend"))

    machine_cmds: list[str] = []
    user_cmds: list[str] = []
    seen_machine: set[str] = set()

    # --- windowsPE ------------------------------------------------------
    bypass_in_answer = bool(opts.bypass_checks) and pe_bypass
    silent = opts.silent_install
    needs_pe = bypass_in_answer or silent or bool(pe_driver_paths)
    if needs_pe:
        pe = _settings(root, "windowsPE")
    if silent:
        lang = opts.locale or setup_language
        comp = _component(pe, "Microsoft-Windows-International-Core-WinPE", arch)
        _add(_add(comp, "SetupUILanguage"), "UILanguage", lang)
        for tag in ("InputLocale", "SystemLocale", "UILanguage", "UserLocale"):
            _add(comp, tag, lang)
    if bypass_in_answer or silent:
        comp = _component(pe, "Microsoft-Windows-Setup", arch)
        user = _add(comp, "UserData")
        _add(user, "AcceptEula", "true")
        _add(_add(user, "ProductKey"), "Key")      # Setup insists on a (blank) key element
        if silent:
            _disk_and_image(comp, opts, install_index)
        cmds = [f"reg add HKLM\\SYSTEM\\Setup\\LabConfig /v {BYPASS_CHECKS[c]} /t REG_DWORD /d 1 /f"
                for c in opts.bypass_checks] if bypass_in_answer else []
        _commands(comp, "RunSynchronous", "RunSynchronousCommand", "Path", cmds)
    if pe_driver_paths:
        comp = _component(pe, "Microsoft-Windows-PnpCustomizationsWinPE", arch)
        paths = _add(comp, "DriverPaths")
        for key_value, path in enumerate(pe_driver_paths, 1):
            entry = _action(_add(paths, "PathAndCredentials"))
            entry.set(f"{{{WCM}}}keyValue", str(key_value))
            _add(entry, "Path", path)

    # --- gather tweak commands -------------------------------------------
    if opts.local_account:
        machine_cmds.append('reg add "HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\OOBE" '
                            "/v BypassNRO /t REG_DWORD /d 1 /f")
    if os_drivers:
        # Runs as SYSTEM during specialize, when the driver store is available but before first logon.
        # Never fail the install over drivers: exit 0 whatever pnputil says (also if the folder is absent).
        machine_cmds.append('cmd /c "pnputil /add-driver %SystemDrive%\\Drivers\\*.inf /subdirs /install & exit /b 0"')
    for tweak_id in opts.tweaks:
        t = tweaks.get(tweak_id)
        if not opts.offline_debloat:          # offline mode writes these into the image's hives instead
            for v in t.values:
                (machine_cmds if v.hive == tweaks.HKLM else user_cmds).append(v.command())
        if not (opts.offline_debloat and t.offline_replaces_commands):
            machine_cmds.extend(t.machine_commands)
    machine_cmds = [c for c in machine_cmds if not (c in seen_machine or seen_machine.add(c))]

    # --- specialize -----------------------------------------------------
    if machine_cmds:
        sp = _settings(root, "specialize")
        comp = _component(sp, "Microsoft-Windows-Deployment", arch)
        _commands(comp, "RunSynchronous", "RunSynchronousCommand", "Path", machine_cmds)

    # --- oobeSystem -----------------------------------------------------
    first_logon = list(user_cmds)
    if os_drivers and not opts.keep_drivers:
        first_logon.append('cmd /c rmdir /s /q "%SystemDrive%\\Drivers"')
    shell_has_content = (opts.no_data_collection or silent or opts.timezone or opts.username or first_logon)
    oobe = _settings(root, "oobeSystem")
    if shell_has_content:
        comp = _component(oobe, "Microsoft-Windows-Shell-Setup", arch)
        if opts.no_data_collection or silent:
            o = _add(comp, "OOBE")
            _add(o, "HideEULAPage", "true")
            _add(o, "ProtectYourPC", "3")          # 3 = do not enable any of the "recommended" privacy toggles
            if silent:
                # Without these OOBE can still stop on "connect to a network" / "sign in" pages.
                _add(o, "HideOnlineAccountScreens", "true")
                _add(o, "HideWirelessSetupInOOBE", "true")
        if opts.timezone:
            _add(comp, "TimeZone", opts.timezone)
        if opts.username:
            accounts = _add(_add(comp, "UserAccounts"), "LocalAccounts")
            acct = _action(_add(accounts, "LocalAccount"))
            _add(acct, "Name", opts.username)
            _add(acct, "DisplayName", opts.username)
            _add(acct, "Group", "Administrators")
            pw = _add(acct, "Password")
            _add(pw, "Value", empty_password_blob())
            _add(pw, "PlainText", "false")
            # Blank password: force a change at first logon and make sure that
            # change does not start a 90-day expiry clock on the machine.
            first_logon.insert(0, f'net user "{opts.username}" /logonpasswordchg:yes')
            first_logon.insert(1, "net accounts /maxpwage:unlimited")
        _commands(comp, "FirstLogonCommands", "SynchronousCommand", "CommandLine", first_logon)

    if opts.locale:
        comp = _component(oobe, "Microsoft-Windows-International-Core", arch)
        _add(comp, "InputLocale", opts.locale)
        _add(comp, "SystemLocale", opts.locale)
        _add(comp, "UILanguage", opts.locale)
        _add(comp, "UILanguageFallback", opts.locale)
        _add(comp, "UserLocale", opts.locale)

    if opts.disable_bitlocker:
        comp = _component(oobe, "Microsoft-Windows-SecureStartup-FilterDriver", arch)
        _add(comp, "PreventDeviceEncryption", "true")
        comp = _component(oobe, "Microsoft-Windows-EnhancedStorage-Adm", arch)
        _add(comp, "TCGSecurityActivationDisabled", "1")

    if len(oobe) == 0:
        root.remove(oobe)

    # --- offlineServicing -----------------------------------------------
    if opts.offline_internal_drives:
        off = _settings(root, "offlineServicing")
        comp = _component(off, "Microsoft-Windows-PartitionManager", arch)
        _add(comp, "SanPolicy", "4")

    if len(root) == 0:
        return None
    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="unicode", xml_declaration=False)
    return Unattend('<?xml version="1.0" encoding="utf-8"?>\n' + xml + "\n", needs_pe)
