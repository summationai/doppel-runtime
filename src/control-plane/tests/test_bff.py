"""This Mac's link to the BFF (docs/specs/bff-split.md §4b), against a fake BFF and a fake Codex account."""

import asyncio
import json
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from doppel_control_plane import config
from doppel_control_plane.bff import PROTOCOL, BffError, BffLink, BffState, add_routes, pair
from doppel_model_provider.broker import Broker
from doppel_model_provider.codex_account import DeviceLogin
from doppel_model_provider.codex_homes import CodexHomes

ORG = str(uuid.uuid4())
DAVID, TROY = str(uuid.uuid4()), str(uuid.uuid4())


class FakeAccount:
    def __init__(self):
        self.approve = asyncio.Event()

    async def refresh(self, home):
        pass

    async def start_device_login(self, home):
        async def wait():
            await self.approve.wait()
            (home / "auth.json").write_text(json.dumps({"tokens": {"access_token": "secret"}}))
            return True

        return DeviceLogin("https://auth.openai.com/codex/device", "ABCD-1234", wait)


class FakeBff:
    """The BFF's side of the protocol, as far as a Mac can tell."""

    def __init__(self):
        self.credential: str | None = None
        self.seat_codes = {"dws_david": DAVID, "dws_troy": TROY}
        self.seats: dict[str, str] = {}  # seat id -> member
        self.check_ins: list[dict] = []
        self.revoked = False
        self.paused = False
        self.protocol: dict | None = {"current": PROTOCOL, "minimum": 0}  # None: a BFF from before the handshake
        self.pairs: list[dict] = []

    def app(self) -> web.Application:
        app = web.Application()

        def authorized(request):
            if request.headers.get("authorization") != f"Bearer {self.credential}":
                raise web.HTTPUnauthorized(
                    text=json.dumps({"error": {"message": "This Mac isn't paired."}}), content_type="application/json"
                )

        def seat_json(sid):
            member = self.seats[sid]
            return {"id": sid, "payer": f"{ORG}/{member}", "user": {"id": member, "name": "David", "login": "david-gh"}}

        def too_old(body):
            if self.protocol and body.get("protocol", 0) < self.protocol["minimum"]:
                message = "Update doppel-runtime on it, then restart it."
                refusal = {"error": {"code": "runtime_outdated", "message": message}}
                return web.json_response({**refusal, "protocol": self.protocol}, status=426)
            return None

        async def do_pair(request):
            body = await request.json()
            self.pairs.append(body)
            if (refused := too_old(body)) is not None:
                return refused
            if body["code"] != "dwp_good":
                refusal = {"error": {"message": "That pair code is wrong, used or expired."}}
                return web.json_response(refusal, status=404)
            self.credential = body["credential"]
            mac = {"runtime": {"id": "r1", "name": "Mac mini"}, "organization": {"id": ORG, "name": "Acme"}}
            if self.protocol:
                mac["protocol"] = self.protocol
            return web.json_response(mac)

        async def redeem(request):
            authorized(request)
            member = self.seat_codes.pop((await request.json())["code"], None)
            if member is None:
                return web.json_response({"error": {"message": "That seat code is wrong."}}, status=404)
            sid = str(uuid.uuid4())
            self.seats[sid] = member
            return web.json_response({"seat": seat_json(sid)})

        async def check_in(request):
            authorized(request)
            body = await request.json()
            self.check_ins.append(body)
            if (refused := too_old(body)) is not None:
                return refused
            if self.revoked:
                return web.json_response({"revoked": True})
            reported = [s["id"] for s in body["seats"]]
            return web.json_response({
                "revoked": False, "runtime": {"id": "r1", "name": "Mac mini", "organization_id": ORG},
                "paused": self.paused, "caps": {"host": None},
                "seats": [seat_json(sid) for sid in self.seats],
                "remove_seats": [sid for sid in reported if sid not in self.seats],
                "jobs": [], "check_in_after_seconds": 30,
                **({"protocol": self.protocol} if self.protocol else {}),
            })  # fmt: skip

        app.router.add_post("/api/runtime/pair", do_pair)
        app.router.add_post("/api/runtime/seats", redeem)
        app.router.add_post("/api/runtime/check-in", check_in)
        return app


@pytest.fixture
async def world(tmp_path):
    bff = FakeBff()
    server = TestServer(bff.app())
    await server.start_server()
    account = FakeAccount()
    broker = Broker(CodexHomes(tmp_path / "tenants"), account)
    state = BffState(tmp_path / "bff")
    link = BffLink(state, broker, host_cap=3, version="test")
    yield bff, str(server.make_url("")), state, link, account, tmp_path
    await server.close()


