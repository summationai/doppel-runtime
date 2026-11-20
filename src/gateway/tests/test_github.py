"""The App's REST ports against a fake GitHub API: tokens, reads, posts, and how they fail."""

import subprocess
import time
from datetime import UTC, datetime, timedelta

import pytest
from doppel_gateway.github import AppAuth, GitHubClient, GitHubError, RateLimited, RestGitHub, RestReader
from doppel_gateway.testing import FakeGitHubAPI
from doppel_shared.ports import InlineComment
from doppel_shared.types import PRRef

REPO = "summationai/billing"
PR = PRRef("summation", REPO, 42)


@pytest.fixture
def api():
    fake = FakeGitHubAPI(installations={REPO: 77, "acme/web": 88}).start()
    yield fake
    fake.stop()


@pytest.fixture
def client(api):
    auth = AppAuth(api.app_id, api.private_key_pem, api.url)
    return GitHubClient(auth, {REPO: 77, "acme/web": 88})


# ---- tokens ----------------------------------------------------------------------------------------


def test_one_token_per_installation_reused_until_near_expiry(api, client):
    now = [time.time()]  # a real time: GitHub refuses a JWT whose times are off
    client.auth.clock = lambda: now[0]
    api.token_lifetime = 3600
    assert client.auth.token(77) == client.auth.token(77)
    client.auth.token(88)
    assert api.tokens_issued == 2  # one each, not one per request
    client.auth._tokens[77] = (client.auth._tokens[77][0], now[0] + 200)  # 200 s left: under the 5 minutes
    client.auth.token(77)
    assert api.tokens_issued == 3


def test_a_wrong_key_or_app_id_gets_no_token(api):
    other = FakeGitHubAPI(fresh_key=True)  # a different key pair
    with pytest.raises(GitHubError, match="401"):
        AppAuth(api.app_id, other.private_key_pem, api.url).token(77)
    with pytest.raises(GitHubError, match="401"):
        AppAuth("9999", api.private_key_pem, api.url).token(77)


def test_a_revoked_token_is_replaced_once(api, client):
    client.request("GET", REPO, "/pulls")
    api.revoke_next = True
    client.request("GET", REPO, "/pulls")  # 401, a new token, the same request again
    assert api.tokens_issued == 2


def test_a_repo_outside_the_installation_is_refused(api, client):
    client.installations["summationai/billing"] = 88  # the wrong installation for this repo
    with pytest.raises(GitHubError, match="404"):
        client.request("GET", REPO, "/pulls")
    with pytest.raises(GitHubError, match="no installation"):
        client.request("GET", "someone/else", "/pulls")
    with pytest.raises(ValueError):
        client.installation("../etc")


def test_a_spent_rate_limit_says_when_it_resets(api, client):
    api.rate_remaining = 0
    with pytest.raises(RateLimited) as e:
        client.request("GET", REPO, "/pulls")
    assert e.value.reset_at and e.value.reset_at > datetime.now(UTC).timestamp()


def test_no_token_or_key_in_reprs(api, client):
    token = client.auth.token(77)
    assert token not in repr(client.auth) and "PRIVATE" not in repr(client.auth)


# ---- reads -------------------------------------------------------------------------------------------


async def test_reads_map_githubs_answers(api, client):
    r = RestReader(client)
    api.open(REPO, 42, "aaa1111", requested=["StevenLiu"], association="CONTRIBUTOR", base="release")
    api.open(REPO, 43, "bbb2222", draft=True)
    prs = await r.open_prs(REPO)
    assert [(p.number, p.head_sha, p.base_ref, p.requested, p.author_association, p.draft) for p in prs] == [
        (42, "aaa1111", "release", frozenset({"stevenliu"}), "CONTRIBUTOR", False),
        (43, "bbb2222", "main", frozenset(), "MEMBER", True),
    ]
    assert await r.pr_state(REPO, 42) == "open"
    api.merge(REPO, 42)
    assert await r.pr_state(REPO, 42) == "merged"
    api.prs[REPO][43]["state"] = "closed"
    assert await r.pr_state(REPO, 43) == "closed"


async def test_comments_are_pr_comments_only(api, client):
    r = RestReader(client)
    api.comment(REPO, 42, "@doppelwerk review")
    api.comment(REPO, 7, "@doppelwerk on an issue", on_issue=True)
    found = await r.comments(REPO, datetime.now(UTC) - timedelta(minutes=5))
    assert [(c.pr, c.body, c.association) for c in found] == [(42, "@doppelwerk review", "MEMBER")]


