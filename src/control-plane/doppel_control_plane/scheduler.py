"""The scheduler role: events in, slots queued, ready slots started in their PR's pod, the merger run.

Every handler takes `now` so tests control the clock. Decisions come from `core`; this module only
reads the ledger, asks `core`, and applies the answer through the ports.

Events (from intake, the gateway, the sandbox):
    pr_opened, review_requested, review_request_removed, synchronize, closed,
    command, ci, verdict, slot_failed, disposition, usage_limit
and `tick(now)`, which starts ready slots and deletes idle pods.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from doppel_shared.capability import capability_hash, mint
from doppel_shared.ports import Directory, GitHub, InlineComment, ReviewJob, Sandbox
from doppel_shared.types import (
    Decision,
    InvalidVerdict,
    MergeInput,
    MergeState,
    OpenFinding,
    PRRef,
    PRState,
    Reviewer,
    ReviewerKind,
    ReviewerStatus,
    SlotReason,
    SlotState,
    Verdict,
)

from doppel_control_plane.core import commands as command_parser
from doppel_control_plane.core import matching, merger
from doppel_control_plane.core.payer import payer_for
from doppel_control_plane.store import Ledger, iso, parse_iso

log = logging.getLogger(__name__)

# Doppelwerk's rule: only org insiders may command; a drive-by comment can't spend anyone's usage.
COMMAND_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}
ACTIVE = (SlotState.QUEUED, SlotState.BLOCKED, SlotState.RUNNING)


class Scheduler:
    def __init__(
        self,
        ledger: Ledger,
        directory: Directory,
        sandbox: Sandbox,
        github: GitHub,
        limits: matching.Limits | None = None,
    ):
        self.ledger = ledger
        self.directory = directory
        self.sandbox = sandbox
        self.github = github
        self.limits = limits or matching.Limits()

    # ---- lookups -----------------------------------------------------------------------------------

    def _pr(self, pr_id: int) -> Any:
        return self.ledger.one("SELECT * FROM prs WHERE id=?", (pr_id,))

    def _pr_id(self, ref: PRRef) -> int | None:
        return self.ledger.value(
            "SELECT id FROM prs WHERE tenant=? AND repo=? AND pr=?", (ref.tenant, ref.repo, ref.number)
        )

    @staticmethod
    def _ref(pr: Any) -> PRRef:
        return PRRef(pr["tenant"], pr["repo"], pr["pr"])

    def _reviewer(self, tenant: str, repo: str, handle: str) -> Reviewer | None:
        return next((r for r in self.directory.reviewers(tenant, repo) if r.handle == handle), None)

    def _payer(self, pr: Any, reviewer: Reviewer) -> str | None:
        app_payer = (
            self.directory.app_payer(pr["tenant"], pr["repo"], reviewer.handle)
            if reviewer.kind is ReviewerKind.APP
            else None
        )
        return payer_for(reviewer, app_payer)

    def _queue(self, pr: Any, reviewer: Reviewer, reason: SlotReason, now: datetime, command_id: str = "") -> int:
        if pr["state"] == PRState.IDLE:  # new work on an idled-out PR: its pod comes back on the next start
            self.ledger.execute("UPDATE prs SET state='open' WHERE id=?", (pr["id"],))
        slot_id, _ = self.ledger.queue_slot(
            pr["id"],
            pr["current_head"],
            reviewer.handle,
            reason.value,
            command_id=command_id,
            payer_user=self._payer(pr, reviewer),
            at=iso(now),
        )
        return slot_id

    def _stop_slot(self, pr: Any, slot: Any, now: datetime) -> None:
        if slot["state"] == SlotState.RUNNING and pr["pod_id"]:
            self.sandbox.stop_reviewer(pr["pod_id"], slot["id"])
        self.ledger.execute(
            "UPDATE slots SET state='cancelled', finished_at=?, waiting_on=NULL, not_before=NULL WHERE id=?",
            (iso(now), slot["id"]),
        )

    # ---- PR events ---------------------------------------------------------------------------------

    def pr_opened(self, ref: PRRef, head: str, now: datetime) -> int:
        """A new PR: the app's reviewers join it and are queued at its head after the settle window."""
        pr_id = self.ledger.upsert_pr(ref.tenant, ref.repo, ref.number, head=head, at=iso(now))
        self.ledger.execute(
            "UPDATE prs SET settle_until=?, state='open' WHERE id=?", (iso(now + self.limits.settle), pr_id)
        )
        pr = self._pr(pr_id)
        for reviewer in self.directory.reviewers(ref.tenant, ref.repo):
            if reviewer.kind is ReviewerKind.APP:
                self._add_reviewer(pr_id, reviewer.handle, "app", now)
                self._queue(pr, reviewer, SlotReason.APP, now)
        self.merge(pr_id, now)
        return pr_id

    def _add_reviewer(self, pr_id: int, handle: str, source: str, now: datetime) -> None:
        self.ledger.execute(
            "INSERT INTO pr_reviewers (pr_id, reviewer, source, added_at) VALUES (?,?,?,?) "
            "ON CONFLICT(pr_id, reviewer) DO NOTHING",
            (pr_id, handle, source, iso(now)),
        )

    def review_requested(self, ref: PRRef, login: str, now: datetime) -> int | None:
        """A member was requested: their personal reviewer joins the PR and is queued at its head."""
        pr_id = self._pr_id(ref)
        reviewer = self.directory.personal_reviewer(ref.tenant, login)
        if pr_id is None or reviewer is None:
            return None
        self._add_reviewer(pr_id, reviewer.handle, "requested", now)
        slot_id = self._queue(self._pr(pr_id), reviewer, SlotReason.REVIEW_REQUESTED, now)
        self.merge(pr_id, now)
        return slot_id

    def review_request_removed(self, ref: PRRef, login: str, now: datetime) -> None:
        pr_id = self._pr_id(ref)
        reviewer = self.directory.personal_reviewer(ref.tenant, login)
        if pr_id is None or reviewer is None:
            return
        pr = self._pr(pr_id)
        self.ledger.execute(
            "DELETE FROM pr_reviewers WHERE pr_id=? AND reviewer=? AND source='requested'", (pr_id, reviewer.handle)
        )
        for slot in self.ledger.rows(
            "SELECT * FROM slots WHERE pr_id=? AND reviewer=? AND reason='review_requested' AND state IN (?,?,?)",
            (pr_id, reviewer.handle, *ACTIVE),
        ):
            self._stop_slot(pr, slot, now)
        self.merge(pr_id, now)

    def synchronize(self, ref: PRRef, head: str, now: datetime) -> None:
        """A push (DSV-10): stop the old head's reviewers, queue the new head in the same pod.

        Command slots follow the head, so every command still gets its answer.
        """
        pr_id = self._pr_id(ref)
        if pr_id is None:
            return
        pr = self._pr(pr_id)
        old_head = pr["current_head"]
        if old_head == head:
            return
        self.ledger.execute(
            "UPDATE prs SET current_head=?, settle_until=?, state=CASE WHEN state='idle' THEN 'open' ELSE state END "
            "WHERE id=?",
            (head, iso(now + self.limits.settle), pr_id),
        )
        moved_commands = []
        for slot in self.ledger.rows(
            "SELECT * FROM slots WHERE pr_id=? AND head_sha!=? AND state IN (?,?,?)", (pr_id, head, *ACTIVE)
        ):
            self._stop_slot(pr, slot, now)
            if slot["reason"] == SlotReason.COMMAND:
                moved_commands.append(slot)

        approval = self.ledger.one(
            "SELECT * FROM merges WHERE pr_id=? AND head_sha=? AND decision='approved'", (pr_id, old_head)
        )
        if approval and approval["review_id"]:
            self.github.dismiss_review(ref, approval["review_id"], "New commits were pushed; reviewing again.")
            self.ledger.execute(
                "UPDATE merges SET decision='dismissed', decided_at=? WHERE pr_id=? AND head_sha=?",
                (iso(now), pr_id, old_head),
            )

        pr = self._pr(pr_id)
        for row in self.ledger.rows("SELECT * FROM pr_reviewers WHERE pr_id=? ORDER BY added_at", (pr_id,)):
            reviewer = self._reviewer(ref.tenant, ref.repo, row["reviewer"])
            if reviewer is not None:
                reason = SlotReason.APP if row["source"] == "app" else SlotReason.REVIEW_REQUESTED
                self._queue(pr, reviewer, reason, now)
        for slot in moved_commands:
            reviewer = self._reviewer(ref.tenant, ref.repo, slot["reviewer"])
            if reviewer is not None:
                self._queue(pr, reviewer, SlotReason.COMMAND, now, command_id=slot["command_id"])
        self.merge(pr_id, now)

    def closed(self, ref: PRRef, merged: bool, now: datetime) -> None:
        """Merged or closed: stop its reviewers, delete its pod. The ledger keeps the history (DSV-14)."""
        pr_id = self._pr_id(ref)
        if pr_id is None:
            return
        pr = self._pr(pr_id)
        for slot in self.ledger.rows("SELECT * FROM slots WHERE pr_id=? AND state IN (?,?,?)", (pr_id, *ACTIVE)):
            self._stop_slot(pr, slot, now)
        if pr["pod_id"]:
            self.sandbox.delete_pod(pr["pod_id"])
        self.ledger.execute(
            "UPDATE prs SET state=?, pod_id=NULL WHERE id=?", (PRState.MERGED if merged else PRState.CLOSED, pr_id)
        )

    def ci(self, ref: PRRef, head: str, state: str, now: datetime) -> None:
        """CI's overall result for a head. Only the merger waits for it."""
        pr_id = self._pr_id(ref)
        if pr_id is None:
            return
        self.ledger.execute(
            "INSERT INTO ci_status (pr_id, head_sha, state, updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(pr_id, head_sha) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (pr_id, head, state, iso(now)),
        )
        self.merge(pr_id, now)

    def disposition(self, ref: PRRef, reviewer: str, fid: str, kind: str, by_login: str, now: datetime) -> None:
        """A person resolved or answered a finding: it no longer blocks, unless raised again later."""
        pr_id = self._pr_id(ref)
        if pr_id is None:
            return
        self.ledger.execute(
            "INSERT INTO dispositions (pr_id, reviewer, fid, disposition, by_login, at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(pr_id, reviewer, fid) DO UPDATE SET disposition=excluded.disposition, "
            "by_login=excluded.by_login, at=excluded.at",
            (pr_id, reviewer, fid, kind, by_login, iso(now)),
        )
        self.merge(pr_id, now)

    def usage_limit(self, payer: str, reset_at: datetime) -> None:
        """The gateway saw a usage-limit answer: that payer's slots wait until the reset (DSV-12)."""
        self.ledger.record_usage_reset(payer, reset_at)

    # ---- commands ----------------------------------------------------------------------------------

    def command(
        self, ref: PRRef, comment_id: str, author: str, association: str, body: str, now: datetime
    ) -> str | None:
        """`@doppelwerk ...`. Returns the command's final or current state, or None if it isn't one.

        Every command gets a visible answer: reviews, or a reply saying why not.
        """
        if author.endswith("[bot]") or association not in COMMAND_ASSOCIATIONS:
            return None
        pr_id = self._pr_id(ref)
        if pr_id is None:
            return None
        handles = {r.handle for r in self.directory.reviewers(ref.tenant, ref.repo)}
        parsed = command_parser.parse(body, handles)
        if parsed is None:
            return None
        cur = self.ledger.execute(
            "INSERT INTO commands (tenant, comment_id, pr_id, author, body, command, targets, description, "
            "received_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(tenant, comment_id) DO NOTHING",
            (
                ref.tenant,
                comment_id,
                pr_id,
                author,
                body,
                parsed.command,
                ",".join(parsed.targets),
                parsed.description,
                iso(now),
            ),
        )
        if cur.rowcount == 0:  # seen before: a command is acted on exactly once
            return self.ledger.value(
                "SELECT state FROM commands WHERE tenant=? AND comment_id=?", (ref.tenant, comment_id)
            )

        if not parsed.known:
            return self._finish_command(
                ref, comment_id, "refused", f"Unknown command `{parsed.command}`. {command_parser.HELP}", now
            )
        if parsed.command == "help":
            return self._finish_command(ref, comment_id, "responded", command_parser.HELP, now)

        pr = self._pr(pr_id)
        if pr["state"] in (PRState.MERGED, PRState.CLOSED):
            return self._finish_command(ref, comment_id, "refused", "This PR is closed.", now)
        if pr["state"] == PRState.IDLE:
            self.ledger.execute("UPDATE prs SET state='open' WHERE id=?", (pr_id,))
            pr = self._pr(pr_id)

        on_pr = {
            r["reviewer"]: r["source"] for r in self.ledger.rows("SELECT * FROM pr_reviewers WHERE pr_id=?", (pr_id,))
        }
        targets = list(parsed.targets) or sorted(on_pr)
        if not targets:
            return self._finish_command(ref, comment_id, "refused", "No reviewer is on this PR yet.", now)

        refusals, queued = [], 0
        for handle in targets:
            reviewer = self._reviewer(ref.tenant, ref.repo, handle)
            if reviewer is None:
                refusals.append((handle, f"No reviewer named {handle} on this repo."))
            elif reviewer.kind is ReviewerKind.PERSONAL and handle not in on_pr:
                # DSV-4: a personal reviewer runs on its owner's sign-in, so only where its owner was requested.
                refusals.append((handle, f"{handle} isn't requested on this PR. Request their review first."))
            else:
                self._queue(pr, reviewer, SlotReason.COMMAND, now, command_id=comment_id)
                queued += 1
        for handle, why in refusals:
            self._command_response(ref.tenant, comment_id, handle, "refused: " + why, now)
        if not queued:
            return self._finish_command(ref, comment_id, "refused", " ".join(why for _, why in refusals), now)
        if refusals:
            self.github.reply(ref, " ".join(why for _, why in refusals))
        self.ledger.execute(
            "UPDATE commands SET state='running' WHERE tenant=? AND comment_id=?", (ref.tenant, comment_id)
        )
        return "running"

    def _finish_command(self, ref: PRRef, comment_id: str, state: str, response: str, now: datetime) -> str:
        self.github.reply(ref, response)
        self.ledger.execute(
            "UPDATE commands SET state=?, response=?, responded_at=? WHERE tenant=? AND comment_id=?",
            (state, response, iso(now), ref.tenant, comment_id),
        )
        return state

    def _command_response(self, tenant: str, comment_id: str, reviewer: str, response: str, now: datetime) -> None:
        self.ledger.execute(
            "INSERT INTO command_responses (tenant, comment_id, reviewer, response, at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(tenant, comment_id, reviewer) DO UPDATE SET response=excluded.response, at=excluded.at",
            (tenant, comment_id, reviewer, response, iso(now)),
        )
        cmd = self.ledger.one("SELECT * FROM commands WHERE tenant=? AND comment_id=?", (tenant, comment_id))
        wanted = set(filter(None, (cmd["targets"] or "").split(","))) or {
            r["reviewer"] for r in self.ledger.rows("SELECT reviewer FROM slots WHERE command_id=?", (comment_id,))
        }
        answered = {
            r["reviewer"]
            for r in self.ledger.rows(
                "SELECT reviewer FROM command_responses WHERE tenant=? AND comment_id=?", (tenant, comment_id)
            )
        }
        if cmd["state"] == "running" and wanted <= answered:
            self.ledger.execute(
                "UPDATE commands SET state='responded', responded_at=? WHERE tenant=? AND comment_id=?",
                (iso(now), tenant, comment_id),
            )

    # ---- slots finishing ---------------------------------------------------------------------------

    def verdict(self, slot_id: int, raw: dict, now: datetime) -> str:
        """A reviewer's verdict file arrived. Post it as a COMMENT review (reviewers never approve)."""
        slot = self.ledger.one("SELECT * FROM slots WHERE id=?", (slot_id,))
        if slot is None or slot["state"] != SlotState.RUNNING:
            return "ignored"
        try:
            verdict = Verdict.parse(raw)
        except InvalidVerdict as e:
            return self.slot_failed(slot_id, f"invalid verdict: {e}", now)
        pr = self._pr(slot["pr_id"])
        ref = self._ref(pr)
        self.ledger.execute(
            "UPDATE slots SET verdict=?, finished_at=? WHERE id=?", (json.dumps(raw), iso(now), slot_id)
        )
        self.ledger.execute("UPDATE prs SET last_review_at=? WHERE id=?", (iso(now), pr["id"]))
        if slot["head_sha"] != pr["current_head"]:  # Doppelwerk's head-moved guard: never post on a stale head
            self.ledger.execute("UPDATE slots SET state='cancelled' WHERE id=?", (slot_id,))
            return "stale"

        comments = [
            InlineComment(f.id, f.file, f.line, f"**{f.severity}** ({f.category}): {f.claim}") for f in verdict.findings
        ]
        body = f"**{slot['reviewer']}**: {verdict.verdict.replace('_', ' ')}. {verdict.summary}".strip()
        posted = self.github.post_review(ref, slot["head_sha"], "COMMENT", body, comments)
        with self.ledger.tx() as db:
            for f in verdict.findings:
                db.execute(
                    "INSERT OR REPLACE INTO findings (slot_id, fid, severity, category, file, line) "
                    "VALUES (?,?,?,?,?,?)",
                    (slot_id, f.id, f.severity, f.category, f.file, f.line),
                )
                db.execute(
                    "INSERT OR REPLACE INTO finding_anchors (slot_id, fid, comment_id, file, line) VALUES (?,?,?,?,?)",
                    (slot_id, f.id, posted.comment_ids.get(f.id), f.file, f.line),
                )
            db.execute(
                "INSERT INTO posts (slot_id, review_id, review_url, posted_at) VALUES (?,?,?,?)",
                (slot_id, posted.review_id, posted.url, iso(now)),
            )
            db.execute("UPDATE slots SET state='posted', capability_hash=NULL WHERE id=?", (slot_id,))
        if slot["reason"] == SlotReason.COMMAND:
            self._command_response(pr["tenant"], slot["command_id"], slot["reviewer"], posted.url, now)
        self.merge(pr["id"], now)
        return "posted"

    def slot_failed(self, slot_id: int, why: str, now: datetime) -> str:
        """A run failed: retry up to the attempt limit, then fail visibly if a command asked for it."""
        slot = self.ledger.one("SELECT * FROM slots WHERE id=?", (slot_id,))
        if slot is None or slot["state"] != SlotState.RUNNING:
            return "ignored"
        log.warning("slot %s failed (attempt %s): %s", slot_id, slot["attempts"], why)
        if slot["attempts"] < self.limits.max_attempts:
            self.ledger.execute(
                "UPDATE slots SET state='queued', started_at=NULL, capability_hash=NULL WHERE id=?", (slot_id,)
            )
            return "queued"
        self.ledger.execute(
            "UPDATE slots SET state='failed', finished_at=?, capability_hash=NULL WHERE id=?", (iso(now), slot_id)
        )
        pr = self._pr(slot["pr_id"])
        if slot["reason"] == SlotReason.COMMAND:
            self.github.reply(self._ref(pr), f"{slot['reviewer']}'s review failed after {slot['attempts']} attempts.")
            self._command_response(pr["tenant"], slot["command_id"], slot["reviewer"], "failed: " + why, now)
        self.merge(pr["id"], now)
        return "failed"

    # ---- matching ----------------------------------------------------------------------------------

    def tick(self, now: datetime) -> list[int]:
        """Start every slot whose conditions hold, oldest first; delete pods idle too long.

        Returns the ids of the slots it started.
        """
        self._reap_idle(now)
        running = self.ledger.rows("SELECT payer_user FROM slots WHERE state='running'")
        host_running = len(running)
        payer_running: dict[str, int] = {}
        for r in running:
            payer_running[r["payer_user"]] = payer_running.get(r["payer_user"], 0) + 1
        resets = {r["payer"]: parse_iso(r["reset_at"]) for r in self.ledger.rows("SELECT * FROM usage_resets")}

        started = []
        candidates = self.ledger.rows(
            "SELECT s.*, p.settle_until, p.current_head FROM slots s JOIN prs p ON p.id = s.pr_id "
            "WHERE s.state IN ('queued', 'blocked') AND p.state = 'open' AND s.head_sha = p.current_head "
            "ORDER BY s.queued_at, s.id"
        )
        for slot in candidates:
            payer = slot["payer_user"]
            outcome = matching.check(
                matching.SlotFacts(
                    payer=payer,
                    payer_signed_in=bool(payer) and self.directory.signed_in(payer),
                    is_command=slot["reason"] == SlotReason.COMMAND,
                    settle_until=parse_iso(slot["settle_until"]),
                    usage_reset=resets.get(payer),
                    payer_running=payer_running.get(payer, 0),
                    host_running=host_running,
                ),
                now,
                self.limits,
            )
            if isinstance(outcome, matching.Start):
                self._start(slot, now)
                started.append(slot["id"])
                host_running += 1
                payer_running[payer] = payer_running.get(payer, 0) + 1
            elif isinstance(outcome, matching.Blocked):
                self._set_blocked(slot)
            else:
                self._set_waiting(slot, outcome)
        return started

    def _start(self, slot: Any, now: datetime) -> None:
        pr = self._pr(slot["pr_id"])
        pod_id = pr["pod_id"]
        if not pod_id:
            pod_id = self.sandbox.create_pod(self._ref(pr))
            self.ledger.execute("UPDATE prs SET pod_id=? WHERE id=?", (pod_id, pr["id"]))
        capability = mint()
        focus = ""
        if slot["command_id"]:
            focus = (
                self.ledger.value(
                    "SELECT description FROM commands WHERE tenant=? AND comment_id=?",
                    (pr["tenant"], slot["command_id"]),
                )
                or ""
            )
        self.ledger.execute(
            "UPDATE slots SET state='running', started_at=?, attempts=attempts+1, capability_hash=?, "
            "waiting_on=NULL, not_before=NULL WHERE id=?",
            (iso(now), capability_hash(capability), slot["id"]),
        )
        self.sandbox.start_reviewer(
            pod_id, ReviewJob(slot["id"], slot["reviewer"], slot["head_sha"], capability, focus=focus)
        )

    def _set_blocked(self, slot: Any) -> None:
        if slot["state"] == SlotState.BLOCKED:
            return
        self.ledger.execute(
            "UPDATE slots SET state='blocked', waiting_on=NULL, not_before=NULL WHERE id=?", (slot["id"],)
        )
        pr = self._pr(slot["pr_id"])
        who = slot["payer_user"] or "nobody"
        self.github.reply(
            self._ref(pr),
            f"{slot['reviewer']}'s review is waiting: {who} has no working sign-in. Connect Codex to run it.",
        )

    def _set_waiting(self, slot: Any, wait: matching.Wait) -> None:
        reason = wait.reason.value
        until = iso(wait.until) if wait.until else None
        if slot["state"] == SlotState.QUEUED and slot["waiting_on"] == reason and slot["not_before"] == until:
            return
        self.ledger.execute(
            "UPDATE slots SET state='queued', waiting_on=?, not_before=? WHERE id=?", (reason, until, slot["id"])
        )
        if reason == "usage_window" and slot["waiting_on"] != "usage_window":
            pr = self._pr(slot["pr_id"])
            self.github.reply(self._ref(pr), f"{slot['reviewer']}'s review is out of capacity until {until}.")

    def _reap_idle(self, now: datetime) -> None:
        """A PR's pod is deleted after `idle_after` with nothing queued or running (spec section 4)."""
        cutoff = iso(now - self.limits.idle_after)
        for pr in self.ledger.rows(
            "SELECT * FROM prs p WHERE p.state='open' AND p.pod_id IS NOT NULL "
            "AND coalesce(p.last_review_at, p.created_at) < ? "
            "AND NOT EXISTS (SELECT 1 FROM slots s WHERE s.pr_id=p.id AND s.state IN ('queued','blocked','running'))",
            (cutoff,),
        ):
            self.sandbox.delete_pod(pr["pod_id"])
            self.ledger.execute("UPDATE prs SET pod_id=NULL, state='idle' WHERE id=?", (pr["id"],))

    # ---- the merger --------------------------------------------------------------------------------

    def merge_input(self, pr_id: int) -> MergeInput:
        pr = self._pr(pr_id)
        head = pr["current_head"]
        reviewers, blocking = [], []
        for row in self.ledger.rows("SELECT * FROM pr_reviewers WHERE pr_id=? ORDER BY reviewer", (pr_id,)):
            handle = row["reviewer"]
            latest = self.ledger.one(
                "SELECT * FROM slots WHERE pr_id=? AND head_sha=? AND reviewer=? AND state='posted' "
                "ORDER BY finished_at DESC, id DESC LIMIT 1",
                (pr_id, head, handle),
            )
            if latest is not None:
                verdict = Verdict.parse(json.loads(latest["verdict"]))
                reviewers.append(ReviewerStatus(handle, row["source"] == "app", "posted", verdict.verdict))
                for f in verdict.findings:
                    if f.blocking and not self._closed_by_person(pr_id, handle, f.id, latest["finished_at"]):
                        blocking.append(OpenFinding(handle, f.id, f.severity, f.file, f.line, f.claim))
                continue
            states = {
                r["state"]
                for r in self.ledger.rows(
                    "SELECT state FROM slots WHERE pr_id=? AND head_sha=? AND reviewer=?", (pr_id, head, handle)
                )
            }
            status = next((s for s in ("running", "queued", "blocked") if s in states), "waiting")
            reviewers.append(ReviewerStatus(handle, row["source"] == "app", status, None))
        ci = self.ledger.value("SELECT state FROM ci_status WHERE pr_id=? AND head_sha=?", (pr_id, head))
        return MergeInput(head, tuple(reviewers), ci, tuple(blocking))

    def _closed_by_person(self, pr_id: int, reviewer: str, fid: str, verdict_at: str) -> bool:
        """A disposition closes a finding, unless the reviewer raised it again after the disposition."""
        at = self.ledger.value(
            "SELECT at FROM dispositions WHERE pr_id=? AND reviewer=? AND fid=?", (pr_id, reviewer, fid)
        )
        return at is not None and at >= verdict_at

    def merge(self, pr_id: int, now: datetime) -> Decision | None:
        """Decide the PR's approval for its current head. Posts a formal review only when the decision changes."""
        pr = self._pr(pr_id)
        if pr is None or pr["state"] in (PRState.MERGED, PRState.CLOSED) or not pr["current_head"]:
            return None
        ref = self._ref(pr)
        inp = self.merge_input(pr_id)
        decision = merger.decide(inp)
        prev = self.ledger.one("SELECT * FROM merges WHERE pr_id=? AND head_sha=?", (pr_id, inp.head))
        prev_state = prev["decision"] if prev else None
        review_id = prev["review_id"] if prev else None

        if decision.state != prev_state:
            if prev_state == MergeState.APPROVED and review_id:
                self.github.dismiss_review(ref, review_id, "No longer approved: " + decision.reason)
                review_id = None
            if decision.state == MergeState.APPROVED:
                review_id = self.github.post_review(
                    ref, inp.head, "APPROVE", merger.review_body(inp, decision)
                ).review_id
            elif decision.state == MergeState.BLOCKED:
                review_id = self.github.post_review(
                    ref, inp.head, "COMMENT", merger.review_body(inp, decision)
                ).review_id

        self.ledger.execute(
            "INSERT INTO merges (pr_id, head_sha, decision, reason, review_id, decided_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(pr_id, head_sha) DO UPDATE SET decision=excluded.decision, reason=excluded.reason, "
            "review_id=excluded.review_id, decided_at=CASE WHEN merges.decision=excluded.decision "
            "THEN merges.decided_at ELSE excluded.decided_at END",
            (pr_id, inp.head, decision.state.value, decision.reason, review_id, iso(now)),
        )
        status = merger.status_text(inp, decision)
        if status != pr["status_body"]:
            comment_id = self.github.upsert_status(ref, pr["status_comment_id"], status)
            self.ledger.execute(
                "UPDATE prs SET status_comment_id=?, status_body=? WHERE id=?", (comment_id, status, pr_id)
            )
        return decision
