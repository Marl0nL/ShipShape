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

from fleetledger import attempts, commands, inbox, net, ops, signinlist, signins
from fleetledger.concierge import audit, providers
from fleetledger.concierge.redact import clean
from fleetledger.errors import FleetError
from fleetledger.web import (
    actions,
    activity,
    adopt,
    dashboard,
    destroy,
    devicesweb,
    fleetactions,
    images,
    inboxweb,
    network,
    overlayweb,
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
TABS = ("overview", "access", "settings", "inbox", "snapshots", "logs", "network")


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
    spool: Any = None  # web.inboxweb.Spool: files dropped for a Ship, waiting to be sent
    start_page: str | None = None  # where a new session lands first (ship-fleet setup: /setup)
    started: set[str] = dataclasses.field(default_factory=set)
    destroyed: dict[str, tuple[float, bool]] = dataclasses.field(  # name -> (when, forgot)
        default_factory=dict)
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


def _clock_dated(wall: float) -> str:
    """A time for a record that outlives the day (a saved attempt): the date too,
    unless it is today."""
    when = datetime.fromtimestamp(wall, UTC).astimezone()
    if when.date() == datetime.now(UTC).astimezone().date():
        return when.strftime("%H:%M:%S")
    return f"{when.day} {when:%b %Y} {when:%H:%M}"


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
        clock_dated=_clock_dated,
        attempt_saved_at=attempts.saved_at,
        took=activity.took,
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
        restartable=shipsettings.restartable,
        restartable_by_name=lambda view: {r["name"]: r for r in shipsettings.restartable(view)},
        human_bytes=net.human_bytes,
        inbox_size=inbox.human,
        since=network.since,
        RANGE_LABELS=dashboard.RANGE_LABELS,
        op_confirm=dashboard.op_confirm,
        ago=signinlist.ago,
        signin_url=_signin_url,
        SIGNIN_GROUPS=signinlist.GROUPS,
        SIGNIN_GROUP_LABELS=signinlist.GROUP_LABELS,
        SIGNIN_SERVICES=signinlist.SERVICES,
        SIGNIN_SERVICE_LABELS=signinlist.SERVICE_LABELS,
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
    snap = ui.fleet.snapshot
    html = ui.env.get_template(template).render(
        csrf=session.csrf,
        # the nav's badge: every sign-in that needs you (the Sign-ins page's first group)
        signins_due=signinlist.needing_action(snap.views, snap.inventory,
                                              ui.concierge.flows()),
        fleet_exists=ui.app.store_factory().exists(),  # the nav: Settings, else Setup
        here=activity.here(request),  # where a card's Dismiss comes back to
        this_device=session.device,  # a paired browser on the remote address, else None
        remote_on=ui.gate.remote is not None,
        machine_due=_machine_due(ui, session),
        **context,
    )
    return HTMLResponse(html, status_code=status_code)


NOT_FROM_A_DEVICE = ("That sets up the FleetLedger computer itself, not this device: open the "
                     "UI on that computer for it. For this device, choose its trusted machine "
                     "and download that machine's bundle (a Ship's Access tab).")


def _machine_due(ui: UI, session: Session) -> dict[str, Any] | None:
    """On a paired device whose machine you said: that machine's certificate, if it lapses
    within a day or has (a banner offers its bundle). Read from the poller's inventory."""
    from fleetledger import ca, trustcmd
    from fleetledger.web import devicesweb

    inv = ui.fleet.snapshot.inventory
    if session.device is None or session.device.machine is None or inv is None:
        return None
    m = devicesweb.machine_of(session.device, inv)
    until = ca.parse_stamp(m.last_valid_before) if m else None
    if m is None or until is None:
        return None
    left = (until - ca.utc_now()).total_seconds()
    if left > trustcmd.RENEW_WITHIN_S:
        return None
    return {"name": m.name, "until": m.last_valid_before, "lapsed": left <= 0}


def partial(request: Request, template: str, **context: Any) -> Response:
    ui = ui_of(request)
    html = ui.env.get_template(template).render(csrf=session_of(request).csrf,
                                                here=activity.here(request), **context)
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


def _attaches(session: Session | None, inv: Any) -> dict[str, Any]:
    """How the alerts' attach commands are worked out for this browser (dashboard.alerts):
    from a paired device, its own machine's (and a fallback that says it runs on the
    FleetLedger computer); else this computer's, as ever."""
    from fleetledger.web import devicesweb

    if session is None or session.device is None:
        return {}
    device = session.device
    return {"attaches": lambda ship: devicesweb.logins_on(device, inv, ship)[0],
            "remote": True}


def _fleet_context(ui: UI, tag: str | None, session: Session | None = None) -> dict[str, Any]:
    snap = ui.fleet.snapshot
    views = snap.views_for(tag)
    items = attention_items(views)
    flows_waiting = [f for f in ui.concierge.active() if f.needs_you]
    alerts = dashboard.alerts(views, items, flows_waiting, snap.inventory,
                              **_attaches(session, snap.inventory))
    jobs = ui.jobs.recent()
    return {
        "tag": tag,
        "views": views,
        "alerts": alerts,
        "summary": dashboard.summary(views, alerts, snap.inventory),
        "sparks": _quietly(dashboard.sparklines, ui.fleet.history, views, time.time()) or {},
        "jobs": [j for j in jobs if not j.dismissed],
        # All jobs, dismissed ones too: a dismissed failed-create card does not come
        # back as its saved attempt's card (the New Ship page still lists it).
        "saved_attempts": _saved_attempts(snap.inventory, jobs),
        "version": _fleet_version(ui),
        "snap": snap,
        "can_renew": _has_ca(snap.inventory),
        "overlay_set": bool(snap.inventory is not None and snap.inventory.overlay),
    }


def _saved_attempts(inv: Any, jobs: list[Any]) -> list[Any]:
    """Failed attempts at making a Ship (attempts.py) no job card here shows already,
    so a failure is still in Activity after a restart."""
    try:
        saved = attempts.current(inv)
    except OSError:
        return []
    # A card offers Edit and retry only for the attempt as saved when its job ended.
    shown = {j.result.get("retry") for j in jobs
             if any(a.name == j.result.get("retry") and a.at == j.result.get("retry_at")
                    for a in saved)}
    shown |= {s for j in jobs if j.running for s in j.ships}
    return [a for a in saved if a.name not in shown]


def _has_ca(inv: Any) -> bool:
    from fleetledger import ca

    try:
        return inv is not None and ca.config(inv) is not None
    except FleetError:
        return False


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
                  charts_version=charts["charts_version"],
                  **_fleet_context(ui, tag, session_of(request)))


