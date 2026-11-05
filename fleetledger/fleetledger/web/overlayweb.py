"""The overlay in the web UI: a Ship's Access tab, Leave, and Settings ▸ Access ▸
Overlay (the access rules and Check).

- The Access tab reads what the page already has (the Ship's status document, the
  inventory, the cached revocation list): opening it contacts nothing.
- Leave runs `ship-fleet overlay leave SHIP` as a job, behind the confirm dialog: the
  CLI's own command, so it clears the Ship's overlay node and overlay name.
- Check runs `overlay_setup.check_items` (the items `ship-fleet overlay check`
  prints) as a job, one at a time, with an optional pasted policy and the probes;
  the job keeps the items for its card. A pasted policy may be POLICY_BYTES long
  once form-encoded, inside the gate's 64 KiB body limit (which answers a bare
  "Request too large"): the page stops a longer one before it is sent (app.js,
  data-max-bytes) and the server refuses one that gets here, each pointing to the
  CLI's --policy FILE.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote_plus

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from fleetledger.errors import FleetError

POLICY_BYTES = 60 * 1024  # the pasted policy, form-encoded
TOO_BIG = ("That policy is too long to send from this page. Check it from a terminal: "
           "ship-fleet overlay check --policy FILE")
TO_OVERLAY = "/settings?said=overlay#overlay"


def _ui(request: Request):
    return request.app.state.ui


def _flash(request: Request, kind: str, text: str) -> None:
    from fleetledger.web.app import session_of

    _ui(request).flash(session_of(request), kind, text)


# --- a Ship's Access tab ----------------------------------------------------------------
def tab_context(inv: Any, ship: Any, doc: dict | None) -> dict[str, Any]:
    """The Ship's host certificate, judged as Settings ▸ Certificates does (the cached
    revocation list: no command runs)."""
    from fleetledger import ca
    from fleetledger.errors import ConfigError

    cfg = ca.config(inv)
    if cfg is None:
        return {"cert": None}
    try:
        judged = ca.judge(cfg, ship, doc, ca.cached_krl(inv))
    except ConfigError:
        return {"cert": None}
    return {"cert": judged}


# --- leave ------------------------------------------------------------------------------
async def leave(request: Request) -> Response:
    from fleetledger.web import jobs
    from fleetledger.web.app import NO_SUCH_SHIP, _known_ship, back, form, not_found, one

    name = _known_ship(request)
    if name is None:
        return not_found(request, NO_SUCH_SHIP)
    fields = await form(request)
    where = f"/ships/{name}/access" if one(fields, "from") == "access" else f"/ships/{name}"
    ui = _ui(request)
    if one(fields, "confirm") != "yes":
        _flash(request, "bad", "Tick the box to confirm.")
        return back(where)
    if any(f.ship == name for f in ui.concierge.active()):
        _flash(request, "bad", f"Not left: a sign-in is open on {name}. Finish or cancel it "
               "on the Sign-ins page first.")
        return back(where)

    def run(job: Any) -> int:
        return jobs.run_cli(ui.app, ["overlay", "leave", name], job)

    try:
        ui.jobs.start("leave", [name], run, title=f"{name} leaves the overlay")
    except FleetError as exc:
        _flash(request, "bad", exc.message)
        return back(where)
    _flash(request, "ok", f"{name} is leaving the overlay; its progress is below.")
    return back(where)


# --- Settings ▸ Access ▸ Overlay ---------------------------------------------------------
def section_context(ui: Any, inv: Any) -> dict[str, Any]:
    """The access rules to paste into the admin console, and the recent checks."""
    from fleetledger import overlay

    if not inv.overlay:
        return {}
    try:
        rules = overlay.policy(inv.overlay).rstrip()
    except FleetError:
        rules = None
    return {"overlay_rules": rules, "POLICY_BYTES": POLICY_BYTES, "TOO_BIG": TOO_BIG,
            "overlay_jobs": [j for j in ui.jobs.recent() if j.kind == "overlay-check"][:2]}


async def check(request: Request) -> Response:
    from fleetledger import overlay_setup
    from fleetledger.web.app import form, one
    from fleetledger.web.jobs import BrowserApp

    fields = await form(request)
    ui = _ui(request)
    policy = one(fields, "policy")
    probe = one(fields, "probe") == "yes"
    if len(quote_plus(policy)) > POLICY_BYTES:
        _flash(request, "bad", TOO_BIG)
        return RedirectResponse(TO_OVERLAY, status_code=303)
    if any(j.kind == "overlay-check" and j.running for j in ui.jobs.recent()):
        # No await since this read, so no other request starts one in between.
        _flash(request, "bad", "A check of the overlay is already running; it shows below.")
        return RedirectResponse(TO_OVERLAY, status_code=303)
    try:
        if not ui.app.store_factory().load().overlay:
            raise FleetError("No overlay is set up: set it up in Setup first.")
    except FleetError as exc:
        _flash(request, "bad", exc.message)
        return RedirectResponse(TO_OVERLAY, status_code=303)

    def run(job: Any) -> bool:
        items = overlay_setup.check_items(BrowserApp(ui.app, job),
                                          policy_text=policy.strip() or None, probe=probe)
        job.result["checks"] = items
        return not any(c.state == "fail" for c in items)

    title = "Check the overlay" + (" and probe from each Ship" if probe else "")
    try:
        ui.jobs.start("overlay-check", [], run, title=title)
    except FleetError as exc:
        _flash(request, "bad", exc.message)
        return RedirectResponse(TO_OVERLAY, status_code=303)
    _flash(request, "ok", f"{title}: started; it shows below.")
    return RedirectResponse(TO_OVERLAY, status_code=303)


def routes() -> list[Route]:
    return [
        Route("/ships/{name}/overlay/leave", leave, methods=["POST"]),
        Route("/settings/overlay/check", check, methods=["POST"]),
    ]
