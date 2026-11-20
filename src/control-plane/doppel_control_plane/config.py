"""Runtime configuration: a TOML file, then `DOPPEL_*` environment variables on top.

No secret belongs in the file. Model sign-ins live in each payer's Codex home under `data_dir`.

    data_dir = "/var/lib/doppel"          # the ledger and per-user Codex homes live under it

    [listeners.model]                     # the gateway's model route: the only thing pods reach
    host = "10.0.0.1"
    port = 8700

    [listeners.internal]                  # posting, broker, the runtime's API: never reachable from pods
    host = "127.0.0.1"
    port = 8701

    [listeners.health]
    host = "127.0.0.1"
    port = 8702

    [broker.homes]                        # a payer's existing Codex home, instead of the default
    "summation/u-steven" = "/Users/steven/.codex"

    [github]
    mode = "off"                          # off: no intake or scheduler | app: the GitHub App
    repo_url = "https://github.com/{repo}.git"
    app_id = "123456"                     # app mode: the App's id ...
    private_key = "/var/lib/doppel/github/app.pem"       # ... and its private key (owner-only file)
    webhook_secret = "/var/lib/doppel/github/webhook-secret"   # optional: a file; webhooks off without it
    api_url = "https://api.github.com"

    [intake]
    poll_seconds = 60
    calls_per_hour = 2000                 # per installation; GitHub allows an App 5000, posting needs the rest
    lookback_minutes = 60                 # how far back the first poll reads comments
    auto_review = "insiders"              # insiders | everyone: whose PRs get the app's reviewers unasked

    [scheduler]
    tick_seconds = 5
    settle_seconds = 90
    payer_cap = 2
    host_cap = 10

    [sandbox]
    image = "doppel-sandbox:dev"
    runtime = "runsc"                     # gVisor on summini; leave out for Docker's default
    memory = "2g"
    cpus = "1"
    gateway_target = ""                   # where pods' model calls go; default host.docker.internal:<model port>

    # The directory: until the BFF's records arrive (checkpoint 8), tenants, repos and reviewers live here.
    [[tenants]]
    id = "summation"
    repos = ["summationai/billing"]
    installation_id = 7890123             # app mode: the App's installation on these repos' owner

      [[tenants.reviewers]]
      handle = "steven"                   # a person's doppel: runs on their sign-in, where they are requested
      kind = "personal"
      owner = "summation/u-steven"
      login = "stevenliu"

      [[tenants.reviewers]]
      handle = "repo-owner"               # the repo's own reviewer: on every PR, required by the merger
      kind = "app"
      payer = "summation/u-steven"        # who opted in to pay for it
      repos = ["summationai/billing"]     # optional: every repo of the tenant when left out

Sign-ins live at `<data_dir>/tenants/<tenant>/<user>/codex/auth.json` (owner-only).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from doppel_shared.types import Reviewer, ReviewerKind

LISTENERS = ("model", "internal", "health")
DEFAULT_PORTS = {"model": 8700, "internal": 8701, "health": 8702}


@dataclass(frozen=True)
class Listener:
    host: str
    port: int


@dataclass(frozen=True)
class TenantReviewer:
    reviewer: Reviewer
    payer: str | None = None  # an app reviewer's payer
    repos: tuple[str, ...] = ()  # empty: every repo of the tenant


@dataclass(frozen=True)
class Tenant:
    id: str
    repos: tuple[str, ...] = ()
    reviewers: tuple[TenantReviewer, ...] = ()
    installation_id: int | None = None


@dataclass(frozen=True)
class GitHubConfig:
    mode: str = "off"  # off | app
    repo_url: str = "https://github.com/{repo}.git"
    app_id: str | None = None
    private_key: Path | None = None
    webhook_secret: Path | None = None
    api_url: str = "https://api.github.com"


@dataclass(frozen=True)
class IntakeConfig:
    poll: timedelta = timedelta(seconds=60)
    calls_per_hour: int = 2000
    lookback: timedelta = timedelta(minutes=60)
    auto_review: str = "insiders"


@dataclass(frozen=True)
class SchedulerConfig:
    tick: timedelta = timedelta(seconds=5)
    settle: timedelta = timedelta(seconds=90)
    payer_cap: int = 2
    host_cap: int = 10


@dataclass(frozen=True)
class SandboxConfig:
    image: str = "doppel-sandbox:dev"
    runtime: str | None = None
    memory: str = "2g"
    cpus: str = "1"
    gateway_target: str | None = None


@dataclass(frozen=True)
class Config:
    data_dir: Path
    listeners: dict[str, Listener] = field(default_factory=dict)
    codex_homes: dict[str, Path] = field(default_factory=dict)
    github: GitHubConfig = GitHubConfig()
    intake: IntakeConfig = IntakeConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    sandbox: SandboxConfig = SandboxConfig()
    tenants: tuple[Tenant, ...] = ()

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def tenants_dir(self) -> Path:
        return self.data_dir / "tenants"


def load(path: Path | None = None, env: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    raw: dict = {}
    if path is not None:
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    data_dir = Path(env.get("DOPPEL_DATA_DIR") or raw.get("data_dir") or "./var")
    listeners = {}
    for name in LISTENERS:
        section = raw.get("listeners", {}).get(name, {})
        host = env.get(f"DOPPEL_{name.upper()}_HOST") or section.get("host") or "127.0.0.1"
        port = env.get(f"DOPPEL_{name.upper()}_PORT")
        listeners[name] = Listener(
            host=host, port=int(port if port is not None else section.get("port", DEFAULT_PORTS[name]))
        )
    homes = {payer: Path(p).expanduser() for payer, p in raw.get("broker", {}).get("homes", {}).items()}
    gh, it, sc, sb = (raw.get(k, {}) for k in ("github", "intake", "scheduler", "sandbox"))
    mode = env.get("DOPPEL_GITHUB_MODE") or gh.get("mode", "off")
    if mode not in ("off", "app"):
        raise ValueError(f"github.mode must be off or app, not {mode!r}")
    github = GitHubConfig(
        mode,
        gh.get("repo_url", GitHubConfig.repo_url),
        str(gh["app_id"]) if gh.get("app_id") else None,
        Path(gh["private_key"]).expanduser() if gh.get("private_key") else None,
        Path(gh["webhook_secret"]).expanduser() if gh.get("webhook_secret") else None,
        gh.get("api_url", GitHubConfig.api_url),
    )
    tenants = tuple(_tenant(t) for t in raw.get("tenants", []))
    if mode == "app":
        if not (github.app_id and github.private_key):
            raise ValueError("github.mode = app needs github.app_id and github.private_key")
        missing = [t.id for t in tenants if t.repos and t.installation_id is None]
        if missing:
            raise ValueError(f"github.mode = app needs an installation_id for tenant(s) {', '.join(missing)}")
    return Config(
        data_dir=data_dir,
        listeners=listeners,
        codex_homes=homes,
        github=github,
        intake=IntakeConfig(
            timedelta(seconds=it.get("poll_seconds", 60)),
            int(it.get("calls_per_hour", 2000)),
            timedelta(minutes=it.get("lookback_minutes", 60)),
            it.get("auto_review", "insiders"),
        ),
        scheduler=SchedulerConfig(
            timedelta(seconds=sc.get("tick_seconds", 5)),
            timedelta(seconds=sc.get("settle_seconds", 90)),
            int(sc.get("payer_cap", 2)),
            int(sc.get("host_cap", 10)),
        ),
        sandbox=SandboxConfig(
            sb.get("image", SandboxConfig.image),
            sb.get("runtime") or None,
            sb.get("memory", SandboxConfig.memory),
            str(sb.get("cpus", SandboxConfig.cpus)),
            sb.get("gateway_target") or None,
        ),
        tenants=tenants,
    )


def read_secret(path: Path) -> str:
    """A secret file the runtime holds: refused unless only its owner can read it."""
    mode = path.stat().st_mode
    if mode & 0o077:
        raise PermissionError(f"{path} is readable by others (mode {oct(mode & 0o777)}); chmod 600 it")
    return path.read_text().strip()


def _tenant(raw: dict) -> Tenant:
    tid = raw["id"]
    reviewers = []
    for r in raw.get("reviewers", []):
        kind = ReviewerKind(r["kind"])
        if kind is ReviewerKind.PERSONAL and not (r.get("owner") and r.get("login")):
            raise ValueError(f"tenant {tid}: personal reviewer {r['handle']} needs an owner and a login")
        for payer in (r.get("owner"), r.get("payer")):
            if payer and not payer.startswith(f"{tid}/"):
                raise ValueError(f"tenant {tid}: reviewer {r['handle']}'s payer {payer} is another tenant's")
        reviewer = Reviewer(r["handle"], kind, owner=r.get("owner"), login=r.get("login"))
        reviewers.append(TenantReviewer(reviewer, r.get("payer"), tuple(r.get("repos", ()))))
    handles = [t.reviewer.handle for t in reviewers]
    if len(handles) != len(set(handles)):
        raise ValueError(f"tenant {tid}: reviewer handles must be unique")
    inst = raw.get("installation_id")
    return Tenant(tid, tuple(raw.get("repos", ())), tuple(reviewers), int(inst) if inst is not None else None)
