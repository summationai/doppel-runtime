"""In-memory stand-ins for the ports. They record every call so tests can assert on what was done."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal

from doppel_shared.ports import PAGE, CIState, InlineComment, OpenPR, PostedReview, PRComment, ReviewJob, Run
from doppel_shared.types import PRRef, Reviewer, ReviewerKind

STEVEN = Reviewer("steven", ReviewerKind.PERSONAL, owner="u-steven", login="stevenliu")
TROY = Reviewer("troy", ReviewerKind.PERSONAL, owner="u-troy", login="troyb")
REPO_OWNER = Reviewer("repo-owner", ReviewerKind.APP)


@dataclass
class FakeDirectory:
    by_repo: list[Reviewer] = field(default_factory=lambda: [STEVEN, TROY, REPO_OWNER])
    app_payers: dict[str, str | None] = field(default_factory=lambda: {"repo-owner": "key:summation"})
    signed: set[str] = field(default_factory=lambda: {"u-steven", "u-troy", "key:summation"})

    def reviewers(self, tenant: str, repo: str) -> list[Reviewer]:
        return list(self.by_repo)

    def personal_reviewer(self, tenant: str, login: str) -> Reviewer | None:
        return next((r for r in self.by_repo if r.kind is ReviewerKind.PERSONAL and r.login == login), None)

    def app_payer(self, tenant: str, repo: str, reviewer: str) -> str | None:
        return self.app_payers.get(reviewer)

    def signed_in(self, payer: str) -> bool:
        return payer in self.signed


@dataclass
class FakeSandbox:
    pods: dict[str, PRRef] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    started: list[tuple[str, ReviewJob]] = field(default_factory=list)
    stopped: list[tuple[str, int]] = field(default_factory=list)
    _n: int = 0

    def create_pod(self, pr: PRRef) -> str:
        self._n += 1
        pod_id = f"pod-{self._n}"
        self.pods[pod_id] = pr
        return pod_id

    def delete_pod(self, pod_id: str) -> None:
        self.pods.pop(pod_id, None)
        self.deleted.append(pod_id)

    def start_reviewer(self, pod_id: str, job: ReviewJob) -> None:
        assert pod_id in self.pods, f"start in a pod that doesn't exist: {pod_id}"
        self.started.append((pod_id, job))

    def stop_reviewer(self, pod_id: str, slot_id: int) -> None:
        self.stopped.append((pod_id, slot_id))

    def job(self, slot_id: int) -> ReviewJob:
        return next(job for _, job in self.started if job.slot_id == slot_id)


@dataclass
class Review:
    pr: PRRef
    head_sha: str
    event: str
    body: str
    comments: list[InlineComment]
    review_id: str


@dataclass
class FakeGitHub:
    reviews: list[Review] = field(default_factory=list)
    dismissed: list[str] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)  # comment id -> body
    status_edits: int = 0
    _n: int = 0

    def _id(self) -> str:
        self._n += 1
        return str(1000 + self._n)

    def post_review(self, pr, head_sha, event, body, comments=None) -> PostedReview:
        review_id = self._id()
        comments = comments or []
        self.reviews.append(Review(pr, head_sha, event, body, comments, review_id))
        return PostedReview(
            review_id,
            f"https://github.com/{pr.repo}/pull/{pr.number}#pullrequestreview-{review_id}",
            {c.fid: self._id() for c in comments},
        )

    def dismiss_review(self, pr, review_id, message) -> None:
        self.dismissed.append(review_id)

    def reply(self, pr, body) -> str:
        self.replies.append(body)
        return self._id()

    def upsert_status(self, pr, comment_id, body) -> str:
        comment_id = comment_id or self._id()
        self.statuses[comment_id] = body
        self.status_edits += 1
        return comment_id

    def events(self, *, merger_only: bool = False) -> list[str]:
        """Review events in order; with merger_only, just the merger's formal reviews."""
        return [r.event for r in self.reviews if not merger_only or r.body.startswith("Doppelwerk merger")]

    @property
    def status(self) -> str:
        return next(iter(self.statuses.values()), "")


def verdict(reviewer: str, head: str, kind: str = "clean", findings: list[dict] | None = None) -> dict:
    return {
        "doppel": reviewer,
        "pr": 42,
        "head_sha": head,
        "engine": "gpt-5.5",
        "verdict": kind,
        "summary": f"{reviewer} looked.",
        "findings": findings or [],
        "scope": {"files_reviewed": 3},
    }


def finding(fid: str, severity: str, *, introduced: bool = True, line: int = 88) -> dict:
    return {
        "id": fid,
        "severity": severity,
        "category": "correctness",
        "file": "sender.py",
        "line": line,
        "introduced_by_diff": introduced,
        "claim": "the backoff never resets between deliveries",
        "evidence": "sender.py:80-95",
    }


