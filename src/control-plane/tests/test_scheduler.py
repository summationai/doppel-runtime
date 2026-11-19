"""The scheduler against a real ledger and fake ports. One PR, #42, unless a test says otherwise.

Cast: Steven and Troy have personal reviewers (`steven`, `troy`) paid by their own sign-ins;
`repo-owner` is the app's reviewer, paid by the tenant's key and required by the merger.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from doppel_control_plane.core.matching import Limits
from doppel_control_plane.scheduler import Scheduler
from doppel_control_plane.store import Ledger
from doppel_shared.testing import FakeDirectory, FakeGitHub, FakeSandbox, finding, verdict
from doppel_shared.types import PRRef

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
PR = PRRef("summation", "summationai/sandbox", 42)


def at(seconds: float = 0, **kw) -> datetime:
    return T0 + timedelta(seconds=seconds, **kw)


class World:
    def __init__(self, tmp_path):
        self.ledger = Ledger(tmp_path / "ledger.db")
        self.directory = FakeDirectory()
        self.sandbox = FakeSandbox()
        self.github = FakeGitHub()
        self.s = Scheduler(self.ledger, self.directory, self.sandbox, self.github, Limits())

    def slots(self, **where) -> list:
        sql = "SELECT * FROM slots"
        if where:
            sql += " WHERE " + " AND ".join(f"{k}=?" for k in where)
        return self.ledger.rows(sql + " ORDER BY id", tuple(where.values()))

    def slot(self, reviewer: str, head: str = "aaa111", command_id: str = ""):
        rows = self.slots(reviewer=reviewer, head_sha=head, command_id=command_id)
        assert len(rows) == 1, rows
        return rows[0]

    def pr(self):
        return self.ledger.one("SELECT * FROM prs WHERE pr=42")

    def open_with_steven(self, now=T0):
        """#42 opened at aaa111, Troy requested Steven's review."""
        self.s.pr_opened(PR, "aaa111", now)
        self.s.review_requested(PR, "stevenliu", now)

    def finish(
        self, reviewer: str, head: str = "aaa111", kind: str = "clean", findings=None, now=None, command_id: str = ""
    ):
        slot = self.slot(reviewer, head, command_id)
        return self.s.verdict(slot["id"], verdict(reviewer, head, kind, findings), now or at(200))


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.ledger.close()


# ---- queuing and payers (DSV-4) ---------------------------------------------------------------------


def test_opening_queues_the_app_reviewer_and_a_request_queues_the_personal_one(w):
    w.open_with_steven()
    assert [(s["reviewer"], s["reason"], s["payer_user"]) for s in w.slots()] == [
        ("repo-owner", "app", "key:summation"),
        ("steven", "review_requested", "u-steven"),
    ]


def test_dsv_4_payer_never_changes_when_the_owner_is_capped_or_out_of_usage(w):
    w.open_with_steven()
    w.s.usage_limit("u-steven", at(hours=2))
    w.s.tick(at(100))
    steven = w.slot("steven")
    assert (steven["state"], steven["payer_user"], steven["waiting_on"]) == ("queued", "u-steven", "usage_window")
    assert all(job.reviewer != "steven" for _, job in w.sandbox.started)


def test_a_request_for_someone_without_a_personal_reviewer_queues_nothing(w):
    w.s.pr_opened(PR, "aaa111", T0)
    assert w.s.review_requested(PR, "someone-else", T0) is None
    assert [s["reviewer"] for s in w.slots()] == ["repo-owner"]


# ---- the settle window, caps, sign-ins ----------------------------------------------------------------


def test_reviews_wait_out_the_settle_window_then_start_in_one_pod(w):
    w.open_with_steven()
    assert w.s.tick(at(30)) == []
    assert w.slot("steven")["waiting_on"] == "settle"
    started = w.s.tick(at(91))
    assert len(started) == 2
    assert {pod for pod, _ in w.sandbox.started} == {"pod-1"}
    assert w.pr()["pod_id"] == "pod-1"


def test_reviewers_start_without_waiting_for_ci(w):
    w.open_with_steven()
    assert len(w.s.tick(at(91))) == 2
    assert w.ledger.value("SELECT count(*) FROM ci_status") == 0


def test_dsv_12_a_payers_third_slot_waits_for_the_cap(w):
    w.directory.by_repo = [*w.directory.by_repo]
    w.open_with_steven()
    w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk review steven first", at(1))
    w.s.command(PR, "c2", "troyb", "MEMBER", "@doppelwerk review steven second", at(2))
    w.s.tick(at(91))
    steven = w.slots(reviewer="steven")
    assert [s["state"] for s in steven] == ["running", "running", "queued"]  # oldest first
    assert steven[2]["waiting_on"] == "payer_cap"


