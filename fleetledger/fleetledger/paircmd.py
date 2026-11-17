"""ship-fleet pair: turn pairing by code on and off, and see, approve or refuse the computers
that ask (pairing.py has the way in, and why it is safe to leave open).

    ship-fleet pair on | off        let unknown computers ask (off by default)
    ship-fleet pair ls              the requests waiting: the name each gives itself (a
                                    claim), its key's fingerprint, where it asked from, when
                                    (as data: ca show --json's pairing)
    ship-fleet pair approve [ID]    type the code the computer shows; choose its name, scope
                                    and logins; it ends in ship-fleet trust NAME --key ...
    ship-fleet pair refuse ID | --all

Each runs on FleetLedger, in a shell of yours: the requests are never shown to the computers
that asked, nor to a trusted machine's bundle-only key. The Fleet panel's Machines page opens
approve and refuse in a terminal tab, and never answers for you: the code is typed there.
A phone is out of scope: it runs no installer, so it never asks (the phone's browser pairs
with the web UI instead: ship-fleet ui link).
"""

from __future__ import annotations

from fleetledger import authkeys, ca, pairing, prompts, sshkeys, trustcmd
from fleetledger.concierge import audit
from fleetledger.errors import FleetError
from fleetledger.pairing import cmd_machine_pair  # noqa: F401  (cli finds it here)

PHONE = ("A phone is out of scope: it runs no installer, so it cannot ask (pair a phone's "
         "browser with the web UI instead: ship-fleet ui link).")


def _here(store, inv) -> None:
    if not authkeys.adds_here(store, inv):
        raise FleetError("Pairing by code works on FleetLedger's own VM, as the copy that acts: "
                         "that is where computers set themselves up from.",
                         hint="Trust a computer by hand instead: ship-fleet trust NAME --key "
                              "'ssh-ed25519 ...' (its ~/.ssh/shipshape/id_ed25519.pub).")


def _set(store, on: bool) -> None:
    with store.edit() as ed:
        if "ca" not in ed.doc:
            raise FleetError("No [ca] in the inventory; run `ship-fleet ca init`.")
        if on:
            ed.doc["ca"]["pairing"] = True
        elif "pairing" in ed.doc["ca"]:
            del ed.doc["ca"]["pairing"]


def _sync(app, store) -> authkeys.Synced:
    done = authkeys.sync(store)
    for line in authkeys.synced_said(done):
        app.say(line)
    return done


def pair_on(args, app, store) -> int:
    inv = store.load()
    ca.require(inv)
    _here(store, inv)
    f = authkeys.read()
    if pairing.BLOB in f.outside:
        raise FleetError(f"{authkeys.where_said()} has a line of its own for the pairing key "
                         f"(the well-known one, {pairing.COMMENT}): anybody may use that key, "
                         "so that line lets anybody in with whatever it allows.",
                         hint="Delete that line, then ship-fleet pair on again.")
    _set(store, True)
    try:
        done = _sync(app, store)
    except FleetError:
        _set(store, False)  # no door half-open: on only with its line in place
        raise
    audit.record("-", "pair-on", "-", "ok")
    state = done.review.pairing_state
    if state != "installed":
        raise FleetError(f"Pairing is on, but its line is not in {authkeys.where_said()} "
                         f"({state}).", hint=f"{authkeys.SYNC}; ship-fleet doctor says more.")
    app.say("Pairing by code is on. On a computer FleetLedger does not know, run "
            "shipshape-setup --herdr-client --fleetledger USER@THIS-VM (from a ShipShape "
            "checkout): it shows a code. Approve it here with ship-fleet pair approve (or the "
            "Fleet panel's Machines page), typing that code.")
    app.say(f"Until you approve one, a request grants nothing; at most {pairing.MAX_PENDING} "
            f"wait at once, each for {int(pairing.TTL.total_seconds() // 60)} minutes. "
            "ship-fleet pair off closes it again.")
    app.say(PHONE)
    return 0


