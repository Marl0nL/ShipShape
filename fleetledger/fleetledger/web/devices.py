"""Browsers paired with the remote web UI: one pairing each, kept across restarts,
revocable (docs/runbooks/remote-ui.md).

- A **pairing link** is a one-time link for the remote address only. It comes from
  `ship-fleet ui link` (over ssh is fine; the UI need not be running), from
  Settings ▸ Devices ▸ Pair a device on a browser already signed in, or from the
  terminal running `ship-fleet ui`. It works once, within PAIR_TTL; only its
  SHA-256 is kept.
- Opening it makes a **device**: a session named after the device ("laptop") and
  tied to the Tailscale login that opened it; every later request must carry the
  same login. devices.json keeps only the SHA-256 of the session id, never the id,
  so the file opens nothing. The CSRF token is derived from the id, so it is never
  stored either.
- A device lapses after IDLE without a request or ABSOLUTE after pairing, whichever
  comes first, and on Revoke (Settings ▸ Devices, or `ship-fleet ui revoke`). The
  running UI notices a change to the file at its next request.
- Danger actions (FRESH_ONLY: destroy, rollback, trusting and untrusting
  machines, setup's keys and tokens, pairing another device ...) need a device
  paired within FRESH, so a stolen laptop's week-old session cannot destroy Ships
  or mint itself a new pairing. Pairing again refreshes it. A browser on this
  computer is never held to it.

Both files are 0600 in a 0700 folder under FleetLedger's data folder, written
atomically under a lock, so `ship-fleet ui link` and a running UI share them. A file
that cannot be read counts as empty: nobody is signed in.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any

from fleetledger import paths
from fleetledger.errors import FleetError

PAIR_TTL = 15 * 60
IDLE = 12 * 3600
ABSOLUTE = 30 * 24 * 3600
FRESH = 4 * 3600
SEEN_EVERY = 5 * 60  # how often a device's last request is written down
MAX_DEVICES = 50
MAX_PAIRINGS = 20
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._'()-]{0,39}$")
KEY_RE = re.compile(r"^[0-9a-f]{64}$")
FROM_LINK = "ship-fleet ui link"
FROM_TERMINAL = "the terminal running ship-fleet ui"
FROM_HERE = "Settings on this computer"
# Changes a remote device may make only within FRESH of pairing (any method but GET
# and HEAD; the gate checks it before reading the body).
FRESH_ONLY = tuple(re.compile(p) for p in (
    r"^/ships/[^/]+/(destroy|forget)$",
    r"^/ships/[^/]+/snapshots/(rollback|delete)$",
    r"^/ships/[^/]+/repin/confirm$",           # accepting a new host key
    r"^/settings/machines/(trust|untrust|scope)$",
    r"^/settings/images/remove$",
    r"^/settings/devices/pair$",               # pairing another device
    r"^/setup/",                               # the fleet's keys, CA, tokens, OAuth
))


def needs_fresh(path: str) -> bool:
    return any(p.match(path) for p in FRESH_ONLY)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_for(session_id: str) -> str:
    """A device's CSRF token, derived from its session id (so never stored)."""
    mac = hmac.new(session_id.encode("utf-8"), b"fleetledger csrf", hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def clean_name(text: str | None) -> str | None:
    """A device name as typed, or None if it is not one (empty, too long, odd characters)."""
    name = " ".join((text or "").split())
    return name if NAME_RE.match(name) else None


def guess_name(user_agent: str | None) -> str:
    """A first name for a device paired without one, from its browser's User-Agent."""
    ua = user_agent or ""
    for needle, name in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"),
                         ("Macintosh", "Mac"), ("Windows", "Windows PC"),
                         ("CrOS", "Chromebook"), ("Linux", "Linux computer")):
        if needle in ua:
            return name
    return "browser"


@dataclass(frozen=True)
class Device:
    key: str  # sha256 of the session id (hex)
    name: str  # what it is called: "laptop"
    login: str  # the Tailscale login that paired it; every request must carry it
    origin: str  # the remote address it was paired on
    paired: float  # wall clock
    seen: float  # wall clock of its last request, written at most every SEEN_EVERY
    how: str  # where it was paired from: FROM_LINK, FROM_TERMINAL, FROM_HERE, or a device name
    machine: str | None = None  # the trusted machine it runs on, once you say

    @property
    def short(self) -> str:
        """An id to name it by on the command line."""
        return self.key[:8]

    def lapsed(self, now: float, seen: float | None = None) -> bool:
        last = max(self.seen, seen or 0.0)
        return now - last > IDLE or now - self.paired > ABSOLUTE

    def fresh(self, now: float) -> bool:
        return now - self.paired <= FRESH


