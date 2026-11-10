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
from fleetledger.web.common import back, session_of, ship_form, ui_of
from fleetledger.web.forms import one

REMEMBER = 3600  # seconds a destroyed Ship's address says so


def gone_since(ui: Any, name: str) -> tuple[float, bool] | None:
    """(when, forgotten) for a Ship destroyed or forgotten from the UI within the hour."""
    gone = ui.destroyed.get(name)
    return gone if gone is not None and time.time() - gone[0] < REMEMBER else None


def _start(request: Request, name: str, forget: bool) -> None:
    from fleetledger.web import jobs

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
                ui.destroyed[name] = (time.time(), forget)
                for earlier in ui.jobs.for_ship(name):  # their Forget has nothing left to do
                    if earlier is not job and earlier.kind == "destroy":
                        earlier.result["forgotten"] = True

    verb = "Forget" if forget else "Destroy"
    try:
        ui.jobs.start("forget" if forget else "destroy", [name], run, title=f"{verb} {name}")
    except FleetError as exc:
        ui.flash(session_of(request), "bad", exc.message)
        return
    ui.flash(session_of(request), "ok", f"{verb} {name}: started; its progress is below.")


async def _post(request: Request, forget: bool) -> Response:
    ui, name, fields, _ = await ship_form(request)
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
