"""Safe process-termination boundary.

This is the only place in the code base that may terminate a process. The TUI
never calls ``psutil.Process.kill()`` directly; it goes through
:class:`ProcessTerminator`, which:

* re-checks the security policy against *live* process metadata (not the UI
  snapshot, and never against user-editable category names);
* re-verifies the process identity (PID + creation time) before killing, so a
  recycled PID can never be hit;
* re-verifies that the selected endpoint still exists, still belongs to the same
  PID, and is still a listening/bound socket;
* distinguishes "exited" from "request sent but exit not confirmed";
* never uses a shell, PowerShell, ``taskkill`` or ctypes.

Everything is injectable so tests exercise the full path against fakes and never
touch a real process.
"""

from __future__ import annotations

import enum
import os
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import psutil

from .models import (
    KILL_WAIT_S,
    EndpointKey,
    ProcessIdentity,
    RawEndpoint,
    SocketState,
)

# Endpoint states that represent "this process occupies the port".
LISTENER_STATES = (SocketState.LISTEN, SocketState.BOUND)

# Creation times are stable floats; allow a tiny tolerance for representation.
_CREATE_TIME_EPSILON = 1e-6

# Windows core / service-host processes that must never be force-stopped.
CORE_PROCESS_NAMES = frozenset(
    {
        "system",
        "registry",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "services.exe",
        "lsass.exe",
        "winlogon.exe",
        "svchost.exe",
        "fontdrvhost.exe",
        "dwm.exe",
        "spoolsv.exe",
    }
)

# Docker Desktop host proxies: stopping one affects every container it fronts.
DOCKER_PROXY_NAMES = frozenset(
    {
        "com.docker.backend",
        "com.docker.backend.exe",
        "wslrelay",
        "wslrelay.exe",
        "dockerd",
        "dockerd.exe",
        "docker",
        "docker.exe",
    }
)

# System service accounts (only used as an *additional* signal, never alone).
SERVICE_ACCOUNTS = frozenset(
    {
        "NT AUTHORITY\\SYSTEM",
        "NT AUTHORITY\\LOCAL SERVICE",
        "NT AUTHORITY\\NETWORK SERVICE",
    }
)


class TerminationStatus(str, enum.Enum):
    EXITED = "exited"
    UNCONFIRMED = "unconfirmed"
    ALREADY_GONE = "already-gone"
    IDENTITY_MISMATCH = "identity-mismatch"
    ENDPOINT_GONE = "endpoint-gone"
    ACCESS_DENIED = "access-denied"
    PROTECTED = "protected"
    INVALID_TARGET = "invalid-target"
    UNEXPECTED_ERROR = "unexpected-error"


@dataclass(frozen=True)
class TerminationResult:
    status: TerminationStatus
    message: str
    pid: Optional[int] = None

    @property
    def is_exited(self) -> bool:
        return self.status is TerminationStatus.EXITED


def _basename(name: Optional[str]) -> str:
    return os.path.basename((name or "").strip()).lower()


def protected_reason(
    *,
    pid: Optional[int],
    name: Optional[str],
    username: Optional[str] = None,
    self_pid: Optional[int] = None,
    has_container_mapping: bool = False,
) -> Optional[str]:
    """Return a human-readable reason if this target must not be stopped.

    Autoritative policy: uses only live metadata (PID / process name / user /
    container mapping), never the user-overridable category name.
    """
    if pid is None:
        return "an endpoint without a process"
    if pid <= 0:
        return "an invalid PID"
    if pid == 4:
        return "the Windows System process (PID 4)"
    if self_pid is not None and pid == self_pid:
        return "LocalhostMaster itself"
    base = _basename(name)
    if base in CORE_PROCESS_NAMES:
        return f"a Windows core process ({name})"
    if base in DOCKER_PROXY_NAMES:
        return f"a Docker host proxy ({name})"
    if has_container_mapping:
        return "a Docker-published container endpoint"
    if username and username.upper() in SERVICE_ACCOUNTS:
        return f"a system service account ({username})"
    return None


def eligibility_reason(
    identity: ProcessIdentity,
    *,
    socket_state: SocketState,
    username: Optional[str] = None,
    self_pid: Optional[int] = None,
) -> Optional[str]:
    """Cheap, read-only eligibility check used for the first ``k``.

    Uses only metadata already present in the UI snapshot. The terminator
    re-runs the full policy against live data before actually killing.
    """
    if socket_state not in LISTENER_STATES:
        return "this endpoint is not a listening or bound socket"
    return protected_reason(
        pid=identity.pid,
        name=identity.process_name,
        username=username,
        self_pid=self_pid,
        has_container_mapping=identity.has_container_mapping,
    )


def _same_create_time(live: Optional[float], expected: Optional[float]) -> bool:
    if live is None or expected is None:
        return False
    return abs(float(live) - float(expected)) <= _CREATE_TIME_EPSILON


def _raw_endpoint_key(raw: RawEndpoint) -> EndpointKey:
    return EndpointKey(
        protocol=raw.protocol,
        family=raw.family,
        local_address=raw.local_address,
        port=raw.port,
        pid=raw.pid,
        remote_address=raw.remote_address,
        remote_port=raw.remote_port,
    )


def _default_endpoint_provider() -> list[RawEndpoint]:
    from .scanner import scan_connections

    # include_established so a state change (LISTEN -> ESTABLISHED) is visible.
    return scan_connections(include_established=True)


