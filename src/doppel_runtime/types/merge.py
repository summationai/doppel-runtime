from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MergeState(StrEnum):
    WAITING_REVIEWS = "waiting_reviews"
    WAITING_CI = "waiting_ci"
    BLOCKED = "blocked"
    APPROVED = "approved"
    DISMISSED = "dismissed"  # an approval withdrawn because a new head arrived


@dataclass(frozen=True)
class ReviewerStatus:
    """One reviewer on the PR, as the merger sees it for the current head."""

    reviewer: str
    required: bool
    status: str  # posted | running | queued | blocked | waiting
    verdict: str | None = None  # the verdict on the current head, once posted


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
