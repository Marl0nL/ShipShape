"""The FleetLedger web UI: Starlette, server-rendered pages, htmx for polling.

Every change the UI makes goes through the same `ship-fleet` command functions
the CLI uses (fleetledger.commands), so the CLI stays complete and the two can
never disagree. Pages read the background poller's cached views (fleet.py) and
never wait on SSH, except for the actions that are SSH by nature (logs,
per-Ship changes, sign-out, refresh), which run in a worker thread.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from fleetledger import commands, net, ops, signins
from fleetledger.concierge import audit, providers
from fleetledger.concierge.redact import clean
from fleetledger.errors import FleetError
from fleetledger.web import (
    actions,
    dashboard,
    network,
    settings,
    setup,
    shipsettings,
    snapshots,
    trust,
)
from fleetledger.web.fleet import SERVICE_PROVIDER, Fleet, attention_items
from fleetledger.web.flows import Concierge, WebFlow
from fleetledger.web.forms import NAME_RE, form, one
from fleetledger.web.jobs import Jobs
from fleetledger.web.security import Gate, GateMiddleware, Session

HERE = Path(__file__).resolve().parent
TAG_RE = NAME_RE
FLOW_ID_RE = re.compile(r"^[0-9]{1,9}-[0-9a-f]{8}$")
BATCH_RE = re.compile(r"^[0-9a-f]{12}$")
SERVICES = ("claude", "gcloud", "gcloud-adc", "github", "all")
AUTH_ORDER = ("claude", "gcloud", "gcloud_adc", "github")
NO_SUCH_SHIP = "No such Ship in the inventory."
LOG_LINES = ("100", "300", "1000", "3000")  # the Logs tab's "last N lines" choices
TABS = ("overview", "settings", "snapshots", "logs", "network")


@dataclasses.dataclass
class UI:
    """Everything the handlers share."""

    app: Any  # cli.App
    gate: Gate
    fleet: Fleet
    concierge: Concierge
    jobs: Jobs  # every background job: update, reboot, join, making a Ship ...
    env: Environment
    setups: Any = None  # web.setup.Setups: each session's progress through /setup
    health: Any = None  # web.settings.Health: the last "Check now" on Settings
    start_page: str | None = None  # where a new session lands first (ship-fleet setup: /setup)
    started: set[str] = dataclasses.field(default_factory=set)
    flashes: dict[str, list[tuple[str, str]]] = dataclasses.field(default_factory=dict)

    def flash(self, session: Session, kind: str, text: str) -> None:
        self.flashes.setdefault(session.id, []).append((kind, text))

    def take_flashes(self, session: Session) -> list[tuple[str, str]]:
        return self.flashes.pop(session.id, [])


# --- helpers ---------------------------------------------------------------------


def _clock(wall: float | None) -> str:
    if wall is None:
        return "never"
    return datetime.fromtimestamp(wall, UTC).astimezone().strftime("%H:%M:%S")


def make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(HERE / "templates")),
        autoescape=select_autoescape(default=True, default_for_string=True),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(
        auth_cell=commands.auth_cell,
        agents_cell=commands.agents_cell,
        usage_cell=commands.usage_cell,
        notes_cell=commands.notes_cell,
        clock=_clock,
        AUTH_ORDER=AUTH_ORDER,
        SERVICE_PROVIDER=SERVICE_PROVIDER,
        SERVICES=SERVICES,
        MODES=commands.MODES,
        SIGNIN_CHOICES=signins.CHOICES,
        SIZE=_size_limits_for_forms(),
        SIGNIN_LABELS=signins.LABELS,
        SETTING_GROUPS=shipsettings.GROUPS,
        SSH_LISTEN=shipsettings.SSH_LISTEN,
        setting_now=shipsettings.current,
        setting_older=shipsettings.older_hint,
        human_bytes=net.human_bytes,
        since=network.since,
        RANGE_LABELS=dashboard.RANGE_LABELS,
        op_confirm=dashboard.op_confirm,
    )
    env.filters["pill"] = _pill
    return env


def _pill(value: str | None) -> str:
    """A CSS class for a state or sign-in label."""
    value = (value or "").lower()
    if value in ("ok", "running", "ready", "working"):
        return "good"
    if value in ("wrong account", "host-key", "identity", "failed", "expired", "error",
                 "invalid", "provision-failed"):
        return "bad"
    if value in ("missing", "expiring", "needs-sign-in", "unreachable", "waiting", "blocked",
                 "provisioning", "timeout", "not-enrolled"):
        return "warn"
    return "muted"


def _flash_outcome(ui: UI, session: Session, outcome: actions.Outcome) -> None:
    text = " ".join([outcome.summary, *outcome.details[:5]])
    ui.flash(session, "ok" if outcome.ok else "bad", f"{outcome.ship}: {text}")


def session_of(request: Request | WebSocket) -> Session:
    return request.scope["state"]["session"]


def ui_of(request: Request | WebSocket) -> UI:
    return request.app.state.ui


def _size_limits_for_forms() -> dict[str, int]:
    """The size contract's limits, as the forms ask (memory in whole GB)."""
    from fleetledger import lifecycle

    try:
        limits = lifecycle.size_limits()
    except FleetError:
        return {}
    return {"cores_min": limits["cores"][0], "cores_max": limits["cores"][1],
            "mem_min": -(-limits["memory_mb"][0] // 1024),
            "mem_max": limits["memory_mb"][1] // 1024,
            "disk_min": limits["disk_gb"][0], "disk_max": limits["disk_gb"][1]}


def render(request: Request, template: str, status_code: int = 200, **context: Any) -> Response:
    ui = ui_of(request)
    session = session_of(request)
    context.setdefault("flashes", ui.take_flashes(session))
    html = ui.env.get_template(template).render(
        csrf=session.csrf,
        waiting_count=sum(1 for f in ui.concierge.active() if f.needs_you),
        fleet_exists=ui.app.store_factory().exists(),  # the nav: Settings, else Setup
        **context,
    )
    return HTMLResponse(html, status_code=status_code)


def partial(request: Request, template: str, **context: Any) -> Response:
    ui = ui_of(request)
    html = ui.env.get_template(template).render(csrf=session_of(request).csrf, **context)
    return HTMLResponse(html)


def back(path: str, **query: str) -> RedirectResponse:
    query = {k: v for k, v in query.items() if v}
    return RedirectResponse(path + (f"?{urlencode(query)}" if query else ""), status_code=303)


def _ship_name(request: Request) -> str | None:
    name = request.path_params.get("name", "")
    return name if NAME_RE.match(name) else None


def _known_ship(request: Request) -> str | None:
    """The {name} in the path, if it is a Ship the poller knows."""
    name = _ship_name(request)
    return name if name and name in ui_of(request).fleet.snapshot.by_name else None


def not_found(request: Request, what: str) -> Response:
    return render(request, "message.html", status_code=404, title="Not found", text=what)


# --- fleet -------------------------------------------------------------------------


def _fleet_version(ui: UI) -> str:
    return f"{ui.fleet.version}.{ui.concierge.version}.{ui.jobs.version}"


def _fleet_context(ui: UI, tag: str | None) -> dict[str, Any]:
    snap = ui.fleet.snapshot
    views = snap.views_for(tag)
    items = attention_items(views)
    flows_waiting = [f for f in ui.concierge.active() if f.needs_you]
    alerts = dashboard.alerts(views, items, flows_waiting, snap.inventory)
    return {
        "tag": tag,
        "views": views,
        "alerts": alerts,
        "summary": dashboard.summary(views, alerts, snap.inventory),
        "sparks": _quietly(dashboard.sparklines, ui.fleet.history, views, time.time()) or {},
        "jobs": ui.jobs.recent(),
        "version": _fleet_version(ui),
        "snap": snap,
    }


def _quietly(build: Any, *args: Any) -> Any:
    """Charts from the history store, or None if it cannot be read: a broken
    history file must never break a page."""
    try:
        return build(*args)
    except (OSError, sqlite3.Error):
        return None


def _charts_context(ui: UI, tag: str | None, range_: str) -> dict[str, Any]:
    views = ui.fleet.snapshot.views_for(tag)
    return {
        "tag": tag,
        "range": range_,
        "ranges": dashboard.RANGES,
        "charts": _quietly(dashboard.fleet_charts, ui.fleet.history, views, range_,
                           time.time()),
        "charts_version": ui.fleet.version,
    }


def _tag_param(request: Request) -> str | None:
    tag = request.query_params.get("tag") or None
    return tag if tag and TAG_RE.match(tag) else None


async def home(request: Request) -> Response:
    ui = ui_of(request)
    session = session_of(request)
    first = session.id not in ui.started
    ui.started.add(session.id)
    if not ui.app.store_factory().exists() or (first and ui.start_page):
        return RedirectResponse(ui.start_page or "/setup", status_code=303)
    ui.fleet.touch()
    tag = _tag_param(request)
    range_ = dashboard.range_key(request.query_params.get("range"))
    charts = _charts_context(ui, tag, range_)
    return render(request, "fleet.html", tags=ui.fleet.snapshot.tags(),
                  range=charts["range"], ranges=charts["ranges"], charts=charts["charts"],
                  charts_version=charts["charts_version"], **_fleet_context(ui, tag))


async def fleet_partial(request: Request) -> Response:
    ui = ui_of(request)
    ui.fleet.touch()
    if request.query_params.get("v") == _fleet_version(ui):
        return Response(status_code=204)
    return partial(request, "_fleet.html", **_fleet_context(ui, _tag_param(request)))


async def charts_partial(request: Request) -> Response:
    ui = ui_of(request)
    ui.fleet.touch()
    if request.query_params.get("v") == str(ui.fleet.version):
        return Response(status_code=204)
    range_ = dashboard.range_key(request.query_params.get("range"))
    return partial(request, "_charts.html", **_charts_context(ui, _tag_param(request), range_))


# --- one Ship ------------------------------------------------------------------------


def _ship_context(ui: UI, name: str) -> dict[str, Any] | None:
    snap = ui.fleet.snapshot
    view = snap.by_name.get(name)
    if view is None or snap.inventory is None:
        return None
    inv = snap.inventory
    expected = {
        service: sorted(inv.expected_account(view.ship, service).accounts)
        for service in AUTH_ORDER
    }
    flows = [f for f in ui.concierge.flows() if f.ship == name]
    agents = (view.status or {}).get("herdr", {}).get("agents", [])
    workspaces: dict[str, list[dict[str, Any]]] = {}
    for agent in agents:
        workspaces.setdefault(agent.get("workspace") or "-", []).append(agent)
    return {
        "view": view,
        "ship": view.ship,
        "doc": view.status,
        "tags": snap.effective_tags(view),
        "items": attention_items([view]),
        "expected": expected,
        "flows": flows,
        "jobs": ui.jobs.for_ship(name),
        "workspaces": workspaces,
        "snap": snap,
        **_attach_context(inv, view.ship),
        "signins_used": signins.used(view.ship),
    }


def _attach_context(inv, ship) -> dict[str, Any]:
    """How to attach from here, whether this computer can be set up for it, and
    whether the Ship is on the overlay."""
    from fleetledger import ca, trustcmd

    return {
        "attach_here": trustcmd.attaches_here(inv, ship),
        "attach_until": trustcmd.this_computer_until(inv),
        "can_trust_here": ca.config(inv) is not None,
        "overlay_set": bool(inv.overlay),
        "on_overlay": ship.overlay_node is not None,
    }


async def ship_page(request: Request) -> Response:
    ui = ui_of(request)
    ui.fleet.touch()
    name = _ship_name(request)
    tab = request.path_params.get("tab", "overview")
    context = _ship_context(ui, name) if name else None
    if context is None or tab not in TABS:
        return not_found(request, NO_SUCH_SHIP)
    if tab == "logs":
        source = request.query_params.get("source", "provision")
        if source not in commands.LOG_SOURCES:
            source = "provision"
        context["source"] = source
        context["sources"] = commands.LOG_SOURCES
        context["log"] = None
        count = request.query_params.get("lines", "300")
        count = int(count) if count in LOG_LINES else 300
        context["lines"], context["line_choices"] = count, LOG_LINES
        if request.query_params.get("source"):
            ok, lines = await run_in_threadpool(actions.logs, ui.app, name, source, count)
            context["log"] = clean("\n".join(lines))
            context["log_ok"] = ok
    if tab == "settings":
        context["tools"] = await run_in_threadpool(actions.tool_choices, ui.app,
                                                   context["view"])
    if tab == "snapshots":
        context.update(await snapshots.tab_context(ui.app, name))
    if tab == "network":  # the same snapshot the rest of the page shows
        context["netview"] = network.ship_view(context["snap"].inventory, context["ship"])
        context["net"] = network.status_digest(context["view"])
        context["week"] = request.query_params.get("week") == "1"
    if tab == "overview":
        context["alerts"] = dashboard.alerts([context["view"]], context["items"], [],
                                             context["snap"].inventory)
        range_ = dashboard.range_key(request.query_params.get("range"))
        context["history"] = _quietly(dashboard.ship_charts, ui.fleet.history,
                                      context["view"], range_, time.time())
        context["ranges"] = dashboard.RANGES
    return render(request, "ship.html", tab=tab, **context)


async def ship_refresh(request: Request) -> Response:
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    await run_in_threadpool(ui.fleet.poll, [name])
    ui.flash(session_of(request), "ok", f"Checked {name}.")
    return back("/" if one(fields, "from") == "fleet" else f"/ships/{name}")


async def ship_settings(request: Request) -> Response:
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    view = ui.fleet.snapshot.by_name.get(name)
    try:  # only what changed; the ssh_listen form sends that one key
        keys = ("ssh_listen",) if one(fields, "part") == "ssh" else None
        assignments = shipsettings.assignments(fields, view, keys)
    except FleetError as exc:
        ui.flash(session_of(request), "bad", exc.message)
        return back(f"/ships/{name}/settings")
    session = session_of(request)
    if not assignments:
        ui.flash(session, "muted", "Nothing to change.")
        return back(f"/ships/{name}/settings")
    outcome = await run_in_threadpool(actions.set_settings, ui.app, name, assignments)
    _flash_outcome(ui, session, outcome)
    await run_in_threadpool(ui.fleet.poll, [name])
    return back(f"/ships/{name}/settings")


async def ship_tools(request: Request) -> Response:
    """The Settings tab's Tools: add and remove catalogue tools, as one job."""
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    session = session_of(request)
    view = ui.fleet.snapshot.by_name.get(name)
    try:
        await run_in_threadpool(actions.change_tools, ui.jobs, ui.app, name,
                                fields.get("tool", []), view)
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(f"/ships/{name}/settings")
    ui.flash(session, "ok", "Changing its tools; its progress is above.")
    return back(f"/ships/{name}/settings")


async def ship_agents(request: Request) -> Response:
    """Add an autostart agent to a Ship (or stop starting one)."""
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    session = session_of(request)
    try:
        if one(fields, "do") == "remove":
            pair = ops.parse_autostart_change("autostart.remove", one(fields, "agent"))
        else:
            mode = one(fields, "mode")
            where = (f"github:{one(fields, 'repo')}" if one(fields, "repo")
                     else one(fields, "cwd"))
            value = f"{one(fields, 'agent')}:{where}" + (f":{mode}" if mode else "")
            pair = ops.parse_autostart_change("autostart.add", value)
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(f"/ships/{name}")
    outcome = await run_in_threadpool(actions.set_settings, ui.app, name, [pair])
    _flash_outcome(ui, session, outcome)
    await run_in_threadpool(ui.fleet.poll, [name])
    return back(f"/ships/{name}")


async def ship_signins(request: Request) -> Response:
    """Choose the sign-ins a Ship uses; the ones turned off are signed out there."""
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    session = session_of(request)
    said: list[str] = []
    try:
        wanted = signins.check(fields.get("signin", []))
        ok = await run_in_threadpool(commands.set_signins, ui.app, ui.app.store_factory(),
                                     name, wanted, said.append)
    except FleetError as exc:
        ok, said = False, [exc.message]
    ui.flash(session, "ok" if ok else "bad", " ".join(said))
    await run_in_threadpool(ui.fleet.poll, [name])
    return back(f"/ships/{name}/settings")


async def ship_size(request: Request) -> Response:
    """Resize a Ship (cores, memory, a disk that only grows), optionally restarting it."""
    from fleetledger import lifecycle

    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    session = session_of(request)
    try:
        size = setup.size_from_form(fields)
        current = ui.app.store_factory().load()
        before = current.effective_size(current.ship(name))
        size = {k: v for k, v in size.items() if v != before.get(k)}
        if not size:
            ui.flash(session, "muted", "Nothing to change.")
            return back(f"/ships/{name}/settings")
        record_only = (one(fields, "record_only") == "yes"
                       and current.ship(name).driver == "manual")  # you resized it by hand
        said = await run_in_threadpool(lifecycle.resize, ui.app, ui.app.store_factory(), name,
                                       size, restart=one(fields, "restart") == "yes",
                                       record_only=record_only)
        if record_only:
            said.append(f"Recorded {name}'s size; its VM was not touched.")
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(f"/ships/{name}/settings")
    ui.flash(session, "ok", " ".join(said))
    return back(f"/ships/{name}/settings")


async def attach_here(request: Request) -> Response:
    """Trust this computer (your key) so plain ssh and herdr --remote reach Ships."""
    from fleetledger import trustcmd

    ui = ui_of(request)
    fields = await form(request)
    name = one(fields, "ship")
    where = f"/ships/{name}" if name and NAME_RE.match(name) else "/"
    said: list[str] = []
    try:
        ok = await run_in_threadpool(trustcmd.trust_this_computer, ui.app,
                                     ui.app.store_factory(), said.append)
    except FleetError as exc:
        ok, said = False, [exc.message]
    ui.flash(session_of(request), "ok" if ok else "bad", " ".join(said) or "Nothing done.")
    return back(where)


async def ship_tags(request: Request) -> Response:
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    wanted = [t for t in re.split(r"[\s,]+", one(fields, "tags")) if t]
    session = session_of(request)
    bad = [t for t in wanted if not TAG_RE.match(t)]
    if bad:
        ui.flash(session, "bad", "Tags use a-z, 0-9 and -, up to 31 characters.")
        return back(f"/ships/{name}/settings")
    outcome = await run_in_threadpool(actions.set_tags, ui.app, name, wanted)
    _flash_outcome(ui, session, outcome)
    await run_in_threadpool(ui.fleet.poll, [name])
    return back(f"/ships/{name}/settings")


async def ship_logout(request: Request) -> Response:
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    service = one(fields, "service")
    session = session_of(request)
    if service not in SERVICES or one(fields, "confirm") != "yes":
        ui.flash(session, "bad", "Choose a service and tick the box to confirm.")
        return back(f"/ships/{name}")
    outcome = await run_in_threadpool(actions.logout, ui.app, name, service)
    _flash_outcome(ui, session, outcome)
    await run_in_threadpool(ui.fleet.poll, [name])
    return back(f"/ships/{name}")


async def ship_power(request: Request) -> Response:
    """Start or stop the Ship's VM (ship-fleet start|stop), as a job. Stop only
    after its confirm dialog."""
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    session = session_of(request)
    verb = one(fields, "power")
    if verb not in actions.POWER:
        ui.flash(session, "bad", "Unknown power action.")
        return back(f"/ships/{name}")
    if verb == "stop" and one(fields, "confirm") != "yes":
        ui.flash(session, "bad", "Tick the box to confirm.")
        return back(f"/ships/{name}")
    if verb == "stop" and any(f.ship == name for f in ui.concierge.active()):
        # A shutdown would cut the sign-in's terminal mid-flow (as a rollback would).
        ui.flash(session, "bad", f"Not stopped: a sign-in is open on {name}. Finish or "
                 "cancel it on the Sign-ins page first.")
        return back(f"/ships/{name}")
    try:
        actions.start_power(ui.jobs, ui.app, verb, name)
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(f"/ships/{name}")
    what = "Starting" if verb == "start" else "Stopping"
    ui.flash(session, "ok", f"{what} {name}'s VM; its progress is below.")
    return back(f"/ships/{name}")


async def fleet_operation(request: Request) -> Response:
    """Update, reboot or join one Ship, every Ship with a tag, or the whole fleet
    (fleetledger.ops)."""
    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    operation = one(fields, "operation")
    name, tag = one(fields, "ship"), one(fields, "tag")
    # Back where it was started: a Ship's page, or the Fleet page (from its batch
    # menu, or from an alert's button there, which says from=fleet).
    on_ship = name and NAME_RE.match(name) and one(fields, "from") != "fleet"
    where = f"/ships/{name}" if on_ship else "/"
    if operation not in actions.KINDS:
        ui.flash(session, "bad", "Unknown operation.")
        return back(where)
    if one(fields, "confirm") != "yes":
        ui.flash(session, "bad", "Tick the box to confirm.")
        return back(where)
    if (name and not NAME_RE.match(name)) or (tag and not TAG_RE.match(tag)):
        ui.flash(session, "bad", "Not a valid Ship name or tag.")
        return back(where)
    try:
        inv = ui.app.store_factory().load()
        chosen = ops.select(inv, [name] if name else [], tag or None, everything=not (name or tag))
        actions.start_operation(ui.jobs, ui.app, operation, [s.name for s in chosen])
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(where)
    label = {"join": "Joining the overlay"}.get(operation, operation.capitalize())
    if on_ship:  # started from the Ship's page: back there, where its job shows
        ui.flash(session, "ok", f"{label} started; its progress is below.")
        return back(where)
    ui.flash(session, "ok", f"{label} started; its progress is under Activity.")
    return back("/", tag=tag)


async def new_ship_page(request: Request) -> Response:
    """/new: make a Ship with every `ship-fleet new` option (web.newship)."""
    from fleetledger.web import newship

    ui = ui_of(request)
    try:
        inv = await run_in_threadpool(lambda: ui.app.store_factory().load())
    except FleetError:
        return RedirectResponse("/setup", status_code=303)  # not set up yet
    try:
        nf = await run_in_threadpool(newship.choices, ui.app, inv)
    except FleetError as exc:  # no ShipShape checkout to read the catalogue from
        return render(request, "message.html", status_code=503,
                      title="Cannot make a Ship here", text=exc.message)
    return render(request, "new.html", nf=nf)


async def new_ship_start(request: Request) -> Response:
    from fleetledger.web import newship

    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    try:
        inv = await run_in_threadpool(lambda: ui.app.store_factory().load())
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back("/new")
    try:
        name, argv = await run_in_threadpool(newship.argv, fields, ui.app, inv)
        newship.start(ui, name, argv)
    except FleetError as exc:  # back to the form as it was posted, with the reason
        try:
            nf = await run_in_threadpool(newship.choices, ui.app, inv)
        except FleetError:
            ui.flash(session, "bad", exc.message)
            return back("/new")
        return render(request, "new.html", status_code=400, flashes=[("bad", exc.message)],
                      nf=newship.refill(nf, fields))
    ui.flash(session, "ok", f"Making {name}; its progress is under Activity. Once it is "
             "up, it has a page of its own.")
    return back("/")


async def finish_enrolling(request: Request) -> Response:
    """A Ship in the inventory but not enrolled: `ship-fleet status N --wait` as a job."""
    from fleetledger.web.jobs import run_cli

    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    session = session_of(request)
    try:
        ship = ui.app.store_factory().load().ship(name)
        if ship.enrolled is not None:
            raise FleetError(f"{name} is enrolled already.")
        ui.jobs.start("enrol", [name],
                      lambda job: run_cli(ui.app, ["status", name, "--wait"], job),
                      f"Finish enrolling {name}")
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(f"/ships/{name}")
    ui.flash(session, "ok", f"Waiting for {name} to finish its first boot, then enrolling "
             "it; its progress is below.")
    return back(f"/ships/{name}")


async def job_card(request: Request) -> Response:
    """One job's card, for htmx polling: 204 while unchanged, 286 (stop) once gone."""
    ui = ui_of(request)
    raw = request.path_params.get("job", "")
    job = ui.jobs.get(int(raw)) if raw.isdigit() else None
    if job is None:
        return Response(status_code=286)
    if request.query_params.get("v") == str(job.version):
        return Response(status_code=204)
    response = partial(request, "_job.html", job=job)
    if job.done and job.kind in actions.POWER:  # the VM is on or off now: redraw the bar
        response.headers["HX-Refresh"] = "true"
    if job.done and job.kind in trust.KINDS:  # certificates or machines changed: redraw
        response.headers["HX-Refresh"] = "true"
    return response


# --- network visibility ----------------------------------------------------------------


async def ship_network_refresh(request: Request) -> Response:
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    session = session_of(request)
    try:
        problems = await run_in_threadpool(network.refresh, ui.app, [name])
    except FleetError as exc:
        problems = {name: exc.message}
    for ship, problem in problems.items():
        ui.flash(session, "bad", f"{ship}: {problem}")
    return back(f"/ships/{name}/network")


async def network_page(request: Request) -> Response:
    ui = ui_of(request)
    snap = ui.fleet.snapshot
    tag = _tag_param(request)
    who = (request.query_params.get("who") or "").strip()[:253]
    found, who_error = None, None
    if snap.inventory is None:
        return render(request, "message.html", title="Network", text=snap.error or "No inventory.")
    if who:
        try:
            found = net.who_talked(snap.inventory, who, tag)
        except FleetError as exc:
            who_error = exc.message
    return render(request, "network.html", tag=tag, tags=snap.tags(), who=who,
                  found=found, who_error=who_error,
                  new=net.new_destinations(snap.inventory, tag)[:200],
                  views=[network.ship_view(snap.inventory, v.ship) for v in snap.views_for(tag)])


async def network_refresh(request: Request) -> Response:
    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    tag = one(fields, "tag")
    if tag and not TAG_RE.match(tag):
        tag = ""
    try:
        problems = await run_in_threadpool(network.refresh, ui.app, None, tag or None)
    except FleetError as exc:
        problems = {"fleet": exc.message}
    for ship, problem in problems.items():
        ui.flash(session, "bad", f"{ship}: {problem}")
    if not problems:
        ui.flash(session, "ok", "Network summaries refreshed.")
    return back("/network", tag=tag)


# --- sign-ins (the concierge) ---------------------------------------------------------


async def signin_page(request: Request) -> Response:
    ui = ui_of(request)
    batch = request.query_params.get("batch")
    batch = batch if batch and BATCH_RE.match(batch) else None
    flows = ui.concierge.batch(batch) if batch else ui.concierge.flows()
    return render(request, "signin.html", flows=flows, batch=batch,
                  tags=ui.fleet.snapshot.tags(),
                  ships=[v.ship.name for v in ui.fleet.snapshot.views])


async def signin_start(request: Request) -> Response:
    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    service = one(fields, "service")
    tag = one(fields, "tag")
    names = [n.strip() for n in fields.get("ship", []) if n.strip()]
    if service not in SERVICES:
        ui.flash(session, "bad", "Choose a service.")
        return back("/signin")
    if any(not NAME_RE.match(n) for n in names) or (tag and not TAG_RE.match(tag)):
        ui.flash(session, "bad", "Not a valid Ship name or tag.")
        return back("/signin")
    try:
        inv = ui.app.store_factory().load()
        ships = [s.name for s in inv.select(names, tag or None)]
        batch = ui.concierge.start(ships, service)
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back("/signin")
    return back("/signin", batch=batch)


def _flow(request: Request) -> WebFlow | None:
    flow_id = request.path_params.get("flow", "")
    if not FLOW_ID_RE.match(flow_id):
        return None
    return ui_of(request).concierge.get(flow_id)


async def flow_card(request: Request) -> Response:
    flow = _flow(request)
    if flow is None:
        return Response(status_code=286)  # htmx: stop polling
    if request.query_params.get("v") == str(flow.version):
        return Response(status_code=204)
    return partial(request, "_flow.html", flow=flow)


async def flow_code(request: Request) -> Response:
    ui = ui_of(request)
    flow = _flow(request)
    if flow is None:
        return not_found(request, "That sign-in has gone.")
    fields = await form(request)
    values = fields.get("code", [])
    refused = flow.submit_code(values[0] if len(values) == 1 else "")
    del fields, values  # the code goes to the engine only
    if refused:
        ui.flash(session_of(request), "bad", refused)
    return back("/signin", batch=flow.batch)


async def flow_cancel(request: Request) -> Response:
    flow = _flow(request)
    if flow is None:
        return not_found(request, "That sign-in has gone.")
    flow.cancel()
    return back("/signin", batch=flow.batch)


async def flow_live(request: Request) -> Response:
    flow = _flow(request)
    if flow is None:
        return not_found(request, "That sign-in has gone.")
    fields = await form(request)
    take_over = one(fields, "take_over") == "yes"
    if flow.decide_live(take_over) and take_over:
        # Wait briefly for the engine to hand over the terminal.
        for _ in range(20):
            if flow.terminal is not None:
                break
            await asyncio.sleep(0.05)
        return back(f"/flows/{flow.id}/terminal")
    return back("/signin", batch=flow.batch)


async def flow_terminal(request: Request) -> Response:
    flow = _flow(request)
    if flow is None:
        return not_found(request, "That sign-in has gone.")
    return render(request, "terminal.html", flow=flow)


async def flow_socket(websocket: WebSocket) -> None:
    """The live terminal. The gate has checked the cookie and Origin; the first
    message must carry the session's CSRF token."""
    ui = ui_of(websocket)
    session = session_of(websocket)
    flow_id = websocket.path_params.get("flow", "")
    flow = ui.concierge.get(flow_id) if FLOW_ID_RE.match(flow_id) else None
    await websocket.accept()
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        hello = json.loads(first)
    except (TimeoutError, WebSocketDisconnect, json.JSONDecodeError, KeyError):
        await websocket.close(code=1008)
        return
    if not isinstance(hello, dict) or not ui.gate.csrf_ok(session, str(hello.get("csrf", ""))):
        await websocket.close(code=1008)
        return
    bridge = flow.terminal if flow is not None else None
    if bridge is None:
        await websocket.send_text(json.dumps({"ended": True}))
        await websocket.close()
        return

    async def pump_out() -> None:
        position = 0
        while True:
            text, position = bridge.read_since(position)
            if text:
                await websocket.send_text(json.dumps({"out": clean(text)}))
            if bridge.ended.is_set() or bridge.closed.is_set():
                text, position = bridge.read_since(position)
                if text:
                    await websocket.send_text(json.dumps({"out": clean(text)}))
                await websocket.send_text(json.dumps({"ended": True}))
                return
            await asyncio.sleep(0.1)

    async def pump_in() -> None:
        while True:
            message = await websocket.receive_text()
            try:
                data = json.loads(message)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("leave"):
                bridge.close()
                return
            text = data.get("in")
            if isinstance(text, str):
                bridge.send(text)

    out_task = asyncio.create_task(pump_out())
    in_task = asyncio.create_task(pump_in())
    try:
        await asyncio.wait({out_task, in_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (out_task, in_task):
            task.cancel()
        try:
            await websocket.close()
        except RuntimeError:
            pass


# --- audit ------------------------------------------------------------------------------


async def audit_page(request: Request) -> Response:
    try:
        entries = audit.read()[-200:]
    except (OSError, ValueError):
        entries = []
    return render(request, "audit.html", entries=list(reversed(entries)))


# --- the app ------------------------------------------------------------------------------


def create_app(app: Any, gate: Gate, *, fleet: Fleet | None = None,
               concierge_: Concierge | None = None, start_page: str | None = None,
               jobs: Jobs | None = None) -> Starlette:
    """The ASGI app behind the gate. `app` is a cli.App (the outside world)."""
    fleet = fleet or Fleet(app)
    concierge_ = concierge_ or Concierge(
        app, on_finished=lambda ship: fleet.poll([ship])
    )
    providers.load_all()  # fail at start, not on first sign-in, if a provider file is bad
    routes = [
        Route("/", home),
        Route("/partials/fleet", fleet_partial),
        Route("/partials/charts", charts_partial),
        Route("/ships/{name}", ship_page),
        Route("/ships/{name}/{tab}", ship_page),
        Route("/ships/{name}/refresh", ship_refresh, methods=["POST"]),
        Route("/ships/{name}/settings", ship_settings, methods=["POST"]),
        Route("/ships/{name}/tags", ship_tags, methods=["POST"]),
        Route("/ships/{name}/agents", ship_agents, methods=["POST"]),
        Route("/ships/{name}/tools", ship_tools, methods=["POST"]),
        Route("/ships/{name}/size", ship_size, methods=["POST"]),
        Route("/ships/{name}/signins", ship_signins, methods=["POST"]),
        Route("/attach-here", attach_here, methods=["POST"]),
        Route("/ships/{name}/logout", ship_logout, methods=["POST"]),
        Route("/ships/{name}/power", ship_power, methods=["POST"]),
        Route("/operations", fleet_operation, methods=["POST"]),
        Route("/ships/{name}/network/refresh", ship_network_refresh, methods=["POST"]),
        Route("/network", network_page),
        Route("/network/refresh", network_refresh, methods=["POST"]),
        Route("/signin", signin_page),
        Route("/signin", signin_start, methods=["POST"]),
        Route("/flows/{flow}/card", flow_card),
        Route("/flows/{flow}/code", flow_code, methods=["POST"]),
        Route("/flows/{flow}/cancel", flow_cancel, methods=["POST"]),
        Route("/flows/{flow}/live", flow_live, methods=["POST"]),
        Route("/flows/{flow}/terminal", flow_terminal),
        WebSocketRoute("/flows/{flow}/ws", flow_socket),
        Route("/audit", audit_page),
        Route("/jobs/{job}", job_card),
        Route("/new", new_ship_page),
        Route("/new", new_ship_start, methods=["POST"]),
        Route("/ships/{name}/enrol", finish_enrolling, methods=["POST"]),
        *setup.routes(),
        *snapshots.routes(),
        *settings.routes(),
        *trust.routes(),
        Mount("/static", app=StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    web = Starlette(routes=routes)
    web.state.ui = UI(app=app, gate=gate, fleet=fleet, concierge=concierge_,
                      jobs=jobs or Jobs(app, on_finished=lambda ships: fleet.poll(ships)),
                      env=make_env(),
                      setups=setup.Setups(), start_page=start_page)
    return GateMiddleware(web, gate)