async def test_ci_from_check_runs(api, client):
    r = RestReader(client)
    assert await r.ci_state(REPO, "aaa1111") is None  # nothing reported yet
    api.check("aaa1111", "completed", "success")
    api.check("aaa1111", "in_progress", None)
    assert await r.ci_state(REPO, "aaa1111") == "pending"
    api.checks["aaa1111"][1].update(status="completed", conclusion="skipped")
    assert await r.ci_state(REPO, "aaa1111") == "success"
    api.check("aaa1111", "completed", "timed_out")
    assert await r.ci_state(REPO, "aaa1111") == "failure"


async def test_review_authors(api, client):
    api.reviews[(REPO, 42)] = [{"id": 1, "user": {"login": "StevenLiu"}, "state": "COMMENTED"}]
    assert await RestReader(client).review_authors(REPO, 42) == ["StevenLiu"]


# ---- posts ---------------------------------------------------------------------------------------


def test_a_review_with_inline_comments_maps_each_finding_to_its_comment(api, client):
    gh = RestGitHub(client)
    api.open(REPO, 42, "aaa1111")
    api.diff_lines[(REPO, 42)] = {("billing.py", 7), ("billing.py", 16)}
    posted = gh.post_review(PR, "aaa1111", "COMMENT", "steven's doppel: 2 findings", [
        InlineComment("F1", "billing.py", 7, "P1: negative charge"),
        InlineComment("F2", "billing.py", 16, "P1: refund"),
    ])  # fmt: skip
    [review] = api.reviews[(REPO, 42)]
    assert (posted.review_id, review["commit_id"], review["state"]) == (str(review["id"]), "aaa1111", "COMMENTED")
    assert set(posted.comment_ids) == {"F1", "F2"} and len(set(posted.comment_ids.values())) == 2
    assert posted.url.endswith(f"#pullrequestreview-{review['id']}")


def test_a_comment_outside_the_diff_moves_into_the_body_instead_of_failing(api, client):
    api.open(REPO, 42, "aaa1111")
    api.diff_lines[(REPO, 42)] = {("billing.py", 7)}
    posted = RestGitHub(client).post_review(PR, "aaa1111", "COMMENT", "summary", [
        InlineComment("F1", "billing.py", 7, "on the diff"), InlineComment("F2", "other.py", 99, "not on the diff"),
    ])  # fmt: skip
    [review] = api.reviews[(REPO, 42)]
    assert "`other.py:99`: not on the diff" in review["body"] and "`billing.py:7`" in review["body"]
    assert posted.comment_ids == {}


def test_approve_dismiss_reply_and_the_status_comment(api, client):
    gh = RestGitHub(client)
    api.open(REPO, 42, "aaa1111")
    approved = gh.post_review(PR, "aaa1111", "APPROVE", "Doppelwerk merger: approved")
    gh.dismiss_review(PR, approved.review_id, "New commits were pushed; reviewing again.")
    assert api.reviews[(REPO, 42)][0]["state"] == "DISMISSED"

    reply = gh.reply(PR, "steven's review is waiting")
    status = gh.upsert_status(PR, None, "**waiting ci**")
    assert status != reply
    assert gh.upsert_status(PR, status, "**approved**") == status  # edited in place
    bodies = {str(c["id"]): c["body"] for c in api.comments[REPO]}
    assert bodies[status] == "**approved**"
    api.comments[REPO] = [c for c in api.comments[REPO] if str(c["id"]) != status]  # someone deleted it
    again = gh.upsert_status(PR, status, "**blocked**")
    assert again != status and {str(c["id"]): c["body"] for c in api.comments[REPO]}[again] == "**blocked**"


# ---- git ---------------------------------------------------------------------------------------------


def test_git_gets_the_token_through_its_environment_only(api, client, tmp_path):
    env = client.git_env(REPO)
    token = client.auth.token(77)
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert token not in env["GIT_CONFIG_VALUE_0"]  # base64 of x-access-token:<token>, not the token itself
    seen = subprocess.run(["git", "config", "--get", "http.https://github.com/.extraheader"],
                          env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", **env},
                          capture_output=True, text=True, cwd=tmp_path)  # fmt: skip
    assert seen.stdout.startswith("AUTHORIZATION: basic ")
