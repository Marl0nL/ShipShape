"""Only one copy of FleetLedger acts on the fleet (handover.py): the web UI checks at
every request, not only when it starts.

`ship-fleet ui` refuses to start on a copy on standby or retired, and holds the
in-use lock while it runs (cli._in_use), so no `ship-fleet migrate` changes its copy
under it. A marker can still appear by hand, or its folder be moved aside and made
anew (the lock is on the old one). So, asking handover.refusal each time:

- CopyGuard refuses every request but static files on a copy that may not act, with
  the line the CLI gives (409; an open page's polling reloads it to that line),
  cancels the sign-ins under way, and ends the sign-in terminal's WebSocket at the
  next message either way;
- the poller polls no Ship (web/fleet.py), no background job starts (web/jobs.py)
  and no queued sign-in starts (web/flows.py). A job already running finishes: it
  checked when it started, as a CLI command does.

Once refused, the UI stays refused until it is restarted, even if the marker goes
(activated, or deleted by hand): it may hold no lock on the folder there now, and
`ship-fleet ui` checks everything again as it starts.
"""

from __future__ import annotations

import threading
from html import escape
from typing import Any

from fleetledger import handover
from fleetledger.errors import FleetError
from fleetledger.web.security import OPEN_PREFIXES

_seen: dict[str, FleetError] = {}  # copy (its ships.toml) -> the first refusal seen
_seen_lock = threading.Lock()


def refusal(app: Any) -> FleetError | None:
    """Why the copy app.store_factory() names may not act, or None: it may. Its
    marker's line now, else the one seen before in this process (never forgotten)."""
    factory = getattr(app, "store_factory", None)
    store = factory() if factory is not None else None
    if getattr(store, "path", None) is None:  # no store with a file: no marker either
        return None
    key = str(store.path)
    why = handover.refusal(store)
    with _seen_lock:
        if why is not None:
            _seen.setdefault(key, why)
            return why
        return _seen.get(key)


def said(why: FleetError) -> str:
    return why.message + (f" {why.hint}" if why.hint else "")


class CopyGuard:
    """Inside the gate (the session is checked already), before anything else."""

    def __init__(self, app: Any, ui: Any) -> None:
        self.app = app
        self.ui = ui

    def __getattr__(self, name: str) -> Any:  # the Starlette app's state, routes ...
        return getattr(self.app, name)

    def _refused(self) -> FleetError | None:
        why = refusal(self.ui.app)
        if why is not None:
            self.ui.concierge.cancel_all()  # stops the sign-ins under way (local only)
        return why

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
            return
        if scope["type"] != "http" or scope.get("path", "").startswith(OPEN_PREFIXES):
            await self.app(scope, receive, send)
            return
        why = self._refused()
        if why is None:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower() for k, _v in scope.get("headers", [])}
        extra = []
        if "hx-request" in headers:  # htmx swaps no 409: it reloads the page instead
            body, kind = said(why).encode(), b"text/plain; charset=utf-8"
            extra = [(b"hx-refresh", b"true")]
        else:  # a page of its own: none of the UI's polling, nothing to click
            body = (f'<!doctype html><meta charset="utf-8"><title>FleetLedger</title>'
                    f'<link rel="stylesheet" href="/static/app.css"><main><h1>This copy '
                    f'does not act on the fleet</h1><p>{escape(why.message)}</p>'
                    f'<p>{escape(why.hint or "")}</p></main>').encode()
            kind = b"text/html; charset=utf-8"
        await send({"type": "http.response.start", "status": 409,
                    "headers": [(b"content-type", kind),
                                (b"content-length", str(len(body)).encode()), *extra]})
        await send({"type": "http.response.body", "body": body})

    async def _websocket(self, scope, receive, send) -> None:
        """Refused at the handshake; and once open, ended at the first message either
        way after the copy may no longer act (that message is not passed on)."""
        if self._refused() is not None:
            message = await receive()  # websocket.connect
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008})
            return
        closed = False

        async def close() -> None:
            nonlocal closed
            if not closed:
                closed = True
                await send({"type": "websocket.close", "code": 1008})

        async def checked() -> dict:
            if closed:
                return {"type": "websocket.disconnect", "code": 1008}
            message = await receive()
            if message["type"] == "websocket.receive" and self._refused() is not None:
                await close()
                return {"type": "websocket.disconnect", "code": 1008}
            return message

        async def guarded_send(message: dict) -> None:
            if closed:
                return  # after our close, the handler's own replies go nowhere
            if message["type"] == "websocket.send" and self._refused() is not None:
                await close()
                return
            await send(message)

        await self.app(scope, checked, guarded_send)
