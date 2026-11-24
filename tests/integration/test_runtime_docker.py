"""The runtime as a runner, end to end in Docker: a job from a (fake) Doppelwerk is reviewed in a real pod.

Codex in the pod is real; so are the job runner, mirrors, pods, the gateway's capability check and token count, and
the result going back to Doppelwerk. Only the model and Doppelwerk are local stand-ins.
"""

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from doppel_control_plane.app import Runtime
from doppel_control_plane.bff import BffState, pair
from doppel_control_plane.config import load
from doppel_control_plane.pods import docker
from doppel_shared.ports import Credential
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


class FakeCredentials:
    """The broker as the gateway sees it, answering with the fake model's address for every payer."""

    def __init__(self, url: str):
        self.url = url

    async def credential(self, payer):
        return Credential(self.url, {"authorization": "Bearer upstream-for-" + payer})

    async def refresh(self, payer):
        return await self.credential(payer)

    def rejected(self, payer, reason):
        pass


def config(tmp_path: Path, image: str) -> Path:
    f = tmp_path / "doppel.toml"
    f.write_text(f"""
data_dir = "{tmp_path / "data"}"
[github]
repo_url = "{tmp_path / "repos"}/{{repo}}"
[sandbox]
image = "{image}"
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


async def fake_bff(jobs: list[dict]) -> tuple[web.AppRunner, str, dict]:
    """Doppelwerk, as far as a runner can tell: pairing, one check-in handing out `jobs`, and results."""
    results: dict = {}

    async def do_pair(request):
        return web.json_response(
            {"runtime": {"id": "r1", "name": "runner"}, "organization": {"id": "org", "name": "Acme"}}
        )

    async def check_in(request):
        given, jobs[:] = list(jobs), []
        return web.json_response({"revoked": False, "paused": False, "caps": {}, "seats": [], "remove_seats": [],
                                  "jobs": given, "stop": [], "check_in_after_seconds": 30})  # fmt: skip

    async def result(request):
        results[request.match_info["id"]] = await request.json()
        return web.json_response({"accepted": True})

    app = web.Application()
    app.router.add_post("/api/runtime/pair", do_pair)
    app.router.add_post("/api/runtime/check-in", check_in)
    app.router.add_post("/api/runtime/jobs/{id}", result)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}", results


def a_job(head: str) -> dict:
    return {"id": "00000000-0000-4000-8000-000000000001", "repository": REPO, "repository_id": "1", "number": 1,
            "head_sha": head, "base_branch": "main", "payer": "org/u-steven", "token": None, "focus": "",
            "doppel": {"handle": "repo-owner", "kind": "app", "files": {"SKILL.md": "Review carefully."}},
            "instructions": "## Your persona: repo-owner"}  # fmt: skip


async def test_a_job_from_doppelwerk_is_reviewed_in_a_real_pod_and_the_pod_goes(tmp_path, image, origin):
    _, head = origin
    model, url, seen = await fake_model()
    jobs = [a_job(head)]
    bff, bff_url, results = await fake_bff(jobs)
    env = {f"DOPPEL_{n}_PORT": "0" for n in ("MODEL", "INTERNAL", "HEALTH")}
    cfg = load(config(tmp_path, image), env=env)
    await pair(BffState(cfg.bff_dir), bff_url, "dwp_x")
    rt = Runtime(cfg)
    await rt.start()
    try:
        await rt.roles["bff"].stop()  # this test checks in itself
        rt.gateway.credentials = FakeCredentials(url)
        await rt.bff.check_in()
        owner = rt.jobs.sandbox.owner
        await until(lambda: "00000000-0000-4000-8000-000000000001" in results)
        result = results["00000000-0000-4000-8000-000000000001"]
        assert result["status"] == "done" and result["verdict"]["verdict"] == "clean"
        assert result["verdict"]["doppel"] == "repo-owner" and result["verdict"]["head_sha"] == head
        assert rt.ledger.value("SELECT tokens_used FROM slots") == 15  # counted by the real gateway
        assert seen == ["Bearer upstream-for-org/u-steven"]  # the pod's capability was swapped for the payer's
        assert len(owned(owner)) == 1  # the pod went with its last job; the egress forwarder stays
    finally:
        await rt.stop()
        await model.cleanup()
        await bff.cleanup()
    assert owned(owner) == []


async def test_a_restart_sweeps_the_pods_a_dead_process_left(tmp_path, image, origin):
    bff, bff_url, _ = await fake_bff([])
    env = {f"DOPPEL_{n}_PORT": "0" for n in ("MODEL", "INTERNAL", "HEALTH")}
    cfg = load(config(tmp_path, image), env=env)
    await pair(BffState(cfg.bff_dir), bff_url, "dwp_x")
    first = Runtime(cfg)
    await first.start()
    owner = first.jobs.sandbox.owner
    first.jobs.sandbox.create_pod(PRRef("org", REPO, 1))
    assert len(owned(owner)) == 2
    for role in first.roles.values():  # the process dies: nothing closes its pods
        await role.stop()
    for r in first._runners:
        await r.cleanup()
    first.ledger.close()

    second = Runtime(cfg)
    await second.start()  # a paired runner prepares its sandbox at start, sweeping what the dead one left
    try:
        assert owned(owner) == []
    finally:
        await second.stop()
        await bff.cleanup()
