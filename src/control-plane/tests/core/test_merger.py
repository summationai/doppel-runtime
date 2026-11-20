from datetime import UTC, datetime

import pytest
from doppel_control_plane.core.merger import decide, review_body, status_text
from doppel_shared.types import MergeInput, MergeState, OpenFinding, ReviewerStatus

UNTIL = datetime(2026, 11, 20, 12, 1, tzinfo=UTC)


def app(name="repo-owner", status="posted", verdict="clean"):
    return ReviewerStatus(name, app=True, required=True, status=status, verdict=verdict)


def person(name="steven", status="posted", verdict="comments", required=True):
    """A requested person's reviewer; `required=False` once its grace period has run out."""
    return ReviewerStatus(name, app=False, required=required, status=status, verdict=verdict, wait_until=UNTIL)


P1 = OpenFinding("troy", "F1", "P1", "sender.py", 88, "the backoff never resets")


def inp(reviewers=None, ci="success", blocking=()):
    reviewers = (app(), person()) if reviewers is None else reviewers
    return MergeInput("bbb222", tuple(reviewers), ci, tuple(blocking))


@pytest.mark.parametrize(
    ("merge_input", "state"),
    [
        (inp(), MergeState.APPROVED),
        (inp(reviewers=(app(status="running", verdict=None), person())), MergeState.WAITING_REVIEWS),
        # a requested person is waited for, within the grace period
        (inp(reviewers=(app(), person("troy", "running", None))), MergeState.WAITING_REVIEWS),
        (inp(reviewers=(app(), person("troy", "blocked", None))), MergeState.WAITING_REVIEWS),
        # once the grace period is over, not any more
        (inp(reviewers=(app(), person("troy", "running", None, required=False))), MergeState.APPROVED),
        # no app reviewer: never approved, however many people reviewed
        (inp(reviewers=(person(),)), MergeState.WAITING_REVIEWS),
        (inp(reviewers=()), MergeState.WAITING_REVIEWS),
        (inp(reviewers=(app(verdict="stood_down"),)), MergeState.BLOCKED),
        (inp(reviewers=(app(), person(verdict="stood_down"))), MergeState.APPROVED),  # only an app's stand-down blocks
        (inp(ci=None), MergeState.WAITING_CI),
        (inp(ci="pending"), MergeState.WAITING_CI),
        (inp(ci="failure"), MergeState.BLOCKED),
        (inp(blocking=(P1,)), MergeState.BLOCKED),  # a personal reviewer's P1 blocks
        # order: reviews before CI before findings
        (
            inp(reviewers=(app(status="queued", verdict=None),), ci="failure", blocking=(P1,)),
            MergeState.WAITING_REVIEWS,
        ),
        (inp(ci="pending", blocking=(P1,)), MergeState.WAITING_CI),
    ],
)
def test_decide(merge_input, state):
    assert decide(merge_input).state is state


def test_blocked_reason_names_the_findings():
    assert decide(inp(blocking=(P1,))).reason == "1 open P1 from troy"


def test_waiting_reason_names_who():
    assert decide(inp(reviewers=(app(), person("troy", "running", None)))).reason == "waiting on troy"


def test_status_text_shows_everyone_and_how_long_people_are_waited_for():
    merge_input = inp(reviewers=(app(), person("troy", "running", None)), ci="pending")
    text = status_text(merge_input, decide(merge_input))
    assert text == "**waiting reviews** · repo-owner ✓ · troy running (waited for until 12:01 UTC) · CI pending"


def test_status_text_after_the_grace_period():
    merge_input = inp(reviewers=(app(), person("troy", "running", None, required=False)))
    assert "troy running (not waited for)" in status_text(merge_input, decide(merge_input))


def test_review_body_lists_blocking_findings():
    merge_input = inp(blocking=(P1,))
    body = review_body(merge_input, decide(merge_input))
    assert body.startswith("Doppelwerk merger: **blocked**")
    assert "- P1 from troy: the backoff never resets (`sender.py:88`)" in body
