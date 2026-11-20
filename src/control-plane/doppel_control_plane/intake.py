"""Intake by polling (spec section 5; review #8): GitHub's state turned into the scheduler's events.

Each cycle reads repo listings, never per-person searches, as each tenant's installation, under a
calls-per-hour budget per installation. The passes run in priority order across all of a tenant's
repos, so a spent budget drops the least important reading first:

    1. PRs       the open-PR listing (heads, requested reviewers); a PR that left it is looked up
                 once, to tell merged from closed
    2. Commands  the repo's PR comments since the last cycle (`@doppelwerk ...`; each answered once)
    3. CI        a head's check runs, only where the merger is waiting on CI for it

Everything here is a diff against the ledger, so a missed cycle is caught up by the next one, and
the same answer read twice changes nothing. Webhooks (after the demo) call the same scheduler events;
this poll stays as the reconcile.

GitHub drops a requested reviewer from the listing once that person reviews. Intake reads that as
the request being removed: their doppel stops waiting, and the merger no longer waits on it.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from doppel_shared.ports import PAGE, Directory, GitHubReader, OpenPR
from doppel_shared.types import MergeState, PRRef, PRState

from doppel_control_plane.scheduler import Scheduler
from doppel_control_plane.store import Ledger, iso, parse_iso

log = logging.getLogger(__name__)

HOUR = timedelta(hours=1)


class OutOfBudget(Exception):
    pass


class Budget:
    """GitHub calls per hour per installation (one installation per tenant), over a sliding hour."""

    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self._calls: dict[str, deque[datetime]] = {}

    def _window(self, key: str, now: datetime) -> deque[datetime]:
        calls = self._calls.setdefault(key, deque())
        while calls and calls[0] <= now - HOUR:
            calls.popleft()
        return calls

    def left(self, key: str, now: datetime) -> int:
        return self.per_hour - len(self._window(key, now))

    def take(self, key: str, now: datetime) -> None:
        calls = self._window(key, now)
        if len(calls) >= self.per_hour:
            raise OutOfBudget(key)
        calls.append(now)


@dataclass(frozen=True)
class Watched:
    """A repo intake polls, and the tenant whose installation reads it."""

    tenant: str
    repo: str


class Poller:
    def __init__(
        self,
        ledger: Ledger,
        scheduler: Scheduler,
        reader: GitHubReader,
        directory: Directory,
        repos: list[Watched],
        budget: Budget,
        *,
        lookback: timedelta = HOUR,  # how far back the first cycle reads comments
        skip_drafts: bool = True,
    ):
        self.ledger, self.scheduler, self.reader, self.directory = ledger, scheduler, reader, directory
        self.repos, self.budget = repos, budget
        self.lookback, self.skip_drafts = lookback, skip_drafts
        self._spent_logged: set[str] = set()

    async def poll(self, now: datetime) -> None:
        """One cycle: every pass over every repo, in priority order, as far as each budget allows."""
        spent: set[str] = set()
        for name, pass_ in (("prs", self._prs), ("commands", self._commands), ("ci", self._ci)):
            for w in self.repos:
                if w.tenant in spent:
                    continue
                try:
                    await pass_(w, now)
                except OutOfBudget:
                    spent.add(w.tenant)
                    if w.tenant not in self._spent_logged:  # once per spell, not every cycle
                        log.warning("intake: %s's budget of %d calls/hour is spent; skipped from %s on %s",
                                    w.tenant, self.budget.per_hour, name, w.repo)  # fmt: skip
                except Exception:  # one repo's failure never stops the others
                    log.exception("intake: %s pass failed on %s", name, w.repo)
        self._spent_logged = spent

    # ---- 1. PRs --------------------------------------------------------------------------------------

    async def _prs(self, w: Watched, now: datetime) -> None:
        listed: dict[int, OpenPR] = {}
        page = 1
        while True:
            self.budget.take(w.tenant, now)
            batch = await self.reader.open_prs(w.repo, page)
            listed.update((pr.number, pr) for pr in batch)
            if len(batch) < PAGE:
                break
            page += 1
        for pr in listed.values():
            self._reconcile(w, pr, now)
        for row in self.ledger.rows(
            "SELECT pr FROM prs WHERE tenant=? AND repo=? AND state IN (?,?)",
            (w.tenant, w.repo, PRState.OPEN, PRState.IDLE),
        ):
            if row["pr"] not in listed:
                self.budget.take(w.tenant, now)
                state = await self.reader.pr_state(w.repo, row["pr"])
                if state != "open":
                    self.scheduler.closed(PRRef(w.tenant, w.repo, row["pr"]), state == "merged", now)

    def _reconcile(self, w: Watched, pr: OpenPR, now: datetime) -> None:
        ref = PRRef(w.tenant, w.repo, pr.number)
        row = self.ledger.one("SELECT * FROM prs WHERE tenant=? AND repo=? AND pr=?", (w.tenant, w.repo, pr.number))
        if row is None or row["state"] in (PRState.MERGED, PRState.CLOSED):  # new, or reopened
            if pr.draft and self.skip_drafts:
                return
            log.info("intake: %s opened at %s", ref, pr.head_sha[:7])
            pr_id = self.scheduler.pr_opened(ref, pr.head_sha, now)
        else:
            pr_id = row["id"]
            if row["current_head"] != pr.head_sha:
                log.info("intake: %s moved to %s", ref, pr.head_sha[:7])
                self.scheduler.synchronize(ref, pr.head_sha, now)

        wanted = {}
        for login in pr.requested:
            reviewer = self.directory.personal_reviewer(w.tenant, login)
            if reviewer is not None:
                wanted[reviewer.handle] = login
        rows = self.ledger.rows("SELECT reviewer FROM pr_reviewers WHERE pr_id=? AND source='requested'", (pr_id,))
        known = {r["reviewer"] for r in rows}
        for handle in sorted(wanted.keys() - known):
            log.info("intake: %s requested %s", ref, wanted[handle])
            self.scheduler.review_requested(ref, wanted[handle], now)
        by_handle = {r.handle: r for r in self.directory.reviewers(w.tenant, w.repo)}
        for handle in sorted(known - wanted.keys()):
            reviewer = by_handle.get(handle)
            if reviewer is not None and reviewer.login:
                log.info("intake: %s no longer requests %s", ref, reviewer.login)
                self.scheduler.review_request_removed(ref, reviewer.login, now)

    # ---- 2. Commands ---------------------------------------------------------------------------------

    async def _commands(self, w: Watched, now: datetime) -> None:
        since = parse_iso(
            self.ledger.value("SELECT comments_since FROM intake_cursors WHERE tenant=? AND repo=?", (w.tenant, w.repo))
        ) or (now - self.lookback)
        newest, page = since, 1
        while True:
            self.budget.take(w.tenant, now)
            batch = await self.reader.comments(w.repo, since, page)
            for c in batch:
                # Seen again (GitHub's `since` is inclusive, edits move a comment up): the ledger acts once.
                ref = PRRef(w.tenant, w.repo, c.pr)
                self.scheduler.command(ref, c.comment_id, c.author, c.association, c.body, now)
                newest = max(newest, c.updated_at)
            if len(batch) < PAGE:
                break
            page += 1
        self.ledger.execute(
            "INSERT INTO intake_cursors (tenant, repo, comments_since) VALUES (?,?,?) "
            "ON CONFLICT(tenant, repo) DO UPDATE SET comments_since=excluded.comments_since",
            (w.tenant, w.repo, iso(newest)),
        )

    # ---- 3. CI ---------------------------------------------------------------------------------------

    async def _ci(self, w: Watched, now: datetime) -> None:
        waiting = self.ledger.rows(
            "SELECT p.pr, p.current_head, c.state AS ci FROM prs p "
            "JOIN merges m ON m.pr_id=p.id AND m.head_sha=p.current_head "
            "LEFT JOIN ci_status c ON c.pr_id=p.id AND c.head_sha=p.current_head "
            "WHERE p.tenant=? AND p.repo=? AND p.state=? AND m.decision=?",
            (w.tenant, w.repo, PRState.OPEN, MergeState.WAITING_CI),
        )
        for row in waiting:
            self.budget.take(w.tenant, now)
            state = await self.reader.ci_state(w.repo, row["current_head"])
            if state is not None and state != row["ci"]:
                self.scheduler.ci(PRRef(w.tenant, w.repo, row["pr"]), row["current_head"], state, now)
