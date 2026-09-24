"""Pure double-``k`` force-stop state machine.

Kept separate from :class:`~localhostmaster.state.OpenArmController` on purpose:
opening a browser and killing a process have very different safety semantics, and
the double-Enter controller is already stable and well tested. This module has no
dependency on psutil or the UI; it only decides *when* a kill target is
confirmed. The actual termination lives in
:mod:`localhostmaster.process_terminator`.

All timing uses :func:`time.monotonic`.
"""

from __future__ import annotations

import enum
import time
from typing import Mapping, Optional

from .models import (
    KILL_GUARD_S,
    KILL_TIMEOUT_S,
    ArmedKillState,
    EndpointKey,
    ProcessIdentity,
)


class KillDecision(str, enum.Enum):
    ARMED = "armed"
    IGNORED = "ignored"
    SUBMIT = "submit"


class KillArmController:
    """Double-``k`` confirmation state machine with identity tracking.

    Identity equality includes ``EndpointKey`` *and* ``pid`` *and*
    ``create_time`` — matching on the endpoint alone would not catch a PID that
    was instantly recycled onto the same socket.
    """

    def __init__(
        self,
        guard_s: float = KILL_GUARD_S,
        timeout_s: float = KILL_TIMEOUT_S,
    ) -> None:
        self.guard_s = guard_s
        self.timeout_s = timeout_s
        self.armed: Optional[ArmedKillState] = None

    @property
    def is_armed(self) -> bool:
        return self.armed is not None

    def target(self) -> Optional[ProcessIdentity]:
        return self.armed.target if self.armed else None

    def press(
        self, identity: ProcessIdentity, now: Optional[float] = None
    ) -> KillDecision:
        now = time.monotonic() if now is None else now
        if self.armed is None:
            self._arm(identity, now)
            return KillDecision.ARMED

        if self.armed.target != identity:
            # A different target counts as a fresh first press.
            self._arm(identity, now)
            return KillDecision.ARMED

        if now < self.armed.accept_after:
            # Keyboard auto-repeat inside the guard window: ignore.
            return KillDecision.IGNORED

        if now <= self.armed.expires_at:
            self.armed = None
            return KillDecision.SUBMIT

        # Expired: this press starts a new confirmation.
        self._arm(identity, now)
        return KillDecision.ARMED

    def cancel(self) -> None:
        self.armed = None

    def reconcile(self, present: Mapping[EndpointKey, ProcessIdentity]) -> None:
        """Cancel if the armed target vanished or its identity changed.

        Call after every refresh; a plain repaint must not call this.
        """
        if self.armed is None:
            return
        current = present.get(self.armed.target.endpoint_key)
        if current != self.armed.target:
            self.armed = None

    def remaining(self, now: Optional[float] = None) -> float:
        if self.armed is None:
            return 0.0
        now = time.monotonic() if now is None else now
        return max(0.0, self.armed.expires_at - now)

    def _arm(self, identity: ProcessIdentity, now: float) -> None:
        self.armed = ArmedKillState(
            target=identity,
            armed_at=now,
            accept_after=now + self.guard_s,
            expires_at=now + self.timeout_s,
        )
