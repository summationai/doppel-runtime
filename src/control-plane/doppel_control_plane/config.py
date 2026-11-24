"""Runtime configuration: a TOML file, then `DOPPEL_*` environment variables on top.

No secret belongs in the file. Model sign-ins live in each payer's Codex home under `data_dir`; this runner's own
credential for Doppelwerk lives under `data_dir/bff/`. Doppelwerk holds everything else: workspaces, doppels and their
rules, repositories, the GitHub App.

    data_dir = "/var/lib/doppel"          # the ledger, the pairing, per-member Codex homes, mirrors and pods

    [listeners.model]                     # the gateway's model route: the only thing pods reach
    host = "10.0.0.1"
    port = 8700

    [listeners.internal]                  # the broker and seats, for this machine's CLI: never reachable from pods
    host = "127.0.0.1"
    port = 8701

    [listeners.health]
    host = "127.0.0.1"
    port = 8702

    [broker.homes]                        # a payer's existing Codex home, instead of the default
    "<workspace id>/<member id>" = "/Users/steven/.codex"

    [bff]                                 # Doppelwerk: this runner pairs with it (`doppel-runtime pair`)
    url = "https://doppelwerk.ac0.app"    # the default for `pair --bff`; once paired, the pairing's own address counts
    check_in_seconds = 30                 # until Doppelwerk says otherwise

    [scheduler]
    host_cap = 10                         # reviews this runner runs at once; Doppelwerk may set it lower

    [github]
    repo_url = "https://github.com/{repo}.git"   # where mirrors fetch from, with each job's one-repo read token

    [sandbox]
    image = "doppel-sandbox:dev"
    runtime = "runsc"                     # gVisor where installed; leave out for Docker's default
    memory = "2g"
    cpus = "1"
    gateway_target = ""                   # where pods' model calls go; default host.docker.internal:<model port>

Sign-ins live at `<data_dir>/tenants/<workspace id>/<member id>/codex/auth.json` (owner-only).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

LISTENERS = ("model", "internal", "health")
DEFAULT_PORTS = {"model": 8700, "internal": 8701, "health": 8702}
REPO_URL = "https://github.com/{repo}.git"


@dataclass(frozen=True)
class Listener:
    host: str
    port: int


@dataclass(frozen=True)
class BffConfig:
    url: str | None = None
    check_in: timedelta = timedelta(seconds=30)


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
    bff: BffConfig = BffConfig()
    sandbox: SandboxConfig = SandboxConfig()
    host_cap: int = 10
    repo_url: str = REPO_URL

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def tenants_dir(self) -> Path:
        return self.data_dir / "tenants"

    @property
    def bff_dir(self) -> Path:
        return self.data_dir / "bff"


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
    gh, sc, sb, bf = (raw.get(k, {}) for k in ("github", "scheduler", "sandbox", "bff"))
    return Config(
        data_dir=data_dir,
        listeners=listeners,
        codex_homes=homes,
        bff=BffConfig(
            env.get("DOPPEL_BFF_URL") or bf.get("url") or None,
            timedelta(seconds=bf.get("check_in_seconds", 30)),
        ),
        sandbox=SandboxConfig(
            sb.get("image", SandboxConfig.image),
            sb.get("runtime") or None,
            sb.get("memory", SandboxConfig.memory),
            str(sb.get("cpus", SandboxConfig.cpus)),
            sb.get("gateway_target") or None,
        ),
        host_cap=int(sc.get("host_cap", 10)),
        repo_url=gh.get("repo_url", REPO_URL),
    )
