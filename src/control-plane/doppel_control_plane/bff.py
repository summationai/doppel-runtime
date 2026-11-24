"""This Mac's link to Doppelwerk's BFF (docs/specs/bff-split.md §2, §4b). The runtime only ever calls out.

    pair()          a one-time pair code from Settings → Macs, plus a credential this Mac makes for itself
                    (the BFF keeps only its hash) -> which Mac this is, in which workspace
    add_seat()      a member's one-time seat code -> their seat, filed under the payer id `<workspace>/<member>`,
                    then Codex's device sign-in in that payer's own home: the member approves it in their browser
    check_in()      every half minute: finished jobs' results, then each seat's state as the broker sees it, the jobs
                    in hand and this Mac's free room out; back in: seats to sign out, pause, caps, jobs to stop, and
                    new jobs, which the JobRunner (jobs.py) starts in the sandbox.
                    A Mac the BFF says was removed signs every seat out and forgets its credential.

What lives on this disk, under `<data_dir>/bff/`, owner-only: `credential` (the Mac's own), `runtime.json`
(the BFF's address and which Mac this is) and `seats.json` (seat id -> payer and member, as the BFF last said).
The sign-ins themselves stay in the broker's Codex homes. Nothing here prints or logs a credential or a code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web
from doppel_model_provider import Broker
from doppel_model_provider.broker import DeviceCode

log = logging.getLogger(__name__)

SEAT_STATES = ("signing_in", "connected", "signed_out", "expired")


@dataclass(frozen=True)
class Identity:
    url: str
    runtime_id: str
    name: str
    organization_id: str
    organization: str


@dataclass(frozen=True)
class Seat:
    id: str
    payer: str  # <workspace id>/<member id>: the broker's Codex home for this member on this Mac
    name: str | None
    login: str | None


class BffError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class NotPaired(Exception):
    pass


def new_credential() -> str:
    return "dwr_" + secrets.token_urlsafe(32)


class BffState:
    """The files under `<data_dir>/bff/`. Each is written whole (a temporary file, then a rename), owner-only."""

    def __init__(self, folder: Path):
        self.folder = folder

    def _write(self, name: str, text: str) -> None:
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = self.folder / f".{name}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, self.folder / name)

    def _read(self, name: str) -> Any:
        try:
            return json.loads((self.folder / name).read_text())
        except (OSError, ValueError):
            return None

    def identity(self) -> Identity | None:
        raw = self._read("runtime.json")
        if not raw or not (self.folder / "credential").exists():
            return None
        return Identity(**raw)

    def credential(self) -> str:
        path = self.folder / "credential"
        if path.stat().st_mode & 0o077:
            raise PermissionError(f"{path} is readable by others; chmod 600 it")
        return path.read_text().strip()

    def save_pairing(self, identity: Identity, credential: str) -> None:
        self._write("credential", credential + "\n")
        self._write("runtime.json", json.dumps(asdict(identity), indent=2) + "\n")
        self.save_seats({})

    def seats(self) -> dict[str, Seat]:
        raw = self._read("seats.json") or {}
        return {sid: Seat(**s) for sid, s in raw.items()}

    def save_seats(self, seats: dict[str, Seat]) -> None:
        self._write("seats.json", json.dumps({sid: asdict(s) for sid, s in seats.items()}, indent=2) + "\n")

    def forget(self) -> None:
        for name in ("credential", "runtime.json", "seats.json"):
            (self.folder / name).unlink(missing_ok=True)


class BffClient:
    """JSON over HTTPS to the BFF. A refusal comes back as BffError with the BFF's own message."""

    def __init__(self, url: str, credential: str | None = None, *, version: str = "0"):
        self.url = url.rstrip("/")
        self.credential = credential
        self.version = version

    async def _post(self, path: str, body: dict) -> dict:
        headers = {"content-type": "application/json", "user-agent": f"doppel-runtime/{self.version}"}
        if self.credential:
            headers["authorization"] = f"Bearer {self.credential}"
        timeout = aiohttp.ClientTimeout(total=30)
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(f"{self.url}/api/runtime/{path}", json=body, headers=headers) as r,
        ):
            try:
                data = await r.json(content_type=None)
            except ValueError:
                data = None
            if r.status >= 400:
                message = ((data or {}).get("error") or {}).get("message") if isinstance(data, dict) else None
                raise BffError(r.status, message or f"the BFF answered {r.status}")
            return data or {}

    async def report(self, job_id: str, result: dict) -> int:
        """A job's result. Returns the BFF's status: 409 means the job isn't this Mac's any more."""
        try:
            await self._post(f"jobs/{job_id}", result)
            return 200
        except BffError as e:
            return e.status

    async def pair(self, code: str, credential: str) -> dict:
        return await self._post("pair", {"code": code, "credential": credential})

    async def redeem_seat(self, code: str) -> dict:
        return await self._post("seats", {"code": code})

    async def check_in(self, body: dict) -> dict:
        return await self._post("check-in", body)


