"""Checkpoint 5's live check: a real review, by real Codex in a pod, through the real gateway.

    docker build -f src/sandbox-runtime/Dockerfile -t doppel-sandbox:dev .
    uv run python scripts/live_sandbox_check.py --payer summation/u-steven \
        --home var/live-broker/tenants/summation/u-steven/codex

Starts the runtime on 127.0.0.1 (the gateway reads the payer's sign-in from --home), makes a small
repo whose PR has a real bug, and runs one reviewer in a pod whose only way out is the gateway.
Prints the verdict and the tokens the gateway counted. Spends one short review on the payer's plan.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

from doppel_control_plane.app import Runtime
from doppel_control_plane.config import Config, Listener
from doppel_control_plane.mirrors import Mirrors
from doppel_control_plane.pods import DockerPods
from doppel_control_plane.store import now
from doppel_shared.capability import capability_hash, mint
from doppel_shared.ports import ReviewJob
from doppel_shared.types import PRRef
from doppel_shared.verdict_schema import problems

PR = PRRef("summation", "summationai/live-sandbox", 1)
BASE = '''def charge(account, cents):
    """Take `cents` from the account. Never lets the balance go below zero."""
    if cents <= 0:
        raise ValueError("cents must be positive")
    if account.balance < cents:
        raise ValueError("insufficient funds")
    account.balance -= cents
    return account.balance
'''
HEAD = '''def charge(account, cents):
    """Take `cents` from the account. Never lets the balance go below zero."""
    if account.balance < cents:
        raise ValueError("insufficient funds")
    account.balance -= cents
    return account.balance


def refund(account, cents):
    """Give `cents` back to the account."""
    return charge(account, -cents)
'''


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "origin"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "billing.py").write_text(BASE)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "billing")
    git(repo, "checkout", "-qb", "pr-1")
    (repo / "billing.py").write_text(HEAD)
    git(repo, "commit", "-qam", "Add refund; simplify charge")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return repo, head


async def main(payer: str, home: Path, image: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        repo, head = make_repo(tmp)
        runtime = Runtime(Config(
            data_dir=tmp / "data", listeners={n: Listener("127.0.0.1", 0) for n in ("model", "internal", "health")},
            codex_homes={payer: home},
        ))  # fmt: skip
        await runtime.start()
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        pods = DockerPods(
            tmp / "pods", Mirrors(tmp / "mirrors", partial=False), image=image,
            gateway_target=f"host.docker.internal:{runtime.bound['model'][1]}",
            repo_url=lambda pr: str(repo), pr_ref=lambda pr: "refs/heads/pr-1", base_branch=lambda pr: "main",
            on_verdict=lambda slot, v: done.done() or done.set_result(("verdict", v)),
            on_failed=lambda slot, why: done.done() or done.set_result(("failed", why)),
        )  # fmt: skip
        try:
            ledger = runtime.ledger
            pr_id = ledger.upsert_pr(PR.tenant, PR.repo, PR.number, head=head)
            slot, _ = ledger.queue_slot(pr_id, head, "steven", "review_requested", payer_user=payer)
            capability = mint()
            ledger.execute("UPDATE slots SET state='running', started_at=?, capability_hash=? WHERE id=?",
                           (now(), capability_hash(capability), slot))  # fmt: skip
            pod = await asyncio.to_thread(pods.create_pod, PR)
            pods.start_reviewer(pod, ReviewJob(slot, "steven", head, capability, pr=PR))
            print(f"reviewing {PR} at {head[:7]} in {pod} ...", flush=True)
            kind, value = await asyncio.wait_for(done, 900)
            tokens = ledger.value("SELECT tokens_used FROM slots WHERE id=?", (slot,))
            if kind != "verdict":
                print(f"FAIL  the review failed: {value}")
                return 1
            wrong = problems(value)
            print(
                f"verdict: {value['verdict']} ({len(value['findings'])} findings, {tokens} tokens through the gateway)"
            )
            print(f"summary: {value.get('summary', '')}")
            for f in value["findings"]:
                print(f"  {f['severity']} {f['file']}:{f['line']}  {f['claim']}")
            print(json.dumps({"schema_problems": wrong}))
            read = value.get("scope", {}).get("files_reviewed", 0)
            ok = not wrong and tokens > 0 and value["verdict"] != "stood_down" and read >= 1  # a real review
            print("PASS" if ok else "FAIL")
            return 0 if ok else 1
        finally:
            await asyncio.to_thread(pods.close)
            await runtime.stop()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--payer", default="summation/u-steven")
    p.add_argument("--home", type=Path, default=Path("var/live-broker/tenants/summation/u-steven/codex"))
    p.add_argument("--image", default="doppel-sandbox:dev")
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    sys.exit(asyncio.run(main(a.payer, a.home.resolve(), a.image)))
