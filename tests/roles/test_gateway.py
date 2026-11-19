"""The gateway's model route against a fake upstream that records every request it receives."""

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from doppel_runtime.ports.credentials import Credential, NotSignedIn
from doppel_runtime.roles.gateway import Gateway, UsageCounter, capability_hash, usage_reset
from doppel_runtime.store import Ledger, iso

NOW = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)


def sse(total_tokens: int = 123) -> bytes:
    events = [
        {"type": "response.created", "response": {"id": "r1"}},
        {"type": "response.output_text.delta", "delta": "ok"},
        {"type": "response.completed", "response": {"id": "r1", "usage": {"total_tokens": total_tokens}}},
    ]
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


class Upstream:
    """Answers from a script of (status, body, headers); then 200 with a Responses stream."""

    def __init__(self):
        self.requests: list[dict] = []
        self.script: list[tuple[int, bytes, dict]] = []

    async def handle(self, request: web.Request) -> web.Response:
        self.requests.append({
            "method": request.method, "path": request.path, "query": dict(request.query),
            "headers": {k.lower(): v for k, v in request.headers.items()}, "body": await request.read(),
        })  # fmt: skip
        status, body, headers = self.script.pop(0) if self.script else (200, sse(), {})
        return web.Response(status=status, body=body, headers={"content-type": "text/event-stream", **headers})


class FakeCredentials:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.version: dict[str, int] = {"u-steven": 1, "u-troy": 1}
        self.refreshes: list[str] = []

    def _cred(self, payer: str) -> Credential:
        if payer not in self.version:
            raise NotSignedIn(payer)
        token = f"tok-{payer}-v{self.version[payer]}"
        return Credential(self.base_url, {"authorization": f"Bearer {token}", "chatgpt-account-id": f"acct-{payer}"})

    async def credential(self, payer: str) -> Credential:
        return self._cred(payer)

    async def refresh(self, payer: str) -> Credential:
        self.refreshes.append(payer)
        if payer in self.version:
            self.version[payer] += 1
        return self._cred(payer)


@pytest.fixture
async def world(tmp_path):
    upstream = Upstream()
    up_app = web.Application()
    up_app.router.add_route("*", "/{tail:.*}", upstream.handle)
    up_server = TestServer(up_app)
    await up_server.start_server()

    ledger = Ledger(tmp_path / "ledger.db")
    creds = FakeCredentials(str(up_server.make_url("/backend-api/codex")))
    gateway = Gateway(ledger, creds, default_budget=1000, clock=lambda: NOW)
    gw_app = web.Application()
    gateway.add_routes(gw_app)
    gw_server = TestServer(gw_app)
    await gw_server.start_server()

    pr = ledger.upsert_pr("summation", "summationai/sandbox", 42, head="aaa111")

    def run(reviewer: str, payer: str, capability: str, *, state="running", started=NOW, budget=None, used=0):
        slot_id, _ = ledger.queue_slot(pr, "aaa111", reviewer, "app", payer_user=payer)
        ledger.execute(
            "UPDATE slots SET state=?, started_at=?, capability_hash=?, token_budget=?, tokens_used=? WHERE id=?",
            (state, iso(started), capability_hash(capability), budget, used, slot_id),
        )
        return slot_id

    async with aiohttp.ClientSession(base_url=str(gw_server.make_url("/"))) as client:

        async def call(capability: str | None, path: str = "/model/responses", **kw):
            headers = {"authorization": f"Bearer {capability}"} if capability else {}
            headers |= kw.pop("headers", {})
            async with client.post(path, headers=headers, data=b'{"model":"gpt-5.5","input":[]}', **kw) as r:
                return r.status, await r.read()

        yield SimpleNamespace(
            upstream=upstream,
            ledger=ledger,
            creds=creds,
            run=run,
            call=call,
            gateway_url=str(gw_server.make_url("")).rstrip("/"),
        )
    await gw_server.close()
    await up_server.close()
    ledger.close()


def tokens(world, slot_id):
    return world.ledger.value("SELECT tokens_used FROM slots WHERE id=?", (slot_id,))


# ---- finding the run ---------------------------------------------------------------------------------


async def test_no_capability_is_refused(world):
    status, body = await world.call(None)
    assert status == 401 and json.loads(body)["error"]["type"] == "invalid_capability"
    assert world.upstream.requests == []


async def test_an_unknown_capability_is_refused(world):
    world.run("steven", "u-steven", "cap-steven")
    status, _ = await world.call("cap-someone-else")
    assert status == 401 and world.upstream.requests == []


async def test_a_finished_runs_capability_is_refused(world):
    world.run("steven", "u-steven", "cap-steven", state="posted")
    status, _ = await world.call("cap-steven")
    assert status == 401 and world.upstream.requests == []


async def test_a_capability_expires_with_its_maximum_run_time(world):
    world.run("steven", "u-steven", "cap-steven", started=NOW - timedelta(hours=3))
    status, body = await world.call("cap-steven")
    assert status == 401 and b"expired" in body


async def test_over_budget_is_refused_before_anything_goes_upstream(world):
    world.run("steven", "u-steven", "cap-steven", budget=500, used=500)
    status, body = await world.call("cap-steven")
    assert status == 403 and json.loads(body)["error"]["type"] == "budget_exceeded"
    assert world.upstream.requests == []


# ---- the swap ----------------------------------------------------------------------------------------


