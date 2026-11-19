from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class PRRef:
    """Which pull request: unique per tenant."""

    tenant: str
    repo: str  # owner/name
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"


class PRState(StrEnum):
    OPEN = "open"
    IDLE = "idle"  # no review for 3 days: its pod was deleted; a command brings it back
    MERGED = "merged"
    CLOSED = "closed"
