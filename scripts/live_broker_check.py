"""Checkpoint 3's live check: real device-code sign-ins, restore from OpenBao, one refresh at a time.

    uv run python scripts/live_broker_check.py --payers u-steven u-troy

Needs the `bao` CLI (starts a throwaway dev server) and each person to approve their code. Keeps
the homes under var/live-broker/homes so later checks can use them. Prints no token.

1. Start a device-code sign-in per payer and print each code; wait for every approval.
2. Every payer shows `connected` and is saved to OpenBao.
3. Delete the homes, start a fresh broker: every sign-in comes back from OpenBao.
4. Two refreshes at once for the first payer: Codex refreshes once, and the sign-in still works.
5. A real `codex exec` through the gateway on each payer's sign-in (scripts/live_gateway_check.py).
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from doppel_model_provider import Broker, CodexHomes, CodexSdkAccount, OpenBaoStore

ROOT_TOKEN = "live-check-root"
HOMES = Path("var/live-broker/homes")


class CountingAccount(CodexSdkAccount):
    def __init__(self):
        self.refreshes = 0

    async def refresh(self, home):
        self.refreshes += 1
        await super().refresh(home)


def start_bao() -> tuple[subprocess.Popen, str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    addr = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        ["bao", "server", "-dev", f"-dev-root-token-id={ROOT_TOKEN}", f"-dev-listen-address=127.0.0.1:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(100):
        try:
            urllib.request.urlopen(f"{addr}/v1/sys/health", timeout=1)
            break
        except OSError:
            time.sleep(0.1)
    req = urllib.request.Request(
        f"{addr}/v1/sys/mounts/kv",
        method="POST",
        headers={"X-Vault-Token": ROOT_TOKEN},
        data=b'{"type": "kv", "options": {"version": "2"}}',
    )
    urllib.request.urlopen(req, timeout=5)
    return proc, addr


def step(n: int, text: str, ok: bool) -> bool:
    print(f"{n}. {'PASS' if ok else 'FAIL'}  {text}", flush=True)
    return ok


async def main(payers: list[str], timeout: float) -> int:
    bao, addr = start_bao()
    store = OpenBaoStore(addr, ROOT_TOKEN)
    results = []
    try:
        HOMES.mkdir(parents=True, exist_ok=True)
        broker = Broker(CodexHomes(HOMES), CodexSdkAccount(), store)
        for payer in payers:
            code = await broker.login(payer)
            print(f"   {payer}: open {code.verification_url} and enter {code.user_code}", flush=True)
        print("   waiting for every approval ...", flush=True)
        approved = await asyncio.wait_for(asyncio.gather(*(broker.wait_for_login(p) for p in payers)), timeout)
        statuses = {p: broker.status(p) for p in payers}
        results.append(step(1, f"signed in: {dict(zip(payers, approved, strict=True))}", all(approved)))
        results.append(step(
            2, "connected and saved: " + ", ".join(f"{p} {s['state']}/saved={s['saved']}" for p, s in statuses.items()),
            all(s["state"] == "connected" and s["saved"] for s in statuses.values()),
        ))  # fmt: skip

        for payer in payers:
            shutil.rmtree(HOMES / payer)
        account = CountingAccount()
        fresh = Broker(CodexHomes(HOMES), account, store)
        await fresh.restore()
        restored = {p: fresh.status(p)["state"] for p in payers}
        results.append(step(3, f"homes deleted, restored from OpenBao: {restored}",
                            all(s == "connected" for s in restored.values())))  # fmt: skip

        first = payers[0]
        before = fresh.status(first)["last_refresh"]
        a, b = await asyncio.gather(fresh.refresh(first), fresh.refresh(first))
        after = fresh.status(first)["last_refresh"]
        results.append(step(
            4, f"two refreshes at once for {first}: Codex refreshed {account.refreshes}x "
            f"(last_refresh {before} -> {after}); both callers got the same credential: {a.headers == b.headers}",
            account.refreshes == 1 and a.headers == b.headers,
        ))  # fmt: skip
    finally:
        await store.close()
        bao.terminate()
        bao.wait(10)

    for payer in payers:
        run = subprocess.run(
            [sys.executable, "scripts/live_gateway_check.py", "--payer", payer, "--home", str(HOMES / payer)],
            capture_output=True, text=True, timeout=400,
        )  # fmt: skip
        line = next((ln for ln in run.stdout.splitlines() if ln.startswith("minted")), run.stdout[-300:])
        results.append(step(5, f"through the gateway on {payer}'s sign-in: {line}", run.returncode == 0))

    print("ALL PASS" if all(results) else "SOME FAILED", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--payers", nargs="+", default=["u-steven", "u-troy"])
    p.add_argument("--timeout", type=float, default=900, help="seconds to wait for every approval")
    a = p.parse_args()
    sys.exit(asyncio.run(main(a.payers, a.timeout)))
