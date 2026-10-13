"""Edit offline Windows registry hives through libhivex (bound with ctypes).

Unlike the ``hivexsh`` shell, libhivex can set a single value without
rewriting the rest of the key, which matters on real hives (SOFTWARE,
NTUSER.DAT) where keys hold values of many types.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
from pathlib import Path

from .tools import ToolError

HIVEX_OPEN_WRITE = 4
REG_SZ, REG_EXPAND_SZ, REG_BINARY, REG_DWORD = 1, 2, 3, 4
_KINDS = {"REG_SZ": REG_SZ, "REG_EXPAND_SZ": REG_EXPAND_SZ, "REG_BINARY": REG_BINARY, "REG_DWORD": REG_DWORD}


class _SetValue(ctypes.Structure):
    _fields_ = [("key", ctypes.c_char_p), ("t", ctypes.c_int),
                ("len", ctypes.c_size_t), ("value", ctypes.c_char_p)]


_lib: ctypes.CDLL | None = None


def _load() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    name = ctypes.util.find_library("hivex") or "libhivex.so.0"
    try:
        lib = ctypes.CDLL(name, use_errno=True)
    except OSError:
        raise ToolError("libhivex not found\n  Debian/Ubuntu: sudo apt install libhivex0") from None
    lib.hivex_open.restype = ctypes.c_void_p
    lib.hivex_open.argtypes = [ctypes.c_char_p, ctypes.c_int]
    lib.hivex_close.argtypes = [ctypes.c_void_p]
    lib.hivex_root.restype = ctypes.c_size_t
    lib.hivex_root.argtypes = [ctypes.c_void_p]
    for fn in (lib.hivex_node_get_child, lib.hivex_node_add_child, lib.hivex_node_get_value):
        fn.restype = ctypes.c_size_t
        fn.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p]
    lib.hivex_node_set_value.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(_SetValue), ctypes.c_int]
    lib.hivex_commit.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    lib.hivex_value_value.restype = ctypes.c_void_p
    # char *hivex_value_value(hive_h *, hive_value_h, hive_type *type, size_t *len)
    lib.hivex_value_value.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_int),
                                      ctypes.POINTER(ctypes.c_size_t)]
    _lib = lib
    return lib


def _fail(what: str) -> ToolError:
    err = ctypes.get_errno()
    return ToolError(f"{what}: {os.strerror(err) if err else 'unknown error'}")


class Hive:
    """An offline registry hive. Use as a context manager; changes are committed on clean exit."""

    def __init__(self, path: str | Path, *, write: bool = False) -> None:
        self.path = str(path)
        self._write = write
        self._lib = _load()
        self._h = self._lib.hivex_open(self.path.encode(), HIVEX_OPEN_WRITE if write else 0)
        if not self._h:
            raise _fail(f"cannot open registry hive {self.path}")

    def __enter__(self) -> "Hive":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None and self._write:
                if self._lib.hivex_commit(self._h, None, 0) != 0:
                    raise _fail(f"cannot save registry hive {self.path}")
        finally:
            self._lib.hivex_close(self._h)
            self._h = None

    # --- navigation -------------------------------------------------------
    def find(self, key: str) -> int:
        """Node handle for a backslash path below the root, or 0 if it does not exist."""
        node = self._lib.hivex_root(self._h)
        for part in filter(None, key.split("\\")):
            node = self._lib.hivex_node_get_child(self._h, node, part.encode()) if node else 0
        return node

    def ensure(self, key: str) -> int:
        """Like find, but creates every missing key along the path."""
        node = self._lib.hivex_root(self._h)
        for part in filter(None, key.split("\\")):
            child = self._lib.hivex_node_get_child(self._h, node, part.encode())
            if not child:
                child = self._lib.hivex_node_add_child(self._h, node, part.encode())
                if not child:
                    raise _fail(f"cannot create registry key '{part}' in '{key}'")
            node = child
        return node

    # --- values -----------------------------------------------------------
    def set_value(self, key: str, name: str, kind: str, data: int | str) -> None:
        """Set one value, creating the key if needed. ``name`` '' is the key's (Default) value."""
        if kind == "REG_DWORD":
            payload = int(data).to_bytes(4, "little")
        elif kind in ("REG_SZ", "REG_EXPAND_SZ"):
            payload = (str(data) + "\0").encode("utf-16-le")
        else:
            raise ToolError(f"unsupported registry value type {kind}")
        node = self.ensure(key)
        buf = ctypes.create_string_buffer(payload, len(payload))
        sv = _SetValue(name.encode(), _KINDS[kind], len(payload), ctypes.cast(buf, ctypes.c_char_p))
        if self._lib.hivex_node_set_value(self._h, node, ctypes.byref(sv), 0) != 0:
            raise _fail(f"cannot set registry value '{name}'")

    def get_value(self, key: str, name: str) -> int | str | bytes | None:
        node = self.find(key)
        val = self._lib.hivex_node_get_value(self._h, node, name.encode()) if node else 0
        if not val:
            return None
        size, kind = ctypes.c_size_t(), ctypes.c_int()
        ptr = self._lib.hivex_value_value(self._h, val, ctypes.byref(kind), ctypes.byref(size))
        if not ptr:
            return None
        raw = ctypes.string_at(ptr, size.value)
        ctypes.CDLL(None).free(ctypes.c_void_p(ptr))
        if kind.value == REG_DWORD:
            return int.from_bytes(raw[:4], "little")
        if kind.value in (REG_SZ, REG_EXPAND_SZ):
            return raw.decode("utf-16-le").rstrip("\0")
        return raw


# --- convenience wrappers ----------------------------------------------------

def set_dwords(hive: Path, key: str, values: dict[str, int]) -> None:
    """Create ``key`` and set DWORD values in it, leaving every other value alone."""
    with Hive(hive, write=True) as h:
        for name, value in values.items():
            h.set_value(key, name, "REG_DWORD", value)


def get_value(hive: Path, key: str, name: str) -> str:
    with Hive(hive) as h:
        value = h.get_value(key, name)
    if value is None:
        raise ToolError(f"{key}\\{name}: key not found")
    return str(value)
