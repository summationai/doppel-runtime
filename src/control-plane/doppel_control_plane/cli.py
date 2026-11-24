"""The `doppel-runtime` command.

    serve [--config PATH]                    run the runtime process (the daemon)
    pair CODE [--bff URL] [--replace]        pair this Mac with Doppelwerk, using a code from Settings → Macs
    seat add CODE                            add a member's seat (their code from Settings → Macs), then sign them in
    seat sign-in SEAT                        sign a seat's member in to Codex again
    status                                   which workspace this Mac serves, and each seat's state

`seat` and `status` talk to the running daemon on its internal listener: its broker holds the sign-ins.
`pair` works with or without the daemon running. Codes and credentials are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from doppel_control_plane import __version__, app, config
from doppel_control_plane.bff import BffError, BffState, pair

SIGN_IN_WAIT = 15 * 60


def main(argv: list[str] | None = None) -> int:
    # --config goes before or after the command: `doppel-runtime serve --config x.toml` reads as before.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="a TOML config file")
    parser = argparse.ArgumentParser(prog="doppel-runtime", parents=[common])
    parser.set_defaults(config=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="run the runtime process", parents=[common])
    p = sub.add_parser("pair", help="pair this Mac with Doppelwerk", parents=[common])
    p.add_argument("code", help="the one-time pair code (dwp_...) from Settings → Macs")
    p.add_argument("--bff", help="Doppelwerk's address, e.g. https://doppelwerk.ac0.app")
    p.add_argument("--replace", action="store_true", help="pair again, replacing this Mac's current pairing")
    seat = sub.add_parser("seat", help="a member's seat on this Mac").add_subparsers(dest="seat", required=True)
    seat.add_parser("add", help="add a seat with its one-time code, then sign its member in",
                    parents=[common]).add_argument("code")  # fmt: skip
    seat.add_parser("sign-in", help="sign a seat's member in to Codex again", parents=[common]).add_argument("seat_id")
    sub.add_parser("status", help="this Mac's pairing and seats", parents=[common])
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = config.load(args.config)
    if args.command == "serve":
        asyncio.run(app.serve(cfg))
        return 0
    if args.command == "pair":
        return _pair(cfg, args.code, args.bff, args.replace)
    if args.command == "status":
        return _status(cfg)
    if args.seat == "add":
        return _seat(cfg, "/runtime/seats", {"code": args.code})
    return _seat(cfg, f"/runtime/seats/{args.seat_id}/sign-in", {})


def _pair(cfg: config.Config, code: str, url: str | None, replace: bool) -> int:
    url = url or cfg.bff.url
    if not url:
        print("Say where Doppelwerk is: --bff https://<its address>", file=sys.stderr)
        return 2
    if not code.startswith("dwp_"):
        print("That isn't a pair code (dwp_...). Get one in Doppelwerk: Settings → Macs → Add a Mac.", file=sys.stderr)
        return 2
    try:
        identity = asyncio.run(pair(BffState(cfg.bff_dir), url, code, replace=replace, version=__version__))
    except ValueError as e:
        print(f"Not paired: {e}", file=sys.stderr)
        return 1
    except BffError as e:
        print(f"Not paired: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Not paired: couldn't reach {url} ({type(e).__name__})", file=sys.stderr)
        return 1
    print(f"Paired: this Mac is {identity.name!r} in {identity.organization!r}.")
    print("Start the runtime (doppel-runtime serve) and it checks in. Then each member adds a seat in Settings → Macs.")

    return 0


def _internal(cfg: config.Config, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    listener = cfg.listeners["internal"]
    host = "127.0.0.1" if listener.host in ("0.0.0.0", "") else listener.host
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(f"http://{host}:{listener.port}{path}", data=data, method=method,
                                     headers={"content-type": "application/json"})  # fmt: skip
    try:
        with urllib.request.urlopen(request, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def _daemon_down(cfg: config.Config) -> int:
    listener = cfg.listeners["internal"]
    where = f"{listener.host}:{listener.port}"
    print(f"The runtime isn't running here (nothing on {where}). Start it: doppel-runtime serve", file=sys.stderr)
    return 1


def _seat(cfg: config.Config, path: str, body: dict) -> int:
    try:
        status, answer = _internal(cfg, "POST", path, body)
    except OSError:
        return _daemon_down(cfg)
    if status != 200:
        print(f"No seat: {answer.get('error') or status}", file=sys.stderr)
        return 1
    seat = answer["seat"]
    who = seat.get("name") or "the member"
    if seat.get("login"):
        who += f" (@{seat['login']})"
    print(f"Seat for {who}.", flush=True)
    print(f"Now {who} signs in to Codex with their own ChatGPT account:", flush=True)
    print(f"  open   {answer['verification_url']}", flush=True)
    print(f"  enter  {answer['user_code']}", flush=True)
    tenant, user = seat["payer"].split("/")
    deadline = time.monotonic() + SIGN_IN_WAIT
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            _, state = _internal(cfg, "GET", f"/broker/{tenant}/{user}/status")
        except OSError:
            return _daemon_down(cfg)
        if state.get("state") == "connected":
            print(f"Signed in. {who} can pay for reviews on this Mac.")
            return 0
        if state.get("state") in ("signed_out", "expired"):
            print("The sign-in didn't finish. Try again: doppel-runtime seat sign-in " + seat["id"], file=sys.stderr)
            return 1
    print("Still waiting after 15 minutes. Try again: doppel-runtime seat sign-in " + seat["id"], file=sys.stderr)
    return 1


def _status(cfg: config.Config) -> int:
    try:
        status, view = _internal(cfg, "GET", "/runtime")
    except OSError:
        state = BffState(cfg.bff_dir)
        identity = state.identity()
        print(f"Paired as {identity.name!r} in {identity.organization!r}; the runtime isn't running."
              if identity else "Not paired, and the runtime isn't running.")  # fmt: skip
        return 1
    if not view.get("paired"):
        print("Not paired. Get a code in Doppelwerk (Settings → Macs → Add a Mac), then: doppel-runtime pair <code>")
        return 0
    runtime = view["runtime"]
    print(f"{runtime['name']!r} in {runtime['organization']!r} at {runtime['url']}")
    print(f"  last check-in: {view.get('last_check_in') or 'not yet'}" + ("  (paused)" if view.get("paused") else ""))
    if view.get("last_error"):
        print(f"  last error: {view['last_error']}")
    for seat in view.get("seats", []):
        login = f" @{seat['login']}" if seat.get("login") else ""
        print(f"  seat {seat['id']}  {seat.get('name') or 'member'}{login}  {seat['state']}")
    if not view.get("seats"):
        print("  no seats yet: each member adds one from Settings → Macs")
    return 0
