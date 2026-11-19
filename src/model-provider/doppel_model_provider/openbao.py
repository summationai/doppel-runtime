"""The durable copy of each payer's sign-in, in OpenBao (KV v2): `<mount>/users/<payer>/codex`.

DSV-13: each payer has their own path and their own policy (`doppel-user-<payer>`), which reaches
that path and nothing else. The broker's own token only manages those policies and mints tokens
from them; every read or write of a sign-in uses a short-lived token scoped to that one payer, then
revokes it. A bug that asked for the wrong payer's path with a scoped token would be refused.

In production the broker's token should create scoped tokens through a token role whose
`allowed_policies_glob` is `doppel-user-*` (pass `token_role`); in a dev server the root token can.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

import aiohttp

log = logging.getLogger(__name__)


class StoreError(Exception):
    pass


class LoginStore(Protocol):
    async def load(self, payer: str) -> str | None: ...  # None: nothing saved
    async def save(self, payer: str, bundle: str) -> None: ...
    async def delete(self, payer: str) -> None: ...
    async def payers(self) -> list[str]: ...


def policy_name(payer: str) -> str:
    return "doppel-user-" + re.sub(r"[^A-Za-z0-9_-]", "-", payer)


class OpenBaoStore:
    def __init__(
        self, addr: str, token: str, *, mount: str = "kv", prefix: str = "users", token_role: str | None = None
    ):
        self.base = addr.rstrip("/") + "/v1"
        self._token = token
        self.mount, self.prefix, self.token_role = mount, prefix, token_role
        self._policies: set[str] = set()
        self._session: aiohttp.ClientSession | None = None

    def path(self, payer: str) -> str:
        return f"{self.prefix}/{payer}/codex"

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def _call(self, method: str, path: str, token: str, body: dict | None = None) -> tuple[int, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        try:
            async with self._session.request(
                method, f"{self.base}/{path}", headers={"X-Vault-Token": token}, json=body
            ) as r:
                data = await r.json(content_type=None) if r.content_length != 0 else None
                return r.status, data
        except (aiohttp.ClientError, TimeoutError) as e:
            raise StoreError(f"OpenBao unreachable: {e!r}") from e

    # ---- scoped tokens -------------------------------------------------------------------------------

    async def _ensure_policy(self, payer: str) -> str:
        name = policy_name(payer)
        if name in self._policies:
            return name
        data, meta = f"{self.mount}/data/{self.prefix}/{payer}/*", f"{self.mount}/metadata/{self.prefix}/{payer}/*"
        policy = (
            f'path "{data}" {{ capabilities = ["create", "read", "update"] }}\n'
            f'path "{meta}" {{ capabilities = ["read", "delete"] }}\n'
        )
        status, _ = await self._call("PUT", f"sys/policies/acl/{name}", self._token, {"policy": policy})
        if status not in (200, 204):
            raise StoreError(f"writing policy {name}: OpenBao answered {status}")
        self._policies.add(name)
        return name

    async def scoped_token(self, payer: str) -> str:
        """A 5-minute token that can reach this payer's path and nothing else."""
        name = await self._ensure_policy(payer)
        path = f"auth/token/create/{self.token_role}" if self.token_role else "auth/token/create"
        status, data = await self._call(
            "POST", path, self._token, {"policies": [name], "ttl": "5m", "no_default_policy": True, "renewable": False}
        )
        if status != 200:
            raise StoreError(f"minting a token for {payer}: OpenBao answered {status}")
        return data["auth"]["client_token"]

    async def _as(self, payer: str, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
        token = await self.scoped_token(payer)
        try:
            return await self._call(method, path, token, body)
        finally:
            await self.revoke(token)

    async def revoke(self, token: str) -> None:
        """Revoke a scoped token with the broker's own token (a scoped token has no default policy,
        so it can't revoke itself). It would expire in 5 minutes anyway."""
        status, _ = await self._call("POST", "auth/token/revoke", self._token, {"token": token})
        if status not in (200, 204):
            log.warning("couldn't revoke a scoped token: OpenBao answered %s", status)

    # ---- the store -----------------------------------------------------------------------------------

    async def load(self, payer: str) -> str | None:
        status, data = await self._as(payer, "GET", f"{self.mount}/data/{self.path(payer)}")
        if status == 404:
            return None
        if status != 200:
            raise StoreError(f"reading {payer}'s sign-in: OpenBao answered {status}")
        try:
            return data["data"]["data"]["value"]
        except (KeyError, TypeError) as e:
            raise StoreError(f"{payer}'s saved sign-in has no field 'value'") from e

    async def save(self, payer: str, bundle: str) -> None:
        status, _ = await self._as(payer, "POST", f"{self.mount}/data/{self.path(payer)}", {"data": {"value": bundle}})
        if status not in (200, 204):
            raise StoreError(f"saving {payer}'s sign-in: OpenBao answered {status}")

    async def delete(self, payer: str) -> None:
        status, _ = await self._as(payer, "DELETE", f"{self.mount}/metadata/{self.path(payer)}")
        if status not in (200, 204, 404):
            raise StoreError(f"deleting {payer}'s sign-in: OpenBao answered {status}")

    async def payers(self) -> list[str]:
        """Every payer with a saved sign-in (the broker's own token lists the folder names, not the values)."""
        status, data = await self._call("LIST", f"{self.mount}/metadata/{self.prefix}", self._token)
        if status == 404:
            return []
        if status != 200:
            raise StoreError(f"listing saved sign-ins: OpenBao answered {status}")
        return sorted(k.rstrip("/") for k in data["data"]["keys"])
