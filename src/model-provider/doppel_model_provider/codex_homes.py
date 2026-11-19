"""A Codex home per payer: `<homes_dir>/<payer>/auth.json`, or an explicit home for a payer.

The read path the gateway uses (a payer's credential), plus the file handling the broker needs
(the sign-in as a bundle, written back atomically with owner-only permissions). Tokens are never
printed or logged.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from doppel_shared.ports.credentials import Credential, NotSignedIn

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"  # a ChatGPT sign-in's model endpoint
OPENAI_BASE = "https://api.openai.com/v1"  # an API key's
PAYER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,63}$")


def check_payer(payer: str) -> str:
    if not PAYER.match(payer) or ".." in payer:
        raise NotSignedIn(f"not a payer id: {payer!r}")
    return payer


def last_refresh(bundle: str | None) -> datetime | None:
    """When Codex last refreshed this sign-in (it writes `last_refresh` into auth.json)."""
    try:
        value = json.loads(bundle or "").get("last_refresh")
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (ValueError, AttributeError):
        return None


class CodexHomes:
    def __init__(self, homes_dir: Path, overrides: dict[str, Path] | None = None):
        self.homes_dir = homes_dir
        self.overrides = dict(overrides or {})

    def home(self, payer: str) -> Path:
        if payer in self.overrides:
            return self.overrides[payer]
        return self.homes_dir / check_payer(payer)

    def payers(self) -> list[str]:
        """Every payer with a Codex home on disk."""
        found = set(self.overrides)
        if self.homes_dir.is_dir():
            found |= {p.name for p in self.homes_dir.iterdir() if p.is_dir() and PAYER.match(p.name)}
        return sorted(found)

    def bundle(self, payer: str) -> str | None:
        """The sign-in exactly as Codex wrote it, or None."""
        try:
            return (self.home(payer) / "auth.json").read_text()
        except OSError:
            return None

    def write(self, payer: str, bundle: str) -> None:
        """Put a sign-in back on disk (restored from OpenBao): owner-only, replaced atomically."""
        home = self.home(payer)
        home.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = home / "auth.json.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(bundle)
        os.replace(tmp, home / "auth.json")

    def read(self, payer: str) -> Credential:
        bundle = self.bundle(payer)
        try:
            auth = json.loads(bundle) if bundle else None
        except ValueError:
            auth = None
        if not auth:
            raise NotSignedIn(f"{payer}: no readable sign-in")
        tokens = auth.get("tokens") or {}
        if tokens.get("access_token"):
            headers = {"authorization": f"Bearer {tokens['access_token']}"}
            if tokens.get("account_id"):
                headers["chatgpt-account-id"] = tokens["account_id"]
            return Credential(CHATGPT_BASE, headers)
        if auth.get("OPENAI_API_KEY"):
            return Credential(OPENAI_BASE, {"authorization": f"Bearer {auth['OPENAI_API_KEY']}"})
        raise NotSignedIn(f"{payer}: signed out")

    def signed_in(self, payer: str) -> bool:
        try:
            self.read(payer)
            return True
        except NotSignedIn:
            return False

    async def credential(self, payer: str) -> Credential:
        return self.read(payer)

    async def refresh(self, payer: str) -> Credential:
        """Without a broker, nothing refreshes: re-read, in case Codex refreshed the file itself."""
        return self.read(payer)
