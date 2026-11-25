"""Jobs from Doppelwerk's BFF, run in this runner's sandbox (docs/specs/bff-split.md §4b "Running a job", checkpoint 4).

    accept()   a job from a check-in: a local slot in the ledger (the gateway finds a run's payer through it, by its
               capability's hash), the PR's pod (one per PR, shared by its reviewers), and the reviewer started with
               the doppel's rules and files, as the BFF sent them. This runner keeps no review rules.
    running()  the BFF ids of jobs in hand, for the check-in, which renews their leases
    stop()     what the BFF says to stop (the PR moved on, or the job was taken back)
    flush()    results the BFF hasn't accepted yet, sent again until it does (or says the job isn't ours any more)

A job's repo token reaches only the mirror's `git fetch`, through git's environment config, never a pod. A job left
running by a process that died is dropped here; the BFF sees it missing from the next check-in and tries it again.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from doppel_shared.capability import capability_hash, mint
from doppel_shared.ports import ReviewJob, Sandbox
from doppel_shared.types import PRRef

from doppel_control_plane.store import Ledger, now

log = logging.getLogger(__name__)

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}$")


class JobRunner:
    def __init__(self, ledger: Ledger, sandbox: Callable[[], Sandbox]):
        self.ledger = ledger
        self._sandbox_factory = sandbox
        self._sandbox: Sandbox | None = None
        self.pods: dict[PRRef, str] = {}
        self.tokens: dict[str, str] = {}  # repo -> its latest job's token, for git fetch
        self.on_result: Callable[[], Awaitable[None]] | None = None  # the link sends results at once when set

    # ---- the sandbox's view -------------------------------------------------------------------------------------

    @property
    def sandbox(self) -> Sandbox:
        if self._sandbox is None:
            self._sandbox = self._sandbox_factory()
        return self._sandbox

    def git_env(self, repo: str) -> dict[str, str]:
        """The token as an HTTP header through git's environment config: never in a URL, a config file, or `ps`."""
        token = self.tokens.get(repo)
        if not token:
            return {"GIT_TERMINAL_PROMPT": "0"}  # a public repo, or a local test origin
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            "GIT_TERMINAL_PROMPT": "0",
        }

    # ---- the check-in's side ------------------------------------------------------------------------------------

    def recover(self) -> int:
        """At startup: a job the last process was running is gone with it. Drop it; the BFF retries it."""
        return self.ledger.execute(
            "UPDATE slots SET state='failed', finished_at=?, capability_hash=NULL "
            "WHERE bff_job IS NOT NULL AND state IN ('queued', 'running') AND result IS NULL",
            (now(),),
        ).rowcount

    def running(self) -> list[str]:
        rows = self.ledger.rows("SELECT bff_job FROM slots WHERE bff_job IS NOT NULL AND state='running'")
        return [r["bff_job"] for r in rows]

    def accept(self, job: dict[str, Any]) -> bool:
        """Start one job. A job already in hand, or one that doesn't make sense, is not started (False)."""
        try:
            jid, repo, number, head = job["id"], job["repository"], int(job["number"]), job["head_sha"]
            payer, doppel = job["payer"], job["doppel"]
            if not (UUID.match(jid) and REPO.match(repo) and SHA.match(head) and number > 0 and "/" in payer):
                raise ValueError("bad job")
        except (KeyError, TypeError, ValueError):
            log.warning("bff: refused a malformed job")
            return False
        if self.ledger.one("SELECT 1 FROM slots WHERE bff_job=?", (jid,)):
            return False
        tenant = payer.split("/")[0]
        pr = PRRef(tenant, repo, number)
        if job.get("token"):
            self.tokens[repo] = job["token"]
        capability = mint()
        pr_id = self.ledger.upsert_pr(tenant, repo, number, head)
        reason = "review_requested" if doppel.get("kind") == "personal" else "app"
        with self.ledger.tx() as db:
            cur = db.execute(
                "INSERT INTO slots (pr_id, head_sha, reviewer, payer_user, reason, command_id, state, attempts, "
                "capability_hash, bff_job, queued_at, started_at) VALUES (?,?,?,?,?,?, 'running', 1, ?,?,?,?)",
                (pr_id, head, doppel["handle"], payer, reason, jid, capability_hash(capability), jid, now(), now()),
            )
            slot_id = cur.lastrowid
        try:
            pod = self.pods.get(pr) or self.sandbox.create_pod(pr)
            self.pods[pr] = pod
            self.sandbox.start_reviewer(pod, ReviewJob(
                slot_id, doppel["handle"], head, capability, pr=pr, instructions=job.get("instructions") or "",
                focus=str(job.get("focus") or "")[:500],
                base_ref=job.get("base_branch") or None, files=dict(doppel.get("files") or {}),
            ))  # fmt: skip
        except Exception as e:
            log.exception("bff: job %s couldn't start", jid)
            self.failed(slot_id, f"couldn't start: {type(e).__name__}")
            return False
        log.info("bff: job %s started (%s on %s)", jid, doppel["handle"], pr)
        return True

    def stop(self, job_ids: list[str]) -> None:
        for jid in job_ids:
            row = self.ledger.one("SELECT s.id, p.tenant, p.repo, p.pr FROM slots s JOIN prs p ON p.id=s.pr_id "
                                  "WHERE s.bff_job=? AND s.state='running'", (jid,))  # fmt: skip
            if row is None:
                continue
            self.ledger.execute("UPDATE slots SET state='cancelled', finished_at=?, capability_hash=NULL WHERE id=?",
                                (now(), row["id"]))  # fmt: skip
            pod = self.pods.get(PRRef(row["tenant"], row["repo"], row["pr"]))
            if pod:
                self.sandbox.stop_reviewer(pod, row["id"])
            log.info("bff: job %s stopped", jid)
            self._tidy(PRRef(row["tenant"], row["repo"], row["pr"]))

    # ---- results ------------------------------------------------------------------------------------------------

    def verdict(self, slot_id: int, verdict: dict) -> None:
        self._finish(slot_id, "posted", {"status": "done", "verdict": verdict})

    def failed(self, slot_id: int, why: str) -> None:
        self._finish(slot_id, "failed", {"status": "failed", "reason": (why or "failed")[:500]})

    def _finish(self, slot_id: int, state: str, result: dict) -> None:
        row = self.ledger.one("SELECT s.state, p.tenant, p.repo, p.pr FROM slots s JOIN prs p ON p.id=s.pr_id "
                              "WHERE s.id=?", (slot_id,))  # fmt: skip
        if row is None or row["state"] != "running":
            return  # stopped meanwhile: the BFF already took it back
        self.ledger.execute(
            "UPDATE slots SET state=?, finished_at=?, capability_hash=NULL, result=?, "
            "verdict=CASE WHEN ? = 'posted' THEN ? END WHERE id=?",
            (state, now(), json.dumps(result), state, json.dumps(result.get("verdict")), slot_id),
        )
        self._tidy(PRRef(row["tenant"], row["repo"], row["pr"]))
        if self.on_result is not None:
            asyncio.get_running_loop().create_task(self.on_result())

    def _tidy(self, pr: PRRef) -> None:
        """The PR's pod goes once none of its jobs is running; the mirror stays for the next head."""
        busy = self.ledger.one("SELECT 1 FROM slots s JOIN prs p ON p.id=s.pr_id WHERE p.tenant=? AND p.repo=? "
                               "AND p.pr=? AND s.state='running'", (pr.tenant, pr.repo, pr.number))  # fmt: skip
        if busy is None and pr in self.pods:
            self.sandbox.delete_pod(self.pods.pop(pr))

    async def flush(self, send: Callable[[str, dict], Awaitable[int]]) -> int:
        """Send each result the BFF hasn't accepted. `send` returns the HTTP status; 2xx or 409 (not ours any more)
        settles it, anything else keeps it for the next try. Returns how many were settled."""
        settled = 0
        pending = self.ledger.rows(
            "SELECT id, bff_job, result FROM slots WHERE bff_job IS NOT NULL AND result IS NOT NULL"
        )
        for row in pending:
            try:
                status = await send(row["bff_job"], json.loads(row["result"]))
            except Exception as e:
                log.warning("bff: couldn't send job %s's result (%s)", row["bff_job"], type(e).__name__)
                break
            if 200 <= status < 300 or status in (404, 409):
                self.ledger.execute("UPDATE slots SET result=NULL WHERE id=?", (row["id"],))
                settled += 1
            elif status == 400:  # the BFF refuses its shape: sending it again won't help
                log.error("bff: the BFF refused job %s's result as malformed", row["bff_job"])
                self.ledger.execute("UPDATE slots SET result=NULL WHERE id=?", (row["id"],))
            else:
                log.warning("bff: job %s's result not accepted (%s); will retry", row["bff_job"], status)
        return settled

    def close(self) -> None:
        close = getattr(self._sandbox, "close", None)
        if close is not None:
            close()
