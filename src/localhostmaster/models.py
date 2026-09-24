"""Typed data model for LocalhostMaster.

All timestamps that are used for *logic* (double-Enter timeouts, first/last
seen bookkeeping) use :func:`time.monotonic` so they are immune to wall-clock
changes.
"""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Optional

# Double-Enter tuning constants (seconds).
ARM_GUARD_S = 0.150
ARM_TIMEOUT_S = 2.500

# Double-``k`` force-stop tuning constants (seconds). Deliberately fixed, not
# user-configurable: this is a destructive action and must stay predictable.
KILL_GUARD_S = 0.150
KILL_TIMEOUT_S = 2.500
# How long the terminator waits for the process to actually exit after kill().
KILL_WAIT_S = 1.5


class Protocol(str, enum.Enum):
    TCP = "TCP"
    UDP = "UDP"


class AddressFamily(str, enum.Enum):
    IPV4 = "IPv4"
    IPV6 = "IPv6"


class SocketState(str, enum.Enum):
    LISTEN = "LISTEN"
    BOUND = "BOUND"
    ESTABLISHED = "ESTABLISHED"
    OTHER = "OTHER"


class AccessState(str, enum.Enum):
    OK = "ok"
    DENIED = "denied"
    UNKNOWN = "unknown"
    NO_PID = "no-pid"
    GONE = "gone"


@dataclass(frozen=True)
class EndpointKey:
    """Stable identity of a single socket endpoint.

    Never deduplicate by port alone: the same port may legitimately be bound by
    several processes / address families. For connected (non-LISTEN) TCP rows
    the *remote* endpoint is part of the identity too, otherwise two
    connections that share a local endpoint but point at different peers would
    collapse into one entry.

    ``remote_address``/``remote_port`` are empty/``0`` for listeners and UDP
    sockets, so the established identity of those rows is unchanged.
    """

    protocol: Protocol
    family: AddressFamily
    local_address: str
    port: int
    pid: Optional[int]
    remote_address: str = ""
    remote_port: int = 0

    @property
    def has_remote(self) -> bool:
        return bool(self.remote_address) or self.remote_port != 0

    def sort_key(self) -> tuple:
        return (
            self.port,
            self.protocol.value,
            self.family.value,
            self.local_address,
            self.pid if self.pid is not None else -1,
            self.remote_address,
            self.remote_port,
        )


@dataclass(frozen=True)
class ProcessIdentity:
    """Everything needed to safely identify a kill target across two ``k``.

    A bare PID is never enough: a PID can be recycled between the first and the
    second ``k``. The (pid, create_time) pair is the same identity tuple psutil
    itself uses, and ``endpoint_key`` ties the target to the row the user
    selected. ``process_name`` is for display only.
    """

    pid: int
    create_time: float
    endpoint_key: EndpointKey
    process_name: str = ""
    # True when the host endpoint has a Docker container mapping attached.
    # Part of the identity so a mapping change between the two ``k`` cancels.
    has_container_mapping: bool = False


@dataclass
class ProcessInfo:
    pid: int
    create_time: Optional[float] = None
    name: str = ""
    executable: str = ""
    # Internal, potentially sensitive. Never logged, never emitted in JSON.
    command_line: Optional[str] = None
    username: Optional[str] = None
    access_state: AccessState = AccessState.UNKNOWN

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name
        if self.executable:
            return self.executable
        return f"pid {self.pid}"


@dataclass
class ContainerInfo:
    id: str = ""
    name: str = ""
    image: str = ""
    host_address: str = ""
    host_port: int = 0
    container_port: int = 0
    protocol: Protocol = Protocol.TCP

    @property
    def short_id(self) -> str:
        return self.id[:12] if self.id else ""


@dataclass
class CategoryAssignment:
    name: str
    color: str = ""
    icon: str = ""
    scheme: str = ""
    open_in_browser: bool = True
    source: str = "builtin"
    rule_id: str = ""


@dataclass
class CategoryRule:
    id: str
    name: str
    color: str = ""
    icon: str = ""
    priority: int = 0
    # Positive conditions. AND across fields, OR within a field.
    process_globs: list[str] = field(default_factory=list)
    executable_globs: list[str] = field(default_factory=list)
    command_line_globs: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    port_ranges: list[tuple[int, int]] = field(default_factory=list)
    protocols: list[str] = field(default_factory=list)
    address_globs: list[str] = field(default_factory=list)
    container_name_globs: list[str] = field(default_factory=list)
    container_image_globs: list[str] = field(default_factory=list)
    match_any_container: bool = False
    # Exclusions: any match vetoes the rule.
    exclude_process_globs: list[str] = field(default_factory=list)
    exclude_executable_globs: list[str] = field(default_factory=list)
    exclude_command_line_globs: list[str] = field(default_factory=list)
    exclude_ports: list[int] = field(default_factory=list)
    scheme: str = ""
    open_in_browser: bool = True
    source: str = "builtin"

    def has_positive_condition(self) -> bool:
        return bool(
            self.process_globs
            or self.executable_globs
            or self.command_line_globs
            or self.ports
            or self.port_ranges
            or self.protocols
            or self.address_globs
            or self.container_name_globs
            or self.container_image_globs
            or self.match_any_container
        )


@dataclass
class PortEntry:
    key: EndpointKey
    protocol: Protocol
    family: AddressFamily
    local_address: str
    port: int
    socket_state: SocketState
    pid: Optional[int]
    process: Optional[ProcessInfo] = None
    container: Optional[ContainerInfo] = None
    category: Optional[CategoryAssignment] = None
    access_state: AccessState = AccessState.UNKNOWN
    first_seen: float = 0.0
    last_seen: float = 0.0
    remote_address: str = ""
    remote_port: int = 0

    @property
    def process_name(self) -> str:
        if self.process is not None:
            return self.process.display_name
        if self.pid is None:
            return "-"
        return f"pid {self.pid}"

    @property
    def has_remote(self) -> bool:
        return bool(self.remote_address) or self.remote_port != 0

    @property
    def remote_display(self) -> str:
        if not self.has_remote:
            return ""
        return f"{self.remote_address}:{self.remote_port}"

    @property
    def is_listener(self) -> bool:
        return self.socket_state in (SocketState.LISTEN, SocketState.BOUND)


@dataclass
class RawEndpoint:
    """A socket row straight from the OS, before enrichment."""

    protocol: Protocol
    family: AddressFamily
    local_address: str
    port: int
    pid: Optional[int]
    socket_state: SocketState
    raw_address: str = ""
    remote_address: str = ""
    remote_port: int = 0


@dataclass
class ContainerEndpoint:
    container: ContainerInfo

    def matches(self, port: int, protocol: Protocol) -> bool:
        return self.container.host_port == port and self.container.protocol == protocol


@dataclass
class ArmedOpenState:
    endpoint_key: EndpointKey
    url: str
    armed_at: float
    accept_after: float
    expires_at: float

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        return now > self.expires_at


@dataclass
class ArmedKillState:
    target: ProcessIdentity
    armed_at: float
    accept_after: float
    expires_at: float

    def is_expired(self, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        return now > self.expires_at


@dataclass
class Snapshot:
    """Immutable-ish scan result handed from the worker to the UI thread."""

    entries: list[PortEntry]
    generation: int
    created_at: float
    error: Optional[str] = None
    docker_ok: bool = True
