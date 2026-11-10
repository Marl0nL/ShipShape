"""Settings ▸ Devices: the browsers paired with the remote web UI (web/devices.py).

- The section lists every paired device: its name, the Tailscale login that paired
  it, when and from where, when it was last used, and until when it may make the
  danger actions (devices.FRESH_ONLY). This browser is marked.
- **Pair a device** makes a one-time pairing link through devices.pair_link, the
  same function as `ship-fleet ui link`, and shows it once, with a QR code for a
  phone, in the POST's own response: it is never stored or shown again. From a
  remote device it needs a fresh pairing (the gate checks it).
- **Rename**, **Runs on** (the trusted machine the device is on, so a Ship's
  pages show that machine's attach commands and offer its bundle), **Revoke**
  (behind its dialog) and **Sign out every other device** change devices.json
  through the same functions `ship-fleet ui devices DEVICE --name/--machine` and
  `ship-fleet ui revoke` use.
- A GET reads remote-ui.json and devices.json only.
"""

from __future__ import annotations

from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from fleetledger.errors import FleetError
from fleetledger.web import devices as devices_mod
from fleetledger.web.common import flash, session_of, ui_of
from fleetledger.web.devices import Devices
from fleetledger.web.forms import NAME_RE, form, one

TO_DEVICES = "/settings?said=devices#devices"


def _store(ui: Any) -> Devices:
    # The running UI's own store when the remote address is on; else the same file.
    return ui.gate.devices or Devices()


def machine_of(device: Any, inv: Any) -> Any:
    """The trusted machine (ca.Machine) a device runs on, if you said and it is still
    trusted; else None. Never this computer's own entry."""
    from fleetledger import ca, trustcmd

    if device is None or not device.machine or inv is None or ca.config(inv) is None:
        return None
    m = ca.machine(inv, device.machine)
    here = trustcmd.this_computer()
    if m is None or m.revoked or (here and here["machine"] == m.name):
        return None
    return m


def logins_on(device: Any, inv: Any, ship: Any) -> tuple[bool, bool]:
    """From the machine a device runs on: may plain ssh / herdr --remote / hc reach this Ship
    (as the agent, as you)? Its machine must be said and trusted, its Ships must
    include this one, and the Ship must have a host certificate."""
    from fleetledger import trustcmd

    m = machine_of(device, inv)
    if m is None or not m.reaches(inv, ship) or not trustcmd.has_cert(ship):
        return False, False
    return "agent" in m.logins, "owner" in m.logins


def machine_choices(inv: Any) -> list[str]:
    """The machines a device may say it runs on: trusted ones, but not this computer."""
    from fleetledger import ca, trustcmd

    if inv is None or ca.config(inv) is None:
        return []
    here = trustcmd.this_computer()
    return [m.name for m in ca.machines(inv)
            if not m.revoked and not (here and here["machine"] == m.name)]


def _inventory_quietly(ui: Any) -> Any:
    try:
        store = ui.app.store_factory()
        return store.load() if store.exists() else None
    except FleetError:
        return None


def section_context(ui: Any, session: Any) -> dict[str, Any]:
    """What Settings ▸ Devices shows. Reads two small files; contacts nothing."""
    from fleetledger.web import remote

    try:
        setting, broken = remote.load(), None
    except FleetError as exc:
        setting, broken = None, exc.message
    now = _store(ui).wall()
    mine = session.device.key if session.device is not None else None
    rows = [{
        "key": d.key, "short": d.short, "name": d.name, "login": d.login,
        "paired": devices_mod.when(d.paired), "how": d.how,
        "seen": devices_mod.when(d.seen), "machine": d.machine,
        "fresh_until": devices_mod.when(d.paired + devices_mod.FRESH) if d.fresh(now) else None,
        "mine": d.key == mine,
    } for d in _store(ui).all()]
    inv = ui.fleet.snapshot.inventory or _inventory_quietly(ui)
    return {
        "remote_ui": setting,
        "remote_broken": broken,
        "remote_served": ui.gate.remote is not None,
        "devices": rows,
        "on_remote": session.device is not None,
        "fresh_hours": devices_mod.FRESH // 3600,
        "pair_minutes": devices_mod.PAIR_TTL // 60,
        "machine_choices": machine_choices(inv),
    }


