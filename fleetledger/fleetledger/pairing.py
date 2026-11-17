"""Pairing by code: a computer FleetLedger does not know asks to be trusted, and you approve
it on a device that already has a shell here (contracts/ship-fleet.md, "ship-fleet pair";
contracts/machine-pair.schema.json; docs/security.md, "Pairing by code").

The way in. A computer with no key that logs in here (no credential at all) reaches
FleetLedger through one well-known key, PUBLIC_KEY, whose private half is in the installer,
shipshape-setup, for anyone to read: it is public by design, and it is a name for a door,
not a credential. Its one line in FleetLedger's block of ~/.ssh/authorized_keys
(authkeys.py) is

    restrict,command="/home/fleetledger/.local/bin/ship-fleet machine-pair" \
        ssh-ed25519 AAAA... shipshape-pairing          (one line; split here to fit)

so sshd runs `ship-fleet machine-pair` (cmd_machine_pair) whatever is asked, with no
terminal, no forwarding and no agent. That line is there only while pairing is on
(`ship-fleet pair on`; [ca] pairing in ships.toml; off by default), only on FleetLedger's
own VM, and only for the copy that acts. No new port and no new listener: the VM's sshd,
keys only, as before.

What anybody on the network can do through it, and nothing else:

- file a request: the computer's own ed25519 public key and the name it gives itself
  (a-z, 0-9 and -: a claim, shown as one, never trusted). The answer is a random token
  (kept here only as its SHA-256), a random nonce, the code and when it lapses;
- ask after its own request with that token: pending, approved, refused or unknown;
- withdraw its own request with that token.

The command is read from SSH_ORIGINAL_COMMAND alone: at most MAX_COMMAND bytes, printable
ASCII, one fixed pattern per verb; stdin is never read. Every answer is one small JSON
document of FleetLedger's own words: nothing about the fleet (no Ship, no machine, no
count), and the same "unknown" for a token never given, withdrawn or lapsed. It writes only
the pending store (REQUESTS, under FleetLedger's data folder) and, for a request accepted,
one audit line. The store is bounded: MAX_PENDING requests waiting at once, MAX_PER_SOURCE
from one address, MAX_PER_HOUR filed in any hour, each lapsing after TTL; a full store is
refused, never grown. A request grants nothing: no bundle, no Ship, no reading of the fleet.

Approval is yours alone, on a device that already has full access: `ship-fleet pair approve`
asks you to type the code the new computer shows (the code is never shown on this side, so
it cannot be copied from here), then a name, scope and logins (defaults shown), and ends in
`ship-fleet trust NAME --key ...` with every rule trust has. The code is derived from the
request's nonce and the computer's own public key (code_for), and the computer works it out
itself from its key: a second computer asking at the same time has another key and another
nonce, so another code, and the code you type approves only the request it belongs to (two
requests with one code: neither is approved). What a request leaves behind once refused,
withdrawn or lapsed: nothing (a refused one keeps, until it would have lapsed, its token's
hash and the word refused, so the computer waiting can say so).
"""

from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fleetledger import paths

# The well-known key (its private half is in shipshape-setup; a test checks they are one pair).
PUBLIC_KEY = ("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHM3wOjluAVyUgz6XOKZaEgE8ue1nCweKlkxeo8yZyCn "
              "shipshape-pairing")
BLOB = PUBLIC_KEY.split()[1]
COMMENT = "shipshape-pairing"
VERB = "machine-pair"
SCHEMA = "shipshape.machine-pair/1"
TTL = timedelta(minutes=10)
MAX_PENDING = 4
MAX_PER_SOURCE = 2
MAX_PER_HOUR = 20
MAX_COMMAND = 200
MAX_STORE = 64 * 1024  # bytes: a larger file is not FleetLedger's, and counts as empty
CODE_DIGITS = 6
FOLDER = "pairing"
REQUESTS = "requests.json"

NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")  # trustcmd.MACHINE_RE, as fullmatch
HEX32 = re.compile(r"[0-9a-f]{32}")
ID_RE = re.compile(r"p[0-9a-f]{6}")
_REQUEST = re.compile(r"request (AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{43,48}={0,2}) "
                      r"([a-z0-9][a-z0-9-]{0,30})")
_TOKEN_VERB = re.compile(r"(status|cancel) ([0-9a-f]{32})")