def test_dsv_12_usage_reset_holds_until_the_reset_and_says_so_once(w):
    w.open_with_steven()
    w.s.usage_limit("u-steven", at(hours=1))
    w.s.tick(at(100))
    w.s.tick(at(200))
    assert w.github.replies == ["steven's review is out of capacity until 2026-11-20T11:00:00+00:00."]
    w.s.tick(at(hours=1, seconds=1))
    assert w.slot("steven")["state"] == "running"


def test_host_cap(w):
    w.s.limits = Limits(host_cap=1)
    w.open_with_steven()
    w.s.tick(at(91))
    assert [s["state"] for s in w.slots()] == ["running", "queued"]
    assert w.slot("steven")["waiting_on"] == "host_cap"


def test_no_sign_in_blocks_says_so_once_and_starts_once_connected(w):
    w.directory.signed.discard("u-steven")
    w.open_with_steven()
    w.s.tick(at(91))
    w.s.tick(at(120))
    assert w.slot("steven")["state"] == "blocked"
    assert w.github.replies == ["steven's review is waiting: u-steven has no working sign-in. Connect Codex to run it."]
    w.directory.signed.add("u-steven")
    w.s.tick(at(130))
    assert w.slot("steven")["state"] == "running"


def test_only_the_capability_hash_is_stored(w):
    w.open_with_steven()
    w.s.tick(at(91))
    slot = w.slot("steven")
    job = w.sandbox.job(slot["id"])
    assert len(job.capability) >= 40
    assert slot["capability_hash"] == hashlib.sha256(job.capability.encode()).hexdigest()
    assert job.capability not in repr(job)
    assert w.ledger.value("SELECT count(*) FROM slots WHERE capability_hash = ?", (job.capability,)) == 0


# ---- pushes and closing (DSV-10) ---------------------------------------------------------------------


def test_dsv_10_a_push_stops_old_reviewers_and_queues_the_new_head_in_the_same_pod(w):
    w.open_with_steven()
    w.s.tick(at(91))
    running = {s["id"] for s in w.slots(state="running")}
    w.s.synchronize(PR, "bbb222", at(120))
    assert {slot_id for _, slot_id in w.sandbox.stopped} == running
    assert {s["state"] for s in w.slots(head_sha="aaa111")} == {"cancelled"}
    assert [s["reviewer"] for s in w.slots(head_sha="bbb222")] == ["repo-owner", "steven"]
    w.s.tick(at(120 + 91))
    assert {pod for pod, _ in w.sandbox.started} == {"pod-1"}
    assert w.sandbox.deleted == []


def test_a_second_push_resets_the_settle_window(w):
    w.open_with_steven()
    w.s.synchronize(PR, "bbb222", at(60))
    assert w.s.tick(at(100)) == []  # 40 s after the second push
    assert len(w.s.tick(at(151))) == 2