async def fleet_partial(request: Request) -> Response:
    ui = ui_of(request)
    ui.fleet.touch()
    if request.query_params.get("v") == _fleet_version(ui):
        return Response(status_code=204)
    return partial(request, "_fleet.html",
                   **_fleet_context(ui, _tag_param(request), session_of(request)))


async def charts_partial(request: Request) -> Response:
    ui = ui_of(request)
    ui.fleet.touch()
    if request.query_params.get("v") == str(ui.fleet.version):
        return Response(status_code=204)
    range_ = dashboard.range_key(request.query_params.get("range"))
    return partial(request, "_charts.html", **_charts_context(ui, _tag_param(request), range_))


# --- one Ship ------------------------------------------------------------------------


def _ship_context(ui: UI, name: str, session: Session | None = None) -> dict[str, Any] | None:
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
        "jobs": ui.jobs.for_ship(name, shown=True),
        "workspaces": workspaces,
        "snap": snap,
        **_attach_context(inv, view.ship, session),
        "signins_used": signins.used(view.ship),
    }


def _attach_context(inv, ship, session: Session | None = None) -> dict[str, Any]:
    """How to attach from the machine this browser is on, whether it can be set up for
    it, and whether the Ship is on the overlay. A browser on this computer: this
    computer's trust (this-computer.json), as ever. A paired device: the trusted
    machine it runs on (devices.Device.machine), if you said; never this computer's."""
    from fleetledger import ca, trustcmd
    from fleetledger.web import devicesweb

    device = session.device if session is not None else None
    common = {"can_trust_here": ca.config(inv) is not None, "overlay_set": bool(inv.overlay),
              "on_overlay": ship.overlay_node is not None, "remote_session": device is not None}
    if device is None:
        here = trustcmd.attaches_here(inv, ship)
        return {**common, "attach_here": here, "attach_owner": here,
                "attach_until": trustcmd.this_computer_until(inv), "here_machine": None,
                "here_label": "this computer",
                "attach_cmd": ("herdr --remote " if here else "ship-fleet ssh ") + ship.name}
    m = devicesweb.machine_of(device, inv)
    agent, owner = devicesweb.logins_on(device, inv, ship)
    return {**common, "attach_here": agent, "attach_owner": owner,
            "attach_until": m.last_valid_before if m else None,
            "here_machine": m.name if m else None, "here_label": m.name if m else device.name,
            "machine_choices": devicesweb.machine_choices(inv),
            "attach_cmd": (f"herdr --remote {ship.name}" if agent else
                           f"ssh {inv.owner}@{ship.name}" if owner else None)}


