"""In-memory stand-ins for the ports. They record every call so tests can assert on what was done."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from doppel_shared.ports import InlineComment, PostedReview, ReviewJob, Run
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

    def running(self, capability_hash: str):
        run = self.by_hash.get(capability_hash)
        if run is None:
            return None
        return replace(run, tokens_used=run.tokens_used + self.tokens.get(run.slot_id, 0))

    def add_tokens(self, slot_id: int, tokens: int) -> None:
        self.tokens[slot_id] = self.tokens.get(slot_id, 0) + tokens

    def record_usage_reset(self, payer: str, reset_at: datetime) -> None:
        self.resets[payer] = reset_at
