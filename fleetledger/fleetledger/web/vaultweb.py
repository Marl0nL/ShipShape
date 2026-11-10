"""The vault in the web UI (vault.py; docs/vault.md).

- While the vault is locked, every page shows the last known state with a banner
  and an Unlock button, and VaultGuard refuses every change but those in LOCKED_OK:
  unlocking, locking, and pairing, renaming and revoking devices (a remote device
  must be able to reach the Unlock page, and a lost one to be signed out). The
  poller does not poll (web/fleet.py), since nothing can log in to a Ship.
- /unlock takes the passphrase (or the recovery code) in a password field, posted
  with the page's CSRF token, and hands it to vaultcmd.unlock, the function `ship-
  fleet unlock` runs. It is never echoed, flashed, logged or kept. Wrong tries are
  slowed there (vault.Tries). A remote device needs no fresh pairing to unlock: the
  passphrase is the proof.
- /vault/lock locks it now (vaultcmd.lock, as `ship-fleet vault lock`), behind its
  dialog.
"""

from __future__ import annotations

from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from fleetledger import vault, vaultcmd
from fleetledger.errors import FleetError
from fleetledger.web.common import flash, session_of, ui_of
from fleetledger.web.forms import form, one

LOCKED_OK = ("/unlock", "/vault/lock", "/settings/devices/pair", "/settings/devices/rename",
             "/settings/devices/machine", "/settings/devices/revoke",
             "/settings/devices/revoke-others")
LOCKED = ("FleetLedger's vault is locked, so it changes nothing until you unlock it "
          "(Unlock, at the top of every page).")


class VaultGuard:
    """Inside the gate (the session is checked already): while the vault is locked,
    refuse every change but LOCKED_OK, before its handler runs."""

    def __init__(self, app: Any, ui: Any) -> None:
        self.app = app
        self.ui = ui

    def __getattr__(self, name: str) -> Any:  # the Starlette app's state, routes ...
        return getattr(self.app, name)

    async def __call__(self, scope, receive, send) -> None:
        if (scope["type"] == "http"
                and scope.get("method", "GET").upper() not in ("GET", "HEAD")
                and scope.get("path") not in LOCKED_OK
                and vault.state() == "locked"):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope.get("headers", [])}
            session = (scope.get("state") or {}).get("session")
            if "hx-request" in headers or session is None:
                body = LOCKED.encode("utf-8")
                await send({"type": "http.response.start", "status": 423,
                            "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            self.ui.flash(session, "bad", LOCKED)
            response = RedirectResponse("/unlock", status_code=303)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _account(request: Request) -> str | None:
    device = session_of(request).device
    return device.login if device is not None else None


async def unlock_page(request: Request) -> Response:
    import time

    from fleetledger.web.app import render

    state = vault.state()
    waiting = vault.tries().wait(time.time()) if state == "locked" else 0
    return render(request, "unlock.html", state=state, waiting=int(waiting) + 1 if waiting
                  else 0, swap=vault.swap_on() if state != "off" else False)


async def unlock(request: Request) -> Response:
    ui = ui_of(request)
    fields = await form(request)
    recovery = one(fields, "with") == "recovery"
    secret = one(fields, "code" if recovery else "passphrase") or ""
    if not secret:
        flash(request, "bad", "Type the " + ("recovery code." if recovery else "passphrase."))
        return RedirectResponse("/unlock", status_code=303)
    try:
        done = await run_in_threadpool(vaultcmd.unlock, secret, recovery=recovery,
                                       account=_account(request))
    except FleetError as exc:  # with its hint: after wrong tries, the way out of the wait
        flash(request, "bad", exc.message + (f" {exc.hint}" if isinstance(
            exc, vault.Waiting) and exc.hint else ""))
        return RedirectResponse("/unlock", status_code=303)
    finally:
        del secret
    if done == "unlocked":
        ui.fleet.wake()  # check every Ship now, not at the next interval
    flash(request, "ok", "Unlocked, until the next boot: FleetLedger acts on the fleet again."
          + (" Set a new passphrase now: ship-fleet vault passphrase --recovery (over ssh)."
             if recovery and done == "unlocked" else ""))
    return RedirectResponse("/", status_code=303)


async def lock(request: Request) -> Response:
    ui = ui_of(request)
    fields = await form(request)
    if one(fields, "confirm") != "yes":
        flash(request, "bad", "Tick the box to confirm.")
        return RedirectResponse("/settings#vault", status_code=303)
    try:
        was = await run_in_threadpool(vaultcmd.lock, ui.app.store_factory(),
                                      _account(request))
    except FleetError as exc:
        flash(request, "bad", exc.message)
        return RedirectResponse("/settings#vault", status_code=303)
    flash(request, "ok", "Locked: FleetLedger is read-only until it is unlocked again. Your "
          "Ships keep running." if was else "The vault was locked already.")
    return RedirectResponse("/", status_code=303)



def routes() -> list[Route]:
    return [
        Route("/unlock", unlock_page),
        Route("/unlock", unlock, methods=["POST"]),
        Route("/vault/lock", lock, methods=["POST"]),
    ]