def pair_off(args, app, store) -> int:
    inv = store.load()
    ca.require(inv)
    _set(store, False)
    pairing.clear()
    _sync(app, store)
    audit.record("-", "pair-off", "-", "ok")
    app.say("Pairing by code is off: no computer can ask, and the requests waiting are gone.")
    return 0


def _ago(stamp: str, now) -> str:
    t = ca.parse_stamp(stamp)
    s = int(abs((now - t).total_seconds())) if t else 0
    return f"{s // 60} min {s % 60:02d} s"


def pair_ls(args, app, store) -> int:
    inv = store.load()
    on = pairing.is_on(inv)
    rows = pairing.listing() if on else []
    if not on:
        app.say("Pairing by code is off (ship-fleet pair on to let a new computer ask).")
        return 0
    if not rows:
        app.say("Pairing by code is on; no computer is waiting.")
        return 0
    now = ca.utc_now()
    app.say("Waiting to be approved (each name is the computer's own claim, unchecked):")
    for r in rows:
        app.say(f"  {r['id']}  calls itself {r['host']!r}, key {r['fingerprint']}, from "
                f"{r['source'] or 'an unknown address'}, asked {_ago(r['asked'], now)} ago, "
                f"lapses in {_ago(r['expires'], now)}")
    app.say("Approve one: ship-fleet pair approve ID (you type the code that computer shows).")
    return 0


def _find(args, app, now) -> pairing.Request:
    """The request to approve: the one named, or, with no ID, the one whose code is typed."""
    p = pairing.read()
    if args.id:
        r = p.by_id(args.id, now)
        if r is None:
            raise FleetError(f"No pairing request {args.id} is waiting (approved, refused, "
                             "withdrawn or lapsed).", hint="ship-fleet pair ls")
        return r
    waiting = p.pending(now)
    if not waiting:
        raise FleetError("No computer is waiting to be approved.", hint="ship-fleet pair ls")
    if len(waiting) == 1:
        return waiting[0]
    code = _code(args, app)
    found = [r for r in waiting if r.code() == code]
    if len(found) != 1:
        raise FleetError("That code is not one waiting request's: nothing approved." if not found
                         else "Two requests have that code, so neither is approved: refuse both "
                         "(ship-fleet pair refuse --all) and ask again.")
    args.code = code
    return found[0]


def _code(args, app) -> str:
    if args.code is not None:
        return pairing.typed_code(args.code)
    if not app.stdin_isatty():
        raise FleetError("Approving needs the code the new computer shows: run this in a "
                         "terminal, or give it with --code.")
    return pairing.typed_code(app.confirm("Type the code the new computer shows: "))


def _defaults(args, app, inv, r: pairing.Request) -> tuple[str, list[str], str]:
    taken = {m.name for m in ca.machines(inv)}
    claimed = r.host if r.host and r.host not in taken else ""
    name, scopes, logins = args.name, args.scope, args.logins
    if name is None:
        if not app.stdin_isatty():
            if not claimed:
                raise FleetError(f"Give it a name with --name ({r.host!r} is taken).")
            name = claimed
        else:
            while True:
                name = prompts.ask(app, f"Name for it [{claimed or 'none: type one'}]: ", claimed)
                if trustcmd.MACHINE_RE.fullmatch(name) and name not in taken:
                    break
                app.note("A name of a-z, 0-9 and -, up to 31, that no machine here has.")
    if scopes is None:
        if app.stdin_isatty():
            typed = prompts.ask(app, "What may it reach: all, tag:TAG or ship:NAME (several "
                                "with spaces) [all]: ", "all")
            scopes = typed.replace(",", " ").split()
        else:
            scopes = ["all"]
    if logins is None:
        logins = prompts.choose(app, "What may it log in to Ships as", ("agent", "owner", "both"),
                                "both") if app.stdin_isatty() else "both"
    return name, scopes, logins


