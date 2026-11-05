"""Adding a Ship FleetLedger did not make (ship-fleet scan, adopt), and re-pinning a
rebuilt one's host key (adopt --replace).

- Scan is a job running `ship-fleet scan`; the Ships it heard announce themselves
  are kept on the job (an app whose browse records them), so the page lists them
  with what the inventory knows of each, and Adopt fills in the form.
- Adopt is two steps, as the CLI's prompt is: first the server checks the name and
  the host (a host name or address, at most 253 characters) and only then asks the
  host for its key (keyscan) and shows its fingerprint; you compare it with the one
  cloud-init printed on the VM console, and "It matches" runs `ship-fleet adopt NAME
  --host H --fingerprint FP` as a job. The CLI scans the key again and refuses a
  different one, so nothing changes if the key changed in between.
- Re-pin (More ▾) is the same for a Ship already in the inventory: `adopt NAME
  --replace --fingerprint FP`, which revokes the old key when there is a CA.
"""

from __future__ import annotations

from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from fleetledger.errors import FleetError
from fleetledger.overlay import HOST_RE

TAG_LIMIT = 8


class _ScanApp:
    """The app for a scan job: what browse heard is kept on the job, for the page."""

    def __init__(self, app: Any, job: Any) -> None:
        self._app = app
        self._job = job

    def browse(self, timeout: float) -> list[Any]:
        found = self._app.browse(timeout)
        self._job.result["found"] = [(a.name, a.host, tuple(a.addresses), a.ship_id)
                                     for a in found]
        return found

    def __getattr__(self, name: str) -> Any:
        return getattr(self._app, name)


def last_scan(ui: Any) -> Any:
    return next((j for j in ui.jobs.recent() if j.kind == "scan"), None)


def scan_rows(ui: Any, job: Any) -> list[dict[str, Any]]:
    """What the last scan heard, with what the inventory knows of each."""
    if job is None or not job.done:
        return []
    try:
        inv = ui.app.store_factory().load()
    except FleetError:
        return []
    by_id = {s.id: s.name for s in inv.ships()}
    rows = []
    for name, host, addresses, ship_id in job.result.get("found", []):
        known = by_id.get(ship_id) if ship_id else None
        if known is None and inv.has_ship(name):
            known = name
        rows.append({"name": name, "host": host, "addresses": addresses, "known": known})
    return rows


def _check_host(host: str) -> str:
    host = host.strip()
    if not host or len(host) > 253 or not HOST_RE.match(host):
        raise FleetError("A host is a name or address to reach it by (a-z, 0-9, . and -, at "
                         "most 253 characters).")
    return host


def _fingerprint(app: Any, host: str) -> str:
    from fleetledger.seed import hostkey

    return hostkey.fingerprint(app.keyscan(host))


# --- adopt ---------------------------------------------------------------------------


def _page(request: Request, status_code: int = 200, **context: Any) -> Response:
    from fleetledger.web.app import render, ui_of

    ui = ui_of(request)
    job = last_scan(ui)
    context.setdefault("form", {"name": "", "host": "", "tags": ""})
    return render(request, "adopt.html", status_code=status_code, scan=job,
                  rows=scan_rows(ui, job), **context)


async def page(request: Request) -> Response:
    from fleetledger.web.app import ui_of

    ui = ui_of(request)
    if not await run_in_threadpool(ui.app.store_factory().exists):
        from starlette.responses import RedirectResponse

        return RedirectResponse("/setup", status_code=303)
    q = request.query_params
    return _page(request, form={"name": q.get("name", ""), "host": q.get("host", ""),
                                "tags": ""})


async def scan(request: Request) -> Response:
    from fleetledger.web.app import back, session_of, ui_of
    from fleetledger.web.jobs import run_cli

    ui = ui_of(request)
    running = last_scan(ui)
    if running is not None and not running.done:  # no Ship to lock: refuse a second one here
        ui.flash(session_of(request), "bad", "A scan is already running; its results show here.")
        return back("/new/existing")
    try:
        ui.jobs.start("scan", [], lambda job: run_cli(_ScanApp(ui.app, job), ["scan"], job),
                      "Scan the LAN for Ships")
    except FleetError as exc:
        ui.flash(session_of(request), "bad", exc.message)
    return back("/new/existing")


def _adopt_fields(fields: dict[str, list[str]], inv: Any) -> dict[str, Any]:
    from fleetledger import ops
    from fleetledger.web.forms import NAME_RE, one

    name = one(fields, "name")
    if not NAME_RE.match(name):
        raise FleetError("A Ship name uses a-z, 0-9 and -, up to 31 characters.")
    if inv.has_ship(name):
        raise FleetError(f"{name} is already in the inventory: to trust a rebuilt one, "
                         "use Re-pin host key on its page.")
    tags = one(fields, "tags").replace(",", " ").split()
    if len(tags) > TAG_LIMIT or not all(ops.TAG_RE.match(t) for t in tags):
        raise FleetError("Tags: a-z, 0-9 and -, separated by spaces or commas.")
    return {"name": name, "host": _check_host(one(fields, "host") or f"{name}.local"),
            "tags": " ".join(tags)}


