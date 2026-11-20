"""Intake by polling: GitHub's listings, diffed against the ledger, become the scheduler's events."""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from doppel_control_plane.core.matching import Limits
from doppel_control_plane.intake import Budget, OutOfBudget, Poller, Watched
from doppel_control_plane.scheduler import Scheduler
from doppel_control_plane.store import Ledger
from doppel_shared.testing import FakeDirectory, FakeGitHub, FakeGitHubHost, FakeSandbox, finding, verdict

T0 = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
REPO = "summationai/billing"
H1, H2 = "aaa1111", "bbb2222"


class World:
    def __init__(self, tmp_path, *, per_hour: int = 1000, repos=(REPO,)):
        self.ledger = Ledger(tmp_path / "ledger.db")
        self.host = FakeGitHubHost()
        self.github = FakeGitHub()
        self.sandbox = FakeSandbox()
        self.directory = FakeDirectory()
        self.scheduler = Scheduler(self.ledger, self.directory, self.sandbox, self.github, Limits(settle=timedelta(0)))
        self.budget = Budget(per_hour)
        self.poller = Poller(self.ledger, self.scheduler, self.host, self.directory,
                             [Watched("summation", r) for r in repos], self.budget)  # fmt: skip

    async def poll(self, at: datetime = T0) -> None:
        await self.poller.poll(at)

    def slots(self) -> list[tuple[str, str, str]]:
        return [(r["reviewer"], r["head_sha"], r["state"]) for r in self.ledger.rows("SELECT * FROM slots ORDER BY id")]

    def pr(self, number: int = 42):
        return self.ledger.one("SELECT * FROM prs WHERE repo=? AND pr=?", (REPO, number))


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


async def test_a_new_pr_brings_its_app_reviewer_and_each_requested_person(w):
    w.host.open(REPO, 42, H1, requested=["stevenliu", "someone-without-a-doppel"])
    await w.poll()
    assert w.pr()["current_head"] == H1
    assert w.slots() == [("repo-owner", H1, "queued"), ("steven", H1, "queued")]
    await w.poll(T0 + timedelta(minutes=1))  # nothing changed: nothing new
    assert len(w.slots()) == 2


async def test_a_push_moves_every_reviewer_to_the_new_head(w):
    w.host.open(REPO, 42, H1, requested=["stevenliu"])
    await w.poll()
    w.scheduler.tick(T0)
    w.host.push(REPO, 42, H2)
    await w.poll(T0 + timedelta(minutes=1))
    assert w.pr()["current_head"] == H2
    assert sorted(w.slots()) == [
        ("repo-owner", H1, "cancelled"), ("repo-owner", H2, "queued"),
        ("steven", H1, "cancelled"), ("steven", H2, "queued"),
    ]  # fmt: skip
    assert len(w.sandbox.stopped) == 2  # the old head's running reviewers were stopped


async def test_a_request_added_later_or_removed(w):
    w.host.open(REPO, 42, H1)
    await w.poll()
    assert w.slots() == [("repo-owner", H1, "queued")]
    w.host.request(REPO, 42, "stevenliu")
    await w.poll(T0 + timedelta(minutes=1))
    assert ("steven", H1, "queued") in w.slots()
    w.host.unrequest(REPO, 42, "stevenliu")
    await w.poll(T0 + timedelta(minutes=2))
    assert ("steven", H1, "cancelled") in w.slots()
    assert w.ledger.rows("SELECT * FROM pr_reviewers WHERE source='requested'") == []


def slot_of(w: World, reviewer: str, head: str) -> int:
    return w.ledger.value("SELECT id FROM slots WHERE reviewer=? AND head_sha=?", (reviewer, head))


async def test_a_persons_own_review_leaves_their_doppel_on_the_pr(w):
    """Steven's doppel posts as doppelwerk, not as Steven: his own review changes nothing for it."""
    w.host.open(REPO, 42, H1, requested=["stevenliu"])
    await w.poll()
    w.scheduler.tick(T0)
    w.host.human_review(REPO, 42, "stevenliu")  # GitHub drops him from the requested list
    w.host.calls.clear()
    await w.poll(T0 + timedelta(minutes=1))
    assert ("steven", H1, "running") in w.slots()  # not stopped
    assert w.sandbox.stopped == []
    assert f"reviews {REPO}#42 1" in w.host.calls  # one read, to tell a review from an un-request
    w.host.calls.clear()
    await w.poll(T0 + timedelta(minutes=2))
    assert not any(c.startswith("reviews") for c in w.host.calls)  # once, not every cycle

    w.host.push(REPO, 42, H2)  # and it reviews every new head
    await w.poll(T0 + timedelta(minutes=3))
    assert ("steven", H2, "queued") in w.slots()


