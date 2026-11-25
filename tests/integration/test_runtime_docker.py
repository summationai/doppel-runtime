"""The runtime as a runner, end to end in Docker: a job from a (fake) Doppelwerk is reviewed in a real pod.

The pod runs Codex through the real gateway, which checks capabilities and counts tokens. The Doppelwerk API,
device approval, and model replies use fixtures.
"""

import asyncio
import json
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from doppel_control_plane.app import Runtime
from doppel_control_plane.bff import BffState, pair
from doppel_control_plane.config import load
from doppel_control_plane.pods import docker
from doppel_model_provider import codex_homes
from doppel_model_provider.codex_account import DeviceLogin
from doppel_shared.types import PRRef

pytestmark = pytest.mark.skipif(
    os.environ.get("DOPPEL_DOCKER_TESTS") != "1" or shutil.which("docker") is None,
    reason="opt-in: set DOPPEL_DOCKER_TESTS=1 (needs Docker)",
)

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "doppel-sandbox:test"
REPO = "summationai/sandbox"
VERDICT = {"verdict": "clean", "summary": "Nothing to fix.", "findings": [],
           "scope": {"files_reviewed": 1, "files_skipped": []}}  # fmt: skip


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def image():
    subprocess.run(["docker", "build", "-q", "-f", "src/sandbox-runtime/Dockerfile", "-t", IMAGE, "."],
                   cwd=ROOT, check=True, capture_output=True, timeout=900)  # fmt: skip
    return IMAGE


@pytest.fixture
def origin(tmp_path) -> tuple[Path, str]:
    """A repo laid out as GitHub serves one: main, and the PR's head at refs/pull/1/head."""
    r = tmp_path / "repos" / REPO
    r.mkdir(parents=True)
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    (r / "app.py").write_text("def add(a, b):\n    return a + b\n")
    git(r, "add", ".")
    git(r, "commit", "-qm", "base")
    git(r, "checkout", "-qb", "pr")
    (r / "app.py").write_text("def add(a, b):\n    return a + b  # checked\n")
    git(r, "commit", "-qam", "head")
    head = git(r, "rev-parse", "HEAD")
    git(r, "update-ref", "refs/pull/1/head", head)
    git(r, "checkout", "-q", "main")
    return r, head


