"""Checkpoint 3's live check: real device-code sign-ins, kept on disk, refreshed one at a time.

    uv run python scripts/live_broker_check.py --payers summation/u-steven summation/u-troy

Needs each person to approve their code. Keeps the sign-ins at
var/live-broker/tenants/<tenant>/<user>/codex/auth.json for later checks. Prints no token.

1. Start a device-code sign-in per payer and print each code; wait for every approval.
2. Every payer shows `connected`, with an owner-only home at tenants/<tenant>/<user>/codex.
3. A fresh broker (a restart) finds every sign-in on disk.
4. Two refreshes at once for the first payer: Codex refreshes once, and the sign-in still works.
5. A real `codex exec` through the gateway on each payer's sign-in (scripts/live_gateway_check.py).
"""

from __future__ import annotations

import argparse
import asyncio
import stat
import subprocess
import sys
from pathlib import Path

from doppel_model_provider import Broker, CodexHomes, CodexSdkAccount

ROOT = Path("var/live-broker/tenants")


class CountingAccount(CodexSdkAccount):
    def __init__(self):
        self.refreshes = 0

    async def refresh(self, home):
        self.refreshes += 1
        await super().refresh(home)


def step(n: int, text: str, ok: bool) -> bool:
    print(f"{n}. {'PASS' if ok else 'FAIL'}  {text}", flush=True)
    return ok


async def main(payers: list[str], timeout: float) -> int:
    results = []
    homes = CodexHomes(ROOT)
    broker = Broker(homes, CodexSdkAccount())
    for payer in payers:
        code = await broker.login(payer)
        print(f"   {payer}: open {code.verification_url} and enter {code.user_code}", flush=True)
    print("   waiting for every approval ...", flush=True)
    approved = await asyncio.wait_for(asyncio.gather(*(broker.wait_for_login(p) for p in payers)), timeout)
    results.append(step(1, f"signed in: {dict(zip(payers, approved, strict=True))}", all(approved)))

    states = {p: broker.status(p)["state"] for p in payers}
    modes = {p: oct(stat.S_IMODE(homes.home(p).stat().st_mode)) for p in payers}
    connected = all(s == "connected" for s in states.values()) and set(modes.values()) == {"0o700"}
    results.append(step(2, f"connected: {states}; home modes {modes}", connected))

    account = CountingAccount()
    fresh = Broker(CodexHomes(ROOT), account)
    found = {p: fresh.status(p)["state"] for p in payers}
    results.append(
        step(3, f"a fresh broker finds them on disk: {found}", all(s == "connected" for s in found.values()))
    )

    first = payers[0]
    before = fresh.status(first)["last_refresh"]
    a, b = await asyncio.gather(fresh.refresh(first), fresh.refresh(first))
    after = fresh.status(first)["last_refresh"]
    results.append(step(
        4, f"two refreshes at once for {first}: Codex refreshed {account.refreshes}x "
        f"(last_refresh {before} -> {after}); same credential for both callers: {a.headers == b.headers}",
        account.refreshes == 1 and a.headers == b.headers,
    ))  # fmt: skip

    for payer in payers:
        run = subprocess.run(
            [sys.executable, "scripts/live_gateway_check.py", "--payer", payer, "--home", str(homes.home(payer))],
            capture_output=True, text=True, timeout=400,
        )  # fmt: skip
        line = next((ln for ln in run.stdout.splitlines() if ln.startswith("minted")), run.stdout[-300:])
        results.append(step(5, f"through the gateway on {payer}'s sign-in: {line}", run.returncode == 0))

    print("ALL PASS" if all(results) else "SOME FAILED", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--payers", nargs="+", default=["summation/u-steven", "summation/u-troy"])
    p.add_argument("--timeout", type=float, default=900, help="seconds to wait for every approval")
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.payers, a.timeout)))