async def pair(state: BffState, url: str, code: str, *, replace: bool = False, version: str = "0") -> Identity:
    """Redeem a pair code. The credential is made here and never leaves this Mac but as its one request."""
    current = state.identity()
    if current and not replace:
        raise ValueError(
            f"this Mac is already paired as {current.name!r} in {current.organization!r}; "
            "remove it in Doppelwerk first, or pass --replace"
        )
    credential = new_credential()
    answer = await BffClient(url, version=version).pair(code, credential)
    identity = Identity(
        url=url.rstrip("/"),
        runtime_id=answer["runtime"]["id"],
        name=answer["runtime"]["name"],
        organization_id=answer["organization"]["id"],
        organization=answer["organization"]["name"],
    )
    state.save_pairing(identity, credential)
    return identity


class BffLink:
    """The daemon's side: check-ins, and seats added through the internal listener (the CLI calls it)."""

    def __init__(self, state: BffState, broker: Broker, *, host_cap: int, version: str, client=BffClient, jobs=None):
        self.state = state
        self.broker = broker
        self.host_cap = host_cap
        self.version = version
        self._client = client
        self._lock = asyncio.Lock()  # check-ins and seat changes take turns over seats.json
        self.paused = False
        self.caps: dict[str, Any] = {}
        self.every: float | None = None  # what the BFF asked for, in seconds
        self.last_check_in: str | None = None
        self.last_error: str | None = None
        self._said_unpaired = False
        self.jobs = jobs  # the JobRunner (jobs.py); None: this runtime takes no jobs
        if jobs is not None:
            jobs.on_result = self.flush

    async def flush(self) -> None:
        """Send finished jobs' results now, rather than at the next check-in."""
        identity = self.state.identity()
        if identity is None or self.jobs is None:
            return
        client = self._client_for(identity)
        await self.jobs.flush(client.report)

    def _client_for(self, identity: Identity):
        return self._client(identity.url, self.state.credential(), version=self.version)

    def _seat_state(self, payer: str) -> str:
        try:
            state = self.broker.status(payer)["state"]
        except Exception:
            return "signed_out"
        return state if state in SEAT_STATES else "signed_out"

    async def check_in(self, now: str = "") -> dict | None:
        identity = self.state.identity()
        if identity is None:
            if not self._said_unpaired:
                log.info("bff: not paired; run `doppel-runtime pair <code> --bff <url>`")
                self._said_unpaired = True
            return None
        self._said_unpaired = False
        async with self._lock:
            # Results first: a job finished here but not yet reported would look lost to the BFF.
            if self.jobs is not None:
                try:
                    await self.jobs.flush(self._client_for(identity).report)
                except Exception as e:
                    log.warning("bff: couldn't send results (%s)", type(e).__name__)
            seats = self.state.seats()
            running = self.jobs.running() if self.jobs is not None else []
            body = {
                "version": self.version,
                "capacity": max(0, self.host_cap - len(running)),
                "running": running,
                "seats": [
                    {"id": sid, "state": self._seat_state(s.payer), "plan": None, "quota": None}
                    for sid, s in seats.items()
                ],
            }
            try:
                answer = await self._client_for(identity).check_in(body)
            except BffError as e:
                self.last_error = str(e)
                if e.status == 401:
                    log.error("bff: %s refused this Mac's credential; pair it again", identity.url)
                else:
                    log.warning("bff: check-in refused (%s)", e.status)
                return None
            except (aiohttp.ClientError, TimeoutError) as e:
                self.last_error = f"unreachable: {type(e).__name__}"
                log.warning("bff: check-in failed (%s)", type(e).__name__)
                return None
            self.last_error = None
            self.last_check_in = now or None
            if answer.get("revoked"):
                if self.jobs is not None:
                    self.jobs.stop(running)
                for seat in seats.values():
                    await self._sign_out(seat)
                self.state.forget()
                log.warning("bff: removed from %s; signed %d seat(s) out", identity.organization, len(seats))
                return answer
            for sid in answer.get("remove_seats", []):
                if sid in seats:
                    await self._sign_out(seats[sid])
                    log.info("bff: seat %s removed in Doppelwerk; signed out", sid)
            self.state.save_seats({s["id"]: _seat(s) for s in answer.get("seats", [])})
            self.paused = bool(answer.get("paused"))
            self.caps = answer.get("caps") or {}
            if self.jobs is not None:
                self.jobs.stop([j for j in answer.get("stop", []) if isinstance(j, str)])
                for job in answer.get("jobs", []):
                    if isinstance(job, dict):
                        self.jobs.accept(job)
            after = answer.get("check_in_after_seconds")
            self.every = float(min(max(after, 5), 300)) if isinstance(after, int | float) else None
            return answer

    async def _sign_out(self, seat: Seat) -> None:
        try:
            await self.broker.disconnect(seat.payer)
        except Exception as e:  # a bad payer id from the BFF: nothing of ours to remove
            log.warning("bff: couldn't sign seat %s out (%s)", seat.id, type(e).__name__)

    async def add_seat(self, code: str) -> tuple[Seat, DeviceCode]:
        identity = self.state.identity()
        if identity is None:
            raise NotPaired("this Mac isn't paired yet")
        async with self._lock:
            answer = await self._client_for(identity).redeem_seat(code)
            seat = _seat(answer["seat"])
            self.broker.homes.home(seat.payer)  # refuses a payer id that isn't <workspace>/<member>
            seats = self.state.seats()
            seats[seat.id] = seat
            self.state.save_seats(seats)
        log.info("bff: seat %s added", seat.id)
        return seat, await self.broker.login(seat.payer)

    async def sign_in(self, seat_id: str) -> tuple[Seat, DeviceCode]:
        seat = self.state.seats().get(seat_id)
        if seat is None:
            raise KeyError(seat_id)
        return seat, await self.broker.login(seat.payer)

    def view(self) -> dict:
        identity = self.state.identity()
        return {
            "paired": identity is not None,
            "runtime": asdict(identity) if identity else None,
            "paused": self.paused,
            "caps": self.caps,
            "last_check_in": self.last_check_in,
            "last_error": self.last_error,
            "seats": [{**asdict(s), "state": self._seat_state(s.payer)} for s in self.state.seats().values()]
            if identity
            else [],
        }


