from __future__ import annotations

from enum import StrEnum


class SlotState(StrEnum):
    QUEUED = "queued"
    BLOCKED = "blocked"  # its payer has no working sign-in
    RUNNING = "running"
    POSTED = "posted"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class SlotReason(StrEnum):
    REVIEW_REQUESTED = "review_requested"
    APP = "app"
    COMMAND = "command"


class WaitReason(StrEnum):
    """Why a queued slot hasn't started. Never CI: reviewers don't wait for it; the merger does."""

    SETTLE = "settle"
    PAYER_CAP = "payer_cap"
    USAGE_WINDOW = "usage_window"
    HOST_CAP = "host_cap"
