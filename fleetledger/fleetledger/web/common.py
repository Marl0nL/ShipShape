"""Request helpers every page module shares.

A leaf module: it imports nothing from the route modules or from app.py (only
the job runner, web/jobs.py), so any of them can import it at the top. Helpers
that render a page (render, not_found, partial) stay in app.py, because rendering
pulls in the rest of the UI.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import RedirectResponse
from starlette.websockets import WebSocket

from fleetledger.errors import FleetError
from fleetledger.web import jobs
from fleetledger.web.forms import NAME_RE, form
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


class NoSuchShip(Exception):
    """The {name} in the path is not a Ship the poller knows. app.py turns it into the
    "No such Ship" 404 page."""


def ship_of(request: Request) -> str:
    """The Ship named in the path; NoSuchShip (a 404) unless the poller knows it."""
    name = known_ship(request)
    if name is None:
        raise NoSuchShip(request.path_params.get("name", ""))
    return name


class ShipPost(NamedTuple):
    """What a POST /ships/{name}/... handler starts with."""

    ui: UI
    name: str
    fields: dict[str, list[str]]
    session: Session


async def ship_form(request: Request) -> ShipPost:
    """For a POST /ships/{name}/... handler: the Ship (checked before the form is read;
    NoSuchShip if the poller does not know it), the posted form and the session."""
    name = ship_of(request)
    return ShipPost(ui_of(request), name, await form(request), session_of(request))


def start_cli_job(request: Request, kind: str, ships: list[str], argv: list[str], title: str,
                  *, said: str | None = None, wrap: Callable[[Any, Any], Any] | None = None,
                  locks: Iterable[str] = (), after: Callable[[Any], None] | None = None) -> bool:
    """Start `ship-fleet ARGV` as a background job and flash how that went: why it was
    refused, or `said` once it has started (nothing if said is None). True if it started.

    wrap(app, job), if given, is the app the command runs with (jobs.RecordingApp keeps
    a driver's answer on the job); after(job) runs once the command has succeeded."""
    ui = ui_of(request)

    def run(job: Any) -> int:
        code = jobs.run_cli(ui.app if wrap is None else wrap(ui.app, job), argv, job)
        if code == 0 and after is not None:
            after(job)
        return code

    try:
        ui.jobs.start(kind, ships, run, title=title, locks=list(locks))
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return False
    if said is not None:
        flash(request, "ok", said)
    return True
