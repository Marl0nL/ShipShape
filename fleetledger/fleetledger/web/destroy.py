"""Destroying a Ship from the browser, and forgetting one (ship-fleet destroy).

- Both are jobs (web.jobs) running the CLI's own command, `ship-fleet destroy NAME
  --yes` (or `--forget --yes`), so its checks, its audit entry, leaving the overlay
  first and its messages are the ones you see. `--yes` is passed only after the
  server has checked the typed confirmation: the Ship's name, as `confirm_name`.
- Refused while anything else runs on the Ship (Jobs.start) or a sign-in is open
  on it (its terminal would be cut mid-flow).
- The driver's answer is kept on the job (jobs.RecordingApp), so a by-hand driver's
  steps show on the card, which then offers Forget (typed too) once you have run
  them. A failed destroy offers Forget as well, for a VM that is already gone.
- Once the Ship is gone from the inventory, its card sends the page to the Fleet
  page (HX-Redirect), and for an hour its old address says when it was destroyed.
"""

from __future__ import annotations

import time
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from fleetledger.errors import FleetError

REMEMBER = 3600  # seconds a destroyed Ship's address says so


def gone_since(ui: Any, name: str) -> float | None:
    """When this Ship was destroyed from the UI, if that was within the hour."""
    at = ui.destroyed.get(name)
    return at if at is not None and time.time() - at < REMEMBER else None


def _start(request: Request, name: str, forget: bool) -> None:
    from fleetledger.web import jobs
    from fleetledger.web.app import session_of, ui_of

    ui = ui_of(request)
    argv = ["destroy", name, "--yes", *(["--forget"] if forget else [])]

    def run(job: Any) -> int:
        job.result["gone"] = False
        try:
            return jobs.run_cli(jobs.RecordingApp(ui.app, job), argv, job)
        finally:  # even when the driver raised: the card then offers Forget
            try:
                gone = not ui.app.store_factory().load().has_ship(name)
            except FleetError:
                gone = False
            job.result["gone"] = gone
            if gone:
                ui.destroyed[name] = time.time()

    verb = "Forget" if forget else "Destroy"
    try:
        ui.jobs.start("forget" if forget else "destroy", [name], run, title=f"{verb} {name}")
    except FleetError as exc:
        ui.flash(session_of(request), "bad", exc.message)
        return
    ui.flash(session_of(request), "ok", f"{verb} {name}: started; its progress is below.")


async def _post(request: Request, forget: bool) -> Response:
    from fleetledger.web.app import (
        NO_SUCH_SHIP,
        _known_ship,
        back,
        form,
        not_found,
        one,
        session_of,
        ui_of,
    )

    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    ui = ui_of(request)
    fields = await form(request)
    verb = "forgotten" if forget else "destroyed"
    if any(f.ship == name for f in ui.concierge.active()):
        ui.flash(session_of(request), "bad", f"Not {verb}: a sign-in is open on {name}. "
                 "Finish or cancel it on the Sign-ins page first.")
    elif one(fields, "confirm_name") != name:
        ui.flash(session_of(request), "bad", f"Not {verb}: type {name} to confirm.")
    else:
        _start(request, name, forget)
    return back(f"/ships/{name}")


async def destroy(request: Request) -> Response:
    return await _post(request, forget=False)


async def forget(request: Request) -> Response:
    return await _post(request, forget=True)


def routes() -> list[Route]:
    return [
        Route("/ships/{name}/destroy", destroy, methods=["POST"]),
        Route("/ships/{name}/forget", forget, methods=["POST"]),
    ]