async def test_requested_again_after_reviewing_then_un_requested_removes_the_doppel(w):
    w.host.open(REPO, 42, H1, requested=["stevenliu"])
    await w.poll()
    w.host.human_review(REPO, 42, "stevenliu")
    await w.poll(T0 + timedelta(minutes=1))
    w.host.request(REPO, 42, "stevenliu")
    await w.poll(T0 + timedelta(minutes=2))
    w.host.reviews.clear()  # (as if his earlier review were older than the new request)
    w.host.unrequest(REPO, 42, "stevenliu")
    await w.poll(T0 + timedelta(minutes=3))
    assert ("steven", H1, "cancelled") in w.slots()


async def test_a_doppels_p1_keeps_blocking_after_the_person_reviews_or_is_un_requested(w):
    """The hole found in review: removing a doppel must never clear the P1 it already posted."""
    w.host.open(REPO, 42, H1, requested=["stevenliu"])
    await w.poll()
    w.scheduler.tick(T0)
    w.scheduler.verdict(slot_of(w, "repo-owner", H1), verdict("repo-owner", H1), T0)
    w.scheduler.verdict(slot_of(w, "steven", H1),
                        verdict("steven", H1, "changes_requested", [finding("F1", "P1")]), T0)  # fmt: skip
    w.host.checks[H1] = "success"
    await w.poll(T0 + timedelta(minutes=1))
    assert w.ledger.value("SELECT decision FROM merges") == "blocked"

    w.host.human_review(REPO, 42, "stevenliu")  # Steven leaves a quick review himself
    await w.poll(T0 + timedelta(minutes=2))
    assert w.ledger.value("SELECT decision FROM merges") == "blocked"

    w.host.reviews.clear()
    w.host.request(REPO, 42, "stevenliu")
    await w.poll(T0 + timedelta(minutes=3))
    w.host.unrequest(REPO, 42, "stevenliu")  # a real un-request: the doppel leaves, its P1 doesn't
    await w.poll(T0 + timedelta(minutes=4))
    assert w.ledger.rows("SELECT * FROM pr_reviewers WHERE source='requested'") == []
    assert w.ledger.value("SELECT decision FROM merges") == "blocked"
    assert "APPROVE" not in w.github.events(merger_only=True)


async def test_a_pr_that_leaves_the_listing_is_looked_up_once_and_closed(w):
    w.host.open(REPO, 42, H1)
    await w.poll()
    w.scheduler.tick(T0)  # starts the app reviewer: the PR gets a pod
    assert w.pr()["pod_id"]
    w.host.close(REPO, 42, merged=True)
    await w.poll(T0 + timedelta(minutes=1))
    assert w.pr()["state"] == "merged" and w.pr()["pod_id"] is None
    assert w.sandbox.deleted == ["pod-1"]
    w.host.calls.clear()
    await w.poll(T0 + timedelta(minutes=2))
    assert not any(c.startswith("pr_state") for c in w.host.calls)  # merged PRs aren't looked up again


async def test_drafts_wait_until_they_are_ready(w):
    w.host.open(REPO, 42, H1, draft=True)
    await w.poll()
    assert w.pr() is None
    w.host.open(REPO, 42, H1, draft=False)
    await w.poll(T0 + timedelta(minutes=1))
    assert w.slots() == [("repo-owner", H1, "queued")]


async def test_a_command_is_acted_on_once_and_the_cursor_survives_a_restart(w, tmp_path):
    w.host.open(REPO, 42, H1, requested=["stevenliu"])
    w.host.comment(REPO, 42, "c1", "@doppelwerk review steven the refund path", T0 + timedelta(seconds=5))
    await w.poll(T0 + timedelta(seconds=10))
    assert w.ledger.value("SELECT state FROM commands WHERE comment_id='c1'") == "running"
    commands = [s for s in w.ledger.rows("SELECT * FROM slots WHERE reason='command'")]
    assert len(commands) == 1
    await w.poll(T0 + timedelta(minutes=1))  # GitHub's `since` is inclusive: c1 comes back, and is ignored
    assert len(w.ledger.rows("SELECT * FROM slots WHERE reason='command'")) == 1
    since = w.ledger.value("SELECT comments_since FROM intake_cursors WHERE repo=?", (REPO,))
    assert since.startswith("2026-11-20T12:00:05")


async def test_the_first_poll_reads_comments_only_an_hour_back(w):
    w.host.open(REPO, 42, H1)
    w.host.comment(REPO, 42, "old", "@doppelwerk help", T0 - timedelta(hours=3))
    await w.poll()
    assert w.ledger.rows("SELECT * FROM commands") == []


async def test_ci_is_read_only_while_the_merger_waits_for_it_and_then_it_approves(w):
    w.host.open(REPO, 42, H1)
    await w.poll()
    assert not any(c.startswith("ci ") for c in w.host.calls)  # the merger waits on reviews, not CI
    w.scheduler.tick(T0)
    w.scheduler.verdict(w.sandbox.started[0][1].slot_id, verdict("repo-owner", H1), T0)
    assert w.ledger.value("SELECT decision FROM merges") == "waiting_ci"

    w.host.calls.clear()
    await w.poll(T0 + timedelta(minutes=1))
    assert f"ci {REPO} {H1}" in w.host.calls  # nothing reported yet: still waiting
    assert w.ledger.value("SELECT decision FROM merges") == "waiting_ci"
    w.host.checks[H1] = "success"
    await w.poll(T0 + timedelta(minutes=2))
    assert w.ledger.value("SELECT decision FROM merges") == "approved"
    assert w.github.events(merger_only=True) == ["APPROVE"]
    w.host.calls.clear()
    await w.poll(T0 + timedelta(minutes=3))
    assert not any(c.startswith("ci ") for c in w.host.calls)