async def ship_page(request: Request) -> Response:
    ui = ui_of(request)
    ui.fleet.touch()
    name = _ship_name(request)
    tab = request.path_params.get("tab", "overview")
    context = _ship_context(ui, name, session_of(request)) if name else None
    if context is None and name and (gone := destroy.gone_since(ui, name)) is not None:
        at, forgotten = gone
        text = (f"{name} was forgotten at {_clock(at)}: it is no longer in the inventory, and "
                "its VM was not touched." if forgotten else
                f"{name} was destroyed at {_clock(at)}: its VM, its seed and its entry in the "
                "inventory are gone.")
        return render(request, "message.html", status_code=410, title=f"{name} is gone",
                      text=text)
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
    if tab == "inbox":
        context.update(await inboxweb.tab_context(ui.app, name))
    if tab == "access":
        context.update(overlayweb.tab_context(context["snap"].inventory, context["ship"],
                                              context["doc"]))
    if tab == "network":  # the same snapshot the rest of the page shows
        context["netview"] = network.ship_view(context["snap"].inventory, context["ship"])
        context["net"] = network.status_digest(context["view"])
        context["week"] = request.query_params.get("week") == "1"
    if tab == "overview":
        context["alerts"] = dashboard.alerts([context["view"]], context["items"], [],
                                             context["snap"].inventory,
                                             **_attaches(session_of(request),
                                                         context["snap"].inventory))
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
    if one(fields, "from") == "signin":
        return _signin_back(fields)
    return back("/" if one(fields, "from") == "fleet" else f"/ships/{name}")


async def ship_settings(request: Request) -> Response:
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    view = ui.fleet.snapshot.by_name.get(name)
    ssh = one(fields, "part") == "ssh"
    if ssh and one(fields, "confirm") != "yes":  # it can cut the LAN path to the Ship
        ui.flash(session_of(request), "bad", "Tick the box to confirm.")
        return back(f"/ships/{name}/settings")
    if ssh and (view is None or view.ship.overlay_node is None):
        ui.flash(session_of(request), "bad", f"{name} is not on the overlay.")
        return back(f"/ships/{name}/settings")
    try:  # only what changed; the ssh_listen form sends that one key
        keys = ("ssh_listen",) if ssh else None
        assignments = shipsettings.assignments(fields, view, keys)
    except FleetError as exc:
        ui.flash(session_of(request), "bad", exc.message)
        return back(f"/ships/{name}/settings")
    session = session_of(request)
    if not assignments:
        ui.flash(session, "muted", "Nothing to change.")
        return back(f"/ships/{name}/settings")
    outcome = await run_in_threadpool(actions.set_settings, ui.app, name, assignments)
    mode_changed = outcome.ok and any(a.startswith("default_mode=") for a in assignments)
    if mode_changed:  # the page offers the swap itself, instead of the CLI's command
        outcome = dataclasses.replace(outcome, details=tuple(
            d for d in outcome.details if not d.startswith("Running agents keep")))
    _flash_outcome(ui, session, outcome)
    if mode_changed:
        ui.flash(session, "muted", "Running agents keep the mode they are in; the new default "
                 "applies to agents started from now on. To switch them now, use Apply the "
                 "default mode to running agents, below.")
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


