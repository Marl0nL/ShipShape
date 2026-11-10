"""Request helpers every page module shares.

A leaf module: it imports nothing from the route modules or from app.py, so any
of them can import it at the top. Helpers that render a page (render, not_found,
partial) stay in app.py, because rendering pulls in the rest of the UI.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.websockets import WebSocket

from fleetledger.web.forms import NAME_RE
from fleetledger.web.security import Session

if TYPE_CHECKING:
    from fleetledger.web.app import UI

NO_SUCH_SHIP = "No such Ship in the inventory."
NOT_FROM_A_DEVICE = ("That sets up the FleetLedger computer itself, not this device: open the "
                     "UI on that computer for it. For this device, choose its trusted machine "
                     "and download that machine's bundle (a Ship's Access tab).")


def session_of(request: Request | WebSocket) -> Session:
    return request.scope["state"]["session"]


def ui_of(request: Request | WebSocket) -> UI:
    return request.app.state.ui


def flash(request: Request, kind: str, text: str) -> None:
    """A message for this session's next page ("ok", "bad" or "muted")."""
    ui_of(request).flash(session_of(request), kind, text)


def back(path: str, **query: str) -> RedirectResponse:
    query = {k: v for k, v in query.items() if v}
    return RedirectResponse(path + (f"?{urlencode(query)}" if query else ""), status_code=303)


def ship_name(request: Request) -> str | None:
    """The {name} in the path, if it is a valid Ship name."""
    name = request.path_params.get("name", "")
    return name if NAME_RE.match(name) else None


def known_ship(request: Request) -> str | None:
    """The {name} in the path, if it is a Ship the poller knows."""
    name = ship_name(request)
    return name if name and name in ui_of(request).fleet.snapshot.by_name else None
