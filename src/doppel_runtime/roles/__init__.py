"""The five roles. A role wires `core`, `store` and `ports` together and handles events.

    intake     GitHub events in (webhooks via the BFF, plus a reconcile poll)       checkpoint 6
    scheduler  queues slots, matches ready ones to their PR's pod, runs the merger  checkpoint 2
    broker     a Codex home per user, refresh under a per-user lock, OpenBao copy   checkpoint 3
    gateway    capability -> payer -> token on the model route; the GitHub writer   checkpoint 4
    sandbox    one pod per PR, one Codex process per reviewer run                   checkpoint 5

Roles may import `types`, `core`, `ports` and `store`. Nothing below them imports a role.
"""

from __future__ import annotations

from dataclasses import dataclass

from doppel_runtime.store import Ledger


@dataclass
class Role:
    """A role's lifecycle in the process. Stubs until their checkpoint gives them work."""

    name: str
    ledger: Ledger
    running: bool = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False


ROLE_NAMES = ("intake", "scheduler", "broker", "gateway", "sandbox")


def make_roles(ledger: Ledger) -> dict[str, Role]:
    # The scheduler's logic (roles/scheduler.py) is wired in once real ports exist (checkpoints 5, 6).
    return {name: Role(name, ledger) for name in ROLE_NAMES}
