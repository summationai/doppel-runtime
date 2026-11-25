"""A fake Doppelwerk and model endpoint for test_runner_container.py, run in a container on the runner's network.

    python doppelwerk_fixture.py '<setup json>'   setup: {organization, runtime, user, job_id, head}

It serves the runtime's half of the BFF protocol (/api/runtime/*) and a model endpoint (/v1/responses) on port 8080,
and writes what it saw to /tmp/fixture.json: whether it was paired, the job's result, and the model calls' credentials.
Nothing is reachable from outside Docker.
"""

import json
import sys

from aiohttp import web

setup = json.loads(sys.argv[1])
seat = {
    "id": setup["seat_id"],
    "payer": f"{setup['organization']}/{setup['user']}",
    "user": {"id": setup["user"], "name": "CI reviewer", "login": "ci-reviewer"},
}
job = {
    "id": setup["job_id"],
    "repository": setup["repo"],
    "number": 1,
    "head_sha": setup["head"],
    "base_branch": "main",
    "payer": seat["payer"],
    "doppel": {"handle": "ci-reviewer", "kind": "app", "files": {"SKILL.md": "Review the fixture."}},
    "instructions": "Review this fixture.",
}
verdict = setup["verdict"]
state = {"credential": None, "claimed": False, "result": None, "authorization": [], "check_ins": 0}


def save() -> None:
    with open("/tmp/fixture.json", "w") as f:
        json.dump({k: v for k, v in state.items() if k != "credential"} | {"paired": bool(state["credential"])}, f)


def authorized(request: web.Request) -> None:
    if not state["credential"] or request.headers.get("Authorization") != f"Bearer {state['credential']}":
        raise web.HTTPUnauthorized()


async def pair(request: web.Request) -> web.Response:
    body = await request.json()
    if body["code"] != "dwp_container_fixture" or state["credential"]:
        return web.json_response({"error": {"message": "That pair code is wrong, used or expired."}}, status=404)
    state["credential"] = body["credential"]
    save()
    return web.json_response({
        "runtime": {"id": setup["runtime"], "name": "Container runner"},
        "organization": {"id": setup["organization"], "name": "CI workspace"},
        "protocol": {"current": 1, "minimum": 0},
    })  # fmt: skip


async def check_in(request: web.Request) -> web.Response:
    authorized(request)
    body = await request.json()
    state["check_ins"] += 1
    ready = any(s["id"] == seat["id"] and s["state"] == "connected" for s in body["seats"])
    jobs = []
    if ready and body["capacity"] > 0 and not state["claimed"]:
        state["claimed"] = True
        jobs.append(job)
    save()
    return web.json_response({
        "revoked": False, "jobs": jobs, "seats": [seat], "remove_seats": [], "stop": [],
        "check_in_after_seconds": 5, "protocol": {"current": 1, "minimum": 0},
    })  # fmt: skip


async def finish(request: web.Request) -> web.Response:
    authorized(request)
    state["result"] = await request.json()
    save()
    return web.json_response({"ok": True})


async def responses(request: web.Request) -> web.StreamResponse:
    state["authorization"].append(request.headers.get("authorization", ""))
    save()
    resp = web.StreamResponse(headers={"content-type": "text/event-stream"})
    await resp.prepare(request)
    message = {"type": "message", "role": "assistant", "id": "m",
               "content": [{"type": "output_text", "text": json.dumps(verdict)}]}  # fmt: skip
    usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    for event in ({"type": "response.created", "response": {"id": "r"}},
                  {"type": "response.output_item.done", "item": message},
                  {"type": "response.completed", "response": {"id": "r", "usage": usage}}):  # fmt: skip
        await resp.write(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode())
    await resp.write_eof()
    return resp


app = web.Application()
app.router.add_post("/api/runtime/pair", pair)
app.router.add_post("/api/runtime/check-in", check_in)
app.router.add_post("/api/runtime/jobs/{job}", finish)
app.router.add_post("/v1/responses", responses)
save()
web.run_app(app, host="0.0.0.0", port=8080, print=None)
