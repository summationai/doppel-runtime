"""Intake by polling (spec section 5; review #8): GitHub's state turned into the scheduler's events.

Each cycle reads repo listings, never per-person searches, as each tenant's installation, under a
calls-per-hour budget per installation. The passes run in priority order across all of a tenant's
repos, so a spent budget drops the least important reading first:

    1. PRs       the open-PR listing (heads, requested reviewers); a PR that left it is looked up
                 once, to tell merged from closed
    2. Commands  the repo's PR comments since the last cycle (`@doppelwerk ...`; each answered once)
    3. CI        a head's check runs, only where the merger is waiting on CI for it

Everything here is a diff against the ledger, so a missed cycle is caught up by the next one, and
the same answer read twice changes nothing. A webhook (webhooks.py) polls its repo at once, through
this same code, so events and the reconcile can't disagree.

Automatic reviews run only on PRs from insiders (OWNER, MEMBER, COLLABORATOR). Anyone can open a PR
on a public repo, so an outsider's PR is recorded but runs nothing until someone requests a review
or an insider's `@doppelwerk` command asks for one.

GitHub drops a requested person from the listing once they review. Their doppel posts as doppelwerk,
not as them, so their own review changes nothing for it: it stays on the PR and reviews every new
head. Only an un-request removes it. Telling the two apart costs one read of the PR's reviews, once,
when the login leaves the listing.
"""

from __future__ import annotations

import asyncio
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
INSIDERS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


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
        auto_review: str = "insiders",  # insiders | everyone: whose PRs get the app's reviewers unasked
    ):
        self.ledger, self.scheduler, self.reader, self.directory = ledger, scheduler, reader, directory
        self.repos, self.budget = repos, budget
        self.lookback, self.skip_drafts = lookback, skip_drafts
        if auto_review not in ("insiders", "everyone"):
            raise ValueError(f"auto_review must be insiders or everyone, not {auto_review!r}")
        self.auto_review = auto_review
        self._spent_logged: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}  # one pass at a time per repo: the timer and webhooks

    def watched(self, repo: str) -> Watched | None:
        return next((w for w in self.repos if w.repo.lower() == repo.lower()), None)

    async def poll(self, now: datetime) -> None:
        """One cycle: every pass over every repo, in priority order, as far as each budget allows."""
        await self._run(self.repos, now)

    async def poll_repo(self, w: Watched, now: datetime) -> None:
        """Every pass over one repo, now: what a webhook asks for."""
        await self._run([w], now)

    async def _run(self, repos: list[Watched], now: datetime) -> None:
        spent: set[str] = set()
        for name, pass_ in (("prs", self._prs), ("commands", self._commands), ("ci", self._ci)):
            for w in repos:
                if w.tenant in spent:
                    continue
                try:
                    async with self._locks.setdefault(w.repo, asyncio.Lock()):
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
            await self._reconcile(w, pr, now)
        for row in self.ledger.rows(
            "SELECT pr FROM prs WHERE tenant=? AND repo=? AND state IN (?,?)",
            (w.tenant, w.repo, PRState.OPEN, PRState.IDLE),
        ):
            if row["pr"] not in listed:
                self.budget.take(w.tenant, now)
                state = await self.reader.pr_state(w.repo, row["pr"])
                if state != "open":
                    self.scheduler.closed(PRRef(w.tenant, w.repo, row["pr"]), state == "merged", now)

    async def _reconcile(self, w: Watched, pr: OpenPR, now: datetime) -> None:
        ref = PRRef(w.tenant, w.repo, pr.number)
        row = self.ledger.one("SELECT * FROM prs WHERE tenant=? AND repo=? AND pr=?", (w.tenant, w.repo, pr.number))
        if row is None or row["state"] in (PRState.MERGED, PRState.CLOSED):  # new, or reopened
            if pr.draft and self.skip_drafts:
                return
            auto = self.auto_review == "everyone" or pr.author_association in INSIDERS
            log.info("intake: %s opened at %s%s", ref, pr.head_sha[:7],
                     "" if auto else f" by an outsider ({pr.author_association}): nothing runs unasked")  # fmt: skip
            pr_id = self.scheduler.pr_opened(ref, pr.head_sha, now, auto=auto)
        else:
            pr_id = row["id"]
            if row["current_head"] != pr.head_sha:
                log.info("intake: %s moved to %s", ref, pr.head_sha[:7])
                self.scheduler.synchronize(ref, pr.head_sha, now)
        if row is None or row["base_ref"] != pr.base_ref:
            self.ledger.execute("UPDATE prs SET base_ref=? WHERE id=?", (pr.base_ref, pr_id))
        await self._requests(w, ref, pr_id, pr, now)

    async def _requests(self, w: Watched, ref: PRRef, pr_id: int, pr: OpenPR, now: datetime) -> None:
        """Requested people: a new request adds their doppel; an un-request removes it; their own review doesn't."""
        wanted = {}
        for login in pr.requested:
            reviewer = self.directory.personal_reviewer(w.tenant, login)
            if reviewer is not None:
                wanted[reviewer.handle] = login
        known = {
            r["reviewer"]: r["person_reviewed"]
            for r in self.ledger.rows(
                "SELECT reviewer, person_reviewed FROM pr_reviewers WHERE pr_id=? AND source='requested'", (pr_id,)
            )
        }
        for handle in sorted(wanted):
            if handle not in known:
                log.info("intake: %s requested %s", ref, wanted[handle])
                self.scheduler.review_requested(ref, wanted[handle], now)
            elif known[handle]:  # requested again after reviewing: a later departure is checked again
                self._mark_reviewed(pr_id, handle, False)

        gone = [h for h, reviewed in sorted(known.items()) if h not in wanted and not reviewed]
        if not gone:
            return
        authors = await self._review_authors(w, pr.number, now)
        by_handle = {r.handle: r for r in self.directory.reviewers(w.tenant, w.repo)}
        for handle in gone:
            reviewer = by_handle.get(handle)
            if reviewer is None or not reviewer.login:
                continue
            if reviewer.login.lower() in authors:
                log.info("intake: %s: %s reviewed it themselves; their doppel stays on", ref, reviewer.login)
                self._mark_reviewed(pr_id, handle, True)
            else:
                log.info("intake: %s no longer requests %s", ref, reviewer.login)
                self.scheduler.review_request_removed(ref, reviewer.login, now)

    async def _review_authors(self, w: Watched, number: int, now: datetime) -> set[str]:
        authors: set[str] = set()
        page = 1
        while True:
            self.budget.take(w.tenant, now)
            batch = await self.reader.review_authors(w.repo, number, page)
            authors |= {login.lower() for login in batch}
            if len(batch) < PAGE:
                return authors
            page += 1

    def _mark_reviewed(self, pr_id: int, handle: str, reviewed: bool) -> None:
        self.ledger.execute(
            "UPDATE pr_reviewers SET person_reviewed=? WHERE pr_id=? AND reviewer=?", (int(reviewed), pr_id, handle)
        )

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
