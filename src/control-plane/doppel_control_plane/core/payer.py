"""Whose sign-in pays for a reviewer (spec section 5, "Who pays"; DSV-4).

A personal reviewer runs on its owner's sign-in, whoever triggered it. An app reviewer runs on the
payer the app's rules name (the tenant's API key, or an owner who opted in). Never anyone else:
there is no fallback onto another person's credential.
"""

from __future__ import annotations

from doppel_shared.types import Reviewer, ReviewerKind


def payer_for(reviewer: Reviewer, app_payer: str | None) -> str | None:
    if reviewer.kind is ReviewerKind.PERSONAL:
        return reviewer.owner
    return app_payer
