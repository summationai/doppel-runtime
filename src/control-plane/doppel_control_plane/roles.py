"""The six roles' lifecycle in the one runtime process, and where each one's code lives.

intake     GitHub's state in: the poll (webhooks via the BFF later)        control-plane/intake.py
scheduler  queues slots, starts ready ones in their PR's pod, the merger    control-plane/scheduler.py
broker     each payer's sign-in and refresh                                 model-provider
gateway    the model route; later the only GitHub writer                    gateway
sandbox    one pod per PR; the harness inside it                            control-plane/pods.py, sandbox-runtime
bff        this Mac's check-in with Doppelwerk: seats out, control in        control-plane/bff.py

A role with a `round` runs it every `every` seconds until stopped. A round that fails is logged and
the next one runs on time: one bad cycle never stops the role.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

ROLE_NAMES = ("intake", "scheduler", "broker", "gateway", "sandbox", "bff")


@dataclass
class Role:
    name: str
    round: Callable[[], Awaitable[None]] | None = None
    every: float = 0.0
    running: bool = False
    _task: asyncio.Task | None = field(default=None, repr=False)

    async def start(self) -> None:
        self.running = True
        if self.round is not None:
            self._task = asyncio.create_task(self._forever(), name=f"role:{self.name}")

    async def _forever(self) -> None:
        while True:
            try:
                await self.round()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s: a round failed", self.name)
            await asyncio.sleep(self.every)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.running = False
