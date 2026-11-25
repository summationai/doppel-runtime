"""The runtime process, a runner for Doppelwerk: open the ledger, recover from a crash, start the roles and three
listeners (docs/specs/bff-split.md). Doppelwerk decides what to review and who pays, posts, and approves; this process
checks in, runs the jobs it's given in sandbox pods on its seats' own sign-ins, and returns the verdicts.

Listeners are separate sockets so the network can keep pods away from all but the model route
(DSV-8): pods reach `model`; `internal` (the broker, seats, status) and `health` stay off the pod network.

Every role always runs. The check-in waits quietly until this runner is paired; a paired runner prepares its sandbox at
start, which sweeps away pods a dead process left.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import signal
import sys
from datetime import UTC, datetime

from aiohttp import web
from doppel_gateway import MODEL_MAX_BYTES, ModelRoute
from doppel_model_provider import Broker, CodexHomes, CodexSdkAccount
from doppel_model_provider import api as broker_api
from doppel_shared.ports import Sandbox

from doppel_control_plane import __version__
from doppel_control_plane.bff import BffLink, BffState
from doppel_control_plane.bff import add_routes as bff_routes
from doppel_control_plane.config import LISTENERS, Config
from doppel_control_plane.jobs import JobRunner
from doppel_control_plane.pods import detect_runtime, isolation
from doppel_control_plane.roles import ROLE_NAMES, Role
from doppel_control_plane.runs import LedgerRuns
from doppel_control_plane.store import Ledger

log = logging.getLogger("doppel_control_plane")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Runtime:
    def __init__(self, config: Config, *, job_sandbox: Sandbox | None = None, clock=utcnow):
        self.config = config
        self.ledger: Ledger | None = None
        self.roles: dict[str, Role] = {}
        self._runners: list[web.AppRunner] = []
        self.bound: dict[str, tuple[str, int]] = {}  # listener -> (host, port) actually bound
        self._model_bridge: str | None = None
        self.gateway: ModelRoute | None = None
        self.pod_runtime: str | None = None  # the pods' OCI runtime, settled at start (detect_runtime)
        self.broker: Broker | None = None
        self.bff: BffLink | None = None
        self.jobs: JobRunner | None = None
        self.job_sandbox = job_sandbox  # tests hand one in; otherwise Docker pods
        self.clock = clock
        self._tasks: list[asyncio.Task] = []

    def _app(self, listener: str) -> web.Application:
        # One model request carries the whole context; nothing else needs more than the default.
        app = web.Application(client_max_size=MODEL_MAX_BYTES if listener == "model" else 1024**2)

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
        if listener == "model" and self.gateway is not None:  # pods reach this listener, and only this one
            self.gateway.add_routes(app)
        if listener == "internal" and self.broker is not None:  # sign-ins: never on the model listener
            broker_api.add_routes(app, self.broker)
        if listener == "internal" and self.bff is not None:  # seats, added from this runner's CLI
            bff_routes(app, self.bff)
        return app

    async def start(self) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.config.ledger_path)
        homes = CodexHomes(self.config.tenants_dir, self.config.codex_homes)
        self.broker = Broker(homes, CodexSdkAccount())
        self._tasks.append(asyncio.create_task(self.broker.keep_fresh()))
        self.gateway = ModelRoute(LedgerRuns(self.ledger), self.broker)
        self.jobs = JobRunner(self.ledger, self._job_pods if self.job_sandbox is None else lambda: self.job_sandbox)
        dropped = self.jobs.recover()
        if dropped:
            log.info("dropped %d job(s) left running by the last process; Doppelwerk retries them", dropped)
        state = BffState(self.config.bff_dir)
        self.bff = BffLink(state, self.broker, host_cap=self.config.host_cap, version=__version__, jobs=self.jobs)
        self.roles = {name: Role(name) for name in ROLE_NAMES}
        self.roles["bff"] = Role("bff", self._check_in, self.config.bff.check_in.total_seconds())
        self.pod_runtime = await asyncio.to_thread(detect_runtime, self.config.sandbox.runtime)
        self.bff.isolation = isolation(self.pod_runtime)
        log.info("pods run %s", "under gVisor" if self.pod_runtime == "runsc" else "with Docker's standard runtime")
        await self._listen()  # first: the sandbox needs the model listener's port
        if state.identity() is not None:
            try:
                _ = self.jobs.sandbox  # made now, so a restart sweeps what a dead process left
            except Exception as e:
                log.error("the sandbox isn't ready (%s): jobs will fail until it is", type(e).__name__)
        for role in self.roles.values():
            await role.start()

    async def _check_in(self) -> None:
        await self.bff.check_in(self.clock().isoformat())
        if self.bff.every:
            self.roles["bff"].every = self.bff.every

    def _job_pods(self) -> Sandbox:
        """Docker pods for the jobs: verdicts and failures go to the job runner, fetches carry each job's token."""
        from doppel_control_plane.mirrors import Mirrors
        from doppel_control_plane.pods import DockerPods

        cfg = self.config
        # Which pods are this runner's, for `sweep`: its data folder, and in a container its volume (every containerized
        # runner's folder is the same path, so two on one Docker host would otherwise sweep each other's pods).
        where = f"{cfg.data_dir.resolve()}" + (f"@{cfg.sandbox.data_volume}" if cfg.sandbox.data_volume else "")
        owner = hashlib.sha256(f"jobs:{where}".encode()).hexdigest()[:12]
        gateway_host = self._model_bridge or "host.docker.internal"
        pods = DockerPods(
            cfg.data_dir / "pods", Mirrors(cfg.data_dir / "mirrors", git_env=self.jobs.git_env),
            image=cfg.sandbox.image,
            gateway_target=cfg.sandbox.gateway_target or f"{gateway_host}:{self.bound['model'][1]}",
            repo_url=lambda pr: cfg.repo_url.format(repo=pr.repo),
            on_verdict=self.jobs.verdict, on_failed=self.jobs.failed,
            runtime=self.pod_runtime, memory=cfg.sandbox.memory, cpus=cfg.sandbox.cpus, owner=owner,
            data_volume=cfg.sandbox.data_volume, data_root=cfg.data_dir, network=cfg.sandbox.network,
        )  # fmt: skip
        pods.sweep()
        return pods

    async def _listen(self) -> None:
        for name in LISTENERS:
            listener = self.config.listeners[name]
            runner = web.AppRunner(self._app(name), access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, listener.host or "127.0.0.1", listener.port)
            await site.start()
            host, port = site._server.sockets[0].getsockname()[:2]
            self.bound[name] = (host, port)
            self._runners.append(runner)
            log.info("listener %s on %s:%d", name, host, port)
            if name == "model" and listener.host is None and sys.platform == "linux":
                await self._listen_on_docker_bridge(runner, port)

    async def _listen_on_docker_bridge(self, runner: web.AppRunner, port: int) -> None:
        from doppel_control_plane.pods import docker_bridge_gateway

        bridge = await asyncio.to_thread(docker_bridge_gateway)
        if bridge is None:
            return  # A broker-only process can run without Docker.
        try:
            await web.TCPSite(runner, bridge, port).start()
        except OSError as exc:
            log.warning("could not bind model listener on Docker bridge %s:%d: %s", bridge, port, exc)
        else:
            self._model_bridge = bridge
            log.info("listener model on Docker bridge %s:%d", bridge, port)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        for role in self.roles.values():
            await role.stop()
        for runner in reversed(self._runners):
            await runner.cleanup()
        self._runners.clear()
        if self.jobs is not None:
            await asyncio.to_thread(self.jobs.close)
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
