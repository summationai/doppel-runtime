"""Runtime configuration: a TOML file, then `DOPPEL_*` environment variables on top.

No secret belongs in the file. Model sign-ins live in each payer's Codex home under `data_dir`; this runner's own
credential for Doppelwerk lives under `data_dir/bff/`. Doppelwerk holds everything else: workspaces, doppels and their
rules, repositories, the GitHub App.

    data_dir = "/var/lib/doppel"          # the ledger, the pairing, per-member Codex homes, mirrors and pods

    [listeners.model]                     # defaults to loopback plus the local Docker bridge on Linux
    # host = "10.0.0.1"                   # override the automatic bind for a custom network
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
                                                 # (DOPPEL_REPO_URL overrides it: fixture repos in tests)

    [sandbox]
    image = "doppel-sandbox:dev"
    runtime = "runsc"                     # gVisor; "auto": gVisor where Docker has it; leave out for Docker's default
    memory = "2g"
    cpus = "1"
    gateway_target = ""                   # optional override for the model address as seen by Docker
    data_volume = ""                      # the runner runs in a container with data_dir on this Docker volume:
                                          # pods mount subpaths of it, not host folders (docs/specs/packaging.md)
    network = ""                          # the Docker network the runner is on: the egress forwarder joins it

Every [sandbox] key can be set as `DOPPEL_SANDBOX_<KEY>` (e.g. DOPPEL_SANDBOX_DATA_VOLUME); the runner image sets them.

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
    host: str | None
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
    data_volume: str | None = None
    network: str | None = None


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
        host = env.get(f"DOPPEL_{name.upper()}_HOST") or section.get("host")
        port = env.get(f"DOPPEL_{name.upper()}_PORT")
        listeners[name] = Listener(
            host=host if name == "model" else host or "127.0.0.1",
            port=int(port if port is not None else section.get("port", DEFAULT_PORTS[name])),
        )
    homes = {payer: Path(p).expanduser() for payer, p in raw.get("broker", {}).get("homes", {}).items()}
    gh, sc, sb, bf = (raw.get(k, {}) for k in ("github", "scheduler", "sandbox", "bff"))

    def sandbox(key: str) -> str | None:
        value = env.get(f"DOPPEL_SANDBOX_{key.upper()}")
        value = value if value is not None else sb.get(key)
        return str(value) if value not in (None, "") else None

    return Config(
        data_dir=data_dir,
        listeners=listeners,
        codex_homes=homes,
        bff=BffConfig(
            env.get("DOPPEL_BFF_URL") or bf.get("url") or None,
            timedelta(seconds=bf.get("check_in_seconds", 30)),
        ),
        sandbox=SandboxConfig(
            sandbox("image") or SandboxConfig.image,
            sandbox("runtime"),
            sandbox("memory") or SandboxConfig.memory,
            str(sandbox("cpus") or SandboxConfig.cpus),
            sandbox("gateway_target"),
            sandbox("data_volume"),
            sandbox("network"),
        ),
        host_cap=int(sc.get("host_cap", 10)),
        repo_url=env.get("DOPPEL_REPO_URL") or gh.get("repo_url", REPO_URL),
    )
