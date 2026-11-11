"""Only one copy of FleetLedger acts on the fleet (handover.py): the web UI checks at
every request, not only when it starts.

`ship-fleet ui` refuses to start on a copy on standby or retired, but its copy can
change under it while it runs: a marker written or edited by hand, or (before the UI
held the in-use lock where there was no FleetLedger folder yet, cli._in_use) a
receive landing a copy on standby under it. So, asking handover.refusal each time:

- CopyGuard refuses every request but static files on a copy that may not act, with
  the line the CLI gives (409), and closes a live connection (the sign-in terminal's
  WebSocket) at its next message;
- the poller polls no Ship (web/fleet.py), and no background job starts (web/jobs.py).

A copy that may act again (activated, or a damaged marker deleted by hand) is served
again at once, as the CLI's commands are.
"""

from __future__ import annotations

from html import escape
from typing import Any

from fleetledger import handover
from fleetledger.errors import FleetError
from fleetledger.web.security import OPEN_PREFIXES


def refusal(app: Any) -> FleetError | None:
    """Why the copy app.store_factory() names may not act now, or None: it may."""
    factory = getattr(app, "store_factory", None)
    store = factory() if factory is not None else None
    if getattr(store, "path", None) is None:  # no store with a file: no marker either
        return None
    return handover.refusal(store)


def said(why: FleetError) -> str:
    return why.message + (f" {why.hint}" if why.hint else "")


class CopyGuard:
    """Inside the gate (the session is checked already), before anything else."""

    def __init__(self, app: Any, ui: Any) -> None:
        self.app = app
        self.ui = ui

    def __getattr__(self, name: str) -> Any:  # the Starlette app's state, routes ...
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
            return
        if scope["type"] != "http" or scope.get("path", "").startswith(OPEN_PREFIXES):
            await self.app(scope, receive, send)
            return
        why = refusal(self.ui.app)
        if why is None:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower() for k, _v in scope.get("headers", [])}
        if "hx-request" in headers or scope.get("method", "GET").upper() not in ("GET",
                                                                                "HEAD"):
            body, kind = said(why).encode("utf-8"), b"text/plain; charset=utf-8"
        else:  # a page of its own: none of the UI's polling, nothing to click
            body = (f'<!doctype html><meta charset="utf-8"><title>FleetLedger</title>'
                    f'<link rel="stylesheet" href="/static/app.css"><main><h1>This copy '
                    f'does not act on the fleet</h1><p>{escape(why.message)}</p>'
                    f'<p>{escape(why.hint or "")}</p></main>').encode()
            kind = b"text/html; charset=utf-8"
        await send({"type": "http.response.start", "status": 409,
                    "headers": [(b"content-type", kind),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def _websocket(self, scope, receive, send) -> None:
        """Refused at the handshake; and once open, ended at the first message that
        arrives after the copy may no longer act (nothing it carries is passed on)."""
        if refusal(self.ui.app) is not None:
            message = await receive()  # websocket.connect
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008})
            return
        closed = False

        async def checked() -> dict:
            nonlocal closed
            message = await receive()
            if message["type"] == "websocket.receive" and refusal(self.ui.app) is not None:
                if not closed:
                    closed = True
                    await send({"type": "websocket.close", "code": 1008})
                return {"type": "websocket.disconnect", "code": 1008}
            return message

        async def guarded_send(message: dict) -> None:
            if not closed:  # after our close, the handler's own replies go nowhere
                await send(message)

        await self.app(scope, checked, guarded_send)
