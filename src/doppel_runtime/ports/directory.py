from __future__ import annotations

from typing import Protocol

from doppel_runtime.types import Reviewer


class Directory(Protocol):
    """Records owned by the BFF (DSV-1), plus the broker's view of who can pay."""

    def reviewers(self, tenant: str, repo: str) -> list[Reviewer]:
        """Every reviewer that may run on this repo: the app's, and each member's personal one."""
        ...

    def personal_reviewer(self, tenant: str, login: str) -> Reviewer | None:
        """The personal reviewer of the member with this GitHub login, if they have one."""
        ...

    def app_payer(self, tenant: str, repo: str, reviewer: str) -> str | None:
        """Who pays for an app reviewer: the tenant's key, or an owner who opted in. None: nobody."""
        ...

    def signed_in(self, payer: str) -> bool:
        """Whether this payer has a working sign-in (or key) right now."""
        ...