async def fake_model() -> tuple[web.AppRunner, str, list[str]]:
    seen: list[str] = []

    async def responses(request: web.Request) -> web.StreamResponse:
        seen.append(request.headers.get("authorization", ""))
        resp = web.StreamResponse(headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        for event in (
            {"type": "response.created", "response": {"id": "r"}},
            {"type": "response.output_item.done", "item": {"type": "message", "role": "assistant", "id": "m",
             "content": [{"type": "output_text", "text": json.dumps(VERDICT)}]}},
            {"type": "response.completed", "response": {"id": "r", "usage": {"input_tokens": 10, "output_tokens": 5,
             "total_tokens": 15}}},
        ):  # fmt: skip
            await resp.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_post("/v1/responses", responses)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/v1", seen


def config(tmp_path: Path, image: str) -> Path:
    f = tmp_path / "doppel.toml"
    runtime = os.environ.get("DOPPEL_TEST_SANDBOX_RUNTIME", "")
    f.write_text(f"""
data_dir = "{tmp_path / "data"}"
[github]
repo_url = "{tmp_path / "repos"}/{{repo}}"
[sandbox]
image = "{image}"
runtime = "{runtime}"
""")
    return f


async def until(check, timeout: float = 240):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        if loop.time() > deadline:
            pytest.fail("timed out")
        await asyncio.sleep(0.2)


def owned(owner_label: str) -> list[str]:
    return docker("ps", "-aq", "--filter", f"label=doppel.owner={owner_label}").split()


class DoppelwerkFixture:
    """The Doppelwerk HTTP contract, with one review available for one signed-in seat."""

    def __init__(self, head: str):
        self.organization = str(uuid.uuid4())
        self.runtime = str(uuid.uuid4())
        self.user = str(uuid.uuid4())
        self.job_id = str(uuid.uuid4())
        self.seat = {
            "id": str(uuid.uuid4()),
            "payer": f"{self.organization}/{self.user}",
            "user": {"id": self.user, "name": "CI reviewer", "login": "ci-reviewer"},
        }
        self.credential: str | None = None
        self.seat_added = False
        self.claimed = False
        self.revoked = False
        self.result: dict | None = None
        self.job = {
            "id": self.job_id,
            "repository": REPO,
            "number": 1,
            "head_sha": head,
            "base_branch": "main",
            "payer": self.seat["payer"],
            "doppel": {"handle": "ci-reviewer", "kind": "app", "files": {"SKILL.md": "Review the fixture."}},
            "instructions": "Review this fixture.",
        }

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/api/runtime/pair", self.pair)
        app.router.add_post("/api/runtime/seats", self.add_seat)
        app.router.add_post("/api/runtime/check-in", self.check_in)
        app.router.add_post("/api/runtime/jobs/{job}", self.finish)
        return app

    def authorize(self, request: web.Request) -> None:
        if self.credential is None or request.headers.get("Authorization") != f"Bearer {self.credential}":
            raise web.HTTPUnauthorized()

    async def pair(self, request: web.Request) -> web.Response:
        body = await request.json()
        assert body["code"] == "dwp_linux_fixture"
        assert self.credential is None and body["credential"].startswith("dwr_")
        self.credential = body["credential"]
        return web.json_response({
            "runtime": {"id": self.runtime, "name": "Ubuntu runner"},
            "organization": {"id": self.organization, "name": "CI workspace"},
        })  # fmt: skip

    async def add_seat(self, request: web.Request) -> web.Response:
        self.authorize(request)
        assert (await request.json())["code"] == "dws_linux_fixture"
        self.seat_added = True
        return web.json_response({"seat": self.seat})

    async def check_in(self, request: web.Request) -> web.Response:
        self.authorize(request)
        body = await request.json()
        if self.revoked:
            return web.json_response({"revoked": True})
        ready = any(seat["id"] == self.seat["id"] and seat["state"] == "connected" for seat in body["seats"])
        jobs = []
        if ready and body["capacity"] > 0 and not self.claimed:
            self.claimed = True
            jobs.append(self.job)
        return web.json_response({
            "revoked": False, "jobs": jobs, "seats": [self.seat] if self.seat_added else [],
            "remove_seats": [], "stop": [], "check_in_after_seconds": 5,
        })  # fmt: skip

    async def finish(self, request: web.Request) -> web.Response:
        self.authorize(request)
        assert request.match_info["job"] == self.job_id
        self.result = await request.json()
        return web.json_response({"ok": True})


class DeviceApprovalFixture:
    async def start_device_login(self, home: Path) -> DeviceLogin:
        async def approve() -> bool:
            (home / "auth.json").write_text(
                json.dumps(
                    {
                        "tokens": {"access_token": "runner-ci-fixture"},
                        "last_refresh": datetime.now(UTC).isoformat(),
                    }
                )
            )
            return True

        return DeviceLogin("https://example.test/device", "TEST-CODE", approve)


async def test_runner_pairs_and_returns_a_bff_verdict(tmp_path, image, origin, monkeypatch):
    _, head = origin
    bff = DoppelwerkFixture(head)
    model, model_url, authorization = await fake_model()
    monkeypatch.setattr(codex_homes, "CHATGPT_BASE", model_url)
    env = {f"DOPPEL_{name}_PORT": "0" for name in ("MODEL", "INTERNAL", "HEALTH")}
    cfg = load(config(tmp_path, image), env=env)
    try:
        async with TestServer(bff.app()) as server:
            await pair(BffState(cfg.bff_dir), str(server.make_url("")), "dwp_linux_fixture")
            rt = Runtime(cfg)
            await rt.start()
            try:
                await rt.roles["bff"].stop()  # this test checks in itself
                rt.broker.account = DeviceApprovalFixture()
                seat, _ = await rt.bff.add_seat("dws_linux_fixture")
                assert await rt.broker.wait_for_login(seat.payer)
                await rt.bff.check_in()
                await until(lambda: bff.result is not None, 90)
                assert bff.result["status"] == "done", bff.result
                assert bff.result["verdict"]["verdict"] == "clean"
                assert bff.result["verdict"]["doppel"] == bff.job["doppel"]["handle"]
                assert bff.result["verdict"]["summary"] == VERDICT["summary"]
                assert bff.result["verdict"]["head_sha"] == head
                assert authorization == ["Bearer runner-ci-fixture"]
                assert rt.ledger.value("SELECT tokens_used FROM slots WHERE bff_job = ?", (bff.job_id,)) == 15
                assert not rt.jobs.pods
                owner = rt.jobs.sandbox.owner
                assert len(owned(owner)) == 1  # only the forwarder remains
                assert rt.bound["internal"][0] == rt.bound["health"][0] == "127.0.0.1"
                bff.revoked = True
                assert await rt.bff.check_in() == {"revoked": True}
                assert not rt.broker.homes.home(seat.payer).exists()
                assert rt.bff.state.identity() is None
            finally:
                await rt.stop()
    finally:
        await model.cleanup()
    assert owned(owner) == []


async def test_a_restart_sweeps_the_pods_a_dead_process_left(tmp_path, image, origin):
    _, head = origin
    bff = DoppelwerkFixture(head)
    env = {f"DOPPEL_{name}_PORT": "0" for name in ("MODEL", "INTERNAL", "HEALTH")}
    cfg = load(config(tmp_path, image), env=env)
    async with TestServer(bff.app()) as server:
        await pair(BffState(cfg.bff_dir), str(server.make_url("")), "dwp_linux_fixture")
        first = Runtime(cfg)
        await first.start()
        owner = first.jobs.sandbox.owner
        first.jobs.sandbox.create_pod(PRRef(bff.organization, REPO, 1))
        assert len(owned(owner)) == 2
        for role in first.roles.values():  # simulate an exit that skipped pod cleanup
            await role.stop()
        for runner in first._runners:
            await runner.cleanup()
        first.ledger.close()

        second = Runtime(cfg)
        await second.start()
        try:
            assert owned(owner) == []
        finally:
            await second.stop()
