"""ship-fleet machine-bundle NAME: one trusted machine's own bundle, as JSON data
(contracts/machine-bundle.schema.json; contracts/ship-fleet.md).

A trusted machine keeps itself current by asking for this over ssh, with a key whose
line in FleetLedger's authorized_keys runs this command alone, for that machine's
name. So it must be safe to be the only thing such a key can run:

- the machine is named by the argument alone (the forced command gives it); there
  are no options. Nothing is read from stdin or from SSH_ORIGINAL_COMMAND, and no
  shell is run (ssh-keygen signs, as `ship-fleet trust` does, with no terminal to
  prompt on and stdin closed);
- it answers only for a machine that is trusted, not untrusted, and not idle past
  ca.IDLE_LIMIT, with only the Ships its scope reaches; an unknown machine and an
  untrusted one get the same refusal, and every refusal prints nothing on stdout;
- it writes only that machine's record in ships.toml (when it last asked; a new
  certificate when one was signed; when it was refused for being idle), the
  certificate beside its bundle, and one audit line per call.

The bundle is data: the machine's certificate, the CA lines for its known_hosts, each
Ship's host names (the one that resolves on the FleetLedger computer and the overlay one)
with its tags and runtime, and FleetLedger's own host key. It also carries the refresher
script, base64, which only an install a person runs ever decodes (the first-install line
`ship-fleet trust` prints, or the installer shipshape-setup)
(fleetledger/refresher): the refresher never reads it. A new
certificate is signed only once the current one is past half its life, by the same
code and for the same lifetime as `ship-fleet trust`. While the vault is locked
nothing is signed: the bundle comes with the current certificate while it is still
valid, and says why it was not renewed.

FleetLedger's own computer renews itself (trustcmd.renew_this_computer_if_due); it may
ask here too, and the idle limit never applies to it.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from typing import Any

from fleetledger import appliance, attach, ca, paths, refresher, sshkeys, trustcmd, vault
from fleetledger.concierge import audit
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory, Ship

SCHEMA = "shipshape.machine-bundle/1"
ACTION = "machine-bundle"
# Asking again within this long does not rewrite ships.toml: when it last asked is
# kept to the minute it matters at (the 30-day idle limit), however often it asks.
RECORD_EVERY = timedelta(minutes=5)
NOT_TRUSTED = "No trusted machine called {name}: FleetLedger gives it no bundle."


class _Refused(FleetError):
    """A refusal, audited with its reason; what the caller sees is the message."""

    def __init__(self, reason: str, message: str, hint: str | None = None):
        super().__init__(message, hint=hint)
        self.reason = reason


def _not_trusted(name: str, reason: str) -> _Refused:
    # The same words for a machine never trusted and one untrusted: the caller learns
    # nothing about which names FleetLedger knows.
    return _Refused(reason, NOT_TRUSTED.format(name=name))


def _when(stamp: str | None) -> str:
    return f"{stamp[:10]} {stamp[11:16]} UTC" if stamp else "never"


def no_prompt_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
    """Run ssh-keygen or ssh-add so that it can never ask for anything: stdin closed,
    no controlling terminal (a new session), and no askpass program."""
    env = {k: v for k, v in os.environ.items() if k not in ("SSH_ASKPASS", "DISPLAY")}
    env["SSH_ASKPASS_REQUIRE"] = "never"
    kw.pop("stdin", None)
    kw.setdefault("timeout", 60)  # a caller's own (ssh-add -L gives 10) wins
    return subprocess.run(argv, stdin=subprocess.DEVNULL, start_new_session=True, env=env,
                          **kw)


def _signer(app, inv: Inventory) -> ca.Signer:
    signer = getattr(app, "signer", None)
    if signer is not None:  # tests
        return signer
    from fleetledger import keys

    keys.ensure_loaded(inv)  # managed CA keys into FleetLedger's ssh-agent, from the store
    return ca.Signer(runner=no_prompt_run)


def cert_path(name: str):
    """Where the machine's current certificate is kept: its bundle's folder (the one
    `ship-fleet trust NAME` writes, which ship-fleet migrate carries)."""
    return ca.bundle_dir(name) / f"{name}-cert.pub"


def current_cert(cfg: ca.CAConfig, m: ca.Machine, now: datetime) -> tuple[str, ca.CertInfo] | None:
    """The machine's last certificate, if FleetLedger has it and it still serves: the one
    its record names (serial), for its key, signed by today's user CA, for exactly its
    scope and logins now (a certificate for more is in the revocation list), and not
    lapsed. None: a new one is needed."""
    try:
        line = cert_path(m.name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    info = ca.cert_info(line)
    if (info is None or m.last_serial is None or info.serial != m.last_serial
            or info.key_blob != sshkeys.blob(m.public_key)
            or info.signer_blob != sshkeys.blob(cfg.user_public)
            or sorted(info.principals) != sorted(m.principals)
            or info.valid_before <= now):
        return None
    return line, info


def _is_this_computer(name: str) -> bool:
    here = trustcmd.this_computer()
    return bool(here and here["machine"] == name)


def _trusted(store, name: str) -> tuple[Inventory, ca.CAConfig, ca.Machine]:
    if not trustcmd.MACHINE_RE.fullmatch(name):  # fullmatch: no trailing newline either
        raise _not_trusted("that name", "not a machine name")
    inv = store.load()
    cfg = ca.config(inv)
    m = ca.machine(inv, name) if cfg is not None else None
    if m is None:
        raise _not_trusted(name, "unknown")
    if m.revoked:
        raise _not_trusted(name, "untrusted")
    return inv, cfg, m


def _refuse_if_idle(store, m: ca.Machine, now: datetime) -> None:
    if _is_this_computer(m.name) or not ca.idle_refused(m, now):
        return
    since = ca.stamp(ca.idle_since(m))
    if m.idle_refused is None:  # its first refusal: the attention item says when
        with store.edit() as ed:
            again = ca.machine(store.load(), m.name)  # as locked: not allowed meanwhile
            if again is not None and again.idle_refused is None and ca.idle_refused(again, now):
                ed.set_machine_field(m.name, "idle_refused", ca.stamp(now))
    days = ca.IDLE_LIMIT.days
    raise _Refused(
        f"idle since {since}",
        f"{m.name} has not asked for its bundle since {_when(since)}, over {days} days, so "
        "FleetLedger gives it none until you allow it again.",
        hint=f"On the FleetLedger computer: ship-fleet trust {m.name} --allow-again")


def _renew(app, store, name: str, now: datetime) -> bool:
    """Sign and record a new certificate unless another ask did meanwhile, with
    ships.toml locked throughout, so two asks at once never lose a record of one
    issued (a narrower scope later revokes only what is recorded). True: signed."""
    signer = _signer(app, store.load())
    with store.edit() as ed:
        inv = store.load()  # as locked: what this edit changes
        cfg = ca.config(inv)  # as locked too: a CA rotated meanwhile signs with the new key
        m = ca.machine(inv, name) if cfg is not None else None
        if m is None or m.revoked:
            raise _not_trusted(name, "untrusted" if m else "unknown")
        found = current_cert(cfg, m, now)
        if found is not None and not ca.half_life_passed(m, cfg, found[1].valid_before, now):
            ed.set_machine_field(name, "last_refreshed", ca.stamp(now))
            return False
        hours = ca.renewal_hours(m, cfg)
        cert, serial, until = ca.sign_user(signer, cfg, m, hours=hours, now=now)
        try:
            paths.private_dir(cert_path(name).parent)
            paths.write_private(cert_path(name), (cert + "\n").encode("utf-8"))
        except OSError as exc:
            raise FleetError(f"Could not keep the new certificate ({exc.strerror or exc}).") \
                from exc
        ed.set_machine_field(name, "last_serial", serial)
        ed.set_machine_field(name, "last_valid_before", ca.stamp(until))
        ed.set_machine_field(name, "last_cert_hours", hours)
        ed.set_machine_field(name, "issued", ca.issued_record(m, cfg, serial, until, now=now))
        ed.set_machine_field(name, "last_refreshed", ca.stamp(now))
    return True


def _recorded_lately(m: ca.Machine | None, now: datetime) -> bool:
    try:
        last = ca.parse_stamp(m.last_refreshed) if m else None
    except ValueError:  # edited by hand: write a good one
        return False
    return last is not None and timedelta(0) <= now - last < RECORD_EVERY


def _note_asked(store, name: str, now: datetime) -> None:
    """Record when it asked, unless that is already recorded within RECORD_EVERY (then
    ships.toml is not written at all, however often it asks)."""
    if _recorded_lately(ca.machine(store.load(), name), now):
        return
    with store.edit() as ed:
        m = ca.machine(store.load(), name)
        if m is None or m.revoked:
            raise _not_trusted(name, "untrusted" if m else "unknown")
        if not _recorded_lately(m, now):
            ed.set_machine_field(name, "last_refreshed", ca.stamp(now))


def _host_names(app, ship: Ship) -> dict[str, str | None]:
    """lan: the first of its other names that resolves on the FleetLedger computer (else
    the first of them); overlay: its overlay name, when it is on the overlay (listed
    first then: overlay.names_with). The machine picks by where it is."""
    overlay = ship.names[0] if ship.overlay_node is not None else None
    others = [n for n in ship.names if n != overlay] or list(ship.names)
    lan = next((n for n in others if app.resolves(n)), others[0])
    return {"lan": lan, "overlay": overlay}


def bundle(app, inv: Inventory, cfg: ca.CAConfig, m: ca.Machine, *, now: datetime,
           cert: tuple[str, ca.CertInfo] | None, renewed: bool,
           not_renewed: dict[str, str] | None) -> dict[str, Any]:
    """The document (contracts/machine-bundle.schema.json)."""
    ships = [s for s in inv.ships() if m.reaches(inv, s)]
    known = ca.known_hosts_text(cfg, ships).splitlines()
    me = appliance.fleet_self(inv)
    return {
        "schema": SCHEMA,
        "machine": m.name,
        "at": ca.stamp(now),
        "certificate": {
            "line": cert[0] if cert else None,
            "valid_before": ca.stamp(cert[1].valid_before) if cert else None,
            "renewed": renewed,
            "not_renewed": not_renewed,
        },
        "scope": list(m.scope),
        "logins": list(m.logins),
        "ssh": {"user": trustcmd.bundle_user(inv, m)},
        "known_hosts": [line for line in known if not line.startswith("#")],
        "fleetledger": {"name": me["name"], "known_hosts": appliance.pinned_line(me)}
        if me and me.get("host_key") else None,
        "ships": [{
            "name": s.name,
            "tags": list(inv.effective_tags(s)),
            "runtime": attach.cached_runtime(s.name),
            "host_names": _host_names(app, s),
            "host_certificate": trustcmd.has_cert(s),
        } for s in ships],
        "idle_limit": {
            "on": m.idle_limit and not _is_this_computer(m.name),
            "days": ca.IDLE_LIMIT.days,
        },
        # For the first-install line alone (refresher/__init__.py); the refresher never
        # reads it, so a refresh applies data only.
        "refresher": refresher.script_b64(),
    }


LOCKED_WHY = ("FleetLedger's vault is locked, so no certificate is signed until it is "
              "unlocked (ship-fleet unlock on the FleetLedger computer).")


# What the caller is told when signing failed, or the bundle could not be made: the
# reason itself (a key's path, a Ship's name from a broken ships.toml) stays on the
# FleetLedger computer, in its audit log.
SIGN_FAILED_WHY = ("FleetLedger's CA could not sign a new certificate; the reason is in "
                   "FleetLedger's audit log, on the FleetLedger computer.")
FAILED = ("FleetLedger could not make this machine's bundle; the reason is in its audit "
          "log, on the FleetLedger computer.")


def serve(app, name: str) -> tuple[dict[str, Any], str | None]:
    """The bundle for machine `name`, and why a due certificate could not be signed
    (for the audit log only); or _Refused. Audits nothing: cmd_ does."""
    store = app.store_factory()
    now = ca.utc_now()
    inv, cfg, m = _trusted(store, name)
    _refuse_if_idle(store, m, now)
    cert = current_cert(cfg, m, now)
    due = cert is None or ca.half_life_passed(m, cfg, cert[1].valid_before, now)
    renewed, not_renewed, detail = False, None, None
    if due and vault.state() == "locked":
        not_renewed = {"code": "vault-locked", "why": LOCKED_WHY}
    elif due:
        try:
            renewed = _renew(app, store, name, now)
        except _Refused:
            raise
        except FleetError as exc:
            detail = exc.message
        except subprocess.TimeoutExpired:
            detail = "ssh-keygen or ssh-add did not finish in time"
        except OSError as exc:
            detail = f"could not run ssh-keygen ({exc.strerror or exc})"
        if detail is not None:
            not_renewed = {"code": "sign-failed", "why": SIGN_FAILED_WHY}
    if not renewed:
        _note_asked(store, name, now)
    inv = store.load()
    cfg = ca.config(inv)
    m = ca.machine(inv, name) if cfg is not None else None
    if m is None or m.revoked:  # untrusted meanwhile
        raise _not_trusted(name, "untrusted")
    cert = current_cert(cfg, m, now)
    return bundle(app, inv, cfg, m, now=now, cert=cert, renewed=renewed,
                  not_renewed=not_renewed), detail


def _outcome(doc: dict[str, Any], detail: str | None) -> str:
    cert = doc["certificate"]
    if cert["renewed"]:
        return "renewed"
    if cert["not_renewed"]:
        said = f"returned, not renewed: {cert['not_renewed']['code']}"
        return f"{said} ({detail[:300]})" if detail else said
    return "returned"


def cmd_machine_bundle(args, app) -> int:
    name = args.machine
    shown = name if trustcmd.MACHINE_RE.fullmatch(name) else "-"
    try:
        doc, detail = serve(app, name)
    except _Refused as exc:
        audit.record("-", ACTION, shown, f"refused: {exc.reason}")
        raise
    except FleetError as exc:  # a broken ships.toml, say: its words stay here
        audit.record("-", ACTION, shown, f"failed: {exc.message[:300]}")
        raise FleetError(FAILED) from None
    except Exception as exc:  # no traceback for the caller either
        audit.record("-", ACTION, shown, f"failed: {type(exc).__name__}")
        raise FleetError(FAILED) from None
    except BaseException:
        audit.record("-", ACTION, shown, "failed")
        raise
    audit.record("-", ACTION, shown, _outcome(doc, detail))
    app.say(json.dumps(doc, indent=2))
    return 0


def add_parsers(sub, add) -> None:
    p = add(sub, "machine-bundle", "one trusted machine's own bundle, as JSON (for its "
            "refresher over ssh)",
            "Print the bundle of the trusted machine NAME as JSON\n"
            "(contracts/machine-bundle.schema.json): its certificate, the CA lines for\n"
            "its known_hosts, the Ships its scope reaches with their host names, tags and\n"
            "runtime, and FleetLedger's own host key; and the refresher script, which\n"
            "only an install you run (trust's line, shipshape-setup) decodes (the refresher\n"
            "applies the rest as data and never reads it).\n\n"
            "A new certificate is signed only past half the current one's life. While the\n"
            "vault is locked it signs nothing and says so. A machine untrusted, unknown,\n"
            f"or that has not asked for {ca.IDLE_LIMIT.days} days (ship-fleet trust NAME\n"
            "--allow-again lets it ask again; --idle-limit off removes the limit) is\n"
            "refused. Every call is audited. It reads nothing from stdin and has no\n"
            "options: it is what a machine's one-command key on the FleetLedger computer\n"
            "runs.")
    p.add_argument("machine", metavar="NAME", help="the trusted machine")