@dataclass(frozen=True)
class Pairing:
    key: str  # sha256 of the link's token (hex)
    expires: float  # wall clock
    name: str | None  # the device's name, if the link was made for one
    how: str
    machine: str | None = None  # the trusted machine the device runs on, if said


TIMES = ("paired", "seen", "expires")
MAYBE = ("machine", "name")  # may be None (a Device's name may not)


def _typed(record: Any) -> bool:
    """Is every field of a record read back the type FleetLedger writes? (Anything else
    is left out, so a hand-edited file signs nobody in rather than failing a page.)"""
    for f in fields(record):
        value = getattr(record, f.name)
        if f.name in TIMES:
            ok = isinstance(value, int | float) and not isinstance(value, bool)
        elif f.name in MAYBE and not (isinstance(record, Device) and f.name == "name"):
            ok = value is None or isinstance(value, str)
        else:
            ok = isinstance(value, str)
        if not ok:
            return False
    return True


class Devices:
    """The paired devices and the pairing links waiting to be opened."""

    def __init__(self, folder: Path | None = None, wall: Callable[[], float] = time.time):
        self.folder = folder or paths.data_dir() / "web"
        self.wall = wall
        self._lock = threading.Lock()
        self._cache: dict[str, Device] = {}
        self._stamp: tuple[int, int, int] | None = None
        self._seen: dict[str, float] = {}  # key -> last request, not yet written down

    # --- files ---------------------------------------------------------------------
    @property
    def devices_file(self) -> Path:
        return self.folder / "devices.json"

    @property
    def pairings_file(self) -> Path:
        return self.folder / "pairings.json"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        paths.private_dir(self.folder)
        with self._lock, open(self.folder / ".lock", "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    @staticmethod
    def _read(path: Path, kind: type) -> list[Any]:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []  # missing or unreadable: nothing in it (fail closed)
        items = doc.get("items") if isinstance(doc, dict) and doc.get("schema") == 1 else None
        names = {f.name for f in fields(kind)}
        out = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not set(item) <= names:
                continue
            try:
                record = kind(**item)
            except TypeError:
                continue
            if KEY_RE.match(str(record.key)) and _typed(record):
                out.append(record)
        return out

    @staticmethod
    def _write(path: Path, records: list[Any]) -> None:
        doc = {"schema": 1, "items": [asdict(r) for r in records]}
        paths.write_private(path, (json.dumps(doc, indent=1) + "\n").encode("utf-8"))

    def _devices(self) -> dict[str, Device]:
        return {d.key: d for d in self._read(self.devices_file, Device)}

    def _save(self, devices: dict[str, Device]) -> None:
        self._write(self.devices_file, list(devices.values()))
        self._stamp = None

    # --- pairing links -------------------------------------------------------------
    def add_pairing(self, name: str | None, how: str, machine: str | None = None) -> str:
        """A fresh one-time pairing token; only its hash is written down."""
        token = secrets.token_urlsafe(32)
        with self._locked():
            now = self.wall()
            waiting = [p for p in self._read(self.pairings_file, Pairing) if p.expires > now]
            if len(waiting) >= MAX_PAIRINGS:
                raise FleetError(f"{len(waiting)} pairing links are already waiting to be "
                                 "opened; let some expire (15 minutes) first.")
            waiting.append(Pairing(digest(token), now + PAIR_TTL, name, how, machine))
            self._write(self.pairings_file, waiting)
        return token

    def take_pairing(self, token: str) -> Pairing | None:
        """Use up a pairing token: the pairing if it was valid and unexpired, else None.
        One use only, valid or expired."""
        key = digest(token)
        with self._locked():
            waiting = self._read(self.pairings_file, Pairing)
            found = next((p for p in waiting if hmac.compare_digest(p.key, key)), None)
            if found is None:
                return None
            now = self.wall()
            self._write(self.pairings_file,
                        [p for p in waiting if p is not found and p.expires > now])
        return found if found.expires > now else None

    # --- devices -------------------------------------------------------------------
    def add(self, session_id: str, *, name: str, login: str, origin: str, how: str,
            replaces: str | None = None, machine: str | None = None) -> Device:
        """Pair a device. replaces: the key of the device this browser held before (it
        is removed; its name and machine carry over when the link named none)."""
        with self._locked():
            now = self.wall()
            devices = {k: d for k, d in self._devices().items() if not d.lapsed(now)}
            old = devices.pop(replaces, None) if replaces else None
            if len(devices) >= MAX_DEVICES:
                raise FleetError(f"{len(devices)} devices are paired already; revoke some "
                                 "first (Settings, Devices).")
            device = Device(key=digest(session_id), name=name, login=login, origin=origin,
                            paired=now, seen=now, how=how,
                            machine=machine or (old.machine if old else None))
            devices[device.key] = device
            self._save(devices)
        return device

    def all(self) -> list[Device]:
        """Every device that has not lapsed, most recently paired first."""
        now = self.wall()
        return sorted((d for d in self._current().values()
                       if not d.lapsed(now, self._seen.get(d.key))),
                      key=lambda d: d.paired, reverse=True)

    def _current(self) -> dict[str, Device]:
        """The devices file as it is now (re-read only when it changed)."""
        try:
            st = self.devices_file.stat()
            stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = (0, 0, 0)
        if stamp != self._stamp:
            self._cache = self._devices()
            self._stamp = stamp
        return self._cache

    def get(self, session_id: str, login: str) -> Device | None:
        """The device this session id belongs to, if it is paired, has not lapsed and was
        paired by this login. Counts as its request (its last-seen time moves on) only
        then: a request from any other login is not one of its own."""
        key = digest(session_id)
        device = next((d for k, d in self._current().items() if hmac.compare_digest(k, key)),
                      None)
        if device is None or not login or device.login != login:
            return None
        now = self.wall()
        if device.lapsed(now, self._seen.get(key)):
            self.remove(key)
            return None
        self._seen[key] = now
        if now - device.seen >= SEEN_EVERY:
            self._update(key, seen=now)
        return device

    def _update(self, key: str, **changes: Any) -> Device | None:
        with self._locked():
            devices = self._devices()
            if key not in devices:
                return None
            devices[key] = replace(devices[key], **changes)
            self._save(devices)
            return devices[key]

    def set_machine(self, key: str, machine: str | None) -> Device | None:
        return self._update(key, machine=machine)

    def rename(self, key: str, name: str) -> Device | None:
        return self._update(key, name=name)

    def remove(self, *keys: str) -> int:
        with self._locked():
            devices = self._devices()
            gone = [k for k in keys if k in devices]
            for key in gone:
                del devices[key]
                self._seen.pop(key, None)
            if gone:
                self._save(devices)
        return len(gone)

    def find(self, what: str) -> list[Device]:
        """Devices named `what`, or whose id starts with it (ship-fleet ui revoke)."""
        what = what.strip()
        if not what:
            return []
        current = self.all()
        by_id = [d for d in current if len(what) >= 4 and d.key.startswith(what.lower())]
        return by_id or [d for d in current if d.name.lower() == what.lower()]


# --- QR codes (a phone opens a pairing link by scanning it) --------------------------


@dataclass(frozen=True)
class QR:
    size: int  # modules per side, the quiet zone included
    path: str  # SVG path data of the dark modules: digits and M h v z only


def qr_code(text: str, border: int = 4) -> QR | None:
    """A QR code of text for an inline SVG the template draws itself (so no markup is
    passed through unescaped and the strict CSP holds), or None when segno is not
    installed (rerun fleetledger/install.sh)."""
    try:
        import segno
    except ImportError:
        return None
    matrix = segno.make(text, error="m").matrix
    runs = []
    for y, row in enumerate(matrix):
        x = 0
        while x < len(row):
            if not row[x]:
                x += 1
                continue
            start = x
            while x < len(row) and row[x]:
                x += 1
            runs.append(f"M{start + border} {y + border}h{x - start}v1h-{x - start}z")
    return QR(len(matrix) + 2 * border, "".join(runs))


def qr_terminal(text: str, out: Any) -> bool:
    try:
        import segno
    except ImportError:
        return False
    segno.make(text, error="m").terminal(out=out, compact=True)
    return True


# --- ship-fleet ui link / devices / revoke ----------------------------------------------


def when(wall: float) -> str:
    """A wall-clock time in UTC, as the rest of Settings shows times (certificates)."""
    from datetime import UTC, datetime

    return datetime.fromtimestamp(wall, UTC).strftime("%Y-%m-%d %H:%M UTC")


def trusted_machine(inv: Any, name: str) -> str:
    """name, if it is a machine the fleet trusts (and has not untrusted); else FleetError.
    What a device runs on, so the UI shows that machine's attach commands."""
    from fleetledger import ca

    m = ca.machine(inv, name) if inv is not None and ca.config(inv) is not None else None
    if m is None or m.revoked:
        raise FleetError(f"No trusted machine called {name!r}.",
                         hint="Trust it first (Settings, Trusted machines, or ship-fleet "
                              "trust NAME --key KEY.pub).")
    return m.name


KEEP = object()  # change(): leave this field as it is


def change(store: Devices, inv: Any, device: Device, *, name: str | None = None,
           machine: Any = KEEP) -> Device:
    """Rename a device, or say which trusted machine it runs on (None: none). Settings,
    Devices and `ship-fleet ui devices DEVICE --name/--machine` both come here."""
    changes: dict[str, Any] = {}
    if name is not None:
        clean = clean_name(name)
        if clean is None:
            raise FleetError(NAME_RULE)
        changes["name"] = clean
    if machine is not KEEP:
        changes["machine"] = None if machine is None else trusted_machine(inv, machine)
    if not changes:
        return device
    changed = store._update(device.key, **changes)
    if changed is None:
        raise FleetError("That device is no longer paired.")
    return changed


NAME_RULE = ("A device name is up to 40 letters, digits, spaces and . _ ' ( ) -, starting "
             "with a letter or digit.")


def pair_link(name_text: str | None, how: str, store: Devices | None = None,
              machine: str | None = None, inv: Any = None) -> tuple[str, Any]:
    """Make a one-time pairing link for one of your devices: (link, the remote setting).
    ship-fleet ui link and Settings, Devices, Pair a device both come here. machine: the
    trusted machine it runs on (checked against inv)."""
    from fleetledger.concierge import audit
    from fleetledger.web import remote

    found = remote.load()
    if found is None:
        raise FleetError("The remote web UI is off, so there is nothing to pair with. On this "
                         "computer, open the link ship-fleet ui prints.",
                         hint=f"To use it from your other devices: {remote.RUNBOOK}.")
    if not found.logins:
        raise FleetError("Nobody may sign in from your other devices yet.",
                         hint="Say whose Tailscale login may: ship-fleet ui remote --login "
                              "you@example.com")
    name = None
    if name_text is not None and name_text.strip():
        name = clean_name(name_text)
        if name is None:
            raise FleetError(NAME_RULE)
    if machine:
        machine = trusted_machine(inv, machine)
    token = (store or Devices()).add_pairing(name, how, machine or None)
    audit.record("-", "ui-link", "web", "ok")
    return f"{found.origin}/login?token={token}", found


def revoke(store: Devices, devices: list[Device]) -> int:
    """Sign devices out: they need a new pairing link to get in again."""
    from fleetledger.concierge import audit

    count = store.remove(*(d.key for d in devices))
    if count:
        audit.record("-", "ui-revoke", "web", f"{count} device(s)")
    return count


def _inventory(app) -> Any:
    store = app.store_factory()
    return store.load() if store.exists() else None


def cmd_link(args, app) -> int:
    link, found = pair_link(args.name, FROM_LINK, machine=args.machine,
                            inv=_inventory(app) if args.machine else None)
    name = clean_name(args.name) if args.name else None
    app.say(f"Pairing link for {name or 'a new device'} (works once, for 15 minutes, on "
            f"{found.origin} only, while ship-fleet ui runs):\n  {link}")
    if args.qr and not qr_terminal(link, app.out):
        app.say("(No QR code: rerun fleetledger/install.sh for the segno package.)")
    app.say(f"Open it signed in to Tailscale as {' or '.join(found.logins)}. Revoke the device "
            "any time: ship-fleet ui revoke NAME.")
    return 0


def _one(store: Devices, what: str) -> Device:
    matches = store.find(what)
    if not matches:
        raise FleetError(f"No paired device named or with id {what!r}.",
                         hint="ship-fleet ui devices lists them.")
    if len(matches) > 1:
        raise FleetError(f"{len(matches)} devices are called {what!r}: name one by "
                         f"its id ({', '.join(d.short for d in matches)}).")
    return matches[0]


def cmd_devices(args, app) -> int:
    store = Devices()
    if args.device:
        if args.machine and args.no_machine:
            raise FleetError("--machine or --no-machine, not both.")
        machine = None if args.no_machine else (args.machine or KEEP)
        device = change(store, _inventory(app) if args.machine else None,
                        _one(store, args.device), name=args.name, machine=machine)
        app.say(f"{device.short}  {device.name}"
                + (f", on {device.machine}" if device.machine else ", on no machine said"))
        return 0
    if args.name or args.machine or args.no_machine:
        raise FleetError("Name the device to change: ship-fleet ui devices DEVICE --name ...")
    devices = store.all()
    if not devices:
        app.say("No device is paired with the remote web UI.")
        return 0
    now = store.wall()
    for d in devices:
        fresh = "" if d.fresh(now) else ", needs pairing again for destroy and the like"
        on = f"; on {d.machine}" if d.machine else ""
        app.say(f"{d.short}  {d.name}  ({d.login}{on}; paired {when(d.paired)} from {d.how}; "
                f"last used {when(d.seen)}{fresh})")
    return 0


def cmd_revoke(args, app) -> int:
    store = Devices()
    if args.all:
        if args.device:
            raise FleetError("Name a device or --all, not both.")
        count = revoke(store, store.all())
        app.say(f"Signed out {count} device(s)." if count else "No device was paired.")
        return 0
    if not args.device:
        raise FleetError("Name the device to revoke (ship-fleet ui devices lists them), or "
                         "--all.")
    device = _one(store, args.device)
    revoke(store, [device])
    app.say(f"Signed out {device.name} ({device.short}). It needs a new pairing link "
            "to sign in again.")
    return 0
