"""The broker's routes, for the internal listener only (never the model listener pods reach).

    POST   /broker/{tenant}/{user}/login    start a device-code sign-in -> {verification_url, user_code}
    GET    /broker/{tenant}/{user}/status   {payer, state, last_refresh, error}
    DELETE /broker/{tenant}/{user}          sign out: the payer's Codex home goes

The BFF calls these (checkpoint 8) and shows the code to the person. Nothing here returns a token.
"""

from __future__ import annotations

from aiohttp import web
from doppel_shared.ports.credentials import NotSignedIn

from doppel_model_provider.broker import Broker


def add_routes(app: web.Application, broker: Broker) -> None:
    def payer(request: web.Request) -> str:
        return f"{request.match_info['tenant']}/{request.match_info['user']}"

    async def login(request: web.Request) -> web.Response:
        try:
            code = await broker.login(payer(request))
        except NotSignedIn as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"verification_url": code.verification_url, "user_code": code.user_code})

    async def status(request: web.Request) -> web.Response:
        try:
            return web.json_response(broker.status(payer(request)))
        except NotSignedIn as e:
            return web.json_response({"error": str(e)}, status=400)

    async def disconnect(request: web.Request) -> web.Response:
        try:
            await broker.disconnect(payer(request))
        except NotSignedIn as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True})

    app.router.add_post("/broker/{tenant}/{user}/login", login)
    app.router.add_get("/broker/{tenant}/{user}/status", status)
    app.router.add_delete("/broker/{tenant}/{user}", disconnect)
