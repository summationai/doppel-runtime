from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class NotSignedIn(Exception):
    """The payer has no working sign-in or key."""


@dataclass(frozen=True)
class Credential:
    """Where a payer's model calls go, and the headers that authorize them. Never logged."""

    base_url: str  # e.g. https://chatgpt.com/backend-api/codex for a ChatGPT sign-in
    headers: dict[str, str] = field(repr=False)

    def __repr__(self) -> str:
        return f"Credential(base_url={self.base_url!r}, headers=<{len(self.headers)} hidden>)"


class Credentials(Protocol):
    """The broker, as the gateway sees it (checkpoint 3 fills in refresh and OpenBao)."""

    async def credential(self, payer: str) -> Credential:
        """The payer's current credential. Raises NotSignedIn."""
        ...

    async def refresh(self, payer: str) -> Credential:
        """Refresh after the provider refused the credential (an upstream 401). Raises NotSignedIn."""
        ...