async def ship_agent_restart(request: Request) -> Response:
    """Restart running agents in place, in a new mode (ship-fleet agent restart), as a
    job, after its confirm dialog: one agent from the Overview's agent list, or the
    agents not in the mode they would start in, from the Settings tab."""
    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    session = session_of(request)
    where = f"/ships/{name}" + ("/settings" if one(fields, "from") == "settings" else "")
    if one(fields, "confirm") != "yes":
        ui.flash(session, "bad", "Tick the box to confirm.")
        return back(where)
    agents = [a.strip() for a in fields.get("agent", []) if a.strip()]
    try:
        actions.start_agent_restart(ui.jobs, ui.app, name, agents,
                                    mode=one(fields, "mode") or None,
                                    fresh=one(fields, "fresh") == "yes",
                                    force=one(fields, "force") == "yes",
                                    poll=ui.fleet.poll)
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(where)
    ui.flash(session, "ok", f"Restarting {', '.join(agents)}; the progress is above.")
    return back(where)


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
    """Trust this computer (your key) so plain ssh and herdr --remote reach Ships. Never
    from a paired device: it would trust the FleetLedger computer, not the device."""
    from fleetledger import trustcmd

    ui = ui_of(request)
    fields = await form(request)
    name = one(fields, "ship")
    where = f"/ships/{name}" if name and NAME_RE.match(name) else "/"
    if where != "/" and one(fields, "from") == "access":
        where += "/access"
    if session_of(request).device is not None:
        ui.flash(session_of(request), "bad", NOT_FROM_A_DEVICE)
        return back(where)
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
    done = _signin_back(fields) if one(fields, "from") == "signin" else back(f"/ships/{name}")
    if service not in SERVICES or one(fields, "confirm") != "yes":
        ui.flash(session, "bad", "Choose a service and tick the box to confirm.")
        return done
    outcome = await run_in_threadpool(actions.logout, ui.app, name, service)
    _flash_outcome(ui, session, outcome)
    await run_in_threadpool(ui.fleet.poll, [name])
    return done


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
    (fleetledger.ops). force=yes forces a reboot past a shutdown lock (one Ship,
    from its Force reboot dialog)."""
    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    operation = one(fields, "operation")
    force = one(fields, "force") == "yes"
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
    if force and (operation != "reboot" or not name):
        ui.flash(session, "bad", "Only one Ship's reboot can be forced.")
        return back(where)
    try:
        inv = ui.app.store_factory().load()
        chosen = ops.select(inv, [name] if name else [], tag or None, everything=not (name or tag))
        if operation == "join":  # only the Ships not on it yet
            chosen = [s for s in chosen if s.overlay_node is None]
            if not chosen:
                raise FleetError("Every one of those Ships is on the overlay already.")
        actions.start_operation(ui.jobs, ui.app, operation, [s.name for s in chosen],
                                force=force)
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(where)
    label = "Force reboot" if force else \
        {"join": "Joining the overlay"}.get(operation, operation.capitalize())
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
    saved = await run_in_threadpool(attempts.current, inv)
    wanted = request.query_params.get("retry", "")
    retry = next((a for a in saved if a.name == wanted), None)
    if wanted and retry is None:
        ui.flash(session_of(request), "bad", f"There is no saved attempt at {wanted} "
                 "(it was made since, or discarded).")
        return back("/new")
    if retry is not None:
        nf = newship.refill(nf, newship.fields_from(retry.options, nf))
    return render(request, "new.html", nf=nf, retry=retry, saved=saved,
                  blocker=retry and retry.blocked,
                  clearing=[retry] if retry is not None and retry.left else [])


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
    retry = await run_in_threadpool(newship.retry_of, fields)
    clearing = await run_in_threadpool(newship.clearing, retry, one(fields, "name"))
    try:
        name, argv = await run_in_threadpool(newship.argv, fields, ui.app, inv)
        # Nothing on the hypervisor is removed unless the dialog named it: the plain
        # form too, when its name is a failed attempt's that left something.
        cleared = newship.check_confirmed(fields, clearing)
        newship.start(ui, name, argv, retry_of=retry.name if retry else None,
                      cleared=cleared, from_device=session_of(request).device is not None)
    except FleetError as exc:  # back to the form as it was posted, with the reason
        try:
            nf = await run_in_threadpool(newship.choices, ui.app, inv)
        except FleetError:
            ui.flash(session, "bad", exc.message)
            return back("/new")
        saved = await run_in_threadpool(attempts.current, inv)
        return render(request, "new.html", status_code=400, flashes=[("bad", exc.message)],
                      nf=newship.refill(nf, fields), retry=retry, saved=saved, blocker=None,
                      clearing=clearing)
    ui.flash(session, "ok", f"Making {name}; its progress is under Activity. Once it is "
             "up, it has a page of its own.")
    return back("/")


async def discard_attempt(request: Request) -> Response:
    """Forget a saved failed attempt at making a Ship (attempts.py)."""
    name = request.path_params.get("name", "")
    if NAME_RE.match(name) and await run_in_threadpool(attempts.drop, name):
        ui_of(request).flash(session_of(request), "ok", f"Discarded the attempt at {name}.")
    return back("/new")


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


def _on_ship_page(request: Request, ships: list[str]) -> bool:
    """Whether htmx polled from one of these Ships' pages (not, say, the Fleet page)."""
    from urllib.parse import urlsplit

    path = urlsplit(request.headers.get("HX-Current-URL", "")).path
    return any(path == f"/ships/{s}" or path.startswith(f"/ships/{s}/") for s in ships)


