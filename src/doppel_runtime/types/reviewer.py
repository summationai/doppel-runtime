from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReviewerKind(StrEnum):
    PERSONAL = "personal"  # one person's doppel: runs on its owner's sign-in, never required, only comments
    APP = "app"  # the repo's own reviewer (e.g. repo-owner): required by the merger


@dataclass(frozen=True)
class Reviewer:
    handle: str  # e.g. "steven", "repo-owner"
    kind: ReviewerKind
    owner: str | None = None  # personal: the user whose sign-in pays
    login: str | None = None  # personal: the owner's GitHub login, matched against review requests

    @property
    def required(self) -> bool:
        return self.kind is ReviewerKind.APP