async def test_pairing_keeps_only_an_owner_only_credential_this_mac_made(world):
    bff, url, state, link, _, tmp = world
    with pytest.raises(BffError) as e:
        await pair(state, url, "dwp_wrong")
    assert e.value.status == 404 and "expired" in str(e.value)
    assert state.identity() is None

    identity = await pair(state, url, "dwp_good")
    assert (identity.name, identity.organization, identity.organization_id) == ("Mac mini", "Acme", ORG)
    assert bff.credential.startswith("dwr_") and len(bff.credential) >= 47
    assert state.credential() == bff.credential
    for name in ("credential", "runtime.json", "seats.json"):
        assert (tmp / "bff" / name).stat().st_mode & 0o777 == 0o600
    assert (tmp / "bff").stat().st_mode & 0o777 == 0o700
    assert bff.credential not in (tmp / "bff" / "runtime.json").read_text()

    with pytest.raises(ValueError, match="already paired"):
        await pair(state, url, "dwp_good")


async def test_an_unpaired_mac_does_not_call_out(world):
    bff, _, _, link, _, _ = world
    assert await link.check_in() is None
    assert bff.check_ins == []


async def test_a_seat_is_redeemed_then_signed_in_and_its_state_reported(world):
    bff, url, state, link, account, tmp = world
    await pair(state, url, "dwp_good")
    seat, code = await link.add_seat("dws_david")
    assert seat.payer == f"{ORG}/{DAVID}" and seat.login == "david-gh"
    assert code.user_code == "ABCD-1234"
    assert state.seats()[seat.id].payer == seat.payer

    await link.check_in()
    assert bff.check_ins[-1]["seats"] == [{"id": seat.id, "state": "signing_in", "plan": None, "quota": None}]
    assert bff.check_ins[-1]["capacity"] == 3 and bff.check_ins[-1]["version"] == "test"

    account.approve.set()
    assert await link.broker.wait_for_login(seat.payer)
    await link.check_in()
    assert bff.check_ins[-1]["seats"][0]["state"] == "connected"
    assert (tmp / "tenants" / ORG / DAVID / "codex" / "auth.json").exists()
    assert "secret" not in json.dumps(bff.check_ins), "a sign-in never leaves the Mac"


async def test_a_seat_removed_in_doppelwerk_is_signed_out(world):
    bff, url, state, link, account, tmp = world
    await pair(state, url, "dwp_good")
    seat, _ = await link.add_seat("dws_david")
    account.approve.set()
    await link.broker.wait_for_login(seat.payer)
    home = tmp / "tenants" / ORG / DAVID / "codex"
    assert home.exists()

    del bff.seats[seat.id]
    answer = await link.check_in()
    assert answer["remove_seats"] == [seat.id]
    assert not home.exists()
    assert state.seats() == {}


async def test_a_seat_the_bff_lists_is_adopted(world):
    bff, url, state, link, _, _ = world
    await pair(state, url, "dwp_good")
    bff.seats["s-lost"] = TROY  # redeemed, but this Mac lost its seats.json
    await link.check_in()
    assert state.seats()["s-lost"].payer == f"{ORG}/{TROY}"
    await link.check_in()
    assert bff.check_ins[-1]["seats"][0] == {"id": "s-lost", "state": "signed_out", "plan": None, "quota": None}


async def test_pause_and_the_bffs_pace_are_followed(world):
    bff, url, state, link, _, _ = world
    await pair(state, url, "dwp_good")
    bff.paused = True
    await link.check_in()
    assert link.paused is True and link.every == 30


async def test_a_removed_mac_signs_every_seat_out_and_forgets_its_credential(world):
    bff, url, state, link, account, tmp = world
    await pair(state, url, "dwp_good")
    seat, _ = await link.add_seat("dws_david")
    account.approve.set()
    await link.broker.wait_for_login(seat.payer)
    bff.revoked = True
    assert (await link.check_in()) == {"revoked": True}
    assert not (tmp / "tenants" / ORG / DAVID / "codex").exists()
    assert state.identity() is None and not (tmp / "bff" / "credential").exists()
    calls = len(bff.check_ins)
    assert await link.check_in() is None and len(bff.check_ins) == calls