async def pair(request: Request) -> Response:
    """Pair a device: a one-time link, shown once (with a QR code), never again."""
    from fleetledger.web.app import render

    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    how = (session.device.name if session.device is not None
           else devices_mod.FROM_HERE)
    machine = one(fields, "machine") or None
    try:
        link, setting = devices_mod.pair_link(
            one(fields, "name"), how, _store(ui), machine=machine,
            inv=await run_in_threadpool(ui.app.store_factory().load) if machine else None)
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return RedirectResponse(TO_DEVICES, status_code=303)
    name = devices_mod.clean_name(one(fields, "name")) if one(fields, "name") else None
    return render(request, "device_pair.html", link=link, qr=devices_mod.qr_code(link),
                  name=name, origin=setting.origin, logins=setting.logins,
                  minutes=devices_mod.PAIR_TTL // 60, served=ui.gate.remote is not None)


def _chosen(ui: Any, key: str):
    return next((d for d in _store(ui).all() if key and d.key == key), None)


async def rename(request: Request) -> Response:
    ui = ui_of(request)
    fields = await form(request)
    device = _chosen(ui, one(fields, "device"))
    if device is None:
        flash(request, "bad", "No such paired device (it may have lapsed).")
        return RedirectResponse(TO_DEVICES, status_code=303)
    try:
        changed = devices_mod.change(_store(ui), None, device, name=one(fields, "name"))
    except FleetError as exc:
        flash(request, "bad", exc.message)
    else:
        flash(request, "ok", f"{device.name} is now called {changed.name}.")
    return RedirectResponse(TO_DEVICES, status_code=303)


async def set_machine(request: Request) -> Response:
    """Which trusted machine a device runs on (`ship-fleet ui devices D --machine M`), or
    none. back=SHIP returns to that Ship's Access tab."""
    ui = ui_of(request)
    fields = await form(request)
    ship = one(fields, "back")
    where = (f"/ships/{ship}/access" if ship and NAME_RE.match(ship) else TO_DEVICES)
    key = one(fields, "device")
    if key == "mine" and session_of(request).device is not None:
        key = session_of(request).device.key
    device = _chosen(ui, key)
    machine = one(fields, "machine") or None
    if device is None:
        flash(request, "bad", "No such paired device (it may have lapsed).")
        return RedirectResponse(where, status_code=303)
    try:
        inv = await run_in_threadpool(ui.app.store_factory().load) if machine else None
        changed = devices_mod.change(_store(ui), inv, device, machine=machine)
    except FleetError as exc:
        flash(request, "bad", exc.message)
    else:
        flash(request, "ok", f"{changed.name} runs on {changed.machine}: its pages show "
                             f"{changed.machine}'s attach commands." if changed.machine else
              f"{changed.name} runs on no machine you trust.")
    return RedirectResponse(where, status_code=303)


async def revoke(request: Request) -> Response:
    """Sign one device out (ship-fleet ui revoke), behind its dialog."""
    from fleetledger.web.app import render

    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    if one(fields, "confirm") != "yes":
        flash(request, "bad", "Tick the box to confirm.")
        return RedirectResponse(TO_DEVICES, status_code=303)
    device = _chosen(ui, one(fields, "device"))
    if device is None:
        flash(request, "bad", "No such paired device (it may have lapsed).")
        return RedirectResponse(TO_DEVICES, status_code=303)
    devices_mod.revoke(_store(ui), [device])
    if session.device is not None and device.key == session.device.key:
        return render(request, "message.html", title="Signed out",
                      text="This browser is signed out of FleetLedger. It needs a new "
                           "pairing link to sign in again.")
    flash(request, "ok", f"Signed out {device.name}. It needs a new pairing link to sign "
                         "in again.")
    return RedirectResponse(TO_DEVICES, status_code=303)


async def revoke_others(request: Request) -> Response:
    """Sign out every paired device but this browser (all of them, from this computer)."""
    ui = ui_of(request)
    session = session_of(request)
    fields = await form(request)
    if one(fields, "confirm") != "yes":
        flash(request, "bad", "Tick the box to confirm.")
        return RedirectResponse(TO_DEVICES, status_code=303)
    mine = session.device.key if session.device is not None else None
    count = devices_mod.revoke(_store(ui), [d for d in _store(ui).all() if d.key != mine])
    flash(request, "ok", f"Signed out {count} device(s)." if count else
          "No other device was paired.")
    return RedirectResponse(TO_DEVICES, status_code=303)


def routes() -> list[Route]:
    return [
        Route("/settings/devices/pair", pair, methods=["POST"]),
        Route("/settings/devices/rename", rename, methods=["POST"]),
        Route("/settings/devices/machine", set_machine, methods=["POST"]),
        Route("/settings/devices/revoke", revoke, methods=["POST"]),
        Route("/settings/devices/revoke-others", revoke_others, methods=["POST"]),
    ]
