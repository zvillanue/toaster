"""Tiny boot payloads and a QEMU runner, so tests can prove an image really boots.

Both stubs just write a marker to COM1 and halt. They stand in for Windows'
``bootmgr`` (loaded by syslinux's chain.c32) and for ``bootx64.efi`` (loaded by
UEFI firmware), which lets the tests check partitioning, boot sectors and file
placement end to end without needing any Microsoft binaries.
"""
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path

BIOS_MARKER = "BOOTMGR-STUB-OK"
EFI_MARKER = "EFI-STUB-OK"
# 16-bit, position independent, reads the string through CS (DS is not set up when chain.c32 jumps here)
BIOS_BOOTMGR = bytes.fromhex("baf803e800005e83c60f2eac84c07403eeebf7faf4424f4f544d47522d535455422d4f4b00")
# x86-64: out dx,al loop over the marker, then halt
_EFI_CODE = bytes.fromhex("66baf803488d350c000000ac84c07403eeebf8faf4ebfc4546492d535455422d4f4b00")

OVMF_CODE = Path("/usr/share/OVMF/OVMF_CODE_4M.fd")
OVMF_VARS = Path("/usr/share/OVMF/OVMF_VARS_4M.fd")


def efi_stub() -> bytes:
    """A minimal PE32+ EFI application (subsystem 10) containing _EFI_CODE as its entry point."""
    headers = 0x200
    code = _EFI_CODE.ljust(0x200, b"\0")
    dos = b"MZ" + bytes(58) + struct.pack("<I", 0x40)
    coff = b"PE\0\0" + struct.pack("<HHIIIHH", 0x8664, 1, 0, 0, 0, 0xF0, 0x0022)
    opt = struct.pack("<HBBIIIIIQIIHHHHHHIIIIHHQQQQII", 0x20B, 1, 0, 0x200, 0, 0, 0x1000, 0x1000,
                      0x400000, 0x1000, 0x200, 0, 0, 0, 0, 0, 0, 0, 0x2000, headers, 0, 10, 0,
                      0x100000, 0x1000, 0x100000, 0x1000, 0, 16) + bytes(16 * 8)
    section = struct.pack("<8sIIIIIIHHI", b".text", len(_EFI_CODE), 0x1000, 0x200, headers, 0, 0, 0, 0, 0x60000020)
    blob = (dos + coff + opt + section).ljust(headers, b"\0") + code
    return blob


def have_qemu() -> bool:
    return shutil.which("qemu-system-x86_64") is not None


def boot_serial(image: Path, expect: str, *, uefi: bool = False, timeout: float = 90.0) -> str:
    """Boot ``image`` in QEMU and return COM1 output once ``expect`` appears (or the timeout passes)."""
    with tempfile.TemporaryDirectory() as tmp:
        serial = Path(tmp) / "serial.txt"
        serial.write_bytes(b"")
        cmd = ["qemu-system-x86_64", "-m", "256", "-display", "none", "-no-reboot",
               "-serial", f"file:{serial}", "-drive", f"file={image},format=raw"]
        if uefi:
            vars_copy = Path(tmp) / "vars.fd"
            shutil.copy(OVMF_VARS, vars_copy)
            cmd += ["-drive", f"if=pflash,format=raw,readonly=on,file={OVMF_CODE}",
                    "-drive", f"if=pflash,format=raw,file={vars_copy}"]
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                text = serial.read_bytes().decode("latin-1")
                if expect in text:
                    return text
                time.sleep(0.5)
            return serial.read_bytes().decode("latin-1")
        finally:
            proc.kill()
            proc.wait()
