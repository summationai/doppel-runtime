"""BFF jobs on this Mac (docs/specs/bff-split.md checkpoint 4): accepted from a check-in into the sandbox, renewed,
stopped, and their results sent until the BFF accepts them. Against a fake BFF and a fake sandbox."""

import asyncio
import json
import uuid

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from doppel_control_plane.bff import BffLink, BffState, pair
from doppel_control_plane.jobs import JobRunner
from doppel_control_plane.runs import LedgerRuns
from doppel_control_plane.store import Ledger
from doppel_model_provider.broker import Broker
from doppel_model_provider.codex_homes import CodexHomes
from doppel_shared.capability import capability_hash
from doppel_shared.testing import FakeSandbox

ORG, DAVID = str(uuid.uuid4()), str(uuid.uuid4())
HEAD = "a" * 40


def job(**over) -> dict:
    return {
        "id": str(uuid.uuid4()), "repository": "acme/api", "repository_id": "1", "installation_id": "9", "number": 7,
        "head_sha": HEAD, "base_branch": "main", "title": "Fix", "author": "eng", "payer": f"{ORG}/{DAVID}",
        "doppel": {"handle": "security", "kind": "app", "config": {},
                   "files": {"SKILL.md": "Hunt.", "references/owasp.md": "A1", "../escape.md": "no"}},
        "instructions": "## Your persona: security\nHunt.", "lease_seconds": 900, "attempt": 1,
        "token": "ghs_secret_repo_token", **over,
    }  # fmt: skip


class FakeBff:
    def __init__(self):
        self.credential = None
        self.to_give: list[dict] = []
        self.stop: list[str] = []
        self.check_ins: list[dict] = []
        self.results: dict[str, dict] = {}
        self.result_status = 200

    def app(self):
        app = web.Application()

        async def do_pair(request):
            self.credential = (await request.json())["credential"]
            return web.json_response(
                {"runtime": {"id": "r1", "name": "mini"}, "organization": {"id": ORG, "name": "Acme"}}
            )

        async def check_in(request):
            body = await request.json()
            self.check_ins.append(body)
            jobs, self.to_give = self.to_give, []
            stop, self.stop = self.stop, []
            return web.json_response({"revoked": False, "paused": False, "caps": {}, "seats": [], "remove_seats": [],
                                      "jobs": jobs, "stop": stop, "check_in_after_seconds": 30})  # fmt: skip

        async def result(request):
            if self.result_status != 200:
                return web.json_response({"error": {"message": "no"}}, status=self.result_status)
            self.results[request.match_info["id"]] = await request.json()
            return web.json_response({"accepted": True})

        app.router.add_post("/api/runtime/pair", do_pair)
        app.router.add_post("/api/runtime/check-in", check_in)
        app.router.add_post("/api/runtime/jobs/{id}", result)
        return app


@pytest.fixture
async def world(tmp_path):
    bff = FakeBff()
    server = TestServer(bff.app())
    await server.start_server()
    ledger = Ledger(tmp_path / "ledger.db")
    sandbox = FakeSandbox()
    runner = JobRunner(ledger, lambda: sandbox)
    state = BffState(tmp_path / "bff")
    link = BffLink(state, Broker(CodexHomes(tmp_path / "tenants"), None), host_cap=3, version="t", jobs=runner)
    await pair(state, str(server.make_url("")), "dwp_x")
    yield bff, ledger, sandbox, runner, link
    ledger.close()
    await server.close()


async def test_a_job_starts_in_its_prs_pod_with_the_doppels_rules_and_its_run_pays_its_payer(world):
    bff, ledger, sandbox, runner, link = world
    given = job()
    bff.to_give = [given]
    await link.check_in()
    assert bff.check_ins[0]["running"] == [] and bff.check_ins[0]["capacity"] == 3
    pod, started = sandbox.started[0]
    assert sandbox.pods[pod].repo == "acme/api" and sandbox.pods[pod].number == 7
    assert (started.reviewer, started.head_sha, started.base_ref) == ("security", HEAD, "main")
    assert started.instructions.startswith("## Your persona: security")
    assert started.files["references/owasp.md"] == "A1"
    assert started.focus == "", "no command, no focus"
    # The gateway finds the run, and who pays for it, by the capability's hash; the capability itself isn't stored.
    run = LedgerRuns(ledger).running(capability_hash(started.capability))
    assert run is not None and run.payer == f"{ORG}/{DAVID}"
    assert started.capability not in json.dumps([dict(r) for r in ledger.rows("SELECT * FROM slots")])
    # The repo token reaches git's environment only, never a URL.
    env = runner.git_env("acme/api")
    assert (
        "ghs_secret_repo_token" not in json.dumps(env)
        and env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    )

    await link.check_in()
    assert bff.check_ins[1]["running"] == [given["id"]] and bff.check_ins[1]["capacity"] == 2
    bff.to_give = [given]
    await link.check_in()
    assert len(sandbox.started) == 1, "the same job twice starts once"


