"""The four roles' lifecycle in the one runtime process, and where each one's code lives.

broker     each seat's sign-in and refresh                                  model-provider
gateway    the model route: the only thing pods reach                       gateway
sandbox    one pod per PR; the harness inside it                            control-plane/pods.py, sandbox-runtime
bff        check-ins with Doppelwerk: seats out, control and jobs in        control-plane/bff.py, jobs.py

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

ROLE_NAMES = ("broker", "gateway", "sandbox", "bff")


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
