"""Build a minimal, empty Windows registry hive (regf) for tests.

hivex can edit hives but not create them, and shipping an opaque binary
fixture is undesirable, so this writes the smallest structure hivex
accepts: a 4 KiB base block plus one 4 KiB hbin holding only a root ``nk``.
"""
import struct

_BLOCK = 4096
_NONE = 0xFFFFFFFF


def _sk_cell(self_off: int) -> bytes:
    """Security cell with an empty (but structurally valid) descriptor."""
    desc = struct.pack("<BBHIIII", 1, 0, 0x8000, 0, 0, 0, 0)  # rev 1, self-relative, no owner/group/ACLs
    return (b"sk\0\0" + struct.pack("<III", self_off, self_off, 1)
            + struct.pack("<I", len(desc)) + desc)


def _nk_cell(name: bytes, sk_off: int = _NONE) -> bytes:
    out = bytearray(0x4C + len(name))
    out[0:2] = b"nk"
    struct.pack_into("<H", out, 0x02, 0x002C)  # root key, compressed name
    struct.pack_into("<I", out, 0x10, _NONE)   # parent
    struct.pack_into("<I", out, 0x1C, _NONE)   # stable subkey list
    struct.pack_into("<I", out, 0x20, _NONE)   # volatile subkey list
    struct.pack_into("<I", out, 0x28, _NONE)   # value list
    struct.pack_into("<I", out, 0x2C, sk_off)  # security
    struct.pack_into("<I", out, 0x30, _NONE)   # class
    struct.pack_into("<H", out, 0x48, len(name))
    out[0x4C:] = name
    return bytes(out)


def build_minimal_hive(root_name: str = "ROOT") -> bytes:
    def cell(payload: bytes) -> bytes:
        size = (4 + len(payload) + 7) & ~7
        return (struct.pack("<i", -size) + payload).ljust(size, b"\0")

    nk_off = 32                                   # offsets are relative to the first hbin
    nk_probe = cell(_nk_cell(root_name.encode("ascii")))
    sk_off = nk_off + len(nk_probe)
    nk_cell = cell(_nk_cell(root_name.encode("ascii"), sk_off))
    sk_cell = cell(_sk_cell(sk_off))
    used = nk_cell + sk_cell
    free = _BLOCK - 32 - len(used)
    hbin = (b"hbin" + struct.pack("<II", 0, _BLOCK)).ljust(32, b"\0")
    body = hbin + used + struct.pack("<i", free) + b"\0" * (free - 4)

    base = bytearray(_BLOCK)
    base[0:4] = b"regf"
    struct.pack_into("<II", base, 4, 1, 1)    # primary/secondary sequence
    struct.pack_into("<II", base, 20, 1, 3)   # major/minor version
    struct.pack_into("<I", base, 28, 1)       # file format
    struct.pack_into("<I", base, 36, 32)      # root cell offset (rel. to first hbin)
    struct.pack_into("<I", base, 40, _BLOCK)  # total hbin length
    struct.pack_into("<I", base, 44, 1)       # clustering factor
    csum = 0
    for i in range(0, 508, 4):
        csum ^= struct.unpack_from("<I", base, i)[0]
    struct.pack_into("<I", base, 508, csum)
    return bytes(base) + body
