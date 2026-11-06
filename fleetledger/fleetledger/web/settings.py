"""The fleet's Settings page (/settings): Health, where Ships run, access, images and
the activity log, in one place once the fleet exists. Setup (/setup) stays the
first-run wizard and links here.

- Health is `ship-fleet doctor` (firstrun.everything). Checking runs commands and
  may ask the hypervisor's API, so it happens only on a POST ("Check now"), in a
  worker thread; the page shows the last result, kept in memory.
- Devices lists the browsers paired with the remote web UI (web.devicesweb).
- Access holds the forms that used to sit in setup's first step (your login,
  what a machine may log in as, the certificate authority, key care). Their routes
  are still /setup/...; a form posted from here says back=settings and returns here.
  Certificates and trusted machines are web.trust's.
- A GET reads the inventory, a few files and the audit log, and asks the keyring
  whether it is usable: it runs no command and contacts no API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from fleetledger.errors import FleetError

AUDIT_SHOWN = 50
# Doctor states -> (alert level for the icon, the word shown). ok and skip get no icon.
STATES = {
    "ok": (None, "ok"),
    "skip": (None, "skipped"),
    "info": ("notice", "info"),
    "warn": ("warning", "warning"),
    "missing": ("warning", "missing"),
    "fail": ("critical", "failed"),
}
# Doctor areas -> where the UI fixes them.
FIX = {
    "fleet": ("/settings#access", "Access"),
    "hypervisor": ("/setup#hypervisor", "Setup"),
    "overlay": ("/setup#overlay", "Setup"),
    "ships": ("/", "Fleet"),
}


@dataclass(frozen=True)
class Health:
    """The last Check now: when, and what doctor found."""

    at: float
    items: list[Any]  # firstrun.Item
    error: str | None = None

    def counts(self) -> dict[str, int]:
        found: dict[str, int] = {}
        for item in self.items:
            found[item.state] = found.get(item.state, 0) + 1
        return found


def _ui(request: Request):
    return request.app.state.ui


async def page(request: Request) -> Response:
    from fleetledger.concierge import audit
    from fleetledger.web import devicesweb, images, overlayweb, setup, trust
    from fleetledger.web.app import render, session_of

    ui = _ui(request)
    store = ui.app.store_factory()
    if not store.exists():
        return RedirectResponse("/setup", status_code=303)
    try:
        inv = await run_in_threadpool(store.load)
    except FleetError as exc:
        return render(request, "message.html", title="Settings", text=exc.message)
    try:
        entries = audit.read()[-AUDIT_SHOWN:]
    except (OSError, ValueError):
        entries = []
    context = await setup.access_context(inv)
    context.update(await run_in_threadpool(images.section_context, ui, inv))
    context.update(await run_in_threadpool(trust.section_context, ui, inv))
    context.update(overlayweb.section_context(ui, inv))
    context.update(devicesweb.section_context(ui, session_of(request)))
    context.update(trust.placed_flashes(request))
    if (context.get("certs") is None and context.get("flash_at") not in ("overlay", "devices")
            and "placed" in context):  # no such block: the top
        context["flashes"] = context.pop("placed")
    return render(request, "settings.html", inv=inv, health=ui.health, STATES=STATES,
                  FIX=FIX, entries=list(reversed(entries)), back="settings",
                  inventory_path=setup._tilde(store.path),
                  proxmox=inv.driver_config("proxmox") or {}, **context)


async def check_health(request: Request) -> Response:
    """Run doctor now and keep the result (it may take a few seconds)."""
    from fleetledger import firstrun

    ui = _ui(request)
    try:
        items = await run_in_threadpool(firstrun.everything, ui.app)
        ui.health = Health(time.time(), items)
    except FleetError as exc:
        ui.health = Health(time.time(), [], error=exc.message)
    return RedirectResponse("/settings#health", status_code=303)


def routes() -> list[Route]:
    return [
        Route("/settings", page),
        Route("/settings/health", check_health, methods=["POST"]),
    ]
