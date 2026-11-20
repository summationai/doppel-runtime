"""Checkpoint 6a with real containers: the runtime as `serve` wires it, pods and all.

Opt-in (needs Docker; builds src/sandbox-runtime/Dockerfile):
    DOPPEL_DOCKER_TESTS=1 uv run pytest tests/integration/test_runtime_docker.py

The runtime builds its own sandbox from the config (DockerPods, mirrors, the egress forwarder). GitHub
is the in-memory fake, the repo a local git repo, and the model a local fake behind the real gateway.
Codex in the pod is real; so are intake, the scheduler, the gateway's capability check and the merger.
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
from doppel_control_plane.config import load
from doppel_control_plane.pods import docker
from doppel_shared.ports import Credential
from doppel_shared.testing import FakeGitHub, FakeGitHubHost
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
[intake]
poll_seconds = 0.2
[scheduler]
tick_seconds = 0.2
settle_seconds = 0
[sandbox]
image = "{image}"
[[tenants]]
id = "summation"
repos = ["{REPO}"]
  [[tenants.reviewers]]
  handle = "repo-owner"
  kind = "app"
  payer = "summation/u-steven"
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


async def test_serve_wiring_reviews_a_pr_in_a_real_pod_and_cleans_up(tmp_path, image, origin):
    _, head = origin
    runner, url, seen = await fake_model()
    env = {f"DOPPEL_{n}_PORT": "0" for n in ("MODEL", "INTERNAL", "HEALTH")}
    host, github = FakeGitHubHost(), FakeGitHub()
    rt = Runtime(load(config(tmp_path, image), env=env), github=github, reader=host)
    await rt.start()
    try:
        rt.gateway.credentials = FakeCredentials(url)
        rt.broker.status = lambda payer: {"state": "connected"}
        owner = rt.sandbox.owner

        host.open(REPO, 1, head)
        await until(lambda: rt.ledger.value("SELECT state FROM slots") == "posted")
        slot = rt.ledger.one("SELECT * FROM slots")
        assert slot["tokens_used"] == 15  # counted by the real gateway
        assert seen == ["Bearer upstream-for-summation/u-steven"]  # the pod's capability was swapped out
        assert len(owned(owner)) == 2  # the pod and its egress forwarder

        host.checks[head] = "success"
        await until(lambda: github.events(merger_only=True) == ["APPROVE"], 10)

        host.close(REPO, 1, merged=True)
        await until(lambda: rt.ledger.value("SELECT state FROM prs") == "merged", 10)
        assert rt.ledger.value("SELECT pod_id FROM prs") is None
        assert len(owned(owner)) == 1  # the pod is gone; the forwarder stays for the next PR
    finally:
        await rt.stop()
        await runner.cleanup()
    assert owned(owner) == []


async def test_a_restart_sweeps_the_pods_a_dead_process_left(tmp_path, image, origin):
    _, head = origin
    env = {f"DOPPEL_{n}_PORT": "0" for n in ("MODEL", "INTERNAL", "HEALTH")}
    cfg = load(config(tmp_path, image), env=env)
    first = Runtime(cfg, github=FakeGitHub(), reader=FakeGitHubHost())
    await first.start()
    owner = first.sandbox.owner
    first.sandbox.create_pod(PRRef("summation", REPO, 1))
    assert len(owned(owner)) == 2
    for role in first.roles.values():  # the process dies: nothing closes its pods
        await role.stop()
    for r in first._runners:
        await r.cleanup()
    first.ledger.close()

    second = Runtime(cfg, github=FakeGitHub(), reader=FakeGitHubHost())
    await second.start()
    try:
        assert owned(owner) == []
    finally:
        await second.stop()
