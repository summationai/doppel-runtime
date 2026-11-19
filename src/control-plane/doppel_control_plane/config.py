"""Runtime configuration: a TOML file, then `DOPPEL_*` environment variables on top.

No secret belongs in the file. Secrets live in OpenBao and reach the runtime through the broker.

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

    [broker.homes]                        # a payer's Codex home, when not <data_dir>/homes/<payer>
    "u-steven" = "/Users/steven/.codex"

    [broker.openbao]                      # the durable copy of every sign-in; omit to keep them on disk only
    addr = "http://127.0.0.1:8200"
    mount = "kv"
    token_role = "doppel-broker"          # optional: mint per-payer tokens through this role

The OpenBao token is never in the file: `DOPPEL_OPENBAO_TOKEN`. `DOPPEL_OPENBAO_ADDR` overrides `addr`.

Environment overrides: `DOPPEL_DATA_DIR`, and `DOPPEL_<LISTENER>_HOST` / `DOPPEL_<LISTENER>_PORT`
(e.g. `DOPPEL_MODEL_PORT=0` for an ephemeral port in tests).
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
class OpenBao:
    addr: str
    token: str = field(repr=False)
    mount: str = "kv"
    token_role: str | None = None


@dataclass(frozen=True)
class Config:
    data_dir: Path
    listeners: dict[str, Listener] = field(default_factory=dict)
    codex_homes: dict[str, Path] = field(default_factory=dict)
    openbao: OpenBao | None = None

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def homes_dir(self) -> Path:
        return self.data_dir / "homes"


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
    broker = raw.get("broker", {})
    homes = {payer: Path(p).expanduser() for payer, p in broker.get("homes", {}).items()}
    bao = broker.get("openbao", {})
    addr, token = env.get("DOPPEL_OPENBAO_ADDR") or bao.get("addr"), env.get("DOPPEL_OPENBAO_TOKEN")
    openbao = OpenBao(addr, token, bao.get("mount", "kv"), bao.get("token_role")) if addr and token else None
    return Config(data_dir=data_dir, listeners=listeners, codex_homes=homes, openbao=openbao)
