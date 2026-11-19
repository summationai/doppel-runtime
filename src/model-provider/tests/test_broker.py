"""The broker against a fake Codex account and an in-memory store."""

import asyncio
import json
import logging
import stat
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from doppel_model_provider import api
from doppel_model_provider.broker import Broker
from doppel_model_provider.codex_homes import CodexHomes
from doppel_model_provider.codex_sdk import DeviceLogin
from doppel_model_provider.openbao import StoreError
from doppel_shared.ports.credentials import NotSignedIn

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def bundle(token: str, refreshed: datetime) -> str:
    return json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {"access_token": token, "account_id": "acct", "refresh_token": "rt-" + token},
        "last_refresh": refreshed.isoformat().replace("+00:00", "Z"),
    })  # fmt: skip


class FakeAccount:
    """Codex, as far as the broker can tell: refresh rewrites auth.json; a login waits for approval."""

    def __init__(self):
        self.refreshes = 0
        self.fail = False
        self.approve = asyncio.Event()
        self.approved = True

    async def refresh(self, home):
        self.refreshes += 1
        await asyncio.sleep(0.05)  # long enough for a second caller to queue behind the lock
        if self.fail:
            raise RuntimeError("refresh_token_reused")
        (home / "auth.json").write_text(bundle(f"secret-v{self.refreshes + 1}", NOW))

    async def start_device_login(self, home):
        home.mkdir(parents=True, exist_ok=True)

        async def wait():
            await self.approve.wait()
            if self.approved:
                (home / "auth.json").write_text(bundle("secret-v1", NOW))
            return self.approved

        return DeviceLogin("https://auth.openai.com/codex/device", "ABCD-1234", wait)


class FakeStore:
    def __init__(self):
        self.data: dict[str, str] = {}
        self.broken = False

    async def load(self, payer):
        if self.broken:
            raise StoreError("down")
        return self.data.get(payer)

    async def save(self, payer, value):
        if self.broken:
            raise StoreError("down")
        self.data[payer] = value

    async def delete(self, payer):
        self.data.pop(payer, None)

    async def payers(self):
        return sorted(self.data)


@pytest.fixture
def world(tmp_path):
    homes = CodexHomes(tmp_path / "homes")
    account, store = FakeAccount(), FakeStore()
    broker = Broker(homes, account, store, clock=lambda: NOW)
    return homes, account, store, broker


def sign_in(homes, payer="u-steven", token="secret-v1", refreshed=NOW):
    homes.write(payer, bundle(token, refreshed))


async def test_credential_reads_the_payers_home(world):
    homes, _, _, broker = world
    sign_in(homes)
    assert (await broker.credential("u-steven")).headers["authorization"] == "Bearer secret-v1"
    with pytest.raises(NotSignedIn):
        await broker.credential("u-troy")


async def test_two_refreshes_at_once_refresh_once(world):
    homes, account, store, broker = world
    sign_in(homes)
    a, b = await asyncio.gather(broker.refresh("u-steven"), broker.refresh("u-steven"))
    assert account.refreshes == 1
    assert a.headers == b.headers and a.headers["authorization"] == "Bearer secret-v2"
    assert json.loads(store.data["u-steven"])["tokens"]["access_token"] == "secret-v2"  # saved


async def test_a_failed_refresh_is_not_signed_in_and_shows_in_status(world):
    homes, account, _, broker = world
    sign_in(homes)
    account.fail = True
    with pytest.raises(NotSignedIn):
        await broker.refresh("u-steven")
    assert broker.status("u-steven")["state"] == "refresh_failed"


async def test_restore_brings_a_lost_home_back_from_openbao(world):
    homes, _, store, broker = world
    store.data["u-steven"] = bundle("secret-v7", NOW)
    await broker.restore()
    assert (await broker.credential("u-steven")).headers["authorization"] == "Bearer secret-v7"
    mode = stat.S_IMODE((homes.home("u-steven") / "auth.json").stat().st_mode)
    assert mode == 0o600


async def test_restore_keeps_the_newest_copy(world):
    homes, _, store, broker = world
    sign_in(homes, "u-steven", "secret-disk-new", NOW)
    store.data["u-steven"] = bundle("secret-saved-old", NOW - timedelta(hours=1))
    sign_in(homes, "u-troy", "secret-disk-old", NOW - timedelta(hours=1))
    store.data["u-troy"] = bundle("secret-saved-new", NOW)
    await broker.restore()
    assert json.loads(store.data["u-steven"])["tokens"]["access_token"] == "secret-disk-new"  # OpenBao caught up
    assert (await broker.credential("u-troy")).headers["authorization"] == "Bearer secret-saved-new"


