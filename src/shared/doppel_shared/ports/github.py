from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from doppel_shared.types import PRRef


@dataclass(frozen=True)
class InlineComment:
    fid: str
    path: str
    line: int
    body: str


@dataclass(frozen=True)
class PostedReview:
    review_id: str
    url: str
    comment_ids: dict[str, str] = field(default_factory=dict)  # finding id -> its inline comment's id


class GitHub(Protocol):
    """Writes go through the gateway's posting route, as the tenant's installation (DSV-2, DSV-6)."""

    def post_review(
        self,
        pr: PRRef,
        head_sha: str,
        event: Literal["COMMENT", "APPROVE"],
        body: str,
        comments: list[InlineComment] | None = None,
    ) -> PostedReview: ...

    def dismiss_review(self, pr: PRRef, review_id: str, message: str) -> None: ...

    def reply(self, pr: PRRef, body: str) -> str:
        """A plain PR comment; returns its id."""
        ...

    def upsert_status(self, pr: PRRef, comment_id: str | None, body: str) -> str:
        """Create the merger's status comment, or edit it in place; returns its id."""
        ...


# ---- reading: what intake polls (review #8: repo listings, never per-person searches) ----------------

PAGE = 100  # GitHub's largest page


@dataclass(frozen=True)
class OpenPR:
    """One open PR as GitHub's PR listing shows it."""

    number: int
    head_sha: str
    base_ref: str
    author: str
    requested: frozenset[str] = frozenset()  # logins whose review is requested (GitHub drops one who reviews)
    draft: bool = False


@dataclass(frozen=True)
class PRComment:
    """A comment on a PR's conversation, from the repo-wide comment listing."""

    comment_id: str
    pr: int
    author: str
    association: str  # OWNER | MEMBER | COLLABORATOR | CONTRIBUTOR | NONE | ...
    body: str
    updated_at: datetime


CIState = Literal["pending", "success", "failure"]


class GitHubReader(Protocol):
    """GitHub as intake reads it, as the tenant's installation. Each method is exactly one request,
    so the poller can count every call against the installation's budget."""

    async def open_prs(self, repo: str, page: int = 1) -> list[OpenPR]:
        """`GET /repos/{repo}/pulls?state=open&per_page=100&page=N`: up to PAGE PRs."""
        ...

    async def pr_state(self, repo: str, number: int) -> Literal["open", "merged", "closed"]:
        """`GET /repos/{repo}/pulls/{number}`: for a PR that left the open listing."""
        ...

    async def comments(self, repo: str, since: datetime, page: int = 1) -> list[PRComment]:
        """`GET /repos/{repo}/issues/comments?since=...&sort=updated&direction=asc`: PR comments only."""
        ...

    async def review_authors(self, repo: str, number: int, page: int = 1) -> list[str]:
        """`GET /repos/{repo}/pulls/{number}/reviews?per_page=100&page=N`: who submitted each review (logins).

        Read only when a requested login leaves the listing, to tell "they reviewed" from "un-requested"."""
        ...

    async def ci_state(self, repo: str, sha: str) -> CIState | None:
        """`GET /repos/{repo}/commits/{sha}/check-runs`: None while nothing has reported."""
        ...
