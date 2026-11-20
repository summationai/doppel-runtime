"""The broker: every payer's model sign-in, kept alive (spec section 7).

    credential()  the payer's current credential, for the gateway
    refresh()     Codex refreshes it, one at a time per payer; callers who waited behind a refresh
                  reuse it (refresh tokens rotate, so two refreshers would break the sign-in)
    keep_fresh()  every 10 minutes: refresh any sign-in whose own `last_refresh` is 6 hours old, so
                  the refresh token stays in use whatever restarts happen
    login()       a device-code sign-in in the payer's own Codex home
    rejected()    the provider refused the sign-in even after a refresh: dead until the person signs in again
    status()      connected | signing_in | signed_out | expired, for the BFF

Sign-ins live in each payer's Codex home on this disk (codex_homes). Tokens are never printed or logged.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from doppel_shared.ports.credentials import Credential, NotSignedIn

from doppel_model_provider.codex_account import Account
from doppel_model_provider.codex_homes import CodexHomes, last_refresh, split_payer

log = logging.getLogger(__name__)

EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class DeviceCode:
    verification_url: str
    user_code: str


class Broker:
    def __init__(
        self,
        homes: CodexHomes,
        account: Account,
        *,
        refresh_every: timedelta = timedelta(hours=6),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.homes = homes
        self.account = account
        self.refresh_every = refresh_every
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._refreshed_at: dict[str, float] = {}  # monotonic time of each payer's last refresh
        self._errors: dict[str, str] = {}
        self._logins: dict[str, tuple[DeviceCode, asyncio.Task]] = {}
        self._rejected: dict[str, str] = {}  # payer -> the provider's status and code, until they sign in again

    # ---- the gateway's port --------------------------------------------------------------------------

    async def credential(self, payer: str) -> Credential:
        if payer in self._rejected:  # don't keep sending a credential the provider has refused
            raise NotSignedIn(f"{payer}: refused by the provider ({self._rejected[payer]}): reconnect Codex")
        return self.homes.read(payer)

    def rejected(self, payer: str, reason: str) -> None:
        self._rejected[payer] = reason
        self._errors[payer] = f"refused by the provider: {reason}"
        log.error("payer %s: sign-in refused by the provider (%s); reconnect needed", payer, reason)

    async def refresh(self, payer: str) -> Credential:
        asked = time.monotonic()
        self.homes.home(payer)  # refuses a bad payer id before taking a lock for it
        async with self._locks[payer]:
            if self._refreshed_at.get(payer, 0) <= asked:  # nobody refreshed while we waited
                if self.homes.bundle(payer) is None:
                    raise NotSignedIn(f"{payer}: no sign-in to refresh")
                before = last_refresh(self.homes.bundle(payer))
                try:
                    await self.account.refresh(self.homes.home(payer))
                except Exception as e:
                    self._errors[payer] = f"refresh failed: {type(e).__name__}"
                    log.error("payer %s: refresh failed (%s)", payer, type(e).__name__)
                    raise NotSignedIn(f"{payer}: refresh failed") from e
                self._refreshed_at[payer] = time.monotonic()
                self._errors.pop(payer, None)
                after = last_refresh(self.homes.bundle(payer))
                log.info("payer %s: refreshed (last_refresh %s -> %s)", payer, before, after)
        return self.homes.read(payer)

    # ---- staying fresh -------------------------------------------------------------------------------

    def refresh_due(self, payer: str) -> bool:
        bundle = self.homes.bundle(payer)
        if bundle is None or not self.homes.signed_in(payer):
            return False
        return self.clock() - (last_refresh(bundle) or EPOCH) >= self.refresh_every

    async def keep_fresh_once(self) -> None:
        for payer in self.homes.payers():
            try:
                if self.refresh_due(payer):
                    await self.refresh(payer)
            except NotSignedIn:
                pass  # recorded in status; the next request's 401 tries again

    async def keep_fresh(self, tick: float = 600) -> None:
        while True:
            await asyncio.sleep(tick)
            try:
                await self.keep_fresh_once()
            except Exception:
                log.exception("scheduled refresh failed")

    # ---- signing in and out --------------------------------------------------------------------------

    async def login(self, payer: str) -> DeviceCode:
        """Start a device-code sign-in in the payer's own home. Asking again while one is open returns it."""
        split_payer(payer)
        open_login = self._logins.get(payer)
        if open_login and not open_login[1].done():
            return open_login[0]
        handle = await self.account.start_device_login(self.homes.make_home(payer))
        code = DeviceCode(handle.verification_url, handle.user_code)
        self._logins[payer] = (code, asyncio.create_task(self._finish_login(payer, handle.wait)))
        log.info("payer %s: device-code sign-in started", payer)
        return code

    async def _finish_login(self, payer: str, wait) -> bool:
        try:
            ok = await wait()
        except Exception as e:
            ok = False
            log.error("payer %s: sign-in failed (%s)", payer, type(e).__name__)
        if ok and self.homes.signed_in(payer):
            self._errors.pop(payer, None)
            self._rejected.pop(payer, None)
            self._refreshed_at[payer] = time.monotonic()
            log.info("payer %s: signed in", payer)
            return True
        self._errors[payer] = "sign-in failed"
        return False

    async def wait_for_login(self, payer: str) -> bool:
        open_login = self._logins.get(payer)
        return await open_login[1] if open_login else self.homes.signed_in(payer)

    async def disconnect(self, payer: str) -> None:
        home = self.homes.home(payer)
        if payer not in self.homes.overrides and home.is_dir():
            shutil.rmtree(home)
        self._errors.pop(payer, None)
        self._rejected.pop(payer, None)
        log.info("payer %s: disconnected", payer)

    def status(self, payer: str) -> dict[str, Any]:
        self.homes.home(payer)  # refuses a bad payer id
        open_login = self._logins.get(payer)
        if open_login and not open_login[1].done():
            state = "signing_in"
        elif payer in self._rejected or self._errors.get(payer, "").startswith("refresh failed"):
            state = "expired"
        elif self.homes.signed_in(payer):
            state = "connected"
        else:
            state = "signed_out"
        refreshed = last_refresh(self.homes.bundle(payer))
        return {
            "payer": payer,
            "state": state,
            "last_refresh": refreshed.isoformat() if refreshed else None,
            "error": self._errors.get(payer),
        }
