"""Optional Docker enrichment layer.

Runs ``docker ps --format "{{json .}}"`` on a background thread at a low
cadence, with a hard timeout. It never blocks the port scan and degrades
silently when Docker is missing, stopped, or slow.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .models import ContainerInfo, Protocol

CREATE_NO_WINDOW = 0x08000000

_PORT_RE = re.compile(
    r"(?:(?P<host>\[[^\]]+\]|[0-9a-fA-F:.]+):)?"
    r"(?P<hostport>\d+)->(?P<cport>\d+)/(?P<proto>tcp|udp)"
)
_LABEL_PORT_RE = re.compile(r"^desktop\.docker\.io/ports/(?P<cport>\d+)/(?P<proto>tcp|udp)$")


def _normalize_host(host: str) -> str:
    if not host:
        return ""
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host


def parse_ports_string(ports: str) -> list[tuple[str, int, int, Protocol]]:
    out: list[tuple[str, int, int, Protocol]] = []
    for match in _PORT_RE.finditer(ports or ""):
        host = _normalize_host(match.group("host") or "")
        proto = Protocol.TCP if match.group("proto") == "tcp" else Protocol.UDP
        out.append((host, int(match.group("hostport")), int(match.group("cport")), proto))
    return out


def parse_labels_for_ports(labels: str) -> dict[tuple[int, Protocol], str]:
    """Extract ``{ (container_port, proto): host_port }`` from Docker labels."""
    found: dict[tuple[int, Protocol], str] = {}
    for item in (labels or "").split(","):
        key, _, value = item.partition("=")
        match = _LABEL_PORT_RE.match(key.strip())
        if not match:
            continue
        proto = Protocol.TCP if match.group("proto") == "tcp" else Protocol.UDP
        host_port = value.strip().lstrip(":")
        if host_port:
            found[(int(match.group("cport")), proto)] = host_port
    return found


def parse_docker_ps_output(output: str) -> list[ContainerInfo]:
    """Parse ``docker ps --format '{{json .}}'`` output (one JSON per line)."""
    containers: list[ContainerInfo] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        cid = str(data.get("ID", "") or "")
        name = str(data.get("Names", "") or "")
        image = str(data.get("Image", "") or "")
        ports = str(data.get("Ports", "") or "")
        labels = str(data.get("Labels", "") or "")

        mappings = parse_ports_string(ports)
        if not mappings:
            for (cport, proto), host_port in parse_labels_for_ports(labels).items():
                mappings.append(("", int(host_port), cport, proto))

        for host, host_port, cport, proto in mappings:
            containers.append(
                ContainerInfo(
                    id=cid,
                    name=name,
                    image=image,
                    host_address=host,
                    host_port=host_port,
                    container_port=cport,
                    protocol=proto,
                )
            )
    return containers


class DockerResolver:
    def __init__(
        self,
        enabled: bool = True,
        ttl_s: float = 15.0,
        timeout_s: float = 2.0,
        runner: Optional[Callable[[float], str]] = None,
    ) -> None:
        self.enabled = enabled and (runner is not None or shutil.which("docker") is not None)
        self.ttl_s = max(1.0, float(ttl_s))
        self.timeout_s = max(0.1, float(timeout_s))
        self._runner = runner or self._default_runner
        self._lock = threading.Lock()
        self._mappings: list[ContainerInfo] = []
        self._last_ok: float = 0.0
        self._last_error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- background lifecycle ------------------------------------------------
    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        # Allow a stopped resolver to be restarted.
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="docker-resolver", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh_once()
            self._stop.wait(self.ttl_s)

    def refresh_once(self) -> None:
        if not self.enabled:
            return
        try:
            output = self._runner(self.timeout_s)
        except Exception as exc:
            with self._lock:
                self._last_error = type(exc).__name__
            return
        mappings = parse_docker_ps_output(output)
        with self._lock:
            self._mappings = mappings
            self._last_ok = time.monotonic()
            self._last_error = None

    def _default_runner(self, timeout: float) -> str:
        docker = shutil.which("docker")
        if not docker:
            raise FileNotFoundError("docker not found")
        result = subprocess.run(
            [docker, "ps", "--format", "{{json .}}"],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("docker ps failed")
        return result.stdout

    # -- lookup --------------------------------------------------------------
    @property
    def available(self) -> bool:
        with self._lock:
            return self._last_ok > 0 and self._last_error is None

    def lookup(self, host_port: int, protocol: Protocol, host_address: str = "") -> Optional[ContainerInfo]:
        with self._lock:
            mappings = list(self._mappings)
        candidates = [m for m in mappings if m.host_port == host_port and m.protocol == protocol]
        if not candidates:
            return None
        wildcard = {"0.0.0.0", "::", ""}
        for candidate in candidates:
            if candidate.host_address and candidate.host_address == _normalize_host(host_address):
                return candidate
        for candidate in candidates:
            if candidate.host_address in wildcard:
                return candidate
        return candidates[0]
