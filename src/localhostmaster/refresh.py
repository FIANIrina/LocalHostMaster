"""Snapshot building and the single background refresh worker."""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable, Optional

from .classifier import Classifier
from .models import (
    AccessState,
    EndpointKey,
    PortEntry,
    Protocol,
    Snapshot,
)
from .process_resolver import ProcessResolver
from .scanner import make_entry, scan_connections


def build_snapshot(
    raw_endpoints,
    resolver: ProcessResolver,
    classifier: Classifier,
    docker=None,
    include_established: bool = False,
    generation: int = 0,
    previous_first_seen: Optional[dict[EndpointKey, float]] = None,
    now: Optional[float] = None,
    docker_ok: bool = True,
) -> Snapshot:
    now = time.monotonic() if now is None else now
    previous_first_seen = previous_first_seen or {}
    # Materialise the input so a caller may pass any iterable (including a
    # generator) without it being consumed by ``begin_scan_endpoints`` and the
    # scan loop below.
    raw_endpoints = list(raw_endpoints)

    process_cache: dict[int, object] = {}
    entries: list[PortEntry] = []

    if hasattr(resolver, "begin_scan_endpoints"):
        resolver.begin_scan_endpoints(raw_endpoints)
    elif hasattr(resolver, "begin_scan"):
        resolver.begin_scan(raw.pid for raw in raw_endpoints if raw.pid is not None)

    try:
        for raw in raw_endpoints:
            process = None
            if raw.pid is not None:
                if raw.pid not in process_cache:
                    process_cache[raw.pid] = resolver.resolve(raw.pid)
                process = process_cache[raw.pid]

            container = None
            if docker is not None and docker.lookup is not None:
                container = docker.lookup(raw.port, raw.protocol, raw.local_address)

            access = AccessState.NO_PID
            if raw.pid is not None and process is not None:
                access = process.access_state

            entry = make_entry(
                raw,
                process=process,
                container=container,
                category=None,
                access_state=access,
                now=now,
            )
            entry.category = classifier.classify(entry)
            entry.first_seen = previous_first_seen.get(entry.key, now)
            entry.last_seen = now
            entries.append(entry)
    finally:
        if hasattr(resolver, "end_scan"):
            resolver.end_scan()

    return Snapshot(
        entries=entries,
        generation=generation,
        created_at=now,
        docker_ok=docker_ok,
    )


class RefreshWorker:
    """Single background thread that periodically produces snapshots."""

    def __init__(
        self,
        build: Callable[[int], Snapshot],
        interval_ms: int = 3000,
        on_snapshot: Optional[Callable[[Snapshot], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> None:
        self._build = build
        self._interval = max(0.25, interval_ms / 1000.0)
        self._on_snapshot = on_snapshot
        self._on_error = on_error
        self._generation = 0
        self._paused = False
        self._stop = threading.Event()
        self._wake = threading.Event()
        # A manual trigger (``r`` / ``a`` / post-kill refresh) must scan even
        # while auto-refresh is paused, unlike a plain interval wake-up.
        self._manual = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread is not None:
            return
        # Allow a stopped worker to be restarted.
        self._stop.clear()
        self._wake.clear()
        self._manual.clear()
        self._thread = threading.Thread(target=self._loop, name="lm-refresh", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None

    def trigger(self) -> None:
        # Manual: bypass the pause guard in ``_loop``.
        self._manual.set()
        self._wake.set()

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False
        self._wake.set()

    @property
    def paused(self) -> bool:
        return self._paused

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_interval_ms(self, interval_ms: int) -> None:
        self._interval = max(0.25, interval_ms / 1000.0)
        self._wake.set()

    def refresh_now(self) -> Optional[Snapshot]:
        """Run one scan synchronously (used for the first paint)."""
        snapshot = self._scan()
        if snapshot is not None and self._on_snapshot is not None:
            self._on_snapshot(snapshot)
        return snapshot

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Wait before the first scan: the application already performs one
            # synchronous pre-run scan for the first paint, so scanning
            # immediately here would duplicate it. A manual trigger(), resume()
            # or interval change sets ``_wake`` and skips the wait.
            self._wake.wait(self._interval)
            self._wake.clear()
            if self._stop.is_set():
                break
            manual = self._manual.is_set()
            self._manual.clear()
            if self._paused and not manual:
                continue
            snapshot = self._scan()
            if snapshot is not None and self._on_snapshot is not None:
                self._on_snapshot(snapshot)

    def _scan(self) -> Optional[Snapshot]:
        with self._lock:
            self._generation += 1
            generation = self._generation
        try:
            return self._build(generation)
        except Exception as exc:  # pragma: no cover - defensive
            if self._on_error is not None:
                self._on_error(exc)
            return None
