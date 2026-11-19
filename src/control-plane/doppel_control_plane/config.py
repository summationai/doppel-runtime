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

Sign-ins live at `<data_dir>/tenants/<tenant>/<user>/codex/auth.json` (owner-only).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

LISTENERS = ("model", "internal", "health")
DEFAULT_PORTS = {"model": 8700, "internal": 8701, "health": 8702}


@dataclass(frozen=True)
class Listener:
    host: str
    port: int


@dataclass(frozen=True)
class Config:
    data_dir: Path
    listeners: dict[str, Listener] = field(default_factory=dict)
    codex_homes: dict[str, Path] = field(default_factory=dict)

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
    return Config(data_dir=data_dir, listeners=listeners, codex_homes=homes)
