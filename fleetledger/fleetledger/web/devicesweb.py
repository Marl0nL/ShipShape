"""Settings ▸ Devices: the browsers paired with the remote web UI (web/devices.py).

- The section lists every paired device: its name, the Tailscale login that paired
  it, when and from where, when it was last used, and until when it may make the
  danger actions (devices.FRESH_ONLY). This browser is marked.
- **Pair a device** makes a one-time pairing link through devices.pair_link, the
  same function as `ship-fleet ui link`, and shows it once, with a QR code for a
  phone, in the POST's own response: it is never stored or shown again. From a
  remote device it needs a fresh pairing (the gate checks it).
- **Rename**, **Revoke** (behind its dialog) and **Sign out every other device**
  change devices.json through the same functions `ship-fleet ui revoke` uses.
- A GET reads remote-ui.json and devices.json only.
"""

from __future__ import annotations

import time
from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from fleetledger.errors import FleetError
from fleetledger.web import devices as devices_mod
from fleetledger.web.devices import Devices

TO_DEVICES = "/settings?said=devices#devices"


def _ui(request: Request):
    return request.app.state.ui


def _store(ui: Any) -> Devices:
    # The running UI's own store when the remote address is on; else the same file.
    return ui.gate.devices or Devices()


def _flash(request: Request, kind: str, text: str) -> None:
    from fleetledger.web.app import session_of

    _ui(request).flash(session_of(request), kind, text)


def section_context(ui: Any, session: Any) -> dict[str, Any]:
    """What Settings ▸ Devices shows. Reads two small files; contacts nothing."""
    from fleetledger.web import remote

    try:
        setting, broken = remote.load(), None
    except FleetError as exc:
        setting, broken = None, exc.message
    now = time.time()
    mine = session.device.key if session.device is not None else None
    rows = [{
        "key": d.key, "short": d.short, "name": d.name, "login": d.login,
        "paired": devices_mod.when(d.paired), "how": d.how,
        "seen": devices_mod.when(d.seen), "machine": d.machine,
        "fresh_until": devices_mod.when(d.paired + devices_mod.FRESH) if d.fresh(now) else None,
        "mine": d.key == mine,
    } for d in _store(ui).all()]
    return {
        "remote_ui": setting,
        "remote_broken": broken,
        "remote_served": ui.gate.remote is not None,
        "devices": rows,
        "on_remote": session.device is not None,
        "fresh_hours": devices_mod.FRESH // 3600,
        "pair_minutes": devices_mod.PAIR_TTL // 60,
    }


async def pair(request: Request) -> Response:
    """Pair a device: a one-time link, shown once (with a QR code), never again."""
    from fleetledger.web.app import form, one, render, session_of

    ui = _ui(request)
    session = session_of(request)
    fields = await form(request)
    how = (session.device.name if session.device is not None
           else devices_mod.FROM_HERE)
    try:
        link, setting = devices_mod.pair_link(one(fields, "name"), how, _store(ui))
    except FleetError as exc:
        _flash(request, "bad", exc.message)
        return RedirectResponse(TO_DEVICES, status_code=303)
    name = devices_mod.clean_name(one(fields, "name")) if one(fields, "name") else None
    return render(request, "device_pair.html", link=link, qr=devices_mod.qr_code(link),
                  name=name, origin=setting.origin, logins=setting.logins,
                  minutes=devices_mod.PAIR_TTL // 60, served=ui.gate.remote is not None)


def _chosen(ui: Any, key: str):
    return next((d for d in _store(ui).all() if key and d.key == key), None)


async def rename(request: Request) -> Response:
    from fleetledger.web.app import form, one

    ui = _ui(request)
    fields = await form(request)
    device = _chosen(ui, one(fields, "device"))
    name = devices_mod.clean_name(one(fields, "name"))
    if device is None:
        _flash(request, "bad", "No such paired device (it may have lapsed).")
    elif name is None:
        _flash(request, "bad", "A device name is up to 40 letters, digits, spaces and "
                               ". _ ' ( ) -, starting with a letter or digit.")
    else:
        _store(ui).rename(device.key, name)
        _flash(request, "ok", f"{device.name} is now called {name}.")
    return RedirectResponse(TO_DEVICES, status_code=303)


async def revoke(request: Request) -> Response:
    """Sign one device out (ship-fleet ui revoke), behind its dialog."""
    from fleetledger.web.app import form, one, render, session_of

    ui = _ui(request)
    session = session_of(request)
    fields = await form(request)
    if one(fields, "confirm") != "yes":
        _flash(request, "bad", "Tick the box to confirm.")
        return RedirectResponse(TO_DEVICES, status_code=303)
    device = _chosen(ui, one(fields, "device"))
    if device is None:
        _flash(request, "bad", "No such paired device (it may have lapsed).")
        return RedirectResponse(TO_DEVICES, status_code=303)
    devices_mod.revoke(_store(ui), [device])
    if session.device is not None and device.key == session.device.key:
        return render(request, "message.html", title="Signed out",
                      text="This browser is signed out of FleetLedger. It needs a new "
                           "pairing link to sign in again.")
    _flash(request, "ok", f"Signed out {device.name}. It needs a new pairing link to sign "
                          "in again.")
    return RedirectResponse(TO_DEVICES, status_code=303)


async def revoke_others(request: Request) -> Response:
    """Sign out every paired device but this browser (all of them, from this computer)."""
    from fleetledger.web.app import form, one, session_of

    ui = _ui(request)
    session = session_of(request)
    fields = await form(request)
    if one(fields, "confirm") != "yes":
        _flash(request, "bad", "Tick the box to confirm.")
        return RedirectResponse(TO_DEVICES, status_code=303)
    mine = session.device.key if session.device is not None else None
    count = devices_mod.revoke(_store(ui), [d for d in _store(ui).all() if d.key != mine])
    _flash(request, "ok", f"Signed out {count} device(s)." if count else
           "No other device was paired.")
    return RedirectResponse(TO_DEVICES, status_code=303)


def routes() -> list[Route]:
    return [
        Route("/settings/devices/pair", pair, methods=["POST"]),
        Route("/settings/devices/rename", rename, methods=["POST"]),
        Route("/settings/devices/revoke", revoke, methods=["POST"]),
        Route("/settings/devices/revoke-others", revoke_others, methods=["POST"]),
    ]