class ProcessTerminator:
    def __init__(
        self,
        process_factory: Callable[[int], object] = psutil.Process,
        endpoint_provider: Optional[Callable[[], Iterable[RawEndpoint]]] = None,
        self_pid_provider: Callable[[], int] = os.getpid,
        wait_timeout_s: float = KILL_WAIT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._process_factory = process_factory
        self._endpoint_provider = endpoint_provider or _default_endpoint_provider
        self._self_pid_provider = self_pid_provider
        self._wait_timeout_s = max(0.1, float(wait_timeout_s))
        self._clock = clock

    def force_stop(self, target: ProcessIdentity) -> TerminationResult:
        pid = target.pid
        self_pid = self._safe_self_pid()

        # -- 1. static validity -------------------------------------------
        if pid is None or pid <= 0:
            return self._result(
                TerminationStatus.INVALID_TARGET,
                "No valid PID for the selected endpoint.",
                pid,
            )
        if target.create_time is None:
            return self._result(
                TerminationStatus.INVALID_TARGET,
                "Process creation time is unavailable; refusing an unverified target.",
                pid,
            )

        # -- 2. live endpoint verification --------------------------------
        endpoint_status = self._verify_endpoint(target)
        if endpoint_status is not None:
            return endpoint_status

        # -- 3. open process and verify identity --------------------------
        try:
            process = self._process_factory(pid)
        except psutil.NoSuchProcess:
            return self._result(
                TerminationStatus.ALREADY_GONE, "The process already exited.", pid
            )
        except psutil.AccessDenied:
            return self._result(
                TerminationStatus.ACCESS_DENIED,
                "Access denied while opening the process.",
                pid,
            )
        except Exception:
            return self._result(
                TerminationStatus.UNEXPECTED_ERROR,
                "Could not open the process.",
                pid,
            )

        try:
            live_create_time = process.create_time()
        except psutil.NoSuchProcess:
            return self._result(
                TerminationStatus.ALREADY_GONE, "The process already exited.", pid
            )
        except psutil.AccessDenied:
            return self._result(
                TerminationStatus.ACCESS_DENIED,
                "Access denied while verifying the process.",
                pid,
            )
        except Exception:
            return self._result(
                TerminationStatus.UNEXPECTED_ERROR,
                "Could not verify the process identity.",
                pid,
            )

        if not _same_create_time(live_create_time, target.create_time):
            return self._result(
                TerminationStatus.IDENTITY_MISMATCH,
                "Process identity changed; nothing was stopped.",
                pid,
            )

        # -- 4. live security policy re-check -----------------------------
        live_name = self._safe_attr(process, "name", "") or target.process_name
        live_user = self._safe_attr(process, "username", None)
        reason = protected_reason(
            pid=pid,
            name=live_name,
            username=live_user,
            self_pid=self_pid,
            has_container_mapping=target.has_container_mapping,
        )
        if reason:
            return self._result(
                TerminationStatus.PROTECTED,
                f"Refusing to stop {reason}.",
                pid,
            )

        # -- 5. request termination ---------------------------------------
        try:
            process.kill()
        except psutil.NoSuchProcess:
            return self._result(
                TerminationStatus.ALREADY_GONE, "The process already exited.", pid
            )
        except psutil.AccessDenied:
            return self._result(
                TerminationStatus.ACCESS_DENIED,
                "Access denied. The process was not stopped.",
                pid,
            )
        except Exception:
            return self._result(
                TerminationStatus.UNEXPECTED_ERROR,
                "The force-stop request failed.",
                pid,
            )

        # -- 6. bounded wait (request sent != exited) ---------------------
        name = live_name or f"pid {pid}"
        try:
            process.wait(timeout=self._wait_timeout_s)
        except psutil.TimeoutExpired:
            return self._result(
                TerminationStatus.UNCONFIRMED,
                "Force-stop was requested, but exit was not confirmed.",
                pid,
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return self._result(
                TerminationStatus.EXITED, f"Process {name} (PID {pid}) exited.", pid
            )
        except psutil.AccessDenied:
            return self._result(
                TerminationStatus.UNCONFIRMED,
                "Force-stop was requested, but exit could not be confirmed.",
                pid,
            )
        except Exception:
            return self._result(
                TerminationStatus.UNCONFIRMED,
                "Force-stop was requested, but exit could not be confirmed.",
                pid,
            )

        return self._result(
            TerminationStatus.EXITED, f"Process {name} (PID {pid}) exited.", pid
        )

    # -- helpers -------------------------------------------------------------
    def _verify_endpoint(self, target: ProcessIdentity) -> Optional[TerminationResult]:
        try:
            endpoints = list(self._endpoint_provider())
        except Exception:
            return self._result(
                TerminationStatus.UNEXPECTED_ERROR,
                "Could not verify the endpoint.",
                target.pid,
            )
        match = None
        for raw in endpoints:
            if _raw_endpoint_key(raw) == target.endpoint_key:
                match = raw
                break
        if match is None:
            return self._result(
                TerminationStatus.ENDPOINT_GONE,
                "The selected endpoint no longer exists.",
                target.pid,
            )
        if getattr(match, "pid", None) != target.pid:
            return self._result(
                TerminationStatus.ENDPOINT_GONE,
                "The selected endpoint is now owned by a different process.",
                target.pid,
            )
        if getattr(match, "socket_state", None) not in LISTENER_STATES:
            return self._result(
                TerminationStatus.ENDPOINT_GONE,
                "The selected endpoint is no longer a listening or bound socket.",
                target.pid,
            )
        return None

    def _safe_self_pid(self) -> Optional[int]:
        try:
            return int(self._self_pid_provider())
        except Exception:
            return None

    @staticmethod
    def _safe_attr(process: object, attr: str, default):
        try:
            value = getattr(process, attr)()
            return value if value is not None else default
        except Exception:
            return default

    @staticmethod
    def _result(
        status: TerminationStatus, message: str, pid: Optional[int]
    ) -> TerminationResult:
        return TerminationResult(status=status, message=message, pid=pid)