async def job_card(request: Request) -> Response:
    """One job's card, for htmx polling: 204 while unchanged, 286 (stop) once gone."""
    ui = ui_of(request)
    raw = request.path_params.get("job", "")
    job = ui.jobs.get(int(raw)) if raw.isdigit() else None
    if job is None:
        return Response(status_code=286)
    if request.query_params.get("v") == str(job.version):
        return Response(status_code=204)
    on_ship_page = _on_ship_page(request, job.ships)
    response = partial(request, "_job.html", job=job, on_ship_page=on_ship_page)
    if job.done and job.result.get("gone") and on_ship_page:
        response.headers["HX-Redirect"] = "/"  # destroyed: the page it was on is gone too
    if job.done and job.kind in actions.POWER:  # the VM is on or off now: redraw the bar
        response.headers["HX-Refresh"] = "true"
    if job.done and job.kind in (*trust.KINDS, "leave"):  # what the page shows changed
        response.headers["HX-Refresh"] = "true"
    if job.done and job.kind == "agent-restart":  # the agent list shows the new modes
        response.headers["HX-Refresh"] = "true"
    if job.done and job.kind == "scan":  # the page lists what it heard, with Adopt
        response.headers["HX-Refresh"] = "true"
    if job.done and job.kind == "inbox" and _on_ship_page(request, job.ships):
        response.headers["HX-Refresh"] = "true"  # the Inbox tab lists the new files
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


SIGNIN_FILTERS = ("ship", "service", "tag", "group")


def _signin_filters(params: Any) -> dict[str, str]:
    """The Sign-ins page's filters from a query (or form), checked: anything that is
    not a plain Ship name, known service, tag or group is dropped."""
    found = {}
    ship, service = params.get("ship") or "", params.get("service") or ""
    tag, group = params.get("tag") or "", params.get("group") or ""
    if ship and NAME_RE.match(ship):
        found["ship"] = ship
    if service in signinlist.SERVICES:
        found["service"] = service
    if tag and TAG_RE.match(tag):
        found["tag"] = tag
    if group in signinlist.GROUPS:
        found["group"] = group
    return found


