"""The gateway's model route: `/model/{path}` on the model listener, the only thing pods can reach (DSV-8).

A pass-through with four jobs, after steven-universe's gateway, made multi-user. It knows runs only
through the `Runs` port and sign-ins only through the `Credentials` port.

- find the run: the capability's hash names one running slot, and through it one payer; an unknown,
  finished or too-old capability gets a 401;
- swap the capability for that payer's credential (the broker's); an upstream 401 refreshes once
  and retries;
- refuse once the run is over its token budget, before anything is sent upstream;
- count the tokens of every response it relays (the `usage` of the Responses API's final event:
  OpenAI's figures, never the pod's), and record a payer's usage-limit reset (DSV-12).

Refusals use OpenAI's error shape, so Codex shows the message, and 403 rather than 429: Codex
retries a 429. Logs name the slot and payer, never a token or capability.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
from aiohttp import web
from doppel_shared.capability import capability_hash
from doppel_shared.ports.credentials import Credential, Credentials, NotSignedIn
from doppel_shared.ports.runs import Run, Runs

log = logging.getLogger(__name__)

MODEL_MAX_BYTES = 16 << 20  # one model request: the whole context
DEFAULT_BUDGET = 2_000_000  # tokens per run, unless the slot sets its own
MAX_RUN = timedelta(hours=2)  # a capability dies with its run, and in any case after this
# Never forwarded upstream: the pod's capability, and hop-by-hop or proxy headers.
DROP_HEADERS = {
    "host", "authorization", "x-api-key", "chatgpt-account-id", "content-length", "connection",
    "accept-encoding", "transfer-encoding", "x-forwarded-for", "x-real-ip",
}  # fmt: skip
# Passed back to Codex: content, request ids, and the plan's usage and rate-limit figures.
PASS_PREFIXES = (
    "content-type",
    "content-encoding",
    "retry-after",
    "x-request-id",
    "x-codex-",
    "x-ratelimit-",
    "openai-",
)
FINAL_EVENTS = (b"response.completed", b"response.incomplete", b"response.failed")


def refuse(status: int, kind: str, message: str) -> web.Response:
    """An error in OpenAI's shape, so Codex shows the message."""
    return web.json_response({"error": {"type": kind, "message": message}}, status=status)


class ModelRoute:
    def __init__(
        self,
        runs: Runs,
        credentials: Credentials,
        *,
        default_budget: int = DEFAULT_BUDGET,
        max_run: timedelta = MAX_RUN,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.runs = runs
        self.credentials = credentials
        self.default_budget = default_budget
        self.max_run = max_run
        self.clock = clock
        self._session: aiohttp.ClientSession | None = None

    def add_routes(self, app: web.Application) -> None:
        app.router.add_route("*", "/model/{path:.*}", self.model)
        app.on_cleanup.append(self._close)

    async def _close(self, _: web.Application) -> None:
        if self._session is not None:
            await self._session.close()

    def _client(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=600), auto_decompress=False
            )
        return self._session

    def _run_for(self, request: web.Request) -> tuple[Run | None, web.Response | None]:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or not auth[7:].strip():
            return None, refuse(401, "invalid_capability", "no capability")
        run = self.runs.running(capability_hash(auth[7:].strip()))
        if run is None:
            return None, refuse(401, "invalid_capability", "unknown or finished run")
        if run.started_at is None or self.clock() - run.started_at > self.max_run:
            return None, refuse(401, "invalid_capability", "this run's capability has expired")
        budget = run.token_budget or self.default_budget
        if run.tokens_used >= budget:
            log.warning("slot %s over its token budget (%d)", run.slot_id, budget)
            return None, refuse(403, "budget_exceeded", f"this run has used its {budget} tokens")
        return run, None

    async def model(self, request: web.Request) -> web.StreamResponse:
        run, refusal = self._run_for(request)
        if refusal is not None:
            return refusal
        payer = run.payer
        body = await request.read()
        if len(body) > MODEL_MAX_BYTES:
            return refuse(413, "too_large", "request too large")
        path = request.match_info["path"]
        headers = {k: v for k, v in request.headers.items() if k.lower() not in DROP_HEADERS}
        headers["accept-encoding"] = "identity"  # plain bytes back: relayed untouched, and readable

        async def send(cred: Credential) -> aiohttp.ClientResponse:
            url = f"{cred.base_url.rstrip('/')}/{path}"
            return await self._client().request(
                request.method, url, params=request.query, headers={**headers, **cred.headers}, data=body
            )

        try:
            resp = await send(await self.credentials.credential(payer))
            if resp.status == 401:  # the sign-in expired: refresh once, then retry
                resp.release()
                resp = await send(await self.credentials.refresh(payer))
        except NotSignedIn as e:
            log.error("slot %s: payer %s has no working sign-in (%s)", run.slot_id, payer, e)
            self.runs.provider_error(run.slot_id, 401, "not_signed_in", "credential")
            return reconnect(payer)
        except aiohttp.ClientError as e:
            log.warning("slot %s: upstream unreachable (%r)", run.slot_id, e)
            return refuse(502, "upstream_unreachable", "the model provider couldn't be reached")

        passthrough = {k: v for k, v in resp.headers.items() if k.lower().startswith(PASS_PREFIXES)}
        if resp.status >= 400:
            data = await resp.read()
            resp.release()
            code, kind = error_fields(data)
            self.runs.provider_error(run.slot_id, resp.status, code, kind)
            if resp.status in (401, 403):
                # Refused even after a refresh, or forbidden outright: the sign-in is dead until the
                # person signs in again. Decided by the status, never by the message's wording.
                self.credentials.rejected(payer, f"{resp.status} {code or kind or ''}".strip())
                log.error("slot %s: the provider refused payer %s's credential (%s %s)", run.slot_id, payer,
                          resp.status, code or kind)  # fmt: skip
                return reconnect(payer)
            if resp.status == 429:
                reset = usage_reset(data, resp.headers, self.clock())
                if reset is not None:
                    self.runs.record_usage_reset(payer, reset)
                    log.warning("slot %s: payer %s hit their usage window; resets %s", run.slot_id, payer, reset)
            else:
                log.warning("slot %s: the provider answered %s (%s)", run.slot_id, resp.status, code or kind)
            return web.Response(status=resp.status, body=data, headers=passthrough)

        out = web.StreamResponse(status=resp.status, headers=passthrough)
        await out.prepare(request)
        counter = UsageCounter()
        try:
            async for chunk in resp.content.iter_any():
                # Count before relaying: Codex may hang up the moment it sees the final event.
                self.runs.add_tokens(run.slot_id, counter.feed(chunk))
                await out.write(chunk)
            await out.write_eof()
        except ConnectionError:  # Codex closed the stream early (it has what it needs): not an error
            pass
        finally:
            resp.release()
            log.info("slot %s payer %s: %s /%s -> %s (%d tokens)", run.slot_id, payer, request.method, path,
                     resp.status, counter.tokens)  # fmt: skip
        return out


