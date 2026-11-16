"""The pages the gate (web/security.py) answers with when it refuses a browser: why, and
how to get back in.

Each says what happened (not signed in here; not paired; which device lapsed unused, and
when; when it was paired, for an action that needs a fresh pairing) and gives the
command that gets a new link, prefilled with the device's name and machine where they
are known, with a Copy button. The rules are the gate's own; these pages only word its
answers. A detail about a device is shown only to a browser that presents that
device's cookie, from the login that paired it (devices.Devices.lapsed_for).

A page is plain HTML with no inline code (the strict CSP holds): app.css for its looks,
app.js for Copy.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from html import escape

from fleetledger.web import devices as devices_mod

LOCAL_LINK = "ship-fleet ui link --local"
FROM_A_BROWSER = ("Or pair it from a browser that is signed in: the FleetLedger computer's "
                  f"own, or one of your devices paired in the last "
                  f"{devices_mod.FRESH // 3600} hours (Settings, Devices, Pair a device).")


@dataclass(frozen=True)
class Page:
    title: str
    why: str
    how: str
    command: str
    after: str = ""  # one more line under the command
    back: bool = False  # a link back to the fleet (the browser is still signed in)

    def html(self) -> bytes:
        cmd = escape(self.command)
        after = f"<p>{escape(self.after)}</p>" if self.after else ""
        back = '<p><a href="/">Back to the fleet</a></p>' if self.back else ""
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta name="color-scheme" content="light dark">'
            f"<title>{escape(self.title)} · FleetLedger</title>"
            '<link rel="stylesheet" href="/static/app.css">'
            '<script src="/static/app.js" defer></script></head>'
            f'<body><main><h1>{escape(self.title)}</h1>'
            f"<p>{escape(self.why)}</p><p>{escape(self.how)}</p>"
            f'<p><code class="cmd">{cmd}</code> '
            f'<button type="button" class="ghost small" data-copy="{cmd}">Copy</button></p>'
            f"{after}{back}</main></body></html>"
        ).encode()


def link_command(name: str | None = None, machine: str | None = None) -> str:
    """`ship-fleet ui link`, keeping a device's name and machine when they are known."""
    argv = ["ship-fleet", "ui", "link"]
    if name:
        argv += ["--name", name]
    if machine:
        argv += ["--machine", machine]
    return " ".join(shlex.quote(a) for a in argv)


def _keeps(name: str | None, machine: str | None) -> str:
    kept = " and ".join(x for x in ("its name" if name else "",
                                    "the machine it runs on" if machine else "") if x)
    return f" It keeps {kept}." if kept else ""


def not_signed_in(had_cookie: bool, idle: float) -> Page:
    """A browser on this computer with no session (or one that ended)."""
    why = (f"This browser's sign-in here has ended: ship-fleet ui restarted, or it went "
           f"unused for {int(idle // 3600)} hours." if had_cookie else
           "This browser is not signed in to this FleetLedger.")
    return Page(
        "Not signed in", why,
        "Get a one-time sign-in link: run this in a terminal on this computer, then open "
        "the link it prints in this browser. It works whether ship-fleet ui runs in a "
        "terminal or as a service.",
        LOCAL_LINK)


def not_paired(had_cookie: bool) -> Page:
    """A browser on the remote address that is not a paired device (any more)."""
    why = ("This browser's pairing with this FleetLedger has ended: it was revoked, or it "
           "lapsed." if had_cookie else "This device is not signed in to this FleetLedger.")
    return Page(
        "Not paired", why,
        "Pair it once: run this on the FleetLedger computer (over ssh is fine), then open "
        "the link it prints here.",
        link_command(), FROM_A_BROWSER)


def lapsed(record: devices_mod.Lapsed) -> Page:
    """A browser whose device lapsed (its lapse record: devices.Lapsed)."""
    when = devices_mod.when
    return Page(
        "Pairing lapsed",
        f"{record.name} was signed out at {when(record.since)}: it went unused for "
        f"{devices_mod.IDLE // (24 * 3600)} days (last used {when(record.seen)}).",
        "Pair it again: run this on the FleetLedger computer (over ssh is fine), then open "
        "the link it prints here." + _keeps(record.name, record.machine),
        link_command(record.name, record.machine), FROM_A_BROWSER)


def not_fresh(device: devices_mod.Device) -> Page:
    """A paired device whose pairing is too old for a danger action (devices.FRESH_ONLY)."""
    hours = devices_mod.FRESH // 3600
    return Page(
        "Pair again to do this",
        f"{device.name} was paired at {devices_mod.when(device.paired)}, more than {hours} "
        "hours ago, and this action (destroy, rollback, trusting machines, setup's keys, "
        "pairing another device ...) needs a fresh pairing.",
        "Pair this browser again: run this on the FleetLedger computer (over ssh is fine), "
        "then open the link it prints here. The new pairing replaces this device."
        + _keeps(device.name, device.machine),
        link_command(device.name, device.machine),
        f"Or from a browser that is signed in: the FleetLedger computer's own, or one of "
        f"your devices paired in the last {hours} hours (Settings, Devices, Pair a device).",
        back=True)
