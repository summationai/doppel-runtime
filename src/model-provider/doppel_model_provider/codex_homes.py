"""Each payer's Codex home, on the runtime's disk: `<root>/<tenant>/<user>/codex/auth.json`.

A payer id is `<tenant>/<user>`, e.g. `summation/u-steven` (a tenant's API key can be
`summation/tenant-key`). Folders are owner-only (0700) and Codex writes auth.json owner-only (0600).
For now this disk is the only copy of a sign-in; a durable copy elsewhere comes later.

The read path the gateway uses (a payer's credential), plus what the broker needs (the sign-in as
Codex wrote it, every payer on disk). Tokens are never printed or logged.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from doppel_shared.ports.credentials import Credential, NotSignedIn

CHATGPT_BASE = "https://chatgpt.com/backend-api/codex"  # a ChatGPT sign-in's model endpoint
OPENAI_BASE = "https://api.openai.com/v1"  # an API key's
SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def split_payer(payer: str) -> tuple[str, str]:
    """`<tenant>/<user>` -> (tenant, user). Anything else, or a segment that could leave the folder, is refused."""
    parts = payer.split("/")
    if len(parts) != 2 or not all(SEGMENT.match(p) and ".." not in p for p in parts):
        raise NotSignedIn(f"not a payer id (want <tenant>/<user>): {payer!r}")
    return parts[0], parts[1]


def last_refresh(bundle: str | None) -> datetime | None:
    """When Codex last refreshed this sign-in (it writes `last_refresh` into auth.json)."""
    try:
        value = json.loads(bundle or "").get("last_refresh")
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except (ValueError, AttributeError):
        return None


class CodexHomes:
    def __init__(self, root: Path, overrides: dict[str, Path] | None = None):
        self.root = root  # <data_dir>/tenants
        self.overrides = dict(overrides or {})  # payer -> an existing Codex home (e.g. a dev's ~/.codex)

    def home(self, payer: str) -> Path:
        if payer in self.overrides:
            return self.overrides[payer]
        tenant, user = split_payer(payer)
        return self.root / tenant / user / "codex"

    def make_home(self, payer: str) -> Path:
        """Create the payer's home, owner-only all the way down from the tenants folder."""
        home = self.home(payer)
        if payer not in self.overrides:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)  # the data folder may not exist yet
            for folder in (home.parent.parent, home.parent, home):
                folder.mkdir(mode=0o700, exist_ok=True)
        return home

    def payers(self) -> list[str]:
        """Every payer with a Codex home on disk."""
        found = set(self.overrides)
        if self.root.is_dir():
            for home in self.root.glob("*/*/codex"):
                tenant, user = home.parent.parent.name, home.parent.name
                if SEGMENT.match(tenant) and SEGMENT.match(user):
                    found.add(f"{tenant}/{user}")
        return sorted(found)

    def bundle(self, payer: str) -> str | None:
        """The sign-in exactly as Codex wrote it, or None."""
        try:
            return (self.home(payer) / "auth.json").read_text()
        except OSError:
            return None

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

    def rejected(self, payer: str, reason: str) -> None:
        """Without a broker there is no status to keep."""
