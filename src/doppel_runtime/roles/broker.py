"""The broker: each payer's model sign-in, kept in their own Codex home.

Checkpoint 4 needs only the read path: a payer's home -> its `auth.json` -> a credential the gateway
can use. Checkpoint 3 adds device-code sign-in, refresh under a per-payer lock, and the durable copy
in OpenBao. Tokens are never printed or logged.
"""

from __future__ import annotations

import json
from pathlib import Path

from doppel_runtime.ports.credentials import Credential, NotSignedIn

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"  # a ChatGPT sign-in's model endpoint
OPENAI_BASE = "https://api.openai.com/v1"  # an API key's


class CodexHomes:
    """A Codex home per payer: `<homes_dir>/<payer>/auth.json`, or an explicit home for a payer."""

    def __init__(self, homes_dir: Path, overrides: dict[str, Path] | None = None):
        self.homes_dir = homes_dir
        self.overrides = dict(overrides or {})

    def home(self, payer: str) -> Path:
        if payer in self.overrides:
            return self.overrides[payer]
        if "/" in payer or payer.startswith("."):
            raise NotSignedIn(f"not a payer id: {payer!r}")
        return self.homes_dir / payer

    def read(self, payer: str) -> Credential:
        path = self.home(payer) / "auth.json"
        try:
            auth = json.loads(path.read_text())
        except (OSError, ValueError) as e:
            raise NotSignedIn(f"{payer}: no readable sign-in") from e
        tokens = auth.get("tokens") or {}
        if tokens.get("access_token"):
            headers = {"authorization": f"Bearer {tokens['access_token']}"}
            if tokens.get("account_id"):
                headers["chatgpt-account-id"] = tokens["account_id"]
            return Credential(CHATGPT_BASE, headers)
        if auth.get("OPENAI_API_KEY"):
            return Credential(OPENAI_BASE, {"authorization": f"Bearer {auth['OPENAI_API_KEY']}"})
        raise NotSignedIn(f"{payer}: signed out")

    async def credential(self, payer: str) -> Credential:
        return self.read(payer)

    async def refresh(self, payer: str) -> Credential:
        # Checkpoint 3: Codex runs the refresh-token flow under a per-payer lock. Until then, re-read:
        # Codex may have refreshed the file since.
        return self.read(payer)