def test_dsv_10_closing_deletes_the_pod_and_keeps_the_history(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.finish("steven")
    w.s.closed(PR, merged=True, now=at(300))
    assert w.sandbox.deleted == ["pod-1"]
    assert w.pr()["state"] == "merged" and w.pr()["pod_id"] is None
    assert w.slot("steven")["state"] == "posted"  # DSV-14
    assert w.ledger.value("SELECT count(*) FROM posts") == 1


def test_a_verdict_for_a_stale_head_is_never_posted(w):
    w.open_with_steven()
    w.s.tick(at(91))
    slot_id = w.slot("steven")["id"]
    w.ledger.execute("UPDATE prs SET current_head='bbb222'")  # the head moved under a running reviewer
    assert w.s.verdict(slot_id, verdict("steven", "aaa111"), at(200)) == "stale"
    assert w.github.events() == []


# ---- reviewers post; the merger decides (DSV-11) -------------------------------------------------------


def test_reviewers_only_comment(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.finish("steven", kind="changes_requested", findings=[finding("F1", "P1")])
    review = w.github.reviews[0]
    assert review.event == "COMMENT" and review.body.startswith("**steven**")
    assert [c.fid for c in review.comments] == ["F1"]
    assert w.ledger.value("SELECT comment_id FROM finding_anchors WHERE fid='F1'") is not None


def test_dsv_11_approves_once_the_required_reviewer_is_done_and_ci_is_green(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.finish("repo-owner")
    assert w.github.events(merger_only=True) == []  # waiting on CI
    w.s.ci(PR, "aaa111", "success", at(300))
    assert w.github.events(merger_only=True) == ["APPROVE"]  # steven still running: not required


def test_dsv_11_a_personal_reviewers_p1_blocks_with_a_comment(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.s.ci(PR, "aaa111", "success", at(100))
    w.finish("steven", kind="changes_requested", findings=[finding("F1", "P1")])
    w.finish("repo-owner")
    assert w.github.events(merger_only=True) == ["COMMENT"]
    assert w.ledger.value("SELECT reason FROM merges") == "1 open P1 from steven"


def test_dsv_11_a_person_resolving_the_p1_unblocks_with_no_model_run(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.s.ci(PR, "aaa111", "success", at(100))
    w.finish("steven", kind="changes_requested", findings=[finding("F1", "P1")])
    w.finish("repo-owner")
    starts = len(w.sandbox.started)
    w.s.disposition(PR, "steven", "F1", "resolved", "troyb", at(400))
    assert w.github.events(merger_only=True) == ["COMMENT", "APPROVE"]
    assert len(w.sandbox.started) == starts


def test_a_finding_raised_again_after_its_disposition_is_open_again(w):
    w.open_with_steven()
    w.s.ci(PR, "aaa111", "success", at(100))
    w.s.disposition(PR, "steven", "F1", "resolved", "troyb", at(150))
    w.s.tick(at(91))
    w.finish("repo-owner")
    w.finish("steven", kind="changes_requested", findings=[finding("F1", "P1")], now=at(300))
    assert w.ledger.value("SELECT decision FROM merges") == "blocked"


def test_a_pre_existing_p1_is_advisory(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.s.ci(PR, "aaa111", "success", at(100))
    w.finish("steven", kind="changes_requested", findings=[finding("F1", "P1", introduced=False)])
    w.finish("repo-owner")
    assert w.github.events(merger_only=True) == ["APPROVE"]


def test_dsv_11_red_ci_blocks(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.finish("repo-owner")
    w.s.ci(PR, "aaa111", "failure", at(300))
    assert w.ledger.value("SELECT decision FROM merges") == "blocked"


def test_dsv_11_a_push_dismisses_the_approval(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.finish("repo-owner")
    w.s.ci(PR, "aaa111", "success", at(300))
    approval = w.github.reviews[-1].review_id
    w.s.synchronize(PR, "bbb222", at(400))
    assert w.github.dismissed == [approval]
    assert w.ledger.value("SELECT decision FROM merges WHERE head_sha='aaa111'") == "dismissed"
    assert w.ledger.value("SELECT decision FROM merges WHERE head_sha='bbb222'") == "waiting_reviews"


def test_the_merger_posts_only_when_its_decision_changes(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.finish("repo-owner")
    w.s.ci(PR, "aaa111", "success", at(300))
    for i in range(3):
        w.s.ci(PR, "aaa111", "success", at(310 + i))
        w.s.merge(w.pr()["id"], at(320 + i))
    assert w.github.events(merger_only=True) == ["APPROVE"]
    assert len(w.github.statuses) == 1  # one status comment, edited in place


def test_the_status_comment_shows_each_reviewer(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.finish("repo-owner")
    assert w.github.status == "**waiting ci** · repo-owner ✓ · steven running · CI not started"


# ---- commands ----------------------------------------------------------------------------------------


def test_a_review_command_records_and_queues_one_slot_that_skips_the_settle_window(w):
    w.open_with_steven()
    state = w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk review steven look at the retry cap", at(5))
    assert state == "running"
    cmd = w.ledger.one("SELECT * FROM commands")
    assert (cmd["command"], cmd["targets"], cmd["description"]) == ("review", "steven", "look at the retry cap")
    slot = w.slot("steven", command_id="c1")
    assert slot["reason"] == "command"
    assert w.s.tick(at(10)) == [slot["id"]]  # the automatic slots are still settling
    assert w.sandbox.job(slot["id"]).focus == "look at the retry cap"


def test_a_command_is_acted_on_once(w):
    w.open_with_steven()
    w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk review steven", at(5))
    w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk review steven", at(6))
    assert len(w.slots(command_id="c1")) == 1


def test_a_command_is_responded_once_every_target_answered(w):
    w.open_with_steven()
    w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk review", at(5))  # every reviewer on the PR
    w.s.tick(at(10))
    w.finish("steven", command_id="c1")
    assert w.ledger.value("SELECT state FROM commands") == "running"
    w.finish("repo-owner", command_id="c1")
    assert w.ledger.value("SELECT state FROM commands") == "responded"


@pytest.mark.parametrize("body", ["@doppelwerk steven", "@doppelwerk"])
def test_doppelwerks_forms_mean_review(w, body):
    w.open_with_steven()
    assert w.s.command(PR, "c1", "troyb", "MEMBER", body, at(5)) == "running"


def test_an_unknown_command_is_refused_with_a_reply(w):
    w.open_with_steven()
    assert w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk deploy it", at(5)) == "refused"
    assert w.github.replies[-1].startswith("Unknown command `deploy`.")
    assert w.slots(command_id="c1") == []


def test_help_replies(w):
    w.open_with_steven()
    assert w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk help", at(5)) == "responded"
    assert w.github.replies[-1].startswith("Commands:")


def test_outsiders_and_bots_cant_command(w):
    w.open_with_steven()
    assert w.s.command(PR, "c1", "rando", "NONE", "@doppelwerk review", at(5)) is None
    assert w.s.command(PR, "c2", "doppelwerk[bot]", "MEMBER", "@doppelwerk review", at(5)) is None
    assert w.ledger.value("SELECT count(*) FROM commands") == 0


def test_dsv_4_troy_commanding_stevens_reviewer_runs_on_stevens_sign_in(w):
    w.open_with_steven()
    w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk review steven", at(5))
    assert w.slot("steven", command_id="c1")["payer_user"] == "u-steven"


def test_dsv_4_a_command_for_someone_not_requested_is_refused(w):
    w.s.pr_opened(PR, "aaa111", T0)  # nobody requested Steven
    assert w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk review steven", at(5)) == "refused"
    assert w.github.replies[-1] == "steven isn't requested on this PR. Request their review first."
    assert w.slots(reviewer="steven") == []


def test_command_slots_follow_the_head(w):
    w.open_with_steven()
    w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk review steven", at(5))
    w.s.synchronize(PR, "bbb222", at(20))
    assert w.slot("steven", "aaa111", "c1")["state"] == "cancelled"
    assert w.slot("steven", "bbb222", "c1")["state"] == "queued"


def test_a_failing_command_review_retries_then_replies(w):
    w.open_with_steven()
    w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk review steven", at(5))
    slot_id = w.slot("steven", command_id="c1")["id"]
    for i in range(3):
        w.s.tick(at(10 + i))
        result = w.s.slot_failed(slot_id, "codex crashed", at(11 + i))
    assert result == "failed"
    assert w.github.replies[-1] == "steven's review failed after 3 attempts."
    assert w.ledger.value("SELECT response FROM command_responses") == "failed: codex crashed"


# ---- idle pods ---------------------------------------------------------------------------------------


def test_a_pr_idle_for_3_days_loses_its_pod_and_a_command_brings_it_back(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.finish("steven")
    w.finish("repo-owner")
    w.s.tick(at(days=2))
    assert w.pr()["pod_id"] == "pod-1"
    w.s.tick(at(days=3, seconds=300))
    assert w.sandbox.deleted == ["pod-1"] and w.pr()["state"] == "idle"

    w.s.command(PR, "c1", "troyb", "MEMBER", "@doppelwerk review steven", at(days=4))
    w.s.tick(at(days=4, seconds=1))
    assert w.pr()["state"] == "open" and w.pr()["pod_id"] == "pod-2"
    assert w.sandbox.started[-1][1].head_sha == "aaa111"


def test_dsv_11_red_ci_after_approval_withdraws_it_on_the_same_head(w):
    """RC's review #1: a COMMENT doesn't cancel an APPROVE on GitHub, so the merger must dismiss its own."""
    w.open_with_steven()
    w.s.tick(at(91))
    w.finish("repo-owner")
    w.s.ci(PR, "aaa111", "success", at(300))
    approval = w.github.reviews[-1].review_id
    w.s.ci(PR, "aaa111", "failure", at(400))  # a re-run goes red; no push
    assert w.github.dismissed == [approval]
    assert w.github.events(merger_only=True) == ["APPROVE", "COMMENT"]
    assert w.ledger.value("SELECT decision FROM merges") == "blocked"


def test_dsv_11_a_late_p1_after_approval_withdraws_it(w):
    w.open_with_steven()
    w.s.tick(at(91))
    w.finish("repo-owner")
    w.s.ci(PR, "aaa111", "success", at(300))
    approval = w.github.reviews[-1].review_id
    w.finish("steven", kind="changes_requested", findings=[finding("F1", "P1")], now=at(400))
    assert w.github.dismissed == [approval]
    assert w.ledger.value("SELECT decision FROM merges") == "blocked"