def is_on(inv) -> bool:
    """[ca] pairing in ships.toml: off unless you turned it on (ship-fleet pair on)."""
    return bool(((getattr(inv, "doc", None) or {}).get("ca") or {}).get("pairing", False))


def code_for(nonce: str, blob: str) -> str:
    """The code both sides work out: from the request's nonce and the computer's key. The
    installer computes the same in sh (shipshape-setup's pair_code)."""
    digest = hashlib.sha256(f"shipshape-pair/1:{nonce}:{blob}".encode("ascii")).hexdigest()
    return f"{int(digest[:8], 16) % 10 ** CODE_DIGITS:0{CODE_DIGITS}d}"


def code_said(code: str) -> str:
    return f"{code[:3]} {code[3:]}"


def typed_code(text: str) -> str:
    """A code as typed: spaces and dashes let go."""
    return re.sub(r"[\s-]", "", str(text or ""))


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


# --- the pending store ----------------------------------------------------------------------


def folder() -> Path:
    return paths.data_dir() / FOLDER


def requests_path() -> Path:
    return folder() / REQUESTS


@dataclass
class Request:
    id: str
    token_sha256: str
    state: str  # pending, approved, refused
    asked: str
    expires: str
    nonce: str | None = None
    key: str | None = None  # "ssh-ed25519 BLOB" (pending only)
    host: str | None = None  # the name it gave itself: a claim (pending only)
    source: str | None = None  # the address sshd saw it from (pending only)
    machine: str | None = None  # approved: the name it is trusted as

    def blob(self) -> str:
        return (self.key or "").split(" ")[-1]

    def code(self) -> str:
        return code_for(self.nonce or "", self.blob())

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def _stamp(when: datetime) -> str:
    from fleetledger import ca

    return ca.stamp(when)


def _parse(text: str) -> datetime | None:
    from fleetledger import ca

    try:
        return ca.parse_stamp(text)
    except (TypeError, ValueError):
        return None


def _now() -> datetime:
    from fleetledger import ca

    return ca.utc_now()


def _well_formed(e: Any) -> Request | None:
    """A stored request, checked field by field: anything else is dropped as it is read."""
    if not isinstance(e, dict) or not all(isinstance(v, str) for v in e.values()):
        return None
    try:
        r = Request(**e)
    except TypeError:
        return None
    if not (ID_RE.fullmatch(r.id) and re.fullmatch(r"[0-9a-f]{64}", r.token_sha256)
            and r.state in ("pending", "approved", "refused")
            and _parse(r.asked) and _parse(r.expires)):
        return None
    if r.state == "pending":
        from fleetledger import authkeys

        if not (r.nonce and HEX32.fullmatch(r.nonce) and r.key and r.key.startswith("ssh-ed25519 ")
                and authkeys.ed25519_blob(r.key) and r.host and NAME_RE.fullmatch(r.host)):
            return None
    if r.state == "approved" and not (r.machine and NAME_RE.fullmatch(r.machine)):
        return None
    return r


@dataclass
class Pending:
    requests: list[Request]
    filed: list[str]  # when each request of the last hour was filed (the hourly limit)

    def live(self, now: datetime) -> list[Request]:
        return [r for r in self.requests if (_parse(r.expires) or now) > now]

    def pending(self, now: datetime) -> list[Request]:
        return [r for r in self.live(now) if r.state == "pending"]

    def by_token(self, token: str, now: datetime) -> Request | None:
        h = _hash(token)
        return next((r for r in self.live(now) if secrets.compare_digest(r.token_sha256, h)),
                    None)

    def by_id(self, rid: str, now: datetime) -> Request | None:
        return next((r for r in self.pending(now) if r.id == rid), None)


def _read() -> Pending:
    """The store, or an empty one: a file that is missing, too large or not FleetLedger's
    counts as empty (and is rewritten whole by the next change)."""
    try:
        p = requests_path()
        if p.is_symlink() or not p.is_file() or p.stat().st_size > MAX_STORE:
            return Pending([], [])
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return Pending([], [])
    if not isinstance(doc, dict):
        return Pending([], [])
    found = [r for e in doc.get("requests", []) if (r := _well_formed(e)) is not None]
    filed = [s for s in doc.get("filed", []) if isinstance(s, str) and _parse(s)]
    return Pending(found[: MAX_PENDING * 4], filed[-MAX_PER_HOUR:])


