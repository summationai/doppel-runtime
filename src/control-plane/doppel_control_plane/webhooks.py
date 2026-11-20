"""GitHub webhooks (RC's review #7): verified here, then turned into an immediate poll of the repo.

The BFF forwards GitHub's request untouched (raw body and headers) over the tailnet to the internal
listener. The runtime holds the webhook secret and checks `X-Hub-Signature-256` itself, so a forward
is trusted only if GitHub signed it. A repeated delivery id is answered and ignored.

An event doesn't carry state the runtime acts on directly: it says "this repo changed", and intake
reads the repo now, through the same diff against the ledger as the timer. So a lost webhook costs
only a poll interval, and a replayed or reordered one changes nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime

from aiohttp import web

from doppel_control_plane.intake import Poller

log = logging.getLogger(__name__)

PATH = "/github/webhook"
MAX_BODY = 25 * 1024**2  # GitHub caps a payload at 25 MB
REMEMBERED = 10_000  # delivery ids kept for dedupe
# Events that can change what intake would read: PRs, requests, reviews, comments, CI.
EVENTS = {"pull_request", "pull_request_review", "pull_request_review_thread", "pull_request_review_comment",
          "issue_comment", "check_run", "check_suite", "status"}  # fmt: skip


def signature_ok(secret: bytes, body: bytes, header: str | None) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


class Webhooks:
    def __init__(self, secret: bytes, poller: Poller, clock: Callable[[], datetime]):
        self._secret, self.poller, self.clock = secret, poller, clock
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._pending: dict[str, asyncio.Task] = {}
        self._dirty: set[str] = set()
        self.burst = 0.5  # seconds to gather a burst of events into one poll

    def __repr__(self) -> str:
        return "Webhooks()"

    def add_routes(self, app: web.Application) -> None:
        app.router.add_post(PATH, self.handle)

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.read()
        if len(body) > MAX_BODY:
            return web.json_response({"error": "too large"}, status=413)
        if not signature_ok(self._secret, body, request.headers.get("X-Hub-Signature-256")):
            log.warning("webhook: refused a delivery with a missing or wrong signature")
            return web.json_response({"error": "bad signature"}, status=401)
        delivery = request.headers.get("X-GitHub-Delivery", "")
        if delivery and delivery in self._seen:
            return web.json_response({"ok": True, "duplicate": True})
        if delivery:
            self._seen[delivery] = None
            while len(self._seen) > REMEMBERED:
                self._seen.popitem(last=False)

        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return web.json_response({"ok": True, "pong": True})
        try:
            repo = json.loads(body).get("repository", {}).get("full_name", "")
        except ValueError:
            return web.json_response({"error": "not JSON"}, status=400)
        w = self.poller.watched(repo) if event in EVENTS else None
        if w is None:
            return web.json_response({"ok": True, "ignored": event})
        self._poke(w)
        return web.json_response({"ok": True, "polling": w.repo}, status=202)

    def _poke(self, w) -> None:
        """Poll the repo soon. Events during a poll mark it dirty: one more poll follows, not one per event."""
        self._dirty.add(w.repo)
        task = self._pending.get(w.repo)
        if task is None or task.done():
            self._pending[w.repo] = asyncio.create_task(self._poll(w))

    async def _poll(self, w) -> None:
        while w.repo in self._dirty:
            await asyncio.sleep(self.burst)  # let a burst (a push fires several events) arrive first
            self._dirty.discard(w.repo)
            try:
                await self.poller.poll_repo(w, self.clock())
            except Exception:
                log.exception("webhook: poll of %s failed", w.repo)