async def test_a_commands_focus_reaches_the_reviewer(world):
    bff, ledger, sandbox, runner, link = world
    bff.to_give = [job(focus="the refund path")]
    await link.check_in()
    assert sandbox.started[0][1].focus == "the refund path"


async def test_a_verdict_is_sent_at_once_and_the_pod_goes_when_its_last_job_ends(world):
    bff, ledger, sandbox, runner, link = world
    a, b = job(), job(doppel={"handle": "infra", "kind": "app", "files": {}})
    bff.to_give = [a, b]
    await link.check_in()
    assert len(sandbox.pods) == 1, "one pod per PR, shared by its reviewers"
    slot_a, slot_b = (j.slot_id for _, j in sandbox.started)
    runner.verdict(slot_a, {"verdict": "clean", "findings": []})
    await asyncio.sleep(0.05)
    assert bff.results[a["id"]] == {"status": "done", "verdict": {"verdict": "clean", "findings": []}}
    assert sandbox.deleted == [], "infra is still running in it"
    runner.failed(slot_b, "codex exited 2")
    await asyncio.sleep(0.05)
    assert bff.results[b["id"]] == {"status": "failed", "reason": "codex exited 2"}
    assert len(sandbox.deleted) == 1
    await link.check_in()
    assert bff.check_ins[-1]["running"] == []


async def test_a_result_the_bff_didnt_take_is_sent_again_before_the_next_check_in(world):
    bff, ledger, sandbox, runner, link = world
    a = job()
    bff.to_give = [a]
    await link.check_in()
    bff.result_status = 503
    runner.verdict(sandbox.started[0][1].slot_id, {"verdict": "clean", "findings": []})
    await asyncio.sleep(0.05)
    assert a["id"] not in bff.results
    bff.result_status = 200
    await link.check_in()
    assert a["id"] in bff.results, "sent before the check-in, so the BFF never thinks it lost"
    calls = len(bff.results)
    await link.check_in()
    assert len(bff.results) == calls, "sent once accepted"


async def test_a_stopped_job_is_killed_and_its_late_verdict_never_sent(world):
    bff, ledger, sandbox, runner, link = world
    a = job()
    bff.to_give = [a]
    await link.check_in()
    slot = sandbox.started[0][1].slot_id
    bff.stop = [a["id"]]
    await link.check_in()
    assert sandbox.stopped == [(sandbox.started[0][0], slot)]
    runner.verdict(slot, {"verdict": "clean", "findings": []})
    await asyncio.sleep(0.05)
    await link.check_in()
    assert a["id"] not in bff.results


async def test_a_malformed_job_is_refused_and_a_restart_drops_what_was_running(world, tmp_path):
    bff, ledger, sandbox, runner, link = world
    bff.to_give = [job(head_sha="nope"), job(repository="../etc"), job()]
    await link.check_in()
    assert len(sandbox.started) == 1
    assert runner.recover() == 1, "the next process drops it; the BFF retries it"
    assert runner.running() == []


def test_doppel_files_stay_inside_the_job_folder(tmp_path):
    from doppel_control_plane.pods import write_doppel_files

    write_doppel_files(tmp_path / "doppel", {"SKILL.md": "x", "references/a.md": "y", "../escape.md": "no",
                                             "/abs.md": "no", ".hidden/x.md": "no"})  # fmt: skip
    found = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert found == ["doppel/SKILL.md", "doppel/references/a.md"]
    assert (tmp_path / "doppel" / "SKILL.md").stat().st_mode & 0o777 == 0o644