async def test_a_refused_credential_is_kept_not_wiped(world):
    bff, url, state, link, _, _ = world
    await pair(state, url, "dwp_good")
    bff.credential = "dwr_someone-else"
    assert await link.check_in() is None
    assert state.identity() is not None and state.credential().startswith("dwr_")
    assert link.last_error == "This Mac isn't paired."


async def test_the_cli_adds_a_seat_through_the_daemons_internal_routes(world):
    bff, url, state, link, _, _ = world
    app = web.Application()
    add_routes(app, link)
    server = TestServer(app)
    await server.start_server()
    try:
        import aiohttp

        async with aiohttp.ClientSession() as s:
            async with s.post(server.make_url("/runtime/seats"), json={"code": "dws_david"}) as r:
                assert r.status == 409  # not paired yet
            await pair(state, url, "dwp_good")
            async with s.post(server.make_url("/runtime/seats"), json={"code": "nope"}) as r:
                assert r.status == 400
            async with s.post(server.make_url("/runtime/seats"), json={"code": "dws_unknown"}) as r:
                assert r.status == 404 and "wrong" in (await r.json())["error"]
            async with s.post(server.make_url("/runtime/seats"), json={"code": "dws_david"}) as r:
                answer = await r.json()
                assert r.status == 200
                assert answer["user_code"] == "ABCD-1234" and answer["seat"]["payer"] == f"{ORG}/{DAVID}"
            async with s.get(server.make_url("/runtime")) as r:
                view = await r.json()
                assert view["paired"] and view["runtime"]["organization"] == "Acme"
                assert view["seats"][0]["state"] == "signing_in"
            async with s.post(server.make_url(f"/runtime/seats/{answer['seat']['id']}/sign-in"), json={}) as r:
                assert r.status == 200
            async with s.post(server.make_url("/runtime/seats/nope/sign-in"), json={}) as r:
                assert r.status == 404
    finally:
        await server.close()


def test_config_reads_the_bff_section(tmp_path):
    path = tmp_path / "runtime.toml"
    path.write_text('[bff]\nurl = "https://doppelwerk.example"\ncheck_in_seconds = 10\n')
    cfg = config.load(path, env={})
    assert cfg.bff.url == "https://doppelwerk.example" and cfg.bff.check_in.total_seconds() == 10
    assert config.load(None, env={"DOPPEL_BFF_URL": "http://127.0.0.1:8792"}).bff.url == "http://127.0.0.1:8792"
    assert cfg.bff_dir == cfg.data_dir / "bff"


async def test_the_handshake_says_which_protocol_and_follows_the_bffs_answer(world):
    bff, url, state, link, _, _ = world
    await pair(state, url, "dwp_good")
    assert bff.pairs[-1]["protocol"] == PROTOCOL
    await link.check_in()
    assert bff.check_ins[-1]["protocol"] == PROTOCOL
    assert link.update == "none" and link.view()["update"] == "none" and link.view()["protocol"] == PROTOCOL

    # A newer BFF: still served, and an update is offered.
    bff.protocol = {"current": PROTOCOL + 1, "minimum": 0}
    assert await link.check_in() is not None
    assert link.update == "available" and link.view()["bff_protocol"] == bff.protocol

    # A BFF from before the handshake says nothing: nothing to update to.
    bff.protocol = None
    assert await link.check_in() is not None
    assert link.update == "none"


async def test_a_runtime_too_old_for_the_bff_takes_no_work_and_checks_back_slowly(world):
    bff, url, state, link, _, _ = world
    await pair(state, url, "dwp_good")
    bff.protocol = {"current": PROTOCOL + 2, "minimum": PROTOCOL + 1}
    assert await link.check_in() is None
    assert link.update == "required" and link.every == 300
    assert "Update doppel-runtime" in link.last_error
    assert state.identity() is not None, "an outdated runtime stays paired: an update is all it needs"
    # The BFF accepts it again (rolled back, or the runtime updated): back to its usual pace.
    bff.protocol = {"current": PROTOCOL, "minimum": 0}
    assert await link.check_in() is not None
    assert (link.update, link.every, link.last_error) == ("none", 30, None)


async def test_pairing_a_runtime_too_old_for_the_bff_says_to_update(tmp_path, world):
    bff, url, _, _, _, _ = world
    bff.protocol = {"current": PROTOCOL + 1, "minimum": PROTOCOL + 1}
    fresh = BffState(tmp_path / "other")
    with pytest.raises(BffError) as e:
        await pair(fresh, url, "dwp_good")
    assert e.value.status == 426 and e.value.protocol == bff.protocol and "Update doppel-runtime" in str(e.value)
    assert fresh.identity() is None
