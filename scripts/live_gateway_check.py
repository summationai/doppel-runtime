"""Checkpoint 4's live check: a real `codex exec` reaches OpenAI only through the gateway.

    uv run python scripts/live_gateway_check.py --payer summation/u-steven --home ~/.codex

Starts the runtime in-process on 127.0.0.1, gives one running slot a capability paid by `--payer`
(whose Codex home is `--home`), and runs `codex exec` with an empty Codex home of its own: it has
no sign-in, only the capability, so the call can succeed only through the gateway. Then tries a
capability nobody minted, which must fail. The gateway reads the home's auth.json and never
writes it. No token or capability is printed.

Spends one tiny prompt on the payer's plan.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

from doppel_control_plane.app import Runtime
from doppel_control_plane.config import Config, Listener
from doppel_control_plane.store import now
from doppel_shared.capability import capability_hash

PROMPT = "Reply with exactly the word: pong"


def codex_exec(gateway: str, capability: str, workdir: Path) -> subprocess.CompletedProcess:
    overrides = {
        "model_provider": '"doppel"',
        "model_providers.doppel.name": '"doppel gateway"',
        "model_providers.doppel.base_url": f'"{gateway}/model"',
        "model_providers.doppel.env_key": '"AGENT_CAPABILITY"',
        "model_providers.doppel.wire_api": '"responses"',
        "model_providers.doppel.requires_openai_auth": "false",
        "project_doc_max_bytes": "0",
        "check_for_update_on_startup": "false",
        "analytics.enabled": "false",
    }
    home = workdir / "codex-home"  # empty: this Codex has no sign-in of its own
    home.mkdir(mode=0o700, exist_ok=True)
    args = ["codex", "exec", "--skip-git-repo-check", "--sandbox", "read-only"]
    for k, v in overrides.items():
        args += ["-c", f"{k}={v}"]
    env = {**os.environ, "CODEX_HOME": str(home), "AGENT_CAPABILITY": capability}
    env.pop("OPENAI_API_KEY", None)
    return subprocess.run(args + [PROMPT], cwd=workdir, env=env, capture_output=True, text=True, timeout=180)


async def main(payer: str, home: Path) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        config = Config(
            data_dir=tmp / "data",
            listeners={n: Listener("127.0.0.1", 0) for n in ("model", "internal", "health")},
            codex_homes={payer: home},
        )
        runtime = Runtime(config)
        await runtime.start()
        try:
            gateway = "http://{}:{}".format(*runtime.bound["model"])
            ledger = runtime.ledger
            pr = ledger.upsert_pr("summation", "summationai/sandbox", 1, head="live0001")
            slot_id, _ = ledger.queue_slot(pr, "live0001", "live-check", "command", "live", payer_user=payer)
            capability = secrets.token_urlsafe(32)
            ledger.execute(
                "UPDATE slots SET state='running', started_at=?, capability_hash=? WHERE id=?",
                (now(), capability_hash(capability), slot_id),
            )

            ok = await asyncio.to_thread(codex_exec, gateway, capability, tmp)
            used = ledger.value("SELECT tokens_used FROM slots WHERE id=?", (slot_id,))
            answer = ok.stdout.strip().splitlines()[-1] if ok.stdout.strip() else ""
            print(f"minted capability -> exit {ok.returncode}, answer {answer!r}, {used} tokens counted for {payer}")

            bad = await asyncio.to_thread(codex_exec, gateway, secrets.token_urlsafe(32), tmp)
            refused = bad.returncode != 0 or "invalid_capability" in (bad.stdout + bad.stderr)
            print(f"unminted capability -> exit {bad.returncode}, refused: {refused}")

            passed = ok.returncode == 0 and "pong" in ok.stdout.lower() and used > 0 and refused
            if not passed:
                print("--- codex stderr (minted) ---", ok.stderr[-2000:], sep="\n")
            print("PASS" if passed else "FAIL")
            return 0 if passed else 1
        finally:
            await runtime.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--payer", default="summation/u-steven")
    p.add_argument("--home", type=Path, default=Path("~/.codex"))
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sys.exit(asyncio.run(main(a.payer, a.home.expanduser())))