def pair_approve(args, app, store) -> int:
    inv = store.load()
    ca.require(inv)
    _here(store, inv)
    if not pairing.is_on(inv):
        raise FleetError("Pairing by code is off, so no computer is waiting.",
                         hint="ship-fleet pair on")
    now = ca.utc_now()
    r = _find(args, app, now)
    me = (inv.doc.get("fleet") or {}).get("self") or {}
    host_fp = sshkeys.fingerprint(me["host_key"]) if me.get("host_key") else "?"
    app.say(f"Pairing request {r.id}: a computer that calls itself {r.host!r} (its own claim, "
            "unchecked)")
    app.say(f"  its key:          ED25519 {sshkeys.fingerprint(r.key or '')}")
    app.say(f"  asked from:       {r.source or 'an unknown address'}, {_ago(r.asked, now)} "
            f"ago (it lapses in {_ago(r.expires, now)})")
    app.say(f"  FleetLedger's host key, which that computer pinned: ED25519 {host_fp}")
    app.say("On that computer the installer shows a code, its key and FleetLedger's host key: "
            "the keys must be these two.")
    code = _code(args, app)
    if not secrets_equal(code, r.code()):
        audit.record("-", "pair-approve", r.id, "refused: wrong code")
        raise FleetError("That is not this request's code: nothing approved.",
                         hint="Type the code the new computer shows; if it shows none, or "
                              "another, refuse this one: ship-fleet pair refuse " + r.id)
    app.say(f"Code {pairing.code_said(code)}: the one this request's key gives.")
    name, scopes, logins = _defaults(args, app, inv, r)
    # Each checked before it is a word on trust's command line (a "--install" is no scope).
    if not trustcmd.MACHINE_RE.fullmatch(name):
        raise FleetError("A machine's name is a-z, 0-9 and -, up to 31 characters (laptop, say).")
    if not scopes or not all(ca.SCOPE.fullmatch(x) for x in scopes) or (
            "all" in scopes and len(scopes) > 1):
        raise FleetError("What it may reach is all (alone), tag:TAG or ship:NAME.")
    argv = ["trust", name, "--key", r.key or ""]
    for s in scopes:
        argv += ["--scope", s]
    argv += ["--logins", logins]
    shown = (f"ship-fleet trust {name} --key '{r.key}' "
             + " ".join(f"--scope {s}" for s in scopes) + f" --logins {logins}")
    app.say(f"Approving runs: {shown}")
    if not args.yes:
        if not app.stdin_isatty():
            raise FleetError("Add --yes to approve as shown.")
        if prompts.ask(app, "Trust it? [y/N] ").lower() not in ("y", "yes"):
            raise FleetError("Not approved; the request still waits (it lapses by itself).")
    from fleetledger import cli

    targs = cli.build_parser().parse_args(argv)
    targs.paired = True  # trust says no line to paste: the installer there finishes itself
    # Still waiting, and the same request (not lapsed or withdrawn while you typed)?
    still = pairing.read().by_id(r.id, ca.utc_now())
    if still is None or still.key != r.key:
        raise FleetError(f"Request {r.id} lapsed or was withdrawn meanwhile: nothing approved.")
    try:
        rc = trustcmd.cmd_trust(targs, app)
    except FleetError as exc:
        # A rule of trust's refused it before anything was recorded: it still waits. Past
        # that point (the machine recorded with this key, then signing or the bundle
        # failed), it is trusted, so the request says so rather than "pending".
        m = ca.machine(store.load(), name)
        if m is None or m.revoked or sshkeys.blob(m.public_key) != r.blob():
            raise
        _approved(r, name)
        audit.record("-", "pair-approve", r.id, f"approved: {name} (trust failed after)")
        raise FleetError(f"{exc.message} {name} is trusted all the same (its key may fetch its "
                         "own bundle here, which signs its certificate once this is fixed).",
                         hint=exc.hint) from None
    _approved(r, name)
    audit.record("-", "pair-approve", r.id, f"approved: {name}")
    app.say(f"Approved: {name}. Its installer, waiting there, finishes by itself within "
            "seconds (its key may fetch its own bundle here and nothing else)."
            + ("" if rc == 0 else " Trust did not finish everything, though: see above."))
    return rc


