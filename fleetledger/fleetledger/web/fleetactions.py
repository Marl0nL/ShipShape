"""Act on every Ship, or every Ship with a tag (/fleet/actions): update with options,
change a setting, add or remove tools, and read a log from each.

- The Ships are `ops.select`'s, as `ship-fleet ... --tag TAG` (or no names) picks
  them, so the page and the CLI always mean the same Ships.
- Each action is one job running the shared day-2 operation the CLI runs
  (ops.update, ops.set_settings, ops.change_tools, ops.fleet_logs), with one
  outcome per Ship on its card.
- Anything that changes a Ship asks first, in the confirm dialog, naming how many
  Ships; reading logs changes nothing and does not ask.
- Settings take the Ship Settings tab's fields (web.shipsettings) but start at
  "(unchanged)": only what you choose is sent. SSH listening stays per Ship (its
  Settings tab), since overlay-only on a Ship not on the overlay cuts it off.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from fleetledger import ops
from fleetledger.errors import FleetError

TAG_RE = ops.TAG_RE
LOG_LINES = ("50", "100", "300")
KINDS = ("fleet-update", "fleet-set", "fleet-tools", "fleet-logs")


def _ui(request: Request):
    return request.app.state.ui


def _flash(request: Request, kind: str, text: str) -> None:
    from fleetledger.web.app import session_of

    _ui(request).flash(session_of(request), kind, text)


def target(inv: Any, value: str) -> tuple[str | None, list[Any]]:
    """("all" or "tag:T") -> (the tag or None, the Ships ops.select picks)."""
    if value == "all":
        return None, ops.select(inv, [], None, everything=True)
    tag = value.removeprefix("tag:")
    if not value.startswith("tag:") or not TAG_RE.match(tag):
        raise FleetError("Choose every Ship or a tag.")
    return tag, ops.select(inv, [], tag)


def targets(inv: Any) -> list[tuple[str, str, int]]:
    """(value, what it says, how many Ships) for the forms' Ships choice: every Ship,
    then each tag a Ship has (its own, or defaults.tags), as ops.select reads them."""
    tags = sorted({t for s in inv.ships() for t in inv.effective_tags(s)})
    found = [("all", "every Ship", len(inv.ships()))]
    for t in tags:
        found.append((f"tag:{t}", f"every {t} Ship", len(ops.select(inv, [], t))))
    return found


def _start(ui: Any, kind: str, ships: list[Any], title: str,
           call: Callable[[Any, list[Any], Callable[[str, str], None]], list[Any]],
           keep: Callable[[Any, list[Any]], None] | None = None) -> Any:
    """call(fleet, ships, event) in the background: its per-Ship outcomes on the card."""
    from fleetledger.web import actions
    from fleetledger.web.jobs import BrowserApp

    names = [s.name for s in ships]

    def run(job: Any) -> None:
        def event(ship: str, text: str) -> None:
            job.write(text if ship == "*" else f"{ship}: {text}")

        fleet = actions._fleet(BrowserApp(ui.app, job))
        chosen = [fleet.inventory.ship(n) for n in names]
        results = call(fleet, chosen, event)
        if keep:
            keep(job, results)
        job.outcomes = [actions.Outcome.of(r) for r in results]
        job.ok = all(o.ok for o in job.outcomes)

    return ui.jobs.start(kind, names, run, title)


def page_context(ui: Any, inv: Any, tag: str | None) -> dict[str, Any]:
    from fleetledger import commands
    from fleetledger.web import shipsettings
    from fleetledger.web.actions import tool_choices

    return {
        "targets": targets(inv),
        "chosen": f"tag:{tag}" if tag else "all",
        "GROUPS": shipsettings.GROUPS,
        "REBOOT_POLICIES": ops.REBOOT_POLICIES,
        "tools": tool_choices(ui.app, None)["list"],
        "sources": commands.LOG_SOURCES,
        "LOG_LINES": LOG_LINES,
        "fleet_jobs": [j for j in ui.jobs.shown() if j.kind in KINDS][:4],
    }


async def page(request: Request) -> Response:
    from fleetledger.web.app import render

    ui = _ui(request)
    tag = request.query_params.get("tag")
    try:
        inv = await run_in_threadpool(ui.app.store_factory().load)
    except FleetError as exc:
        return render(request, "message.html", title="Every Ship", text=exc.message)
    context = await run_in_threadpool(page_context, ui, inv, None)
    if tag:
        chosen = f"tag:{tag}"
        if not TAG_RE.match(tag) or chosen not in {v for v, _, _ in context["targets"]}:
            # Never act on more than was asked: no forms until a target is chosen here.
            return render(request, "fleet_actions.html", **context, unknown_tag=tag)
        context["chosen"] = chosen
    return render(request, "fleet_actions.html", **context)


def _back(tag: str | None = None) -> Response:
    from fleetledger.web.app import back

    return back("/fleet/actions", tag=tag or "")


async def _chosen(request: Request, fields: dict[str, list[str]]):
    """The form's Ships (tag, ships), or None (flashed)."""
    from fleetledger.web.app import one

    try:
        inv = await run_in_threadpool(_ui(request).app.store_factory().load)
        tag, ships = target(inv, one(fields, "ships"))
    except FleetError as exc:
        _flash(request, "bad", exc.message)
        return None
    if not ships:
        _flash(request, "bad", "No Ships to act on.")
        return None
    return tag, ships