def _write(p: Pending, now: datetime) -> None:
    hour = now - timedelta(hours=1)
    doc = {"requests": [r.as_dict() for r in p.live(now)],
           "filed": [s for s in p.filed if (_parse(s) or hour) > hour][-MAX_PER_HOUR:]}
    if not doc["requests"] and not doc["filed"]:
        requests_path().unlink(missing_ok=True)
        return
    paths.write_private(requests_path(), (json.dumps(doc, indent=1) + "\n").encode("utf-8"))


@contextmanager
def locked() -> Iterator[Pending]:
    """The store, read under its folder's lock (flock: no lock file), written back on the
    way out; lapsed requests are left out of what is written."""
    d = paths.private_dir(folder())
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        p = _read()
        yield p
        _write(p, _now())
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read() -> Pending:
    """The store as it is (no lock, no write): what ca show and the lists read."""
    return _read()


def clear() -> None:
    """Every request gone (pair off)."""
    try:
        requests_path().unlink(missing_ok=True)
    except OSError:
        pass


# --- the forced command: what an unknown computer may do ------------------------------------

OFF = "Pairing by code is off on this FleetLedger."
BAD = "Not a pairing request this FleetLedger takes."
BUSY = ("FleetLedger has as many pairing requests waiting as it takes just now: try again "
        "in a few minutes.")
FAILED = "FleetLedger could not take the request just now."


class _Answer(Exception):
    def __init__(self, state: str, why: str):
        super().__init__(why)
        self.state, self.why = state, why


def _source() -> str | None:
    """The address sshd saw, without an IPv6 zone (fe80::1%eth0 is fe80::1): digits, a-f,
    dots and colons only, as the contract says."""
    words = os.environ.get("SSH_CONNECTION", "").split()
    try:
        return str(ipaddress.ip_address(words[0].split("%", 1)[0])) if words else None
    except ValueError:
        return None


def off_answer() -> str:
    return json.dumps({"schema": SCHEMA, "state": "off", "why": OFF})


FAILURE_MARK = ".last-failure"  # in the store's folder: when a failure was last audited


def _audit_failure(what: str) -> None:
    """One audit line for an internal failure, at most once a minute, however often an
    unknown computer asks while it lasts."""
    from fleetledger.concierge import audit

    mark = folder() / FAILURE_MARK
    try:
        import time

        if mark.exists() and time.time() - mark.stat().st_mtime < 60:
            return
        paths.private_dir(folder())
        mark.touch(mode=0o600)
        os.utime(mark)
    except OSError:
        pass
    audit.record("-", "pair-request", "-", f"failed: {what}")


def _accepting(store) -> None:
    """Pairing on, and here: FleetLedger's own VM, the copy that acts. Else OFF."""
    from fleetledger import authkeys

    inv = store.load()
    if not is_on(inv) or not authkeys.adds_here(store, inv):
        raise _Answer("off", OFF)


def _new_id(p: Pending) -> str:
    taken = {r.id for r in p.requests}
    while True:
        rid = f"p{secrets.token_hex(3)}"
        if rid not in taken:
            return rid


def file_request(store, blob: str, host: str, source: str | None) -> dict[str, Any]:
    """A new request (or this key's earlier one, replaced): the answer for the computer."""
    from fleetledger import authkeys
    from fleetledger.concierge import audit

    if authkeys.ed25519_blob(f"ssh-ed25519 {blob}") is None or blob == BLOB:
        raise _Answer("refused", BAD)
    _accepting(store)
    now = _now()
    key = f"ssh-ed25519 {blob}"
    with locked() as p:
        # This key's own earlier request makes way for this one (an installer run again).
        p.requests = [r for r in p.live(now) if not (r.state == "pending" and r.key == key)]
        hour = now - timedelta(hours=1)
        p.filed = [s for s in p.filed if (_parse(s) or hour) > hour]
        waiting = p.pending(now)
        if (len(waiting) >= MAX_PENDING or len(p.filed) >= MAX_PER_HOUR
                or len(p.requests) >= MAX_PENDING * 4
                or (source and sum(r.source == source for r in waiting) >= MAX_PER_SOURCE)):
            raise _Answer("busy", BUSY)
        token, nonce = secrets.token_hex(16), secrets.token_hex(16)
        r = Request(id=_new_id(p), token_sha256=_hash(token), state="pending",
                    asked=_stamp(now), expires=_stamp(now + TTL), nonce=nonce, key=key,
                    host=host, source=source)
        p.requests.append(r)
        p.filed.append(_stamp(now))
    audit.record("-", "pair-request", r.id, "pending")
    return {"schema": SCHEMA, "state": "pending", "id": r.id, "token": token, "nonce": nonce,
            "code": r.code(), "expires_in": int(TTL.total_seconds())}