async def check(request: Request) -> Response:
    """Step 1: check the name and host, then show the key the host presents."""
    from fleetledger.web.app import form, ui_of

    ui = ui_of(request)
    fields = await form(request)
    try:
        inv = await run_in_threadpool(ui.app.store_factory().load)
        chosen = _adopt_fields(fields, inv)
    except FleetError as exc:
        from fleetledger.web.forms import one

        return _page(request, 400, flashes=[("bad", exc.message)],
                     form={k: one(fields, k) for k in ("name", "host", "tags")})
    try:
        fp = await run_in_threadpool(_fingerprint, ui.app, chosen["host"])
    except FleetError as exc:
        return _page(request, 400, flashes=[("bad", exc.message)], form=chosen)
    return _page(request, form=chosen, fingerprint=fp)


async def adopt(request: Request) -> Response:
    """Step 2, "It matches": `ship-fleet adopt` as a job."""
    from fleetledger.web.app import back, form, one, session_of, ui_of
    from fleetledger.web.jobs import run_cli

    ui = ui_of(request)
    fields = await form(request)
    session = session_of(request)
    try:
        inv = await run_in_threadpool(ui.app.store_factory().load)
        chosen = _adopt_fields(fields, inv)
        fp = one(fields, "fingerprint")
        if not fp.startswith("SHA256:") or one(fields, "matches") != "yes":
            raise FleetError("Compare the fingerprint with the VM console first.")
        argv = ["adopt", chosen["name"], "--host", chosen["host"], "--fingerprint", fp,
                *[a for t in chosen["tags"].split() for a in ("--tag", t)]]
        ui.jobs.start("adopt", [chosen["name"]], lambda job: run_cli(ui.app, argv, job),
                      f"Adopt {chosen['name']}")
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back("/new/existing")
    ui.flash(session, "ok", f"Adopting {chosen['name']}; its progress is under Activity.")
    return back("/")


# --- re-pin --------------------------------------------------------------------------


async def repin_check(request: Request) -> Response:
    """Re-pin, step 1: the key its host presents now, beside the pinned one."""
    from fleetledger.seed import hostkey
    from fleetledger.web.app import (
        NO_SUCH_SHIP,
        _known_ship,
        back,
        not_found,
        render,
        session_of,
        ui_of,
    )

    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    try:
        inv = await run_in_threadpool(ui.app.store_factory().load)
        ship = inv.ship(name)
        host = _check_host(ship.names[0])
        fp = await run_in_threadpool(_fingerprint, ui.app, host)
    except FleetError as exc:
        ui.flash(session_of(request), "bad", exc.message)
        return back(f"/ships/{name}")
    try:
        pinned = hostkey.fingerprint(ship.host_key_type_and_blob)
    except FleetError:
        pinned = None  # an inventory entry that does not hold a usable key line
    return render(request, "repin.html", name=name, host=host, fingerprint=fp,
                  pinned=pinned, same=fp == pinned, ca=bool(inv.doc.get("ca")))


async def repin(request: Request) -> Response:
    """Re-pin, step 2: `ship-fleet adopt NAME --replace --fingerprint FP` as a job."""
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
    from fleetledger.web.jobs import run_cli

    ui = ui_of(request)
    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    session = session_of(request)
    fp = one(fields, "fingerprint")
    if not fp.startswith("SHA256:") or one(fields, "matches") != "yes":
        ui.flash(session, "bad", "Compare the fingerprint with the VM console first.")
        return back(f"/ships/{name}")
    try:
        ui.jobs.start("repin", [name], lambda job: run_cli(
            ui.app, ["adopt", name, "--replace", "--fingerprint", fp], job),
            f"Re-pin {name}'s host key")
    except FleetError as exc:
        ui.flash(session, "bad", exc.message)
        return back(f"/ships/{name}")
    ui.flash(session, "ok", f"Re-pinning {name}'s host key; its progress is below.")
    return back(f"/ships/{name}")


def routes() -> list[Route]:
    return [
        Route("/new/existing", page),
        Route("/new/scan", scan, methods=["POST"]),
        Route("/new/existing/check", check, methods=["POST"]),
        Route("/new/existing/adopt", adopt, methods=["POST"]),
        Route("/ships/{name}/repin", repin_check, methods=["POST"]),
        Route("/ships/{name}/repin/confirm", repin, methods=["POST"]),
    ]
