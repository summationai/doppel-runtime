from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Run:
    """A running reviewer, as the gateway sees it: who pays, and how much it has spent."""

    slot_id: int
    payer: str | None
    started_at: datetime | None
    tokens_used: int
    token_budget: int | None  # None: the gateway's default


class Runs(Protocol):
    """The control plane's runs, as the gateway needs them. The gateway never sees the ledger."""

    def running(self, capability_hash: str) -> Run | None:
        """The running slot this capability was minted for, or None (unknown or finished)."""
        ...

    def add_tokens(self, slot_id: int, tokens: int) -> None: ...

    def record_usage_reset(self, payer: str, reset_at: datetime) -> None:
        """The payer hit their usage window: their runs wait until `reset_at` (DSV-12)."""
        ...
