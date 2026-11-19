"""The runtime process: open the ledger, recover from a crash, start the roles and three listeners.

Listeners are separate sockets so the network can keep pods away from all but the model route
(DSV-8): pods reach `model`; `internal` (posting, broker, API) and `health` stay off the pod network.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web

from doppel_runtime import __version__
from doppel_runtime.config import LISTENERS, Config
from doppel_runtime.roles import Role, make_roles
from doppel_runtime.store import Ledger

log = logging.getLogger("doppel_runtime")


class Runtime:
    def __init__(self, config: Config):
        self.config = config
        self.ledger: Ledger | None = None
        self.roles: dict[str, Role] = {}
        self._runners: list[web.AppRunner] = []
        self.bound: dict[str, tuple[str, int]] = {}  # listener -> (host, port) actually bound

    def _app(self, listener: str) -> web.Application:
        app = web.Application()

        async def healthz(_: web.Request) -> web.Response:
            return web.json_response(
                {
                    "ok": True,
                    "listener": listener,
                    "version": __version__,
                    "roles": {name: role.running for name, role in self.roles.items()},
                }
            )

        app.router.add_get("/healthz", healthz)
        return app

    async def start(self) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.config.ledger_path)
        requeued = self.ledger.requeue_running()
        if requeued:
            log.info("requeued %d slot(s) left running by the last process", requeued)
        self.roles = make_roles(self.ledger)
        for role in self.roles.values():
            await role.start()
        for name in LISTENERS:
            listener = self.config.listeners[name]
            runner = web.AppRunner(self._app(name), access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, listener.host, listener.port)
            await site.start()
            host, port = site._server.sockets[0].getsockname()[:2]
            self.bound[name] = (host, port)
            self._runners.append(runner)
            log.info("listener %s on %s:%d", name, host, port)

    async def stop(self) -> None:
        for runner in reversed(self._runners):
            await runner.cleanup()
        self._runners.clear()
        for role in self.roles.values():
            await role.stop()
        if self.ledger is not None:
            self.ledger.close()


async def serve(config: Config) -> None:
    runtime = Runtime(config)
    await runtime.start()
    print("ready " + " ".join(f"{n}={h}:{p}" for n, (h, p) in runtime.bound.items()), flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        await runtime.stop()
