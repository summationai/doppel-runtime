"""The `Directory` port read from the config file, until the BFF's records arrive (checkpoint 8).

Whether a payer can pay right now comes from the broker: a `connected` sign-in.
"""

from __future__ import annotations

from collections.abc import Callable

from doppel_shared.types import Reviewer, ReviewerKind

from doppel_control_plane.config import Tenant


class ConfigDirectory:
    def __init__(self, tenants: tuple[Tenant, ...], signed_in: Callable[[str], bool]):
        self._tenants = {t.id: t for t in tenants}
        self._signed_in = signed_in

    def reviewers(self, tenant: str, repo: str) -> list[Reviewer]:
        t = self._tenants.get(tenant)
        if t is None or repo not in t.repos:
            return []
        return [r.reviewer for r in t.reviewers if not r.repos or repo in r.repos]

    def personal_reviewer(self, tenant: str, login: str) -> Reviewer | None:
        t = self._tenants.get(tenant)
        return next(
            (r.reviewer for r in (t.reviewers if t else ())
             if r.reviewer.kind is ReviewerKind.PERSONAL and (r.reviewer.login or "").lower() == login.lower()),
            None,
        )  # fmt: skip

    def app_payer(self, tenant: str, repo: str, reviewer: str) -> str | None:
        t = self._tenants.get(tenant)
        return next((r.payer for r in (t.reviewers if t else ()) if r.reviewer.handle == reviewer), None)

    def signed_in(self, payer: str) -> bool:
        try:
            return self._signed_in(payer)
        except Exception:  # a bad payer id, or an unreadable home: can't pay
            return False
