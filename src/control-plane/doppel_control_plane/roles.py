"""The five roles' lifecycle in the one runtime process, and where each one's code lives.

intake     GitHub events in (webhooks via the BFF, plus a reconcile poll)   control-plane, checkpoint 6
scheduler  queues slots, starts ready ones in their PR's pod, the merger    control-plane/scheduler.py
broker     each payer's sign-in, refresh, OpenBao copy                      model-provider, checkpoint 3
gateway    the model route; later the only GitHub writer                    gateway
sandbox    one pod per PR; the harness inside it                            sandbox-runtime, checkpoint 5
"""

from __future__ import annotations

from dataclasses import dataclass

from doppel_control_plane.store import Ledger


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
