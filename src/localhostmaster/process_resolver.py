"""Resolve a PID into lightweight, non-sensitive process metadata.

Steady-state performance matters: a full ``psutil`` metadata query costs a few
milliseconds per PID, which would blow the refresh budget. We therefore avoid
re-querying a PID that was already present in the previous scan.

Reusing metadata across scans is only safe while the PID still refers to the
same process. To bound that risk without paying a full query every scan:

* A PID that was *not* in the previous scan, or whose set of endpoints changed,
  or that previously resolved to ``GONE``/``UNKNOWN``, is fully re-resolved.
* Every ``verify_ttl_s`` (45 s) an otherwise-unchanged PID is re-verified by
  reading only ``create_time`` (one cheap syscall). If it is unchanged the
  cached metadata is reused; if it changed the PID was recycled and a full
  resolve runs.
* ``AccessDenied``/``NoSuchProcess`` degrade normally and never raise. A
  ``DENIED`` PID reuses its cached metadata only within the TTL and is fully
  re-resolved afterwards, so a permission change or PID recycle is picked up.

Command lines are stored because classification may need them, but they are
never logged or emitted.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import psutil

from .models import AccessState, ProcessInfo

DEFAULT_VERIFY_TTL_S = 45.0


@dataclass
class _CacheEntry:
    info: ProcessInfo
    create_time: Optional[float]
    last_verified: float
    fingerprint: frozenset


class ProcessResolver:
    """PID -> :class:`ProcessInfo` with a bounded-verification cache."""

    def __init__(
        self,
        verify_ttl_s: float = DEFAULT_VERIFY_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.verify_ttl_s = max(1.0, float(verify_ttl_s))
        self._clock = clock
        self._cache: dict[int, _CacheEntry] = {}
        self._previous_pids: set[int] = set()
        self._current_pids: set[int] = set()
        # pid -> frozenset(endpoint identities) for the scan in progress.
        self._fingerprints: dict[int, frozenset] = {}

    def clear(self) -> None:
        self._cache.clear()
        self._previous_pids.clear()
        self._current_pids.clear()
        self._fingerprints.clear()

    # -- scan lifecycle ------------------------------------------------------
    def begin_scan(self, pids: Iterable[int]) -> None:
        # No endpoint information: drop any fingerprints from a previous scan
        # so a mixed caller cannot accidentally reuse a stale fingerprint.
        self._fingerprints = {}
        self._current_pids = set(pids)

    def begin_scan_endpoints(self, endpoints: Iterable[object]) -> None:
        """Like :meth:`begin_scan` but records each PID's endpoint fingerprint."""
        fingerprints: dict[int, set] = {}
        for endpoint in endpoints:
            pid = getattr(endpoint, "pid", None)
            if pid is None:
                continue
            fingerprints.setdefault(pid, set()).add(_fingerprint(endpoint))
        self._fingerprints = {pid: frozenset(fp) for pid, fp in fingerprints.items()}
        self._current_pids = set(fingerprints)

    def end_scan(self) -> None:
        self._previous_pids = set(self._current_pids)
        for pid in list(self._cache):
            if pid not in self._previous_pids:
                del self._cache[pid]
        for pid in list(self._fingerprints):
            if pid not in self._current_pids:
                del self._fingerprints[pid]

    # -- resolution ----------------------------------------------------------
    def resolve(self, pid: Optional[int]) -> Optional[ProcessInfo]:
        if pid is None:
            return None

        now = self._clock()
        cached = self._cache.get(pid)
        fingerprint = self._fingerprints.get(pid, frozenset())
        seen_before = pid in self._previous_pids

        if cached is not None and seen_before and cached.fingerprint == fingerprint:
            state = cached.info.access_state
            if state not in (AccessState.GONE, AccessState.UNKNOWN):
                if (now - cached.last_verified) <= self.verify_ttl_s:
                    return cached.info
                # TTL expired. An OK entry is verified cheaply via create_time;
                # a DENIED entry cannot expose create_time, so it goes straight
                # to a full re-resolve (this is what stops a denied PID from
                # keeping stale metadata forever).
                if state != AccessState.DENIED:
                    create_time = self._probe_create_time(pid)
                    if create_time is not None and create_time == cached.create_time:
                        cached.last_verified = now
                        return cached.info
                # create_time changed / unreadable, or DENIED -> full re-resolve.

        info = self._full_resolve(pid)
        self._cache[pid] = _CacheEntry(
            info=info,
            create_time=info.create_time,
            last_verified=now,
            fingerprint=fingerprint,
        )
        return info

    def _probe_create_time(self, pid: int) -> Optional[float]:
        try:
            return psutil.Process(pid).create_time()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return None
        except psutil.AccessDenied:
            return None
        except Exception:
            return None

    def _full_resolve(self, pid: int) -> ProcessInfo:
        try:
            process = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return ProcessInfo(pid=pid, access_state=AccessState.GONE)
        except psutil.AccessDenied:
            return ProcessInfo(pid=pid, access_state=AccessState.DENIED)
        except Exception:
            return ProcessInfo(pid=pid, access_state=AccessState.UNKNOWN)

        try:
            with process.oneshot():
                return self._build(pid, process)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return ProcessInfo(pid=pid, access_state=AccessState.GONE)
        except psutil.AccessDenied:
            return ProcessInfo(pid=pid, access_state=AccessState.DENIED)
        except Exception:
            return ProcessInfo(pid=pid, access_state=AccessState.UNKNOWN)

    def _build(self, pid: int, process: "psutil.Process") -> ProcessInfo:
        info = ProcessInfo(pid=pid)
        denied = False
        gone = False

        try:
            info.create_time = process.create_time()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            gone = True
        except psutil.AccessDenied:
            denied = True
        except Exception:
            pass

        if gone:
            return ProcessInfo(pid=pid, access_state=AccessState.GONE)

        try:
            info.name = process.name() or ""
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return ProcessInfo(pid=pid, access_state=AccessState.GONE)
        except psutil.AccessDenied:
            denied = True
        except Exception:
            pass

        try:
            info.executable = _safe_str(process.exe())
        except psutil.AccessDenied:
            denied = True
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return ProcessInfo(pid=pid, access_state=AccessState.GONE)
        except Exception:
            pass

        try:
            parts = process.cmdline()
            if parts:
                info.command_line = " ".join(_safe_str(part) for part in parts)
        except psutil.AccessDenied:
            denied = True
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return ProcessInfo(pid=pid, access_state=AccessState.GONE)
        except Exception:
            pass

        try:
            info.username = _safe_str(process.username())
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            pass
        except Exception:
            pass

        info.access_state = AccessState.DENIED if denied else AccessState.OK
        return info


def _fingerprint(endpoint: object) -> tuple:
    """Endpoint identity used to detect a PID's socket set changing."""
    protocol = getattr(endpoint, "protocol", None)
    family = getattr(endpoint, "family", None)
    return (
        getattr(protocol, "value", protocol),
        getattr(family, "value", family),
        getattr(endpoint, "local_address", ""),
        getattr(endpoint, "port", 0),
        getattr(endpoint, "remote_address", "") or "",
        int(getattr(endpoint, "remote_port", 0) or 0),
    )


def _safe_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode(os.device_encoding(0) or "utf-8", "replace")
        except Exception:
            return value.decode("utf-8", "replace")
    return str(value)
