"""Whether one queued slot can start now (spec section 5, "Slot conditions").

The scheduler walks queued slots oldest first and asks `check` for each, counting the slots it
starts as it goes. None of the conditions is CI: reviewers don't wait for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from doppel_shared.types import WaitReason


@dataclass(frozen=True)
class Limits:
    settle: timedelta = timedelta(seconds=90)  # after a push, before automatic reviews start
    payer_cap: int = 2  # running slots per payer
    host_cap: int = 10  # running slots on the host
    idle_after: timedelta = timedelta(days=3)  # a PR's pod is deleted after this long with no review
    max_attempts: int = 3
    requested_grace: timedelta = timedelta(hours=2)  # the merger waits this long for a requested person's review


@dataclass(frozen=True)
class SlotFacts:
    payer: str | None
    payer_signed_in: bool
    is_command: bool  # an explicit ask: skips the settle window
    settle_until: datetime | None
    usage_reset: datetime | None  # the payer's usage window resets then
    payer_running: int
    host_running: int


@dataclass(frozen=True)
class Start:
    pass


@dataclass(frozen=True)
class Blocked:
    """No payer, or the payer has no working sign-in. The PR says so once."""


@dataclass(frozen=True)
class Wait:
    reason: WaitReason
    until: datetime | None = None


Outcome = Start | Blocked | Wait


def check(facts: SlotFacts, now: datetime, limits: Limits) -> Outcome:
    if facts.payer is None or not facts.payer_signed_in:
        return Blocked()
    if not facts.is_command and facts.settle_until is not None and facts.settle_until > now:
        return Wait(WaitReason.SETTLE, facts.settle_until)
    if facts.usage_reset is not None and facts.usage_reset > now:
        return Wait(WaitReason.USAGE_WINDOW, facts.usage_reset)
    if facts.payer_running >= limits.payer_cap:
        return Wait(WaitReason.PAYER_CAP)
    if facts.host_running >= limits.host_cap:
        return Wait(WaitReason.HOST_CAP)
    return Start()