def _signin_url(filters: dict[str, str], **changes: str) -> str:
    """/signin with these filters, some changed ("" drops one)."""
    query = {k: v for k, v in {**filters, **changes}.items() if v and k in SIGNIN_FILTERS}
    return "/signin" + (f"?{urlencode(query)}" if query else "")


def _signin_back(fields: dict[str, list[str]]) -> RedirectResponse:
    """Back to the Sign-ins page as it was: its filters ride along as form fields."""
    return RedirectResponse(_signin_url(_signin_filters(
        {k: one(fields, f"f_{k}") for k in SIGNIN_FILTERS})), status_code=303)


def _signin_version(ui: UI) -> str:
    return f"{ui.fleet.version}.{ui.concierge.version}"


def _signin_context(ui: UI, filters: dict[str, str]) -> dict[str, Any]:
    """Every sign-in on every Ship (fleetledger.signinlist), narrowed by the filters;
    the counts per group are of the Ship, service and tag chosen."""
    snap = ui.fleet.snapshot
    flows = ui.concierge.flows()
    every = signinlist.rows(snap.views, snap.inventory, flows)
    scoped = signinlist.narrow(every, ship=filters.get("ship"), service=filters.get("service"),
                               tag=filters.get("tag"))
    shown = signinlist.narrow(scoped, group=filters.get("group"))
    by_name = snap.by_name

    def flow_in_scope(flow: WebFlow) -> bool:
        view = by_name.get(flow.ship)
        service = signinlist.SERVICE_OF_PROVIDER.get(flow.provider, flow.provider)
        return ((not filters.get("ship") or flow.ship == filters["ship"])
                and (not filters.get("service") or service == filters["service"])
                and (not filters.get("tag") or (view is not None and filters["tag"]
                                                in snap.effective_tags(view))))

    group = filters.get("group")

    def card_in_group(flow: WebFlow) -> bool:  # a card goes with its row's group
        return group is None or (group == "action") == flow.needs_you and group in (
            "action", "underway")

    return {
        "rows": shown,
        "grouped": [(g, [r for r in shown if r.group == g]) for g in signinlist.GROUPS
                    if any(r.group == g for r in shown)],
        "counts": signinlist.counts(scoped),
        "total": len(scoped),
        "filters": filters,
        # waiting for you first, the link that runs out first; then the rest, oldest first
        "open_flows": sorted((f for f in flows if not f.done and flow_in_scope(f)
                              and card_in_group(f)),
                             key=lambda f: (not f.needs_you, f.deadline or float("inf"),
                                            f.started_at)),
        "done_flows": [f for f in flows if f.done and flow_in_scope(f)],
        "ships": [v.ship.name for v in snap.views],
        "tags": snap.tags(),
        "stale": snap.polled_at is None,
        "version": _signin_version(ui),
    }


def _signin_filters_known(ui: UI, params: Any) -> dict[str, str]:
    """The filters, with a Ship the fleet does not have dropped (as a bad value is)."""
    filters = _signin_filters(params)
    if filters.get("ship") not in (None, *ui.fleet.snapshot.by_name):
        del filters["ship"]
    return filters


async def signin_page(request: Request) -> Response:
    ui = ui_of(request)
    ui.fleet.touch()
    batch = request.query_params.get("batch")
    batch = batch if batch and BATCH_RE.match(batch) else None
    snap = ui.fleet.snapshot
    common = {"tags": snap.tags(), "ships": [v.ship.name for v in snap.views]}
    if batch:  # the sign-ins just started together: only their cards
        return render(request, "signin.html", flows=ui.concierge.batch(batch), batch=batch,
                      filters={}, **common)
    return render(request, "signin.html", batch=None,
                  **{**common, **_signin_context(ui, _signin_filters_known(
                      ui, request.query_params))})


async def signins_partial(request: Request) -> Response:
    ui = ui_of(request)
    ui.fleet.touch()
    if request.query_params.get("v") == _signin_version(ui):
        return Response(status_code=204)
    return partial(request, "_signins.html", oob=True,
                   **_signin_context(ui, _signin_filters_known(ui, request.query_params)))


