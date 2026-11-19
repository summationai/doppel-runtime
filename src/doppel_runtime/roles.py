"""The five roles. Each is a stub at checkpoint 1; later checkpoints fill them in.

intake     GitHub events in (webhooks via the BFF, plus a reconcile poll)       checkpoint 6
scheduler  matches ready slots to their PR's pod                                checkpoint 2
broker     a Codex home per user, refresh under a per-user lock, OpenBao copy   checkpoint 3
gateway    capability -> payer -> token on the model route; the GitHub writer   checkpoint 4
sandbox    one pod per PR, a read-only checkout per head                        checkpoint 5
"""

from __future__ import annotations

from dataclasses import dataclass

from doppel_runtime.ledger import Ledger


@dataclass
class Role:
    name: str
    ledger: Ledger
    running: bool = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False


ROLE_NAMES = ("intake", "scheduler", "broker", "gateway", "sandbox")


def make_roles(ledger: Ledger) -> dict[str, Role]:
    return {name: Role(name, ledger) for name in ROLE_NAMES}
