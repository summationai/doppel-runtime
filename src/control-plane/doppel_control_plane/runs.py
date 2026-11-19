"""The `Runs` port on the ledger: how the gateway sees the control plane's running slots."""

from __future__ import annotations

from datetime import datetime

from doppel_shared.ports.runs import Run

from doppel_control_plane.store import Ledger, parse_iso


class LedgerRuns:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def running(self, capability_hash: str) -> Run | None:
        row = self.ledger.one("SELECT * FROM slots WHERE capability_hash=? AND state='running'", (capability_hash,))
        if row is None:
            return None
        return Run(row["id"], row["payer_user"], parse_iso(row["started_at"]), row["tokens_used"], row["token_budget"])

    def add_tokens(self, slot_id: int, tokens: int) -> None:
        self.ledger.add_tokens(slot_id, tokens)

    def record_usage_reset(self, payer: str, reset_at: datetime) -> None:
        self.ledger.record_usage_reset(payer, reset_at)