async def test_the_capability_is_swapped_for_the_payers_credential(world):
    world.run("steven", "u-steven", "cap-steven")
    status, body = await world.call("cap-steven", headers={"session-id": "s-1", "x-forwarded-for": "10.0.0.9"})
    assert status == 200 and b"response.completed" in body
    sent = world.upstream.requests[0]
    assert sent["path"] == "/backend-api/codex/responses"
    assert sent["headers"]["authorization"] == "Bearer tok-u-steven-v1"
    assert sent["headers"]["chatgpt-account-id"] == "acct-u-steven"
    assert sent["headers"]["session-id"] == "s-1"  # Codex's own headers pass through
    assert "x-forwarded-for" not in sent["headers"]
    assert b"cap-steven" not in json.dumps(sent["headers"]).encode() + sent["body"]


async def test_each_run_goes_out_on_its_own_payer(world):
    world.run("steven", "u-steven", "cap-steven")
    world.run("troy", "u-troy", "cap-troy")
    await world.call("cap-troy")
    await world.call("cap-steven")
    assert [r["headers"]["authorization"] for r in world.upstream.requests] == [
        "Bearer tok-u-troy-v1",
        "Bearer tok-u-steven-v1",
    ]


async def test_paths_and_queries_are_forwarded(world):
    world.run("steven", "u-steven", "cap-steven")
    await world.call("cap-steven", path="/model/alpha/search", params={"q": "x"})
    assert world.upstream.requests[0]["path"] == "/backend-api/codex/alpha/search"
    assert world.upstream.requests[0]["query"] == {"q": "x"}


async def test_an_upstream_401_refreshes_once_and_retries(world):
    world.run("steven", "u-steven", "cap-steven")
    world.upstream.script = [(401, b'{"error":{"message":"expired"}}', {})]
    status, _ = await world.call("cap-steven")
    assert status == 200
    assert world.creds.refreshes == ["u-steven"]
    assert [r["headers"]["authorization"] for r in world.upstream.requests] == [
        "Bearer tok-u-steven-v1",
        "Bearer tok-u-steven-v2",
    ]


async def test_a_payer_without_a_sign_in_gets_503(world):
    world.run("alice", "u-alice", "cap-alice")
    status, body = await world.call("cap-alice")
    assert status == 503 and json.loads(body)["error"]["type"] == "not_signed_in"


# ---- counting and limits -----------------------------------------------------------------------------


async def test_tokens_are_counted_from_the_final_event(world):
    slot_id = world.run("steven", "u-steven", "cap-steven")
    await world.call("cap-steven")
    await world.call("cap-steven")
    assert tokens(world, slot_id) == 246


async def test_a_usage_limit_answer_records_the_payers_reset(world):
    world.run("steven", "u-steven", "cap-steven")
    reset = int((NOW + timedelta(hours=2)).timestamp())
    error = {"error": {"type": "usage_limit_reached", "resets_at": reset}}
    world.upstream.script = [(429, json.dumps(error).encode(), {"content-type": "application/json"})]
    status, _ = await world.call("cap-steven")
    assert status == 429  # passed through: Codex shows it
    assert world.ledger.value("SELECT reset_at FROM usage_resets WHERE payer='u-steven'") == iso(
        NOW + timedelta(hours=2)
    )


async def test_an_ordinary_429_records_nothing(world):
    world.run("steven", "u-steven", "cap-steven")
    world.upstream.script = [(429, b'{"error":{"type":"rate_limit_exceeded"}}', {"retry-after": "3"})]
    status, _ = await world.call("cap-steven")
    assert status == 429
    assert world.ledger.value("SELECT count(*) FROM usage_resets") == 0


def test_usage_reset_forms():
    assert usage_reset(b'{"error":{"type":"usage_limit_reached","resets_in_seconds":60}}', {}, NOW) == NOW + timedelta(
        seconds=60
    )
    assert usage_reset(b'{"error":{"type":"usage_limit_reached"}}', {}, NOW) == NOW + timedelta(hours=1)
    assert usage_reset(b"not json", {}, NOW) is None


def test_usage_counter_reads_events_split_across_chunks():
    counter = UsageCounter()
    data = sse(77)
    for i in range(0, len(data), 7):
        counter.feed(data[i : i + 7])
    assert counter.tokens == 77


async def test_logs_name_the_payer_never_the_token_or_capability(world, caplog):
    world.run("steven", "u-steven", "cap-steven")
    with caplog.at_level(logging.DEBUG):
        await world.call("cap-steven")
        world.upstream.script = [(401, b"{}", {})]
        await world.call("cap-steven")
    text = caplog.text
    assert "u-steven" in text
    assert "tok-u-steven" not in text and "cap-steven" not in text


async def test_tokens_are_recorded_before_the_final_event_reaches_codex(world):
    """Codex hangs up as soon as it reads response.completed; the count must already be in the ledger."""
    slot_id = world.run("steven", "u-steven", "cap-steven")
    async with aiohttp.ClientSession() as session:
        url = world.gateway_url + "/model/responses"
        async with session.post(url, headers={"authorization": "Bearer cap-steven"}, data=b"{}") as r:
            async for line in r.content:
                if b"response.completed" in line:
                    assert tokens(world, slot_id) == 123
                    break  # hang up early, as Codex does
    assert tokens(world, slot_id) == 123
