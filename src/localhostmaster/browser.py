"""Open a URL in the OS default browser.

Injectable so tests never launch a real browser.
"""

from __future__ import annotations

import os
import sys
from typing import Protocol


class BrowserError(RuntimeError):
    pass


class Opener(Protocol):
    def open(self, url: str) -> None:  # pragma: no cover - protocol
        ...


class SystemBrowserOpener:
    """Uses ``os.startfile`` on Windows, which honours the default handler."""

    def open(self, url: str) -> None:
        if not url:
            raise BrowserError("empty URL")
        if os.name != "nt":
            raise BrowserError("system browser opening is only implemented on Windows")
        try:
            os.startfile(url)  # type: ignore[attr-defined]
        except OSError as exc:
            raise BrowserError(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            raise BrowserError(str(exc)) from exc


class RecordingOpener:
    """Test double: records calls instead of opening anything."""

    def __init__(self, fail: bool = False) -> None:
        self.urls: list[str] = []
        self.fail = fail

    def open(self, url: str) -> None:
        if self.fail:
            raise BrowserError("fake failure")
        self.urls.append(url)


def default_opener() -> Opener:
    if os.name == "nt":
        return SystemBrowserOpener()
    return _UnsupportedOpener()


class _UnsupportedOpener:
    def open(self, url: str) -> None:
        raise BrowserError(f"opening browsers is not supported on {sys.platform}")
