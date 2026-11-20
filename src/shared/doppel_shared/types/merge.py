from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class MergeState(StrEnum):
    WAITING_REVIEWS = "waiting_reviews"
    WAITING_CI = "waiting_ci"
    BLOCKED = "blocked"
    APPROVED = "approved"
    DISMISSED = "dismissed"  # an approval withdrawn because a new head arrived


@dataclass(frozen=True)
class ReviewerStatus:
    """One reviewer on the PR, as the merger sees it for the current head.

    App reviewers are always required. A requested personal reviewer is required until `wait_until`
    (its grace period), after which the merger stops waiting for it; its P0/P1s still block whenever
    they arrive.
    """

    reviewer: str
    app: bool  # the app's own reviewer (repo-owner), as opposed to a requested person's
    required: bool  # the merger waits for this reviewer's verdict on the current head
    status: str  # posted | running | queued | blocked | waiting
    verdict: str | None = None  # the verdict on the current head, once posted
    wait_until: datetime | None = None  # personal: when the merger stops waiting for it


@dataclass(frozen=True)
class OpenFinding:
    reviewer: str
    fid: str
    severity: str
    file: str
    line: int
    claim: str


@dataclass(frozen=True)
class MergeInput:
    head: str
    reviewers: tuple[ReviewerStatus, ...]
    ci: str | None  # None (no checks yet) | pending | success | failure
    blocking: tuple[OpenFinding, ...]  # open P0/P1, from any reviewer, after dispositions


@dataclass(frozen=True)
class Decision:
    state: MergeState
    reason: str
