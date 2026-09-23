"""UI-facing state helpers: double-Enter state machine, sorting, filtering,
and selection retention across refreshes.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from .models import (
    ARM_GUARD_S,
    ARM_TIMEOUT_S,
    ArmedOpenState,
    EndpointKey,
    PortEntry,
)


class ArmDecision(str, enum.Enum):
    ARMED = "armed"
    OPEN = "open"
    IGNORED = "ignored"


class OpenArmController:
    """Pure double-Enter state machine. All timing uses ``time.monotonic``."""

    def __init__(
        self,
        guard_s: float = ARM_GUARD_S,
        timeout_s: float = ARM_TIMEOUT_S,
    ) -> None:
        self.guard_s = guard_s
        self.timeout_s = timeout_s
        self.armed: Optional[ArmedOpenState] = None

    @property
    def is_armed(self) -> bool:
        return self.armed is not None

    def armed_key(self) -> Optional[EndpointKey]:
        return self.armed.endpoint_key if self.armed else None

    def press(self, key: EndpointKey, url: str, now: Optional[float] = None) -> ArmDecision:
        now = time.monotonic() if now is None else now
        if self.armed is None:
            self._arm(key, url, now)
            return ArmDecision.ARMED

        same_target = self.armed.endpoint_key == key and self.armed.url == url
        if not same_target:
            self._arm(key, url, now)
            return ArmDecision.ARMED

        if now < self.armed.accept_after:
            # Keyboard auto-repeat within the guard window: ignore.
            return ArmDecision.IGNORED

        if now <= self.armed.expires_at:
            self.armed = None
            return ArmDecision.OPEN

        # Expired: this press counts as a fresh first press.
        self._arm(key, url, now)
        return ArmDecision.ARMED

    def cancel(self) -> None:
        self.armed = None

    def reconcile(self, present: dict[EndpointKey, str]) -> None:
        """Cancel the armed state if its target vanished or its URL changed.

        Called after every refresh; a plain repaint must not call this.
        """
        if self.armed is None:
            return
        current_url = present.get(self.armed.endpoint_key)
        if current_url is None or current_url != self.armed.url:
            self.armed = None

    def _arm(self, key: EndpointKey, url: str, now: float) -> None:
        self.armed = ArmedOpenState(
            endpoint_key=key,
            url=url,
            armed_at=now,
            accept_after=now + self.guard_s,
            expires_at=now + self.timeout_s,
        )

    def remaining(self, now: Optional[float] = None) -> float:
        if self.armed is None:
            return 0.0
        now = time.monotonic() if now is None else now
        return max(0.0, self.armed.expires_at - now)


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------
SORT_MODES = ("port", "process", "category", "pid")
SORT_LABELS = {
    "port": "port",
    "process": "process",
    "category": "category",
    "pid": "pid",
}


def sort_entries(entries: list[PortEntry], mode: str = "port") -> list[PortEntry]:
    if mode == "process":
        key: Callable[[PortEntry], tuple] = lambda e: (
            e.process_name.lower(),
            e.port,
        )
    elif mode == "category":
        key = lambda e: (
            (e.category.name.lower() if e.category else ""),
            e.port,
        )
    elif mode == "pid":
        key = lambda e: (e.pid if e.pid is not None else -1, e.port)
    else:
        key = lambda e: (e.port, e.protocol.value, e.family.value, e.local_address)
    return sorted(entries, key=key)


def next_sort_mode(mode: str) -> str:
    try:
        index = SORT_MODES.index(mode)
    except ValueError:
        return SORT_MODES[0]
    return SORT_MODES[(index + 1) % len(SORT_MODES)]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def apply_filter(entries: list[PortEntry], spec: str) -> list[PortEntry]:
    if not spec or spec == "all":
        return list(entries)
    if spec == "container":
        return [e for e in entries if e.container is not None]
    if spec == "openable":
        from .url_builder import is_openable

        return [e for e in entries if is_openable(e)]
    if spec.startswith("category:"):
        name = spec.split(":", 1)[1]
        return [e for e in entries if e.category is not None and e.category.name == name]
    return list(entries)


def matches_search(entry: PortEntry, query: str) -> bool:
    if not query:
        return True
    q = query.lower()
    haystack = [
        entry.local_address.lower(),
        str(entry.port),
        entry.process_name.lower(),
        entry.category.name.lower() if entry.category else "",
        entry.container.name.lower() if entry.container else "",
        entry.protocol.value.lower(),
    ]
    if entry.has_remote:
        haystack.append(entry.remote_address.lower())
        haystack.append(str(entry.remote_port))
    return any(q in field for field in haystack)


def apply_search(entries: list[PortEntry], query: str) -> list[PortEntry]:
    if not query:
        return list(entries)
    return [e for e in entries if matches_search(e, query)]


# ---------------------------------------------------------------------------
# Selection retention
# ---------------------------------------------------------------------------
def index_of_key(entries: list[PortEntry], key: Optional[EndpointKey]) -> int:
    if key is None:
        return 0
    for index, entry in enumerate(entries):
        if entry.key == key:
            return index
    return -1


def retain_selection(entries: list[PortEntry], key: Optional[EndpointKey]) -> int:
    """Return a sensible cursor index after ``entries`` changed.

    Prefers the exact same endpoint; otherwise the nearest port.
    """
    if not entries:
        return 0
    index = index_of_key(entries, key)
    if index >= 0:
        return index
    if key is None:
        return 0
    nearest = 0
    best = None
    for i, entry in enumerate(entries):
        distance = abs(entry.port - key.port)
        if best is None or distance < best:
            best = distance
            nearest = i
    return nearest


def clamp_cursor(cursor: int, length: int) -> int:
    if length <= 0:
        return 0
    return max(0, min(cursor, length - 1))