@dataclass
class FakeRuns:
    """The `Runs` port in memory: running slots by capability hash, tokens and usage resets recorded."""

    by_hash: dict[str, Run] = field(default_factory=dict)
    tokens: dict[int, int] = field(default_factory=dict)
    resets: dict[str, datetime] = field(default_factory=dict)
    errors: dict[int, tuple] = field(default_factory=dict)

    def running(self, capability_hash: str):
        run = self.by_hash.get(capability_hash)
        if run is None:
            return None
        return replace(run, tokens_used=run.tokens_used + self.tokens.get(run.slot_id, 0))

    def add_tokens(self, slot_id: int, tokens: int) -> None:
        self.tokens[slot_id] = self.tokens.get(slot_id, 0) + tokens

    def record_usage_reset(self, payer: str, reset_at: datetime) -> None:
        self.resets[payer] = reset_at

    def provider_error(self, slot_id: int, status: int, code: str | None, kind: str | None) -> None:
        self.errors[slot_id] = (status, code, kind)


@dataclass
class FakeGitHubHost:
    """GitHub's read side for intake: repos with open PRs, comments and CI. Counts every request."""

    prs: dict[str, dict[int, OpenPR]] = field(default_factory=dict)  # repo -> number -> open PR
    finished: dict[tuple[str, int], Literal["merged", "closed"]] = field(default_factory=dict)
    pr_comments: dict[str, list[PRComment]] = field(default_factory=dict)
    checks: dict[str, CIState] = field(default_factory=dict)  # sha -> state
    reviews: dict[tuple[str, int], list[str]] = field(default_factory=dict)  # (repo, pr) -> review authors
    calls: list[str] = field(default_factory=list)

    # ---- driving it from a test ----

    def open(self, repo: str, number: int, head: str, *, author: str = "troyb", requested=(), draft=False) -> None:
        self.prs.setdefault(repo, {})[number] = OpenPR(number, head, "main", author, frozenset(requested), draft)

    def push(self, repo: str, number: int, head: str) -> None:
        self.prs[repo][number] = replace(self.prs[repo][number], head_sha=head)

    def request(self, repo: str, number: int, *logins: str) -> None:
        pr = self.prs[repo][number]
        self.prs[repo][number] = replace(pr, requested=pr.requested | set(logins))

    def unrequest(self, repo: str, number: int, *logins: str) -> None:
        pr = self.prs[repo][number]
        self.prs[repo][number] = replace(pr, requested=pr.requested - set(logins))

    def human_review(self, repo: str, number: int, login: str) -> None:
        """A person submits a review: as GitHub does, they leave the requested list."""
        self.reviews.setdefault((repo, number), []).append(login)
        self.unrequest(repo, number, login)

    def close(self, repo: str, number: int, *, merged: bool = True) -> None:
        self.prs[repo].pop(number)
        self.finished[(repo, number)] = "merged" if merged else "closed"

    def comment(self, repo: str, number: int, cid: str, body: str, at: datetime, *,
                author: str = "stevenliu", association: str = "MEMBER") -> None:  # fmt: skip
        self.pr_comments.setdefault(repo, []).append(PRComment(cid, number, author, association, body, at))

    # ---- the GitHubReader port ----

    async def open_prs(self, repo: str, page: int = 1) -> list[OpenPR]:
        self.calls.append(f"open_prs {repo} {page}")
        prs = sorted(self.prs.get(repo, {}).values(), key=lambda p: p.number)
        return prs[(page - 1) * PAGE : page * PAGE]

    async def pr_state(self, repo: str, number: int):
        self.calls.append(f"pr_state {repo}#{number}")
        return "open" if number in self.prs.get(repo, {}) else self.finished.get((repo, number), "closed")

    async def comments(self, repo: str, since: datetime, page: int = 1) -> list[PRComment]:
        self.calls.append(f"comments {repo} {page}")
        found = sorted((c for c in self.pr_comments.get(repo, []) if c.updated_at >= since), key=lambda c: c.updated_at)
        return found[(page - 1) * PAGE : page * PAGE]

    async def review_authors(self, repo: str, number: int, page: int = 1) -> list[str]:
        self.calls.append(f"reviews {repo}#{number} {page}")
        return self.reviews.get((repo, number), [])[(page - 1) * PAGE : page * PAGE]

    async def ci_state(self, repo: str, sha: str) -> CIState | None:
        self.calls.append(f"ci {repo} {sha}")
        return self.checks.get(sha)
