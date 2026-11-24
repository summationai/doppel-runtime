"""The runtime process: open the ledger, recover from a crash, start the roles and three listeners.

Listeners are separate sockets so the network can keep pods away from all but the model route
(DSV-8): pods reach `model`; `internal` (posting, broker, API) and `health` stay off the pod network.

The broker, the gateway and the BFF check-in always run (the check-in waits quietly until this runner
is paired). Intake, the scheduler and the sandbox run once GitHub is configured (`github.mode =
"app"`: the App's REST ports), or when a caller hands in the GitHub ports (tests, the local demo).
Webhooks are taken on the internal listener when a secret is configured.
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
from doppel_shared.ports import GitHub, GitHubReader, Sandbox

from doppel_control_plane import __version__
from doppel_control_plane.bff import BffLink, BffState
from doppel_control_plane.bff import add_routes as bff_routes
from doppel_control_plane.config import LISTENERS, Config, read_secret
from doppel_control_plane.core.matching import Limits
from doppel_control_plane.directory import ConfigDirectory
from doppel_control_plane.intake import Budget, Poller, Watched
from doppel_control_plane.jobs import JobRunner
from doppel_control_plane.roles import ROLE_NAMES, Role
from doppel_control_plane.runs import LedgerRuns
from doppel_control_plane.scheduler import Scheduler
from doppel_control_plane.store import Ledger
from doppel_control_plane.webhooks import PATH as WEBHOOK_PATH
from doppel_control_plane.webhooks import Webhooks

log = logging.getLogger("doppel_control_plane")


def utcnow() -> datetime:
    return datetime.now(UTC)


class Runtime:
    def __init__(
        self,
        config: Config,
        *,
        github: GitHub | None = None,
        reader: GitHubReader | None = None,
        sandbox: Sandbox | None = None,
        job_sandbox: Sandbox | None = None,
        clock=utcnow,
    ):
        self.config = config
        self.ledger: Ledger | None = None
        self.roles: dict[str, Role] = {}
        self._runners: list[web.AppRunner] = []
        self.bound: dict[str, tuple[str, int]] = {}  # listener -> (host, port) actually bound
        self._model_bridge: str | None = None
        self.gateway: ModelRoute | None = None
        self.broker: Broker | None = None
        self.bff: BffLink | None = None
        self.github, self.reader, self.sandbox = github, reader, sandbox
        self.scheduler: Scheduler | None = None
        self.poller: Poller | None = None
        self.clock = clock
        self.webhooks: Webhooks | None = None
        self._git_env = None  # repo -> git environment carrying the App's token (app mode)
        self.jobs: JobRunner | None = None
        self.job_sandbox = job_sandbox  # tests hand one in; otherwise Docker pods, made at the first job
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
        if listener == "internal" and self.bff is not None:  # seats, added from the runner's CLI
            bff_routes(app, self.bff)
        if listener == "internal":  # GitHub's events, forwarded by the BFF; 404 until webhooks are configured

            async def webhook(request: web.Request) -> web.StreamResponse:
                if self.webhooks is None:
                    raise web.HTTPNotFound()
                return await self.webhooks.handle(request)

            app.router.add_post(WEBHOOK_PATH, webhook)
        return app

    async def start(self) -> None:
        if self.config.github.mode == "app" and (self.github is None or self.reader is None):
            self._github_app()
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(self.config.ledger_path)
        requeued = self.ledger.requeue_running()
        if requeued:
            log.info("requeued %d slot(s) left running by the last process", requeued)
        homes = CodexHomes(self.config.tenants_dir, self.config.codex_homes)
        self.broker = Broker(homes, CodexSdkAccount())
        self._tasks.append(asyncio.create_task(self.broker.keep_fresh()))
        self.gateway = ModelRoute(LedgerRuns(self.ledger), self.broker)
        self.jobs = JobRunner(self.ledger, self._job_pods if self.job_sandbox is None else lambda: self.job_sandbox)
        dropped = self.jobs.recover()
        if dropped:
            log.info("dropped %d BFF job(s) left running by the last process; Doppelwerk retries them", dropped)
        self.bff = BffLink(BffState(self.config.bff_dir), self.broker, host_cap=self.config.scheduler.host_cap,
                           version=__version__, jobs=self.jobs)  # fmt: skip
        self.roles = {name: Role(name) for name in ROLE_NAMES}
        self.roles["bff"] = Role("bff", self._check_in, self.config.bff.check_in.total_seconds())
        await self._listen()  # first: the sandbox needs the model listener's port
        if self.github is not None or self.config.github.mode != "off":
            self._wire_reviews()
        for role in self.roles.values():
            if role.name in ("broker", "gateway", "bff") or self.scheduler is not None:
                await role.start()

    async def _check_in(self) -> None:
        await self.bff.check_in(self.clock().isoformat())
        if self.bff.every:
            self.roles["bff"].every = self.bff.every

    def _github_app(self) -> None:
        """The App's REST ports: one client, its installation tokens shared by reads, posts and fetches."""
        from doppel_gateway.github import AppAuth, GitHubClient, RestGitHub, RestReader

        gh = self.config.github
        auth = AppAuth(gh.app_id, read_secret(gh.private_key), gh.api_url)
        installations = {repo: t.installation_id for t in self.config.tenants for repo in t.repos}
        client = GitHubClient(auth, installations)
        self.github, self.reader, self._git_env = RestGitHub(client), RestReader(client), client.git_env
        log.info("github: App %s, %d repo(s)", gh.app_id, len(installations))

    def _wire_reviews(self) -> None:
        """Intake, the scheduler and the sandbox: GitHub's state in, reviews and the merger's decisions out."""
        cfg = self.config
        if self.sandbox is None:
            self.sandbox = self._docker_pods()
        directory = ConfigDirectory(cfg.tenants, lambda payer: self.broker.status(payer)["state"] == "connected")
        limits = Limits(settle=cfg.scheduler.settle, payer_cap=cfg.scheduler.payer_cap, host_cap=cfg.scheduler.host_cap)
        self.scheduler = Scheduler(self.ledger, directory, self.sandbox, self.github, limits)
        repos = [Watched(t.id, repo) for t in cfg.tenants for repo in t.repos]
        self.poller = Poller(self.ledger, self.scheduler, self.reader, directory, repos,
                             Budget(cfg.intake.calls_per_hour), lookback=cfg.intake.lookback,
                             auto_review=cfg.intake.auto_review)  # fmt: skip
        if cfg.github.webhook_secret is not None:
            self.webhooks = Webhooks(read_secret(cfg.github.webhook_secret).encode(), self.poller, self.clock)

        async def poll() -> None:
            await self.poller.poll(self.clock())

        async def tick() -> None:
            self.scheduler.tick(self.clock())

        self.roles["intake"] = Role("intake", poll, cfg.intake.poll.total_seconds())
        self.roles["scheduler"] = Role("scheduler", tick, cfg.scheduler.tick.total_seconds())
        log.info("reviews on: %d repo(s) polled every %ss", len(repos), cfg.intake.poll.total_seconds())

    def _job_pods(self) -> Sandbox:
        """Pods for the BFF's jobs: the same sandbox, its verdicts and failures going to the job runner."""
        from doppel_control_plane.mirrors import Mirrors
        from doppel_control_plane.pods import DockerPods

        cfg = self.config
        owner = hashlib.sha256(f"jobs:{cfg.data_dir.resolve()}".encode()).hexdigest()[:12]
        pods = DockerPods(
            cfg.data_dir / "pods", Mirrors(cfg.data_dir / "mirrors", git_env=self.jobs.git_env),
            image=cfg.sandbox.image,
            gateway_target=self._gateway_target(),
            repo_url=lambda pr: cfg.github.repo_url.format(repo=pr.repo),
            on_verdict=self.jobs.verdict, on_failed=self.jobs.failed,
            runtime=cfg.sandbox.runtime, memory=cfg.sandbox.memory, cpus=cfg.sandbox.cpus, owner=owner,
        )  # fmt: skip
        pods.sweep()
        return pods

    def _docker_pods(self) -> Sandbox:
        from doppel_control_plane.mirrors import Mirrors
        from doppel_control_plane.pods import DockerPods

        cfg = self.config
        # Pods from a process that died are orphans: this process can't drive them. Start clean.
        self.ledger.execute("UPDATE prs SET pod_id=NULL WHERE pod_id IS NOT NULL")
        owner = hashlib.sha256(str(cfg.data_dir.resolve()).encode()).hexdigest()[:12]
        pods = DockerPods(
            cfg.data_dir / "pods", Mirrors(cfg.data_dir / "mirrors", git_env=self._git_env), image=cfg.sandbox.image,
            gateway_target=self._gateway_target(),
            repo_url=lambda pr: cfg.github.repo_url.format(repo=pr.repo),
            on_verdict=lambda slot, v: self.scheduler.verdict(slot, v, self.clock()),
            on_failed=lambda slot, why: self.scheduler.slot_failed(slot, why, self.clock()),
            runtime=cfg.sandbox.runtime, memory=cfg.sandbox.memory, cpus=cfg.sandbox.cpus, owner=owner,
        )  # fmt: skip
        pods.sweep()
        return pods

    def _gateway_target(self) -> str:
        host = self._model_bridge or "host.docker.internal"
        return self.config.sandbox.gateway_target or f"{host}:{self.bound['model'][1]}"

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
        close = getattr(self.sandbox, "close", None)
        if close is not None and self.scheduler is not None:
            # The pods go; the ledger keeps their slots, which go back to queued at the next start.
            self.ledger.execute("UPDATE prs SET pod_id=NULL WHERE pod_id IS NOT NULL")
            await asyncio.to_thread(close)
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
