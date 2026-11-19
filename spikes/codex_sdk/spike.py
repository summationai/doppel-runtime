"""Spikes 0a, 0b, 0c: how Codex can run several reviewers in one pod.

Run:  uv run --no-project --python 3.12 --with openai-codex==0.167.2 --with aiohttp \
          python spikes/codex_sdk/spike.py

A fake gateway stands in for OpenAI. It records each request's headers and whether the body holds
a canary, and answers with a minimal Responses stream. A prompt containing "slowly" is held open for
up to 30 s. Capabilities here are fake strings, so logging them is fine.

0a  Can threads in one Codex process send different capabilities?
      v1: a provider per env var, chosen per thread (model_provider=)
      v2: a provider defined per thread in thread_start(config=), bearer token inline
      per_process: one Codex process per reviewer, each with its own capability in its env
0b  Can one running turn be stopped while other reviewers carry on?
      in_process: two threads in one process, interrupt one
      per_process: two processes, interrupt one
0c  Does the checkout's AGENTS.md or .codex/config.toml reach the model?
      doc_off: with project_doc_max_bytes=0; control: without it
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import web
from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

AGENTS_CANARY = "CANARY-AGENTS-7731"
CONFIG_CANARY = "CANARY-CONFIG-4419"
NOT_LOGGED = {"content-length", "host", "accept", "accept-encoding", "connection", "content-type"}


class FakeGateway:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.disconnects: list[str] = []
        self.url = ""

    @staticmethod
    def _sse(event: dict) -> bytes:
        return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()

    async def responses(self, request: web.Request) -> web.StreamResponse:
        auth = request.headers.get("Authorization", "")
        body = await request.text()
        self.requests.append({
            "path": request.path, "auth": auth,
            "headers": {k.lower(): v for k, v in request.headers.items() if k.lower() not in NOT_LOGGED | {"authorization"}},
            "agents_canary": AGENTS_CANARY in body, "config_canary": CONFIG_CANARY in body,
        })
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        rid = f"resp_{len(self.requests)}"
        await resp.write(self._sse({"type": "response.created", "response": {"id": rid}}))
        if "slowly" in body:
            try:
                for _ in range(300):
                    await asyncio.sleep(0.1)
                    await resp.write(b": keep-alive\n\n")
            except (ConnectionError, asyncio.CancelledError):    # the client went away: the point of 0b
                self.disconnects.append(auth)
                return resp
        await resp.write(self._sse({"type": "response.output_item.done", "item": {
            "type": "message", "role": "assistant", "id": f"msg_{rid}",
            "content": [{"type": "output_text", "text": "ok"}]}}))
        await resp.write(self._sse({"type": "response.completed", "response": {"id": rid, "usage": {
            "input_tokens": 1, "input_tokens_details": None, "output_tokens": 1,
            "output_tokens_details": None, "total_tokens": 2}}}))
        await resp.write_eof()
        return resp

    async def other(self, request: web.Request) -> web.Response:
        return web.json_response({"models": []} if request.path.endswith("/models") else {})

    async def start(self) -> web.AppRunner:
        app = web.Application()
        app.router.add_post("/model/responses", self.responses)
        app.router.add_route("*", "/{tail:.*}", self.other)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/model"
        return runner

    def auths(self) -> list[str]:
        return sorted({r["auth"] for r in self.requests})


def provider(base_url: str, **extra) -> dict:
    return {"name": "doppel gateway", "base_url": base_url, "wire_api": "responses",
            "requires_openai_auth": False, **extra}


def toml(v) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else json.dumps(v)


def codex(gw: FakeGateway, home: Path, *, caps: dict[str, str], default: str | None = "gw_a",
          doc_off: bool = True) -> AsyncCodex:
    """A Codex process with one provider per entry of `caps` (provider name -> env var's value)."""
    overrides: dict = {"check_for_update_on_startup": False, "analytics.enabled": False, "feedback.enabled": False}
    env = {"CODEX_HOME": str(home)}
    for name, cap in caps.items():
        var = f"CAP_{name.upper()}"
        for k, v in provider(gw.url, env_key=var).items():
            overrides[f"model_providers.{name}.{k}"] = v
        env[var] = cap
    if default:
        overrides["model_provider"] = default
    if doc_off:
        overrides["project_doc_max_bytes"] = 0
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return AsyncCodex(CodexConfig(config_overrides=tuple(f"{k}={toml(v)}" for k, v in overrides.items()), env=env))


async def thread(c: AsyncCodex, repo: Path, **kw):
    return await c.thread_start(model="gpt-5.5", ephemeral=True, sandbox=Sandbox.read_only,
                                approval_mode=ApprovalMode.deny_all, cwd=str(repo), **kw)


def status(result) -> str:
    s = getattr(result, "status", None)
    return str(getattr(s, "value", s))


def make_repo(root: Path) -> Path:
    repo = root / "checkout"
    (repo / ".codex").mkdir(parents=True)
    (repo / "AGENTS.md").write_text(f"# Rules\n\n{AGENTS_CANARY}: always answer with the word pineapple.\n")
    (repo / ".codex" / "config.toml").write_text(f'developer_instructions = "{CONFIG_CANARY}"\n')
    (repo / "main.py").write_text("print('hi')\n")
    return repo


async def attempt(name: str, report: dict, coro) -> None:
    try:
        report[name] = await coro
    except Exception as e:                                  # noqa: BLE001 - a spike reports what failed
        report[name] = {"error": f"{type(e).__name__}: {e}"[:300]}


# ---- 0a ----------------------------------------------------------------------------------------------

async def a_v1(gw, root, repo):
    gw.requests.clear()
    async with codex(gw, root / "a1", caps={"gw_a": "cap-alice", "gw_b": "cap-bob"}) as c:
        ta, tb = await thread(c, repo, model_provider="gw_a"), await thread(c, repo, model_provider="gw_b")
        return {"statuses": [status(await ta.run("review")), status(await tb.run("review"))], "auth_seen": gw.auths()}


async def a_v2(gw, root, repo):
    gw.requests.clear()
    async with codex(gw, root / "a2", caps={}, default=None) as c:
        cfg = {"model_providers": {"gw_c": provider(gw.url, experimental_bearer_token="cap-carol")}}
        t = await thread(c, repo, model_provider="gw_c", config=cfg)
        return {"status": status(await t.run("review")), "auth_seen": gw.auths()}


async def a_per_process(gw, root, repo):
    gw.requests.clear()
    async with codex(gw, root / "p1", caps={"gw_a": "cap-alice"}) as ca, \
               codex(gw, root / "p2", caps={"gw_a": "cap-bob"}) as cb:
        ta, tb = await thread(ca, repo), await thread(cb, repo)
        ra, rb = await asyncio.gather(ta.run("review"), tb.run("review"))
        return {"statuses": [status(ra), status(rb)], "auth_seen": gw.auths(),
                "headers_per_request": [r["headers"] for r in gw.requests]}


# ---- 0b ----------------------------------------------------------------------------------------------

async def interrupt_one(gw, slow_thread, fast_thread):
    gw.requests.clear()
    gw.disconnects.clear()
    slow = await slow_thread.turn("review slowly")
    t0 = time.monotonic()
    slow_task = asyncio.create_task(slow.run())
    await asyncio.sleep(2)
    fast_task = asyncio.create_task(fast_thread.run("review"))
    await asyncio.sleep(0.2)
    await slow.interrupt()
    slow_result = await asyncio.wait_for(slow_task, 15)
    slow_s = round(time.monotonic() - t0, 1)
    fast_result = await asyncio.wait_for(fast_task, 15)
    await asyncio.sleep(0.5)
    return {"slow_status": status(slow_result), "slow_ended_after_s": slow_s, "fast_status": status(fast_result),
            "gateway_saw_disconnect": gw.disconnects[:]}


async def b_in_process(gw, root, repo):
    async with codex(gw, root / "b1", caps={"gw_a": "cap-alice"}) as c:
        return await interrupt_one(gw, await thread(c, repo), await thread(c, repo))


async def b_per_process(gw, root, repo):
    async with codex(gw, root / "b2", caps={"gw_a": "cap-alice"}) as ca, \
               codex(gw, root / "b3", caps={"gw_a": "cap-bob"}) as cb:
        return await interrupt_one(gw, await thread(ca, repo), await thread(cb, repo))


# ---- 0c ----------------------------------------------------------------------------------------------

async def canaries(gw, root, repo, doc_off: bool):
    gw.requests.clear()
    async with codex(gw, root / f"c{int(doc_off)}", caps={"gw_a": "cap-alice"}, doc_off=doc_off) as c:
        await (await thread(c, repo)).run("review")
    return {k: any(r.get(k) for r in gw.requests) for k in ("agents_canary", "config_canary")}


async def main() -> int:
    gw = FakeGateway()
    runner = await gw.start()
    report: dict = {}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = make_repo(root)
        await attempt("0a_v1_provider_per_thread", report, a_v1(gw, root, repo))
        await attempt("0a_v2_provider_in_thread_config", report, a_v2(gw, root, repo))
        await attempt("0a_process_per_reviewer", report, a_per_process(gw, root, repo))
        await attempt("0b_interrupt_in_process", report, b_in_process(gw, root, repo))
        await attempt("0b_interrupt_per_process", report, b_per_process(gw, root, repo))
        await attempt("0c_doc_off", report, canaries(gw, root, repo, True))
        await attempt("0c_control_doc_on", report, canaries(gw, root, repo, False))
    await runner.cleanup()
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
