"""Dismissing job cards, and the Activity page: every operation that ended, again.

- A finished job's card has Dismiss (POST /jobs/{id}/dismiss). It hides the card on
  every page and for every browser session: jobs are shared (the Fleet page's
  Activity shows the same list in each), so a card dismissed in one tab does not
  come back in another. A running job has no Dismiss and is refused, so a live
  operation is always in sight. Nothing is deleted: the record stays on /activity.
- "Dismiss all finished" (POST /jobs/dismiss) does the same for every finished card
  shown, or one Ship's.
- Both are plain forms (CSRF field, back to `next`), so they work without script;
  with htmx the single Dismiss removes its card in place (an empty 200 and
  hx-swap="delete") and app.js moves the focus to the next card.
- /activity lists the operations history (fleetledger.history.Operations), newest
  first, dismissed ones too, filtered by Ship (?ship=, linked from each Ship page)
  or outcome, 50 a page. Each log loads when it is opened (/activity/{id}/log), or
  whole on its own page (/activity/{id}) without script.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from fleetledger.history import OUTCOMES
from fleetledger.web.forms import NAME_RE, form, one

PAGE = 50
# A path on this server to go back to: /, /ships/x/settings, /?tag=lab ... (never
# //host or a scheme: an open redirect).
LOCAL_RE = re.compile(r"^/(?![/\\])[A-Za-z0-9/_.\-]*(?:\?[A-Za-z0-9_.\-=&%]*)?$")
OUTCOME_LABELS = {"ok": "done", "failed": "failed", "by-hand": "by hand",
                  "stopped": "stopped"}
OUTCOME_PILLS = {"ok": "good", "failed": "bad", "by-hand": "muted", "stopped": "warn"}


def local_path(raw: str | None, default: str = "/") -> str:
    return raw if raw and LOCAL_RE.match(raw) else default


def here(request: Request) -> str:
    """The page the person is on: the request's own path, or for an htmx request
    (a card polling, a partial) the page it came from."""
    current = request.headers.get("HX-Current-URL")
    if current:
        parts = urlsplit(current)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
    else:
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    return local_path(path)


def took(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def _htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


# --- dismissing ------------------------------------------------------------------


async def dismiss_one(request: Request) -> Response:
    from fleetledger.web.app import session_of, ui_of

    ui = ui_of(request)
    fields = await form(request)
    back = local_path(one(fields, "next"))
    raw = request.path_params.get("job", "")
    job = ui.jobs.get(int(raw)) if raw.isdigit() else None
    if job is not None and job.running:
        if _htmx(request):
            return Response(status_code=409)  # htmx leaves the card as it is
        ui.flash(session_of(request), "bad",
                 f"{job.title} is still running; it can be dismissed once it ends.")
        return RedirectResponse(back, status_code=303)
    if job is not None:
        ui.jobs.dismiss([job.id])
    # A job no longer in memory has no card to show anywhere: gone either way.
    if _htmx(request):
        return HTMLResponse("")
    return RedirectResponse(back, status_code=303)


async def dismiss_finished(request: Request) -> Response:
    from fleetledger.web.app import session_of, ui_of

    ui = ui_of(request)
    fields = await form(request)
    back = local_path(one(fields, "next"))
    ship = one(fields, "ship")
    if ship and not NAME_RE.match(ship):
        ship = ""
    jobs = ui.jobs.for_ship(ship, shown=True) if ship else ui.jobs.shown()
    gone = ui.jobs.dismiss([j.id for j in jobs if j.done])
    if gone:
        n = len(gone)
        ui.flash(session_of(request), "ok",
                 f"Dismissed {n} finished card{'s' if n != 1 else ''}. "
                 f"{'They stay' if n != 1 else 'It stays'} in Activity.")
    return RedirectResponse(back, status_code=303)


# --- the Activity page -----------------------------------------------------------


def _filters(request: Request) -> tuple[str | None, str | None, int | None]:
    q = request.query_params
    ship = q.get("ship") or None
    outcome = q.get("outcome") or None
    before = q.get("before") or ""
    return (ship if ship and NAME_RE.match(ship) else None,
            outcome if outcome in OUTCOMES else None,
            int(before) if before.isdigit() else None)


def _query(**params: Any) -> str:
    kept = {k: v for k, v in params.items() if v not in (None, "")}
    return "/activity" + (f"?{urlencode(kept)}" if kept else "")


async def activity_page(request: Request) -> Response:
    from fleetledger.web.app import render, ui_of

    ui = ui_of(request)
    ship, outcome, before = _filters(request)
    store = ui.jobs.store
    ops: list[Any] = []
    ships: list[str] = []
    problem = None
    if store is not None:
        try:
            ops = store.find(ship=ship, outcome=outcome, before=before, limit=PAGE + 1)
            ships = store.ships()
        except (OSError, sqlite3.Error):
            problem = ("The history file could not be read. Delete history.sqlite3 in "
                       "FleetLedger's data folder to start a new one.")
    older = _query(ship=ship, outcome=outcome, before=ops[PAGE - 1].id) \
        if len(ops) > PAGE else None
    running = [j for j in ui.jobs.recent() if j.running and (ship is None or ship in j.ships)]
    return render(request, "activity.html", ops=ops[:PAGE], ship=ship, outcome=outcome,
                  ships=sorted(set(ships) | ({ship} if ship else set())),
                  before=before, older=older, newest=_query(ship=ship, outcome=outcome),
                  running=running, problem=problem, kept=store is not None,
                  OUTCOMES=OUTCOMES, OUTCOME_LABELS=OUTCOME_LABELS,
                  OUTCOME_PILLS=OUTCOME_PILLS)


def _record(request: Request) -> Any:
    from fleetledger.web.app import ui_of

    store = ui_of(request).jobs.store
    raw = request.path_params.get("op", "")
    if store is None or not raw.isdigit():
        return None
    try:
        return store.get(int(raw))
    except (OSError, sqlite3.Error):
        return None


async def operation_page(request: Request) -> Response:
    from fleetledger.web.app import not_found, render

    op = _record(request)
    if op is None:
        return not_found(request, "No such operation in the history (it may have been "
                                  "pruned: the history keeps 30 days, at most 200).")
    return render(request, "operation.html", op=op, OUTCOME_LABELS=OUTCOME_LABELS,
                  OUTCOME_PILLS=OUTCOME_PILLS)


async def operation_log(request: Request) -> Response:
    from fleetledger.web.app import partial

    op = _record(request)
    if op is None:
        return HTMLResponse('<p class="quiet">This log is no longer kept.</p>')
    return partial(request, "_operation_log.html", op=op)


def routes() -> list[Route]:
    return [
        Route("/jobs/dismiss", dismiss_finished, methods=["POST"]),
        Route("/jobs/{job}/dismiss", dismiss_one, methods=["POST"]),
        Route("/activity", activity_page),
        Route("/activity/{op}", operation_page),
        Route("/activity/{op}/log", operation_log),
    ]
