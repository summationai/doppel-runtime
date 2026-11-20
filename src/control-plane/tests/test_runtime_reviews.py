"""The runtime with reviews on: intake polls, the scheduler ticks, pods are asked for, the merger posts.

GitHub and the sandbox are the in-memory fakes; everything else is the real process wiring.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from doppel_control_plane.app import Runtime
from doppel_control_plane.config import Config, Listener, load
from doppel_shared.testing import FakeGitHub, FakeGitHubHost, FakeSandbox, verdict

REPO = "summationai/billing"
CONFIG = """
[intake]
poll_seconds = 0.05
[scheduler]
tick_seconds = 0.05
settle_seconds = 0
[[tenants]]
id = "summation"
repos = ["summationai/billing"]
  [[tenants.reviewers]]
  handle = "steven"
  kind = "personal"
  owner = "summation/u-steven"
  login = "stevenliu"
  [[tenants.reviewers]]
  handle = "repo-owner"
  kind = "app"
  payer = "summation/u-steven"
"""


async def until(check, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            pytest.fail("timed out")
        await asyncio.sleep(0.02)


@pytest.fixture
async def runtime(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text(CONFIG)
    cfg = load(f, env={"DOPPEL_DATA_DIR": str(tmp_path / "data"),
                       **{f"DOPPEL_{n}_PORT": "0" for n in ("MODEL", "INTERNAL", "HEALTH")}})  # fmt: skip
    host, github, sandbox = FakeGitHubHost(), FakeGitHub(), FakeSandbox()
    rt = Runtime(cfg, github=github, reader=host, sandbox=sandbox)
    await rt.start()
    connected = {"summation/u-steven"}
    rt.broker.status = lambda payer: {"state": "connected" if payer in connected else "signed_out"}
    rt.host, rt.fake_github, rt.fake_sandbox, rt.connected = host, github, sandbox, connected
    yield rt
    await rt.stop()


async def test_a_pr_is_reviewed_and_approved_through_the_running_roles(runtime):
    assert all(role.running for role in runtime.roles.values())
    host, sandbox, github = runtime.host, runtime.fake_sandbox, runtime.fake_github
    host.open(REPO, 42, "aaa1111", requested=["stevenliu"])
    await until(lambda: len(sandbox.started) == 2)  # polled, queued, ticked, started in one pod
    assert len(sandbox.pods) == 1
    assert {job.reviewer for _, job in sandbox.started} == {"steven", "repo-owner"}

    now = datetime.now(UTC)
    for _, job in sandbox.started:
        runtime.scheduler.verdict(job.slot_id, verdict(job.reviewer, "aaa1111"), now)
    host.checks["aaa1111"] = "success"
    await until(lambda: github.events(merger_only=True) == ["APPROVE"])

    host.close(REPO, 42, merged=True)
    await until(lambda: sandbox.deleted == ["pod-1"])


async def test_a_payer_without_a_sign_in_blocks_and_says_so_once(runtime):
    runtime.connected.clear()
    runtime.host.open(REPO, 42, "aaa1111")
    await until(lambda: runtime.ledger.value("SELECT state FROM slots") == "blocked")
    await asyncio.sleep(0.2)  # several more ticks
    assert len(runtime.fake_github.replies) == 1 and "Reconnect Codex" in runtime.fake_github.replies[0]
    runtime.connected.add("summation/u-steven")
    await until(lambda: len(runtime.fake_sandbox.started) == 1)


async def test_github_app_mode_waits_for_6b(tmp_path):
    cfg = Config(data_dir=tmp_path, listeners={n: Listener("127.0.0.1", 0) for n in ("model", "internal", "health")})
    from dataclasses import replace

    from doppel_control_plane.config import GitHubConfig

    with pytest.raises(NotImplementedError, match="6b"):
        await Runtime(replace(cfg, github=GitHubConfig(mode="app"))).start()
