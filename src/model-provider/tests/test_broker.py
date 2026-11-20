"""The broker against a fake Codex account, on a temporary disk."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from doppel_model_provider import api
from doppel_model_provider.broker import Broker
from doppel_model_provider.codex_account import DeviceLogin
from doppel_model_provider.codex_homes import CodexHomes
from doppel_shared.ports.credentials import NotSignedIn

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
STEVEN, TROY = "summation/u-steven", "summation/u-troy"


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
        async def wait():
            await self.approve.wait()
            if self.approved:
                (home / "auth.json").write_text(bundle("secret-v1", NOW))
            return self.approved

        return DeviceLogin("https://auth.openai.com/codex/device", "ABCD-1234", wait)


@pytest.fixture
def world(tmp_path):
    homes = CodexHomes(tmp_path / "tenants")
    account = FakeAccount()
    return homes, account, Broker(homes, account, clock=lambda: NOW)


def sign_in(homes, payer=STEVEN, token="secret-v1", refreshed=NOW):
    homes.make_home(payer)
    (homes.home(payer) / "auth.json").write_text(bundle(token, refreshed))


async def test_credential_reads_the_payers_home(world):
    homes, _, broker = world
    sign_in(homes)
    assert (await broker.credential(STEVEN)).headers["authorization"] == "Bearer secret-v1"
    with pytest.raises(NotSignedIn):
        await broker.credential(TROY)


async def test_two_refreshes_at_once_refresh_once(world):
    homes, account, broker = world
    sign_in(homes)
    a, b = await asyncio.gather(broker.refresh(STEVEN), broker.refresh(STEVEN))
    assert account.refreshes == 1
    assert a.headers == b.headers and a.headers["authorization"] == "Bearer secret-v2"


async def test_a_failed_refresh_is_not_signed_in_and_shows_in_status(world):
    homes, account, broker = world
    sign_in(homes)
    account.fail = True
    with pytest.raises(NotSignedIn):
        await broker.refresh(STEVEN)
    assert broker.status(STEVEN)["state"] == "expired"


async def test_a_sign_in_the_provider_refused_is_expired_until_signed_in_again(world):
    homes, account, broker = world
    sign_in(homes, TROY)
    broker.rejected(TROY, "401 token_revoked")
    assert broker.status(TROY)["state"] == "expired"
    assert broker.status(TROY)["error"] == "refused by the provider: 401 token_revoked"
    with pytest.raises(NotSignedIn):  # not handed out again
        await broker.credential(TROY)
    await broker.login(TROY)
    account.approve.set()
    assert await broker.wait_for_login(TROY)
    assert broker.status(TROY)["state"] == "connected"
    assert (await broker.credential(TROY)).headers["authorization"] == "Bearer secret-v1"


async def test_keep_fresh_refreshes_only_sign_ins_6_hours_old(world):
    homes, account, broker = world
    sign_in(homes, STEVEN, refreshed=NOW - timedelta(hours=7))
    sign_in(homes, TROY, refreshed=NOW - timedelta(hours=1))
    await broker.keep_fresh_once()
    assert account.refreshes == 1
    assert (await broker.credential(STEVEN)).headers["authorization"] == "Bearer secret-v2"
    assert (await broker.credential(TROY)).headers["authorization"] == "Bearer secret-v1"


async def test_a_restarted_broker_finds_every_sign_in_on_disk(world):
    homes, account, _ = world
    sign_in(homes, STEVEN)
    sign_in(homes, TROY)
    fresh = Broker(CodexHomes(homes.root), account, clock=lambda: NOW)
    assert fresh.homes.payers() == [STEVEN, TROY]
    assert {fresh.status(p)["state"] for p in (STEVEN, TROY)} == {"connected"}


async def test_device_code_sign_in(world):
    homes, account, broker = world
    code = await broker.login(TROY)
    assert (code.verification_url, code.user_code) == ("https://auth.openai.com/codex/device", "ABCD-1234")
    assert await broker.login(TROY) == code  # asking again returns the open one
    assert broker.status(TROY)["state"] == "signing_in"
    account.approve.set()
    assert await broker.wait_for_login(TROY)
    assert broker.status(TROY)["state"] == "connected"
    assert (homes.root / "summation" / "u-troy" / "codex" / "auth.json").is_file()


async def test_a_refused_sign_in(world):
    _, account, broker = world
    await broker.login(TROY)
    account.approved = False
    account.approve.set()
    assert not await broker.wait_for_login(TROY)
    status = broker.status(TROY)
    assert (status["state"], status["error"]) == ("signed_out", "sign-in failed")


async def test_disconnect_removes_the_home(world):
    homes, _, broker = world
    sign_in(homes)
    await broker.disconnect(STEVEN)
    assert homes.bundle(STEVEN) is None


@pytest.mark.parametrize("payer", ["u-steven", "../etc/x", "summation/..", "summation/.hidden", "a/b/c", ""])
async def test_bad_payer_ids_are_refused(world, payer):
    _, _, broker = world
    with pytest.raises(NotSignedIn):
        await broker.login(payer)


async def test_logs_never_hold_a_token(world, caplog):
    homes, account, broker = world
    sign_in(homes, refreshed=NOW - timedelta(hours=7))
    with caplog.at_level(logging.DEBUG):
        await broker.keep_fresh_once()
        account.fail = True
        with pytest.raises(NotSignedIn):
            await broker.refresh(STEVEN)
    assert STEVEN in caplog.text
    assert "secret-" not in caplog.text and "rt-" not in caplog.text


async def test_the_routes(world):
    homes, account, broker = world
    app = web.Application()
    api.add_routes(app, broker)
    server = TestServer(app)
    await server.start_server()
    async with aiohttp.ClientSession(base_url=str(server.make_url("/"))) as http:
        async with http.post("/broker/summation/u-troy/login") as r:
            assert r.status == 200 and (await r.json())["user_code"] == "ABCD-1234"
        account.approve.set()
        await broker.wait_for_login(TROY)
        async with http.get("/broker/summation/u-troy/status") as r:
            body = await r.json()
            assert body["state"] == "connected" and "secret" not in json.dumps(body)
        async with http.get("/broker/summation/..%2F..%2Fetc/status") as r:
            assert r.status in (400, 404)
        async with http.delete("/broker/summation/u-troy") as r:
            assert r.status == 200
        assert homes.bundle(TROY) is None
    await server.close()
