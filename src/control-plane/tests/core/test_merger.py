import pytest
from doppel_control_plane.core.merger import decide, review_body, status_text
from doppel_shared.types import MergeInput, MergeState, OpenFinding, ReviewerStatus

OWNER_DONE = ReviewerStatus("repo-owner", True, "posted", "clean")
STEVEN_DONE = ReviewerStatus("steven", False, "posted", "comments")
P1 = OpenFinding("troy", "F1", "P1", "sender.py", 88, "the backoff never resets")


def inp(reviewers=(OWNER_DONE, STEVEN_DONE), ci="success", blocking=()):
    return MergeInput("bbb222", tuple(reviewers), ci, tuple(blocking))


@pytest.mark.parametrize(
    ("merge_input", "state"),
    [
        (inp(), MergeState.APPROVED),
        (inp(reviewers=(ReviewerStatus("repo-owner", True, "running"), STEVEN_DONE)), MergeState.WAITING_REVIEWS),
        (inp(reviewers=(STEVEN_DONE,)), MergeState.WAITING_REVIEWS),  # no required reviewer: never approved
        (inp(reviewers=()), MergeState.WAITING_REVIEWS),
        (inp(reviewers=(ReviewerStatus("repo-owner", True, "posted", "stood_down"),)), MergeState.BLOCKED),
        (inp(reviewers=(OWNER_DONE, ReviewerStatus("steven", False, "posted", "stood_down"))), MergeState.APPROVED),
        (inp(ci=None), MergeState.WAITING_CI),
        (inp(ci="pending"), MergeState.WAITING_CI),
        (inp(ci="failure"), MergeState.BLOCKED),
        (inp(blocking=(P1,)), MergeState.BLOCKED),  # a personal reviewer's P1 blocks
        # a personal reviewer still running doesn't hold the PR up
        (inp(reviewers=(OWNER_DONE, ReviewerStatus("troy", False, "running"))), MergeState.APPROVED),
        # order: reviews before CI before findings
        (
            inp(reviewers=(ReviewerStatus("repo-owner", True, "queued"),), ci="failure", blocking=(P1,)),
            MergeState.WAITING_REVIEWS,
        ),
        (inp(ci="pending", blocking=(P1,)), MergeState.WAITING_CI),
    ],
)
def test_decide(merge_input, state):
    assert decide(merge_input).state is state


def test_blocked_reason_names_the_findings():
    assert decide(inp(blocking=(P1,))).reason == "1 open P1 from troy"


def test_status_text_shows_everyone():
    merge_input = inp(reviewers=(OWNER_DONE, ReviewerStatus("troy", False, "running")), ci="pending")
    text = status_text(merge_input, decide(merge_input))
    assert text == "**waiting ci** · repo-owner ✓ · troy running · CI pending"


def test_review_body_lists_blocking_findings():
    merge_input = inp(blocking=(P1,))
    body = review_body(merge_input, decide(merge_input))
    assert body.startswith("Doppelwerk merger: **blocked**")
    assert "- P1 from troy: the backoff never resets (`sender.py:88`)" in body
