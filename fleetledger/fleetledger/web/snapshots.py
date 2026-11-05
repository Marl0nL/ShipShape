"""A Ship's disk snapshots, on its Snapshots tab: list, take, roll back, delete.

- The list is `driver.snapshots(ship)`, read in a worker thread when the tab is
  opened: one call to the hypervisor per view; nothing polls it.
- Take, roll back and delete are jobs (web.jobs) running the CLI's own command,
  `ship-fleet snapshot take|rollback|rm` (`run_cli`), so its checks and messages are
  the ones you see. Rollback gets `--yes` only after the server has checked the
  typed confirmation (the Ship's name, as `confirm_name`); delete needs the
  dialog's tick box (`confirm=yes`); taking one needs neither.
- A driver that cannot do something raises NotSupported (the list says so); one
  that leaves it to you (manual) returns the steps. The job records the driver's
  answer (`job.result["outcome"]`), and _outcome.html shows it on the job's card.
"""

from __future__ import annotations

from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from fleetledger.drivers.base import NotSupported, check_snapshot_name
from fleetledger.errors import FleetError
from fleetledger.lifecycle import SNAPSHOT_WARNING

NOTE_MAX = 200


def listing(app: Any, name: str) -> dict[str, Any]:
    """What the Snapshots tab shows: the snapshots, or why there is no list."""
    inv = app.store_factory().load()
    ship = inv.ship(name)
    found: dict[str, Any] = {"snapshots": [], "snap_unsupported": None, "snap_error": None,
                             "snap_by_hand": ship.driver == "manual",
                             "SNAPSHOT_WARNING": SNAPSHOT_WARNING}
    try:
        found["snapshots"] = app.driver_factory(inv, ship.driver).snapshots(ship)
    except NotSupported as exc:
        found["snap_unsupported"] = exc.message
    except FleetError as exc:
        found["snap_error"] = exc.message
    return found


def _start(request: Request, name: str, argv: list[str], title: str) -> None:
    from fleetledger.web import jobs
    from fleetledger.web.app import session_of, ui_of

    ui = ui_of(request)

    def run(job: Any) -> int:
        return jobs.run_cli(jobs.RecordingApp(ui.app, job), argv, job)

    try:
        ui.jobs.start("snapshot", [name], run, title=title)
    except FleetError as exc:
        ui.flash(session_of(request), "bad", exc.message)
        return
    ui.flash(session_of(request), "ok", f"{title}: started; it shows above.")


def _snapshot_name(value: str) -> str | None:
    try:
        return check_snapshot_name(value)
    except FleetError:
        return None


async def take(request: Request) -> Response:
    from fleetledger.web.app import NO_SUCH_SHIP, _known_ship, back, form, not_found, one

    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    snap, note = one(fields, "snapshot"), one(fields, "note")[:NOTE_MAX]
    if snap and _snapshot_name(snap) is None:
        _bad(request, "A snapshot name is a letter, then letters, digits, - or _ (2 to 40).")
        return back(f"/ships/{name}/snapshots")
    # --note=TEXT, not --note TEXT: a note that starts with "-" stays a note.
    argv = ["snapshot", "take", name, *([snap] if snap else []),
            *([f"--note={note}"] if note else [])]
    _start(request, name, argv, f"Snapshot {name}")
    return back(f"/ships/{name}/snapshots")


async def rollback(request: Request) -> Response:
    from fleetledger.web.app import NO_SUCH_SHIP, _known_ship, back, form, not_found, one

    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    snap = _snapshot_name(one(fields, "snapshot"))
    if snap is None:
        _bad(request, "Choose a snapshot to roll back to.")
    elif _signing_in(request, name):
        # A rollback restarts the VM: it would cut the sign-in's terminal mid-flow.
        _bad(request, f"Not rolled back: a sign-in is open on {name}. Finish or cancel it "
             "on the Sign-ins page first.")
    elif one(fields, "confirm_name") != name:
        _bad(request, f"Not rolled back: type {name} to confirm.")
    else:
        _start(request, name, ["snapshot", "rollback", name, snap, "--yes"],
               f"Roll {name} back to {snap}")
    return back(f"/ships/{name}/snapshots")


async def delete(request: Request) -> Response:
    from fleetledger.web.app import NO_SUCH_SHIP, _known_ship, back, form, not_found, one

    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    snap = _snapshot_name(one(fields, "snapshot"))
    if snap is None:
        _bad(request, "Choose a snapshot to delete.")
    elif one(fields, "confirm") != "yes":
        _bad(request, "Tick the box to confirm.")
    else:
        _start(request, name, ["snapshot", "rm", name, snap], f"Delete {snap} of {name}")
    return back(f"/ships/{name}/snapshots")


def _signing_in(request: Request, name: str) -> bool:
    from fleetledger.web.app import ui_of

    return any(f.ship == name for f in ui_of(request).concierge.active())


def _bad(request: Request, text: str) -> None:
    from fleetledger.web.app import session_of, ui_of

    ui_of(request).flash(session_of(request), "bad", text)


async def tab_context(app: Any, name: str) -> dict[str, Any]:
    return await run_in_threadpool(listing, app, name)


def routes() -> list[Route]:
    return [
        Route("/ships/{name}/snapshots/take", take, methods=["POST"]),
        Route("/ships/{name}/snapshots/rollback", rollback, methods=["POST"]),
        Route("/ships/{name}/snapshots/delete", delete, methods=["POST"]),
    ]