async def signin_start(request: Request) -> Response:
    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    service = one(fields, "service")
    tag = one(fields, "tag")
    names = [n.strip() for n in fields.get("ship", []) if n.strip()]
    on_list = one(fields, "from") == "signin"  # a row of every sign-in: back to it

    def failed() -> RedirectResponse:
        return _signin_back(fields) if on_list else back("/signin")

    if service not in SERVICES:
        ui.flash(session, "bad", "Choose a service.")
        return failed()
    if any(not NAME_RE.match(n) for n in names) or (tag and not TAG_RE.match(tag)):
        ui.flash(session, "bad", "Not a valid Ship name or tag.")
        return failed()
    try:
        inv = ui.app.store_factory().load()
        ships = [s.name for s in inv.select(names, tag or None)]
        batch = ui.concierge.start(ships, service)
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return failed()
    if on_list:  # the same view, at the new sign-in's card
        # without the group filter: the cards are drawn with the page, and a sign-in
        # just started is in no group's cards yet (nor its row in the group it left)
        filters = _signin_filters({k: one(fields, f"f_{k}") for k in SIGNIN_FILTERS})
        started = ui.concierge.batch(batch)
        anchor = f"#flow-{started[0].id}" if started else ""
        return RedirectResponse(_signin_url(filters, group="") + anchor, status_code=303)
    return back("/signin", batch=batch)


def _flow_back(fields: dict[str, list[str]], flow: WebFlow) -> RedirectResponse:
    """After a card's button: the page it was on (every sign-in, or its batch)."""
    if one(fields, "from") == "signin":
        return _signin_back(fields)
    return back("/signin", batch=flow.batch)


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
    on_list = request.query_params.get("from") == "signin"
    return partial(request, "_flow.html", flow=flow, card_from="signin" if on_list else None,
                   filters=_signin_filters(request.query_params) if on_list else {})


async def flow_code(request: Request) -> Response:
    ui = ui_of(request)
    flow = _flow(request)
    if flow is None:
        return not_found(request, "That sign-in has gone.")
    fields = await form(request)
    values = fields.get("code", [])
    refused = flow.submit_code(values[0] if len(values) == 1 else "")
    done = _flow_back(fields, flow)
    del fields, values  # the code goes to the engine only
    if refused:
        ui.flash(session_of(request), "bad", refused)
    return done


async def flow_cancel(request: Request) -> Response:
    flow = _flow(request)
    if flow is None:
        return not_found(request, "That sign-in has gone.")
    flow.cancel()
    return _flow_back(await form(request), flow)


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
    return _flow_back(fields, flow)


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
        Route("/partials/signins", signins_partial),
        Route("/partials/charts", charts_partial),
        Route("/ships/{name}", ship_page),
        Route("/ships/{name}/{tab}", ship_page),
        Route("/ships/{name}/refresh", ship_refresh, methods=["POST"]),
        Route("/ships/{name}/settings", ship_settings, methods=["POST"]),
        Route("/ships/{name}/tags", ship_tags, methods=["POST"]),
        Route("/ships/{name}/agents", ship_agents, methods=["POST"]),
        Route("/ships/{name}/agents/restart", ship_agent_restart, methods=["POST"]),
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
        Route("/new/attempts/{name}/discard", discard_attempt, methods=["POST"]),
        Route("/ships/{name}/enrol", finish_enrolling, methods=["POST"]),
        *activity.routes(),
        *setup.routes(),
        *snapshots.routes(),
        *inboxweb.routes(),
        *adopt.routes(),
        *destroy.routes(),
        *settings.routes(),
        *devicesweb.routes(),
        *images.routes(),
        *trust.routes(),
        *fleetactions.routes(),
        *overlayweb.routes(),
        Mount("/static", app=StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    web = Starlette(routes=routes)
    web.state.ui = UI(app=app, gate=gate, fleet=fleet, concierge=concierge_,
                      jobs=jobs or Jobs(app, on_finished=lambda ships: fleet.poll(ships)),
                      env=make_env(),
                      setups=setup.Setups(), start_page=start_page)
    return GateMiddleware(web, gate)
