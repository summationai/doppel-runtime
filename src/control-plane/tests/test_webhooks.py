"""Webhooks (RC's review #7): GitHub's signature checked here; an event polls its repo at once."""

import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from doppel_control_plane.intake import Watched
from doppel_control_plane.webhooks import PATH, Webhooks, signature_ok

SECRET = b"webhook-secret-for-tests"
REPO = "summationai/billing"


class Poller:
    def __init__(self):
        self.polled: list[str] = []
        self.gate = asyncio.Event()
        self.gate.set()

    def watched(self, repo):
        return Watched("summation", REPO) if repo.lower() == REPO else None

    async def poll_repo(self, w, now):
        self.polled.append(w.repo)
        await self.gate.wait()


def signed(body: bytes, secret: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


@pytest.fixture
async def hooks():
    poller = Poller()
    wh = Webhooks(SECRET, poller, lambda: datetime.now(UTC))
    wh.burst = 0.01
    app = web.Application()
    wh.add_routes(app)
    async with TestClient(TestServer(app)) as client:
        yield client, wh, poller


async def send(client, event: str, payload: dict, *, delivery: str = "d1", secret: bytes = SECRET, sig=None):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
               "X-Hub-Signature-256": sig if sig is not None else signed(body, secret)}  # fmt: skip
    return await client.post(PATH, data=body, headers=headers)


def test_signature_check():
    assert signature_ok(SECRET, b"x", signed(b"x"))
    assert not signature_ok(SECRET, b"x", signed(b"y"))
    assert not signature_ok(SECRET, b"x", signed(b"x", b"other"))
    assert not signature_ok(SECRET, b"x", None)
    assert not signature_ok(SECRET, b"x", "sha1=" + hashlib.sha1(b"x").hexdigest())


async def test_rc_7_an_unsigned_or_wrongly_signed_forward_is_refused(hooks):
    client, _, poller = hooks
    pr = {"repository": {"full_name": REPO}}
    assert (await send(client, "pull_request", pr, sig="")).status == 401
    assert (await send(client, "pull_request", pr, secret=b"guessed")).status == 401
    await asyncio.sleep(0.05)
    assert poller.polled == []


async def test_an_event_polls_its_repo_and_a_redelivery_is_ignored(hooks):
    client, _, poller = hooks
    r = await send(client, "pull_request", {"action": "opened", "repository": {"full_name": "SummationAI/Billing"}})
    assert r.status == 202
    again = await send(client, "pull_request", {"repository": {"full_name": REPO}})  # same delivery id
    assert (await again.json())["duplicate"]
    await asyncio.sleep(0.05)
    assert poller.polled == [REPO]


async def test_unwatched_repos_and_other_events_are_ignored(hooks):
    client, _, poller = hooks
    assert (await (await send(client, "ping", {"zen": "hi"}, delivery="p")).json())["pong"]
    r = await send(client, "pull_request", {"repository": {"full_name": "someone/else"}}, delivery="d2")
    assert (await r.json())["ignored"] == "pull_request"
    r = await send(client, "star", {"repository": {"full_name": REPO}}, delivery="d3")
    assert (await r.json())["ignored"] == "star"
    await asyncio.sleep(0.05)
    assert poller.polled == []


async def test_a_burst_becomes_one_poll_and_events_during_a_poll_one_more(hooks):
    client, _, poller = hooks
    for i in range(5):  # a push fires several events at once
        await send(client, "pull_request", {"repository": {"full_name": REPO}}, delivery=f"b{i}")
    await asyncio.sleep(0.05)
    assert poller.polled == [REPO]

    poller.gate.clear()  # the next poll is slow
    await send(client, "check_run", {"repository": {"full_name": REPO}}, delivery="c1")
    await asyncio.sleep(0.05)
    for i in range(3):  # arrive while it runs: GitHub's state may have moved past what it read
        await send(client, "check_run", {"repository": {"full_name": REPO}}, delivery=f"c{i + 2}")
    poller.gate.set()
    await asyncio.sleep(0.1)
    assert poller.polled == [REPO, REPO, REPO]  # the burst, the slow one, and one more for what came during it
