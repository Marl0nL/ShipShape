"""The FleetLedger web UI: Starlette, server-rendered pages, htmx for polling.

Every change the UI makes goes through the same `ship-fleet` command functions
the CLI uses (fleetledger.commands), so the CLI stays complete and the two can
never disagree. Pages read the background poller's cached views (fleet.py) and
never wait on SSH, except for the few actions that are SSH by nature (logs,
settings, sign-out, refresh), which run in a worker thread.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from fleetledger import commands, net, ops
from fleetledger.concierge import audit, providers
from fleetledger.concierge.redact import clean
from fleetledger.errors import FleetError
from fleetledger.web import actions, network, setup
from fleetledger.web.fleet import SERVICE_PROVIDER, Fleet, attention_items
from fleetledger.web.flows import Concierge, WebFlow
from fleetledger.web.security import Gate, GateMiddleware, Session

HERE = Path(__file__).resolve().parent
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
TAG_RE = NAME_RE
FLOW_ID_RE = re.compile(r"^[0-9]{1,9}-[0-9a-f]{8}$")
BATCH_RE = re.compile(r"^[0-9a-f]{12}$")
SERVICES = ("claude", "gcloud", "gcloud-adc", "github", "all")
AUTH_ORDER = ("claude", "gcloud", "gcloud_adc", "github")
SETTINGS = {
    # form field -> (ship-fleet set key, allowed values, label)
    "remote_control": ("remote_control", ("on", "off", "blocked"), "Remote Control"),
    "default_mode": ("default_mode", commands.MODES, "Default permission mode"),
    "allow_bypass": ("allow_bypass", ("true", "false"), "Allow bypassPermissions"),
    "update_channel": ("update_channel", ("latest", "stable"), "Claude update channel"),
    "sudo": ("sudo", ("true", "false"), "Agent sudo"),
    "lan_guard": ("lan_guard", ("true", "false"), "LAN guard"),
    "auto_reboot": ("auto_reboot", ("true", "false"), "Reboot for OS updates on its own"),
}
# Free-text settings: fleetledger.ops checks their format.
SETTINGS_TEXT = {
    "reboot_time": "Reboot time for OS updates (HH:MM)",
    "lan_guard_allow": "LAN guard also allows (addresses or CIDRs, comma-separated)",
}
TABS = ("overview", "settings", "logs", "network")


@dataclasses.dataclass
class UI:
    """Everything the handlers share."""

    app: Any  # cli.App
    gate: Gate
    fleet: Fleet
    concierge: Concierge
    operations: actions.Operations
    env: Environment
    setups: Any = None  # web.setup.Setups: each session's progress through /setup
    start_page: str | None = None  # where a new session lands first (ship-fleet setup: /setup)
    started: set[str] = dataclasses.field(default_factory=set)
    flashes: dict[str, list[tuple[str, str]]] = dataclasses.field(default_factory=dict)

    def flash(self, session: Session, kind: str, text: str) -> None:
        self.flashes.setdefault(session.id, []).append((kind, text))

    def take_flashes(self, session: Session) -> list[tuple[str, str]]:
        return self.flashes.pop(session.id, [])


# --- helpers ---------------------------------------------------------------------


def _ago(wall: float | None) -> str:
    if wall is None:
        return "not yet"
    seconds = max(0, int(time.time() - wall))
    if seconds < 60:
        return f"{seconds}s ago"
    return f"{seconds // 60} min ago"


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
        ago=_ago,
        clock=_clock,
        AUTH_ORDER=AUTH_ORDER,
        SERVICE_PROVIDER=SERVICE_PROVIDER,
        SERVICES=SERVICES,
        MODES=commands.MODES,
        SETTINGS=SETTINGS,
        SETTINGS_TEXT=SETTINGS_TEXT,
        human_bytes=net.human_bytes,
        since=network.since,
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
                 "provisioning", "timeout"):
        return "warn"
    return "muted"


async def form(request: Request) -> dict[str, list[str]]:
    """The posted form (the gate has already capped its size and checked CSRF)."""
    body = await request.body()
    try:
        return parse_qs(body.decode("utf-8"), max_num_fields=100)
    except (UnicodeDecodeError, ValueError):
        return {}


def one(fields: dict[str, list[str]], key: str) -> str:
    values = fields.get(key, [])
    return values[0].strip() if len(values) == 1 else ""


def _flash_outcome(ui: UI, session: Session, outcome: actions.Outcome) -> None:
    text = " ".join([outcome.summary, *outcome.details[:5]])
    ui.flash(session, "ok" if outcome.ok else "bad", f"{outcome.ship}: {text}")


def session_of(request: Request | WebSocket) -> Session:
    return request.scope["state"]["session"]


def ui_of(request: Request | WebSocket) -> UI:
    return request.app.state.ui


def render(request: Request, template: str, status_code: int = 200, **context: Any) -> Response:
    ui = ui_of(request)
    session = session_of(request)
    waiting = [f for f in ui.concierge.active() if f.needs_you]
    context.setdefault("flashes", ui.take_flashes(session))
    html = ui.env.get_template(template).render(
        csrf=session.csrf,
        waiting_count=len(waiting),
        snapshot=ui.fleet.snapshot,
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


def not_found(request: Request, what: str) -> Response:
    return render(request, "message.html", status_code=404, title="Not found", text=what)


# --- fleet -------------------------------------------------------------------------


def _fleet_version(ui: UI) -> str:
    return f"{ui.fleet.version}.{ui.concierge.version}.{ui.operations.version}"


def _fleet_context(ui: UI, tag: str | None) -> dict[str, Any]:
    snap = ui.fleet.snapshot
    views = snap.views_for(tag)
    items = attention_items(views)
    return {
        "tag": tag,
        "views": views,
        "items": items,
        "flows_waiting": [f for f in ui.concierge.active() if f.needs_you],
        "operations": ui.operations.recent(),
        "version": _fleet_version(ui),
        "snap": snap,
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
    return render(request, "fleet.html", tags=ui.fleet.snapshot.tags(),
                  **_fleet_context(ui, _tag_param(request)))


async def fleet_partial(request: Request) -> Response:
    ui = ui_of(request)
    ui.fleet.touch()
    if request.query_params.get("v") == _fleet_version(ui):
        return Response(status_code=204)
    return partial(request, "_fleet.html", **_fleet_context(ui, _tag_param(request)))


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
        "workspaces": workspaces,
        "snap": snap,
        **_attach_context(inv, view.ship),
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
        return not_found(request, "No such Ship in the inventory.")
    if tab == "logs":
        source = request.query_params.get("source", "provision")
        if source not in commands.LOG_SOURCES:
            source = "provision"
        context["source"] = source
        context["sources"] = commands.LOG_SOURCES
        context["log"] = None
        if request.query_params.get("source"):
            ok, lines = await run_in_threadpool(actions.logs, ui.app, name, source, 300)
            context["log"] = clean("\n".join(lines))
            context["log_ok"] = ok
    if tab == "network":
        snap = ui.fleet.snapshot
        context["netview"] = network.ship_view(snap.inventory, context["ship"])
        context["net"] = network.status_digest(context["view"])
        context["week"] = request.query_params.get("week") == "1"
    return render(request, "ship.html", tab=tab, **context)


async def ship_refresh(request: Request) -> Response:
    ui = ui_of(request)
    name = _ship_name(request)
    if not name or name not in ui.fleet.snapshot.by_name:
        return not_found(request, "No such Ship in the inventory.")
    await run_in_threadpool(ui.fleet.poll, [name])
    ui.flash(session_of(request), "ok", f"Checked {name}.")
    return back(f"/ships/{name}")


async def ship_settings(request: Request) -> Response:
    ui = ui_of(request)
    name = _ship_name(request)
    if not name or name not in ui.fleet.snapshot.by_name:
        return not_found(request, "No such Ship in the inventory.")
    fields = await form(request)
    assignments = []
    for key, (set_key, allowed, _label) in SETTINGS.items():
        value = one(fields, key)
        if not value:
            continue  # "(unchanged)"
        if value not in allowed:
            ui.flash(session_of(request), "bad", f"{key}: not an allowed value.")
            return back(f"/ships/{name}/settings")
        assignments.append(f"{set_key}={value}")
    for key in SETTINGS_TEXT:
        value = one(fields, key)
        if value:
            assignments.append(f"{key}={value}")
    session = session_of(request)
    if not assignments:
        ui.flash(session, "muted", "Nothing to change.")
        return back(f"/ships/{name}/settings")
    outcome = await run_in_threadpool(actions.set_settings, ui.app, name, assignments)
    _flash_outcome(ui, session, outcome)
    await run_in_threadpool(ui.fleet.poll, [name])
    return back(f"/ships/{name}/settings")


async def ship_agents(request: Request) -> Response:
    """Add an autostart agent to a Ship (or stop starting one)."""
    ui = ui_of(request)
    name = _ship_name(request)
    if not name or name not in ui.fleet.snapshot.by_name:
        return not_found(request, "No such Ship in the inventory.")
    fields = await form(request)
    session = session_of(request)
    try:
        if one(fields, "action") == "remove":
            pair = ops.parse_autostart_change("autostart.remove", one(fields, "agent"))
        else:
            mode = one(fields, "mode")
            value = f"{one(fields, 'agent')}:{one(fields, 'cwd')}" + (f":{mode}" if mode else "")
            pair = ops.parse_autostart_change("autostart.add", value)
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(f"/ships/{name}")
    outcome = await run_in_threadpool(actions.set_settings, ui.app, name, [pair])
    _flash_outcome(ui, session, outcome)
    await run_in_threadpool(ui.fleet.poll, [name])
    return back(f"/ships/{name}")


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
    name = _ship_name(request)
    if not name or name not in ui.fleet.snapshot.by_name:
        return not_found(request, "No such Ship in the inventory.")
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
    name = _ship_name(request)
    if not name or name not in ui.fleet.snapshot.by_name:
        return not_found(request, "No such Ship in the inventory.")
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


async def fleet_operation(request: Request) -> Response:
    """Update or reboot one Ship, or every Ship with a tag (fleetledger.ops)."""
    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    operation = one(fields, "operation")
    name, tag = one(fields, "ship"), one(fields, "tag")
    where = f"/ships/{name}" if name and NAME_RE.match(name) else "/"
    if operation not in ("update", "reboot", "join"):
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
        ui.operations.start(operation, [s.name for s in chosen])
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(where)
    label = {"join": "Joining the overlay"}.get(operation, operation.capitalize())
    ui.flash(session, "ok", f"{label} started; its progress is on the "
             "Fleet page.")
    return back("/", tag=tag)


# --- network visibility ----------------------------------------------------------------


async def ship_network_refresh(request: Request) -> Response:
    ui = ui_of(request)
    name = _ship_name(request)
    if not name or name not in ui.fleet.snapshot.by_name:
        return not_found(request, "No such Ship in the inventory.")
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
               concierge_: Concierge | None = None, start_page: str | None = None
               ) -> Starlette:
    """The ASGI app behind the gate. `app` is a cli.App (the outside world)."""
    fleet = fleet or Fleet(app)
    concierge_ = concierge_ or Concierge(
        app, on_finished=lambda ship: fleet.poll([ship])
    )
    providers.load_all()  # fail at start, not on first sign-in, if a provider file is bad
    routes = [
        Route("/", home),
        Route("/partials/fleet", fleet_partial),
        Route("/ships/{name}", ship_page),
        Route("/ships/{name}/{tab}", ship_page),
        Route("/ships/{name}/refresh", ship_refresh, methods=["POST"]),
        Route("/ships/{name}/settings", ship_settings, methods=["POST"]),
        Route("/ships/{name}/tags", ship_tags, methods=["POST"]),
        Route("/ships/{name}/agents", ship_agents, methods=["POST"]),
        Route("/attach-here", attach_here, methods=["POST"]),
        Route("/ships/{name}/logout", ship_logout, methods=["POST"]),
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
        *setup.routes(),
        Mount("/static", app=StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    web = Starlette(routes=routes)
    operations = actions.Operations(app, on_finished=fleet.poll)
    web.state.ui = UI(app=app, gate=gate, fleet=fleet, concierge=concierge_,
                      operations=operations, env=make_env(), setups=setup.Setups(),
                      start_page=start_page)
    return GateMiddleware(web, gate)