async def test_keep_fresh_refreshes_only_sign_ins_6_hours_old(world):
    homes, account, _, broker = world
    sign_in(homes, "u-steven", refreshed=NOW - timedelta(hours=7))
    sign_in(homes, "u-troy", refreshed=NOW - timedelta(hours=1))
    await broker.keep_fresh_once()
    assert account.refreshes == 1
    assert (await broker.credential("u-steven")).headers["authorization"] == "Bearer secret-v2"
    assert (await broker.credential("u-troy")).headers["authorization"] == "Bearer secret-v1"


async def test_keep_fresh_catches_openbao_up(world):
    homes, _, store, broker = world
    sign_in(homes)
    await broker.keep_fresh_once()
    assert "u-steven" in store.data


async def test_a_store_outage_keeps_the_disk_working_and_is_retried(world):
    homes, _, store, broker = world
    sign_in(homes)
    store.broken = True
    await broker.keep_fresh_once()
    assert broker.status("u-steven")["error"] == "not saved to OpenBao"
    assert (await broker.credential("u-steven")).headers["authorization"] == "Bearer secret-v1"
    store.broken = False
    await broker.keep_fresh_once()
    assert broker.status("u-steven")["error"] is None and broker.status("u-steven")["saved"]


async def test_device_code_sign_in(world):
    homes, account, store, broker = world
    code = await broker.login("u-troy")
    assert (code.verification_url, code.user_code) == ("https://auth.openai.com/codex/device", "ABCD-1234")
    assert await broker.login("u-troy") == code  # asking again returns the open one
    assert broker.status("u-troy")["state"] == "signing_in"
    account.approve.set()
    assert await broker.wait_for_login("u-troy")
    assert broker.status("u-troy")["state"] == "connected"
    assert "u-troy" in store.data


async def test_a_refused_sign_in(world):
    _, account, _, broker = world
    await broker.login("u-troy")
    account.approved = False
    account.approve.set()
    assert not await broker.wait_for_login("u-troy")
    assert broker.status("u-troy") | {"last_refresh": None} == {
        "payer": "u-troy", "state": "signed_out", "last_refresh": None, "saved": False, "error": "sign-in failed",
    }  # fmt: skip


async def test_disconnect_removes_the_home_and_the_saved_copy(world):
    homes, _, store, broker = world
    sign_in(homes)
    await broker.save("u-steven")
    await broker.disconnect("u-steven")
    assert homes.bundle("u-steven") is None and store.data == {}


@pytest.mark.parametrize("payer", ["../etc", ".hidden", "a/b", ""])
async def test_bad_payer_ids_are_refused(world, payer):
    _, _, _, broker = world
    with pytest.raises(NotSignedIn):
        await broker.login(payer)


async def test_logs_never_hold_a_token(world, caplog):
    homes, account, _, broker = world
    sign_in(homes, refreshed=NOW - timedelta(hours=7))
    with caplog.at_level(logging.DEBUG):
        await broker.restore()
        await broker.keep_fresh_once()
        account.fail = True
        with pytest.raises(NotSignedIn):
            await broker.refresh("u-steven")
    assert "u-steven" in caplog.text
    assert "secret-" not in caplog.text and "rt-" not in caplog.text


async def test_the_routes(world):
    homes, account, _, broker = world
    app = web.Application()
    api.add_routes(app, broker)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession(base_url=str(server.make_url("/"))) as http:
        async with http.post("/broker/u-troy/login") as r:
            assert r.status == 200 and (await r.json())["user_code"] == "ABCD-1234"
        account.approve.set()
        await broker.wait_for_login("u-troy")
        async with http.get("/broker/u-troy/status") as r:
            body = await r.json()
            assert body["state"] == "connected" and "secret" not in json.dumps(body)
        async with http.get("/broker/..%2Fetc/status") as r:
            assert r.status in (400, 404)
        async with http.delete("/broker/u-troy") as r:
            assert r.status == 200
        assert homes.bundle("u-troy") is None
    await server.close()