def _seat(raw: dict) -> Seat:
    user = raw.get("user") or {}
    return Seat(id=raw["id"], payer=raw["payer"], name=user.get("name"), login=user.get("login"))


def add_routes(app: web.Application, link: BffLink) -> None:
    """For the internal listener only: the CLI on this Mac adds seats through the running daemon, whose broker
    holds the sign-ins.

        GET  /runtime                        paired?, which Mac, paused, each seat and its state
        POST /runtime/seats                  {code} -> the seat, and Codex's device sign-in to approve
        POST /runtime/seats/{seat}/sign-in   sign a seat's member in again -> the device sign-in to approve
    """

    def refused(e: Exception) -> web.Response:
        if isinstance(e, NotPaired):
            return web.json_response({"error": str(e)}, status=409)
        if isinstance(e, BffError):
            return web.json_response({"error": str(e)}, status=e.status if 400 <= e.status < 500 else 502)
        if isinstance(e, KeyError):
            return web.json_response({"error": "no such seat on this Mac"}, status=404)
        return web.json_response({"error": f"couldn't reach Doppelwerk ({type(e).__name__})"}, status=502)

    def seat_answer(seat: Seat, code: DeviceCode) -> web.Response:
        return web.json_response(
            {"seat": asdict(seat), "verification_url": code.verification_url, "user_code": code.user_code}
        )

    async def view(_: web.Request) -> web.Response:
        return web.json_response(link.view())

    async def add_seat(request: web.Request) -> web.Response:
        body = await request.json()
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str) or not code.startswith("dws_"):
            return web.json_response({"error": "that isn't a seat code (dws_...)"}, status=400)
        try:
            return seat_answer(*await link.add_seat(code))
        except (NotPaired, BffError, aiohttp.ClientError, TimeoutError) as e:
            return refused(e)

    async def sign_in(request: web.Request) -> web.Response:
        try:
            return seat_answer(*await link.sign_in(request.match_info["seat"]))
        except KeyError as e:
            return refused(e)

    app.router.add_get("/runtime", view)
    app.router.add_post("/runtime/seats", add_seat)
    app.router.add_post("/runtime/seats/{seat}/sign-in", sign_in)
