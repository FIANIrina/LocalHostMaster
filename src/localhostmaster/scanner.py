"""Port scanning backend built on psutil's native IP Helper access.

``psutil.net_connections(kind="inet")`` is by far the fastest backend on this
machine (measured ~2 ms for ~190 sockets vs ~800-2200 ms for the PowerShell
cmdlets and ~100 ms for parsing ``netstat``).
"""

from __future__ import annotations

import socket
from typing import Iterable, Optional

import psutil

from .models import (
    AccessState,
    AddressFamily,
    PortEntry,
    Protocol,
    RawEndpoint,
    SocketState,
)


def family_for_af(af: int) -> AddressFamily:
    return AddressFamily.IPV6 if af == socket.AF_INET6 else AddressFamily.IPV4


def normalize_address(address: str) -> str:
    """Return a display-stable local address.

    IPv6 addresses keep any scope suffix (``%12``); callers that build URLs are
    responsible for stripping it.
    """
    if address is None:
        return ""
    return address.strip()


def split_scope(address: str) -> tuple[str, str]:
    if "%" in address:
        base, _, scope = address.partition("%")
        return base, scope
    return address, ""


def _normalize_tcp_status(status: str) -> Optional[SocketState]:
    if status == psutil.CONN_LISTEN:
        return SocketState.LISTEN
    if status == psutil.CONN_ESTABLISHED:
        return SocketState.ESTABLISHED
    return SocketState.OTHER


def scan_connections(include_established: bool = False) -> list[RawEndpoint]:
    """Scan TCP/UDP endpoints.

    * TCP LISTEN      -> always returned.
    * UDP with laddr  -> always returned, normalized to ``BOUND``.
    * other TCP states -> only when ``include_established`` is true.
    * rows without a local address are skipped.
    """
    try:
        connections = psutil.net_connections(kind="inet")
    except psutil.Error:
        return []

    results: list[RawEndpoint] = []
    for conn in connections:
        if conn.laddr is None:
            continue
        raw_address = normalize_address(conn.laddr.ip)
        port = int(conn.laddr.port)
        if not raw_address and port == 0:
            continue

        if conn.type == socket.SOCK_STREAM:
            protocol = Protocol.TCP
            state = _normalize_tcp_status(conn.status)
            if state is None:
                continue
            if state is SocketState.LISTEN:
                pass
            elif include_established:
                pass
            else:
                continue
        elif conn.type == socket.SOCK_DGRAM:
            protocol = Protocol.UDP
            state = SocketState.BOUND
        else:
            continue

        remote_address = ""
        remote_port = 0
        # Only connected sockets carry a peer address. psutil returns an empty
        # tuple (not None) for LISTEN/UDP rows, so test truthiness.
        raddr = conn.raddr
        if raddr:
            remote_address = normalize_address(raddr.ip)
            remote_port = int(raddr.port)

        results.append(
            RawEndpoint(
                protocol=protocol,
                family=family_for_af(conn.family),
                local_address=raw_address,
                port=port,
                pid=conn.pid,
                socket_state=state,
                raw_address=raw_address,
                remote_address=remote_address,
                remote_port=remote_port,
            )
        )
    return results


def iter_unique_pids(endpoints: Iterable[RawEndpoint]) -> Iterable[int]:
    seen: set[int] = set()
    for endpoint in endpoints:
        if endpoint.pid is not None and endpoint.pid not in seen:
            seen.add(endpoint.pid)
            yield endpoint.pid


def make_entry(
    raw: RawEndpoint,
    process=None,
    container=None,
    category=None,
    access_state: AccessState = AccessState.UNKNOWN,
    now: float = 0.0,
) -> PortEntry:
    from .models import EndpointKey

    key = EndpointKey(
        protocol=raw.protocol,
        family=raw.family,
        local_address=raw.local_address,
        port=raw.port,
        pid=raw.pid,
        remote_address=raw.remote_address,
        remote_port=raw.remote_port,
    )
    return PortEntry(
        key=key,
        protocol=raw.protocol,
        family=raw.family,
        local_address=raw.local_address,
        port=raw.port,
        socket_state=raw.socket_state,
        pid=raw.pid,
        process=process,
        container=container,
        category=category,
        access_state=access_state,
        first_seen=now,
        last_seen=now,
        remote_address=raw.remote_address,
        remote_port=raw.remote_port,
    )
