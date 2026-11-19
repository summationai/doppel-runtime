"""The broker: every payer's model sign-in, kept alive (spec section 7).

    credential()  the payer's current credential, for the gateway
    refresh()     Codex refreshes it, one at a time per payer; callers who waited behind a refresh
                  reuse it (refresh tokens rotate, so two refreshers would break the sign-in)
    keep_fresh()  every 10 minutes: refresh any sign-in whose own `last_refresh` is 6 hours old (so
                  the refresh token stays in use whatever restarts happen), and catch OpenBao up
    restore()     at start: per payer, keep whichever copy was refreshed last, on disk or in OpenBao
    login()       a device-code sign-in in the payer's own Codex home
    status()      connected | signing_in | signed_out | refresh_failed, for the BFF

Every sign-in that changes is saved to OpenBao. A failed save is logged and retried by keep_fresh;
the disk's copy keeps working. Tokens are never printed or logged.
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

from doppel_model_provider.codex_homes import CodexHomes, check_payer, last_refresh
from doppel_model_provider.codex_sdk import Account
from doppel_model_provider.openbao import LoginStore, StoreError

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
        store: LoginStore | None = None,
        *,
        refresh_every: timedelta = timedelta(hours=6),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.homes = homes
        self.account = account
        self.store = store
        self.refresh_every = refresh_every
        self.clock = clock
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._refreshed_at: dict[str, float] = {}  # monotonic time of each payer's last refresh
        self._saved: dict[str, str] = {}  # the bundle last saved to OpenBao, per payer
        self._errors: dict[str, str] = {}
        self._logins: dict[str, tuple[DeviceCode, asyncio.Task]] = {}

    # ---- the gateway's port --------------------------------------------------------------------------

    async def credential(self, payer: str) -> Credential:
        return self.homes.read(payer)

    async def refresh(self, payer: str) -> Credential:
        asked = time.monotonic()
        async with self._locks[check_payer(payer)]:
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
                log.info(
                    "payer %s: refreshed (last_refresh %s -> %s)", payer, before, last_refresh(self.homes.bundle(payer))
                )
                await self.save(payer)
        return self.homes.read(payer)

    # ---- staying fresh and durable -------------------------------------------------------------------

    def refresh_due(self, payer: str) -> bool:
        bundle = self.homes.bundle(payer)
        if bundle is None or not self.homes.signed_in(payer):
            return False
        return self.clock() - (last_refresh(bundle) or EPOCH) >= self.refresh_every

    async def save(self, payer: str) -> None:
        """Save the payer's sign-in to OpenBao if it changed since the last save."""
        bundle = self.homes.bundle(payer)
        if self.store is None or bundle is None or bundle == self._saved.get(payer):
            return
        try:
            await self.store.save(payer, bundle)
        except StoreError as e:
            self._errors[payer] = "not saved to OpenBao"
            log.error("payer %s: couldn't save the sign-in to OpenBao (%s); the disk's copy still works", payer, e)
            return
        self._saved[payer] = bundle
        if self._errors.get(payer) == "not saved to OpenBao":
            self._errors.pop(payer)
        log.info("payer %s: sign-in saved to OpenBao", payer)

    async def keep_fresh_once(self) -> None:
        for payer in self.homes.payers():
            try:
                if self.refresh_due(payer):
                    await self.refresh(payer)
                else:
                    await self.save(payer)
            except NotSignedIn:
                pass  # recorded in status; the next request's 401 tries again

    async def keep_fresh(self, tick: float = 600) -> None:
        while True:
            await asyncio.sleep(tick)
            try:
                await self.keep_fresh_once()
            except Exception:
                log.exception("scheduled refresh or save failed")

    async def restore(self) -> None:
        """At start: per payer, whichever copy was refreshed last wins (the disk's on a tie: it's in use)."""
        payers = set(self.homes.payers())
        if self.store is not None:
            try:
                payers |= set(await self.store.payers())
            except StoreError as e:
                log.error("couldn't list saved sign-ins in OpenBao: %s", e)
        for payer in sorted(payers):
            disk = self.homes.bundle(payer)
            saved = None
            if self.store is not None:
                try:
                    saved = await self.store.load(payer)
                except StoreError as e:
                    log.error("payer %s: couldn't read the saved sign-in: %s", payer, e)
            if saved is not None:
                self._saved[payer] = saved
            if saved is not None and (disk is None or (last_refresh(saved) or EPOCH) > (last_refresh(disk) or EPOCH)):
                self.homes.write(payer, saved)
                log.info("payer %s: sign-in restored from OpenBao (last_refresh %s)", payer, last_refresh(saved))
            else:
                await self.save(payer)

    # ---- signing in and out --------------------------------------------------------------------------

    async def login(self, payer: str) -> DeviceCode:
        """Start a device-code sign-in in the payer's own home. Asking again while one is open returns it."""
        check_payer(payer)
        open_login = self._logins.get(payer)
        if open_login and not open_login[1].done():
            return open_login[0]
        handle = await self.account.start_device_login(self.homes.home(payer))
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
            self._refreshed_at[payer] = time.monotonic()
            await self.save(payer)
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
        if self.store is not None:
            await self.store.delete(payer)
        self._saved.pop(payer, None)
        self._errors.pop(payer, None)
        log.info("payer %s: disconnected", payer)

    def status(self, payer: str) -> dict[str, Any]:
        check_payer(payer)
        open_login = self._logins.get(payer)
        if open_login and not open_login[1].done():
            state = "signing_in"
        elif self._errors.get(payer, "").startswith("refresh failed"):
            state = "refresh_failed"
        elif self.homes.signed_in(payer):
            state = "connected"
        else:
            state = "signed_out"
        refreshed = last_refresh(self.homes.bundle(payer))
        return {
            "payer": payer,
            "state": state,
            "last_refresh": refreshed.isoformat() if refreshed else None,
            "saved": self.store is not None
            and self.homes.bundle(payer) is not None
            and self._saved.get(payer) == self.homes.bundle(payer),
            "error": self._errors.get(payer),
        }
