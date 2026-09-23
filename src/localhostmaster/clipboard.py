"""Clipboard access via the Win32 Unicode clipboard API (no extra deps)."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Protocol

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


class ClipboardError(RuntimeError):
    pass


class Clipboard(Protocol):
    def copy(self, text: str) -> None:  # pragma: no cover - protocol
        ...


class WindowsClipboard:
    def __init__(self) -> None:
        if os.name != "nt":
            raise ClipboardError("clipboard is only implemented on Windows")
        self._user32 = ctypes.windll.user32
        self._kernel32 = ctypes.windll.kernel32
        self._configure()

    def _configure(self) -> None:
        u = self._user32
        k = self._kernel32
        u.OpenClipboard.argtypes = [wintypes.HWND]
        u.OpenClipboard.restype = wintypes.BOOL
        u.EmptyClipboard.restype = wintypes.BOOL
        u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        u.SetClipboardData.restype = wintypes.HANDLE
        u.CloseClipboard.restype = wintypes.BOOL
        k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        k.GlobalAlloc.restype = wintypes.HGLOBAL
        k.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k.GlobalLock.restype = wintypes.LPVOID
        k.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        k.GlobalUnlock.restype = wintypes.BOOL
        k.GlobalFree.argtypes = [wintypes.HGLOBAL]
        k.GlobalFree.restype = wintypes.HGLOBAL

    def copy(self, text: str) -> None:
        if not self._user32.OpenClipboard(None):
            raise ClipboardError("clipboard is busy")
        handle = None
        try:
            if not self._user32.EmptyClipboard():
                raise ClipboardError("could not empty clipboard")
            size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
            handle = self._kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                raise ClipboardError("could not allocate clipboard memory")
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                raise ClipboardError("could not lock clipboard memory")
            try:
                buffer = ctypes.create_unicode_buffer(text)
                ctypes.memmove(pointer, buffer, size)
            finally:
                self._kernel32.GlobalUnlock(handle)
            if not self._user32.SetClipboardData(CF_UNICODETEXT, handle):
                raise ClipboardError("could not set clipboard data")
            # Ownership transferred to the clipboard; do not free.
            handle = None
        finally:
            self._user32.CloseClipboard()
            if handle:
                self._kernel32.GlobalFree(handle)


class NullClipboard:
    def copy(self, text: str) -> None:
        raise ClipboardError("clipboard unavailable on this platform")


class RecordingClipboard:
    """Test double."""

    def __init__(self, fail: bool = False) -> None:
        self.items: list[str] = []
        self.fail = fail

    def copy(self, text: str) -> None:
        if self.fail:
            raise ClipboardError("fake clipboard failure")
        self.items.append(text)


def default_clipboard() -> Clipboard:
    if os.name == "nt":
        try:
            return WindowsClipboard()
        except Exception:
            return NullClipboard()
    return NullClipboard()