async def test_listings_are_paged(w):
    for n in range(1, 151):
        w.host.open(REPO, n, f"{n:07x}")
    await w.poll()
    assert [c for c in w.host.calls if c.startswith("open_prs")] == [f"open_prs {REPO} 1", f"open_prs {REPO} 2"]
    assert w.ledger.value("SELECT count(*) FROM prs") == 150


# ---- the budget (review #8) ------------------------------------------------------------------------


def test_budget_is_a_sliding_hour_per_installation():
    b = Budget(2)
    b.take("summation", T0)
    b.take("summation", T0 + timedelta(minutes=10))
    with pytest.raises(OutOfBudget):
        b.take("summation", T0 + timedelta(minutes=20))
    b.take("other", T0 + timedelta(minutes=20))  # another installation has its own
    assert b.left("summation", T0 + timedelta(minutes=61)) == 1  # the first call has aged out


async def test_a_spent_budget_drops_ci_then_commands_never_prs(tmp_path, caplog):
    w = World(tmp_path, per_hour=3, repos=(REPO, "summationai/other"))
    w.host.open(REPO, 42, H1)
    w.host.open("summationai/other", 7, H2)
    with caplog.at_level(logging.WARNING):
        await w.poll()
    # Both repos' PR listings first (2 calls), then one comment listing; the second repo's comments wait.
    assert w.host.calls == [f"open_prs {REPO} 1", "open_prs summationai/other 1", f"comments {REPO} 1"]
    assert w.ledger.value("SELECT count(*) FROM prs") == 2
    assert sum("budget of 3 calls/hour is spent" in r.message for r in caplog.records) == 1
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await w.poll(T0 + timedelta(minutes=1))  # still spent: said once, not every cycle
    assert not any("spent" in r.message for r in caplog.records)
    w.host.calls.clear()
    await w.poll(T0 + timedelta(minutes=61))  # an hour on, the calls have aged out
    assert w.host.calls[:2] == [f"open_prs {REPO} 1", "open_prs summationai/other 1"]


async def test_one_repo_failing_never_stops_the_others(w, tmp_path, caplog):
    w = World(tmp_path, repos=("summationai/broken", REPO))
    real = w.host.open_prs

    async def flaky(repo, page=1):
        if repo == "summationai/broken":
            raise ConnectionError("GitHub 502")
        return await real(repo, page)

    w.host.open_prs = flaky
    w.host.open(REPO, 42, H1)
    with caplog.at_level(logging.ERROR):
        await w.poll()
    assert w.pr()["current_head"] == H1
    assert any("prs pass failed on summationai/broken" in r.message for r in caplog.records)


# ---- outsiders' PRs (checkpoint 6b) ----------------------------------------------------------------


async def test_an_outsiders_pr_runs_nothing_unasked(w):
    w.host.open(REPO, 42, H1, association="NONE")  # e.g. a fork PR from a stranger
    await w.poll()
    assert w.pr() is not None and w.slots() == []
    w.host.push(REPO, 42, H2)
    await w.poll(T0 + timedelta(minutes=1))
    assert w.slots() == []  # a push doesn't add the app's reviewers either


async def test_an_outsiders_pr_runs_what_an_insider_asks_for(w):
    w.host.open(REPO, 42, H1, association="CONTRIBUTOR", requested=["stevenliu"])  # requested by a maintainer
    w.host.comment(REPO, 42, "c1", "@doppelwerk review repo-owner", T0 + timedelta(seconds=1))
    w.host.comment(REPO, 42, "c2", "@doppelwerk review repo-owner", T0 + timedelta(seconds=2),
                   author="stranger", association="NONE")  # fmt: skip
    await w.poll(T0 + timedelta(seconds=5))
    assert sorted((r, s) for r, _, s in w.slots()) == [("repo-owner", "queued"), ("steven", "queued")]
    assert w.ledger.value("SELECT count(*) FROM commands") == 1  # the stranger's command isn't one


async def test_everyone_mode_reviews_outsiders_too(tmp_path):
    w = World(tmp_path)
    w.poller.auto_review = "everyone"
    w.host.open(REPO, 42, H1, association="NONE")
    await w.poll()
    assert w.slots() == [("repo-owner", H1, "queued")]


async def test_the_prs_base_reaches_the_reviewer_job(w):
    w.host.open(REPO, 42, H1, base="release/1.2")
    await w.poll()
    w.scheduler.tick(T0)
    assert w.sandbox.started[0][1].base_ref == "release/1.2"
