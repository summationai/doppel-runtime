from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from doppel_runtime.types import PRRef


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