def _who(tag: str | None, ships: list[Any]) -> str:
    n = len(ships)
    return f"{n} {tag + ' ' if tag else ''}Ship{'s' if n != 1 else ''}"


def _started(request: Request, call: Callable[[], Any], title: str) -> None:
    try:
        call()
    except FleetError as exc:
        _flash(request, "bad", exc.message)
        return
    _flash(request, "ok", f"{title}: started; its progress is below.")


# --- the four actions -------------------------------------------------------------------
async def update(request: Request) -> Response:
    from fleetledger.web.app import form, one

    fields = await form(request)
    chosen = await _chosen(request, fields)
    if chosen is None:
        return _back()
    tag, ships = chosen
    policy, force = one(fields, "reboot") or "auto", one(fields, "force") == "yes"
    if policy not in ops.REBOOT_POLICIES:
        _flash(request, "bad", "Choose when to reboot.")
    elif one(fields, "confirm") != "yes":
        _flash(request, "bad", "Tick the box to confirm.")
    else:
        title = f"Update {_who(tag, ships)}" + (" (forced)" if force else "")
        _started(request, lambda: _start(
            _ui(request), "fleet-update", ships, title,
            lambda fleet, chosen, event: ops.update(fleet, chosen, reboot_policy=policy,
                                                    force=force, wait=True, on_event=event)),
            title)
    return _back(tag)


async def set_settings(request: Request) -> Response:
    from fleetledger.web import shipsettings
    from fleetledger.web.app import form, one

    fields = await form(request)
    chosen = await _chosen(request, fields)
    if chosen is None:
        return _back()
    tag, ships = chosen
    try:
        wanted = shipsettings.assignments(fields, None)  # nothing known: only what is chosen
    except FleetError as exc:
        _flash(request, "bad", exc.message)
        return _back(tag)
    if not wanted:
        _flash(request, "bad", "Choose at least one setting to change.")
    elif one(fields, "confirm") != "yes":
        _flash(request, "bad", "Tick the box to confirm.")
    else:
        title = f"Set {', '.join(wanted)} on {_who(tag, ships)}"
        _started(request, lambda: _start(
            _ui(request), "fleet-set", ships, title,
            lambda fleet, chosen, event: ops.set_settings(fleet, chosen, wanted, event)),
            title)
    return _back(tag)


async def tools(request: Request) -> Response:
    from fleetledger.web.actions import tool_choices
    from fleetledger.web.app import form, one

    fields = await form(request)
    chosen = await _chosen(request, fields)
    if chosen is None:
        return _back()
    tag, ships = chosen
    action = one(fields, "do")
    ids = list(dict.fromkeys(fields.get("tool", [])))
    known = {t for t, _, _ in tool_choices(_ui(request).app, None)["list"]}
    if action not in ("add", "remove"):
        _flash(request, "bad", "Add or remove?")
    elif not ids or set(ids) - known:
        _flash(request, "bad", "Tick the catalogue's tools to add or remove.")
    elif one(fields, "confirm") != "yes":
        _flash(request, "bad", "Tick the box to confirm.")
    else:
        title = f"{action.capitalize()} {', '.join(ids)} on {_who(tag, ships)}"
        _started(request, lambda: _start(
            _ui(request), "fleet-tools", ships, title,
            lambda fleet, chosen, event: ops.change_tools(fleet, chosen, action, ids, event)),
            title)
    return _back(tag)


async def logs(request: Request) -> Response:
    from fleetledger import commands
    from fleetledger.web.app import form, one

    fields = await form(request)
    chosen = await _chosen(request, fields)
    if chosen is None:
        return _back()
    tag, ships = chosen
    source, lines = one(fields, "source"), one(fields, "lines")
    if source not in commands.LOG_SOURCES or lines not in LOG_LINES:
        _flash(request, "bad", "Choose a log and how many lines.")
        return _back(tag)

    def keep(job: Any, results: list[Any]) -> None:
        from fleetledger.concierge.redact import clean

        # A Ship that failed has no lines: its red line on the card says why.
        job.result["logs"] = [(r.ship, [clean(line) for line in r.data.get("lines", [])])
                              for r in results if r.ok]

    title = f"The {source} log ({lines} lines) of {_who(tag, ships)}"
    _started(request, lambda: _start(
        _ui(request), "fleet-logs", ships, title,
        lambda fleet, chosen, event: ops.fleet_logs(fleet, chosen, source, int(lines), event),
        keep), title)
    return _back(tag)


def routes() -> list[Route]:
    return [
        Route("/fleet/actions", page),
        Route("/fleet/actions/update", update, methods=["POST"]),
        Route("/fleet/actions/set", set_settings, methods=["POST"]),
        Route("/fleet/actions/tools", tools, methods=["POST"]),
        Route("/fleet/actions/logs", logs, methods=["POST"]),
    ]