def reconnect(payer: str) -> web.Response:
    """The payer's sign-in doesn't work: Codex shows this, and the run fails without retrying."""
    return refuse(401, "credential_expired", f"{payer}'s Codex sign-in was refused: reconnect Codex")


def error_fields(body: bytes) -> tuple[str | None, str | None]:
    """The provider's error `code` and `type`, from OpenAI's error shape or ChatGPT's `detail`."""
    try:
        data = json.loads(body) or {}
    except (ValueError, TypeError):
        return None, None
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        error = data.get("detail") if isinstance(data, dict) and isinstance(data.get("detail"), dict) else {}
    code, kind = error.get("code"), error.get("type")
    return (str(code) if code else None), (str(kind) if kind else None)


def usage_reset(body: bytes, headers: Any, now: datetime) -> datetime | None:
    """When a 429 is a usage-limit answer, when the window resets. None for an ordinary rate limit.

    Best effort, from Codex's handling of ChatGPT's 429s: an error of type `usage_limit_reached`
    carrying `resets_at` (epoch seconds) or `resets_in_seconds`. Not yet seen live.
    """
    try:
        error = (json.loads(body) or {}).get("error") or {}
    except (ValueError, AttributeError):
        return None
    if error.get("type") != "usage_limit_reached":
        return None
    if error.get("resets_at"):
        return datetime.fromtimestamp(int(error["resets_at"]), UTC)
    if error.get("resets_in_seconds"):
        return now + timedelta(seconds=int(error["resets_in_seconds"]))
    after = headers.get("x-codex-primary-reset-after-seconds")
    return now + timedelta(seconds=int(after)) if after and after.isdigit() else now + timedelta(hours=1)


class UsageCounter:
    """Reads the Responses API's server-sent events as they pass and adds up the `usage` of each
    response's final event. The bytes themselves are relayed untouched."""

    def __init__(self) -> None:
        self.tokens = 0
        self._buffer = b""

    def feed(self, chunk: bytes) -> int:
        """Read a chunk; returns the tokens counted from it (0 until a final event completes)."""
        before = self.tokens
        self._buffer += chunk
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            if line.startswith(b"data:") and any(e in line for e in FINAL_EVENTS):
                self._count(line[5:])
        return self.tokens - before

    def _count(self, data: bytes) -> None:
        try:
            usage = (json.loads(data).get("response") or {}).get("usage") or {}
            self.tokens += int(usage.get("total_tokens") or 0)
        except (ValueError, TypeError, AttributeError):
            pass