def _approved(r: pairing.Request, name: str) -> None:
    """The request, approved: kept (no key, claim or address) for TTL from now, so a computer
    approved in its last seconds still hears of it at its next ask."""
    now = ca.utc_now()
    with pairing.locked() as p:
        for x in p.requests:
            if x.id == r.id and x.state == "pending":
                x.state, x.machine = "approved", name
                x.key = x.host = x.source = x.nonce = None
                x.expires = ca.stamp(now + pairing.TTL)


def secrets_equal(a: str, b: str) -> bool:
    import secrets

    return secrets.compare_digest(a.encode("ascii", "replace"), b.encode("ascii"))


def pair_refuse(args, app, store) -> int:
    now = ca.utc_now()
    if not args.all and not args.id:
        raise FleetError("Which request? ship-fleet pair refuse ID (or --all).",
                         hint="ship-fleet pair ls")
    refused = []
    with pairing.locked() as p:
        for x in p.pending(now):
            if args.all or x.id == args.id:
                x.state = "refused"
                x.key = x.host = x.source = x.nonce = None
                refused.append(x.id)
    if not refused:
        raise FleetError(f"No pairing request {args.id} is waiting.", hint="ship-fleet pair ls")
    for rid in refused:
        audit.record("-", "pair-refuse", rid, "ok")
    app.say(f"Refused: {', '.join(refused)}. Nothing of it is kept but the word refused, for "
            "the computer waiting to say so, until it would have lapsed.")
    return 0


def cmd_pair(args, app) -> int:
    store = app.store_factory()
    return {"on": pair_on, "off": pair_off, "ls": pair_ls, "approve": pair_approve,
            "refuse": pair_refuse}[args.action](args, app, store)


def add_parsers(sub, add) -> None:
    p = add(sub, "pair", "pairing by code: let a new computer ask to be trusted, and approve it",
            "Pairing by code, for a computer with no key that logs in here: it runs\n"
            "shipshape-setup --herdr-client, which asks FleetLedger to trust it and shows a\n"
            "code; you approve it here by typing that code.\n\n"
            "  ship-fleet pair on | off        let unknown computers ask (off by default)\n"
            "  ship-fleet pair ls              the requests waiting\n"
            "  ship-fleet pair approve [ID]    type its code; choose name, scope, logins\n"
            "  ship-fleet pair refuse ID       (or --all)\n\n"
            "A request grants nothing until you approve it; approving runs ship-fleet\n"
            "trust NAME --key ... with all of trust's rules. " + PHONE)
    p.add_argument("action", choices=("on", "off", "ls", "approve", "refuse"))
    p.add_argument("id", nargs="?", metavar="ID", help="approve, refuse: the request (pair ls)")
    p.add_argument("--code", help="approve: the code the new computer shows (else asked)")
    p.add_argument("--name", help="approve: the name to trust it as (default: the one it gives)")
    p.add_argument("--scope", action="append", metavar="all|tag:TAG|ship:NAME",
                   help="approve: what it may reach (repeatable; default all)")
    p.add_argument("--logins", choices=("agent", "owner", "both"),
                   help="approve: what it logs in to Ships as (default both)")
    p.add_argument("--yes", action="store_true", help="approve: without asking once more")
    p.add_argument("--all", action="store_true", help="refuse: every request waiting")
    add(sub, pairing.VERB, "what an unknown computer's pairing request runs (a forced command)",
        "What the well-known pairing key's line in ~/.ssh/authorized_keys runs, while\n"
        "pairing is on: it reads SSH_ORIGINAL_COMMAND (request, status or cancel) and\n"
        "nothing else, and answers one JSON document (contracts/machine-pair.schema.json).\n"
        "You never run it yourself: see ship-fleet pair.")