def approved_answer(store, r: Request) -> dict[str, Any]:
    """What the computer learns once you approved it: the name it is trusted as, and how to
    reach this VM (its user, names and host key, as machine-setup gives them) and which
    refresher to run, so it ends as a trusted run of the installer does."""
    import getpass

    from fleetledger import appliance, refresher

    me = appliance.fleet_self(store.load()) or {}
    hosts = list(dict.fromkeys([*me.get("names", []), me.get("name", "")]))
    return {"schema": SCHEMA, "state": "approved", "machine": r.machine,
            "fleetledger": {"user": getpass.getuser(), "hosts": [h for h in hosts if h],
                            "host_key": " ".join(str(me.get("host_key", "")).split()[:2])},
            "refresher_sha256": hashlib.sha256(refresher.script()).hexdigest()}


def status(store, token: str) -> dict[str, Any]:
    now = _now()
    r = read().by_token(token, now)
    if r is None:
        return {"schema": SCHEMA, "state": "unknown"}
    if r.state == "approved":
        return approved_answer(store, r)
    if r.state == "refused":
        return {"schema": SCHEMA, "state": "refused"}
    left = int(((_parse(r.expires) or now) - now).total_seconds())
    return {"schema": SCHEMA, "state": "pending", "expires_in": max(left, 0)}


def cancel(token: str) -> dict[str, Any]:
    """The computer withdraws its own request (Ctrl-C in the installer): gone, unless it was
    approved meanwhile (that stays: the machine is trusted; untrust it if need be)."""
    now = _now()
    if read().by_token(token, now) is None:
        return {"schema": SCHEMA, "state": "unknown"}
    with locked() as p:
        h = _hash(token)
        p.requests = [r for r in p.live(now)
                      if not (r.state != "approved" and secrets.compare_digest(r.token_sha256, h))]
    return {"schema": SCHEMA, "state": "unknown"}


def answer(store, command: str | None, source: str | None) -> dict[str, Any]:
    """One command, as SSH_ORIGINAL_COMMAND gives it, answered (or _Answer)."""
    text = command or ""
    if len(text) > MAX_COMMAND or not re.fullmatch(r"[ -~]*", text):
        raise _Answer("refused", BAD)
    if m := _REQUEST.fullmatch(text):
        return file_request(store, m.group(1), m.group(2), source)
    if m := _TOKEN_VERB.fullmatch(text):
        _accepting(store)
        return status(store, m.group(2)) if m.group(1) == "status" else cancel(m.group(2))
    raise _Answer("refused", BAD)


def cmd_machine_pair(args, app) -> int:
    """The forced command. Prints one JSON document, whatever happens; exit 0 when it was
    answered, 1 when refused. No traceback and none of FleetLedger's own words reach the
    caller: a failure says FAILED, and its reason stays here, in the audit log."""
    store = app.store_factory()
    try:
        doc, code = answer(store, os.environ.get("SSH_ORIGINAL_COMMAND"), _source()), 0
    except _Answer as exc:
        doc, code = {"schema": SCHEMA, "state": exc.state, "why": exc.why}, 1
    except Exception as exc:  # noqa: BLE001 - the caller learns nothing of it
        _audit_failure(type(exc).__name__)
        doc, code = {"schema": SCHEMA, "state": "refused", "why": FAILED}, 1
    app.say(json.dumps(doc))
    return code


# --- what you see and do (a shell on FleetLedger) -------------------------------------------


def listing(now: datetime | None = None) -> list[dict[str, Any]]:
    """The requests waiting, for pair ls and ca show --json: never the code or the token."""
    from fleetledger import sshkeys

    now = now or _now()
    out = []
    for r in read().pending(now):
        out.append({"id": r.id, "host": r.host, "fingerprint": sshkeys.fingerprint(r.key or ""),
                    "source": r.source, "asked": r.asked, "expires": r.expires})
    return out


def summary(inv) -> dict[str, Any]:
    """ca show --json's pairing: on, and the requests waiting (none while off)."""
    on = is_on(inv)
    return {"on": on, "pending": listing() if on else []}
