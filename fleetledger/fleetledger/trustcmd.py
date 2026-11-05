"""ship-fleet ca, trust and untrust: the SSH certificate authority (see ca.py).

- `ca init` makes the host and user CA keys and records them in ships.toml.
- `ca sign` certifies each Ship's pinned host key and installs it, with the user
  CA keys and the revocation list, over the pinned connection; then proves the
  Ship now presents a certificate a CA-only known_hosts accepts. This is the
  migration from pinned keys: the certificate is made from the key FleetLedger
  already trusts, never from one read off the network.
- `trust` certifies one of your machines (its own key) for a scope of Ships and
  writes its bundle: certificate, SSH config, CA line, herdr saved machines.
- `untrust` revokes a machine everywhere.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from fleetledger import ca, paths, status
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Inventory, Ship, now_timestamp
from fleetledger.shipctl import ShipCtl, ShipCtlError


def _signer(app, inventory=None) -> ca.Signer:
    signer = getattr(app, "signer", None)
    if signer is not None:
        return signer
    if inventory is not None:  # managed CA keys go into ssh-agent first (keys.py)
        from fleetledger import keys

        keys.ensure_loaded(inventory)
    return ca.Signer()


def _ssh(app, inventory):
    return app.ssh_factory(inventory)


# --- ca init --------------------------------------------------------------------
def ca_init(args, app) -> int:
    cfg = make_ca(app, app.store_factory(), args.host_key, args.user_key,
                  force=args.force, no_passphrase=args.no_passphrase)
    app.say(f"Host CA {ca.fingerprint(cfg.host_public)}")
    app.say(f"User CA {ca.fingerprint(cfg.user_public)}")
    app.say("Next:")
    app.say(f"  ssh-add {args.host_key} {args.user_key}   # sign without retyping passphrases")
    app.say("  ship-fleet ca sign                 # certify every Ship")
    app.say("  ship-fleet trust <machine> --key <its .pub>")
    return 0


def make_ca(app, store, host_key: str, user_key: str, *, force: bool = False,
            no_passphrase: bool = False, managed: str | None = None) -> ca.CAConfig:
    """The host and user CA keys (each made if missing) and [ca] in ships.toml.
    managed (keyring | file): made with random passphrases FleetLedger keeps."""
    from fleetledger import keys

    inv = store.load()
    made: list[str] = []
    if ca.config(inv) is not None and not force:
        raise FleetError("FleetLedger already has a certificate authority ([ca] in ships.toml).",
                         hint="`ship-fleet ca show` lists it.")
    for path_text, comment, what in (
        (host_key, ca.HOST_CA_COMMENT, "host CA"),
        (user_key, ca.USER_CA_COMMENT, "user CA"),
    ):
        path = paths.expand(path_text)
        if path.exists():
            app.say(f"Using the existing {what} key {path}.")
            continue
        if managed:
            keys.make(path, comment, managed)
            made.append(path_text)
            continue
        paths.private_dir(path.parent)
        command = ["ssh-keygen", "-t", "ed25519", "-C", comment, "-f", str(path)]
        if no_passphrase:
            command += ["-N", ""]
        else:
            app.note(f"Choose a passphrase for the {what} key (ssh-agent can hold it).")
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise FleetError(f"ssh-keygen could not make the {what} key.") from exc
    with store.edit() as ed:
        ed.set_ca({"host_key": host_key, "user_key": user_key})
        if made:
            keys.record(ed, managed, made)
    return ca.require(store.load())


# --- ca sign ----------------------------------------------------------------------
def _ca_only_known_hosts(cfg: ca.CAConfig, ship: Ship, folder: Path) -> Path:
    path = folder / "known_hosts"
    names = ",".join(ca.host_principals(ship))
    path.write_text(f"@cert-authority {names} {cfg.host_public}\n", encoding="utf-8")
    return path


def certify(app, inv: Inventory, ship: Ship, *, force: bool = False,
            view: status.ShipView | None = None) -> list[str]:
    """Bring one Ship's certificates up to date. Returns messages; raises FleetError."""
    cfg = ca.require(inv)
    ssh = _ssh(app, inv)
    view = view or status.fetch(ssh, inv, ship)
    if view.problem:
        raise FleetError(f"{ship.name}: {view.message or view.problem}")
    if view.status is None:
        raise FleetError(f"{ship.name} is still provisioning; certify it once it has finished.")
    krl = ca.build_krl(inv)
    judged = ca.judge(cfg, ship, view.status, krl)
    if not judged.supported:
        raise FleetError(f"{ship.name}'s tooling predates certificates.",
                         hint=f"Update it first: ship-fleet update {ship.name}.")
    if not (force or judged.needs_cert or judged.trust_behind):
        return [f"{ship.name}: up to date."]
    cert = ca.sign_host(_signer(app, inv), cfg, ship) if (force or judged.needs_cert) else None
    doc = ca.trust_document(cfg, krl, cert)
    ctl = ShipCtl(ssh, ship)
    info = ctl.ensure_compatible()
    if info and info.get("contracts", {}).get("ssh-trust") != 1:
        raise FleetError(f"{ship.name}'s ship-ctl has no ssh-trust command.",
                         hint=f"Update it first: ship-fleet update {ship.name}.")
    try:
        ctl.call("ssh-trust", "--file", "-", input=json.dumps(doc).encode("utf-8"), timeout=60)
    except ShipCtlError as exc:
        raise FleetError(exc.message) from exc
    status.fetch(ssh, inv, ship)  # refresh the cached status, so `ca show` is current
    messages = []
    if cert:
        messages.append(f"{ship.name}: host certificate installed "
                        f"({', '.join(ca.host_principals(ship))}; {judged.reason or 'renewed'}).")
        with tempfile.TemporaryDirectory(prefix="fleetledger-verify-") as tmp:
            problem = ssh.probe_certificate(ship, _ca_only_known_hosts(cfg, ship, Path(tmp)))
        if problem:
            raise FleetError(f"{ship.name} took the certificate but a CA-only check failed: "
                             f"{problem}")
        messages.append(f"{ship.name}: checked: it presents the certificate to a client that "
                        "trusts only the CA.")
    else:
        messages.append(f"{ship.name}: user CA keys and revocation list updated.")
    return messages


def ca_sign(args, app) -> int:
    migrate_logins(app.store_factory(), app.say)
    inv = app.store_factory().load()
    ca.require(inv)
    ships = inv.select(args.names, args.tag)
    failed = 0
    for ship in ships:
        try:
            for message in certify(app, inv, ship, force=args.force):
                app.say(message)
        except FleetError as exc:
            failed += 1
            app.note(f"ship-fleet: {exc.message}")
            if exc.hint:
                app.note(f"  {exc.hint}")
    return 1 if failed else 0


# --- ca show -----------------------------------------------------------------------
def ca_show(args, app) -> int:
    inv = app.store_factory().load()
    cfg = ca.require(inv)
    krl = ca.build_krl(inv)
    rows = []
    for ship in inv.ships():
        doc = status.read_cache(ship.name)
        try:
            judged = ca.judge(cfg, ship, doc, krl)
        except ConfigError:
            judged = None
        cert = judged.cert if judged else None
        rows.append({
            "ship": ship.name,
            "certificate_valid_before": cert.get("valid_before") if cert else None,
            "principals": cert.get("principals") if cert else [],
            "needs": (judged.reason if judged and judged.needs_cert else None)
            if judged and judged.supported else "tooling update",
            "trust_behind": bool(judged and judged.trust_behind),
        })
    machines = [
        {"machine": m.name, "scope": list(m.scope), "valid_before": m.last_valid_before,
         "revoked": m.revoked, "fingerprint": ca.fingerprint(m.public_key)}
        for m in ca.machines(inv)
    ]
    if args.json:
        app.say(json.dumps({
            "host_ca": ca.fingerprint(cfg.host_public),
            "user_ca": [ca.fingerprint(k) for k in cfg.user_ca_keys()],
            "ships": rows, "machines": machines,
        }, indent=2))
        return 0
    app.say(f"Host CA {ca.fingerprint(cfg.host_public)}  "
            f"({cfg.host_cert_days} days per certificate)")
    app.say(f"User CA {ca.fingerprint(cfg.user_public)}  ({cfg.user_cert_hours} h by default)")
    app.say("Ships (from the last status poll):")
    for r in rows:
        state = r["needs"] or "ok"
        until = r["certificate_valid_before"] or "-"
        behind = ", trust behind" if r["trust_behind"] else ""
        app.say(f"  {r['ship']:<14} until {until:<21} {state}{behind}")
    app.say("Machines:")
    for m in machines:
        what = f"revoked {m['revoked']}" if m["revoked"] else f"until {m['valid_before'] or '-'}"
        app.say(f"  {m['machine']:<14} {','.join(m['scope']):<20} {what}")
    if not machines:
        app.say("  none (ship-fleet trust <machine> --key <its .pub>)")
    return 0


def cmd_ca(args, app) -> int:
    return {"init": ca_init, "sign": ca_sign, "show": ca_show}[args.action](args, app)


# --- trust and untrust -----------------------------------------------------------------
def _read_key(text_or_path: str) -> str:
    text = text_or_path
    if not text.startswith("ssh-"):
        path = paths.expand(text)
        if path.name.endswith("-cert.pub") or not path.name.endswith(".pub"):
            raise FleetError("Give the machine's public key (a .pub file), never a private key.")
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise FleetError(f"Cannot read {path}.") from exc
    line = text.strip().splitlines()[0] if text.strip() else ""
    if not ca.ED25519_LINE.match(line):
        raise FleetError("The machine key must be an ed25519 public key (ssh-ed25519 AAAA...).")
    return line


def cmd_trust(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    cfg = ca.require(inv)
    migrate_logins(store, app.note if args.print_cert else app.say)
    inv = store.load()
    logins = ca.parse_logins(args.logins) if args.logins else None
    if args.this_computer:
        if args.no_herdr:
            app.herdr = False
        if logins is not None:
            raise FleetError("This computer gets both logins (it is yours); --logins is for "
                             "other machines.")
        return 0 if trust_this_computer(app, store, app.say) else 1
    name = args.machine
    if not name:
        raise FleetError("Which machine? ship-fleet trust NAME --key PATH.pub (or "
                         "--this-computer).")
    existing = ca.machine(inv, name)
    if existing is None:
        if not args.key:
            raise FleetError(f"{name} is new: give its public key with --key PATH.pub.",
                             hint="Make one on that machine: ssh-keygen -t ed25519 "
                             "-f ~/.ssh/shipshape/id_ed25519")
        key = _read_key(args.key)
        if ca._blob(key) in {ca._blob(m.public_key) for m in ca.machines(inv)}:
            raise FleetError("That key is already trusted under another name.")
        if ca._blob(key) in {ca._blob(k) for k in (cfg.host_public, cfg.user_public)}:
            raise FleetError("That is a CA key, not a machine key.")
        scope = args.scope or ["all"]
        for s in scope:
            ca.principal_for(s)
        entry = {"public_key": key, "added": now_timestamp(), "scope": scope,
                 "logins": logins or list(ca.LOGINS)}
        if args.hours:
            entry["cert_hours"] = args.hours
        with store.edit() as ed:
            ed.set_machine(name, entry)
        inv = store.load()
    else:
        if existing.revoked:
            raise FleetError(f"{name} was untrusted on {existing.revoked}; it stays untrusted.",
                             hint="Trust the machine again under a new name with a new key.")
        if args.key and ca._blob(_read_key(args.key)) != ca._blob(existing.public_key):
            raise FleetError(f"{name} is trusted with a different key.",
                             hint=f"`ship-fleet untrust {name}`, then trust the new key under a "
                             "new name.")
        scope = list(dict.fromkeys(args.scope)) if args.scope else None
        if (scope and scope != list(existing.scope)) or (
                logins and logins != list(existing.logins)):
            # With --print-cert, stdout is the certificate alone.
            say = app.note if args.print_cert else app.say
            try:
                inv = _change_scope(app, store, inv, existing, scope or list(existing.scope),
                                    say=say, logins=logins)
            except PushFailed:
                inv = store.load()  # recorded; said above; the certificate follows
    m = ca.machine(inv, name)
    cert, until = _sign_machine(app, store, inv, m, hours=args.hours)
    ships = [s for s in inv.ships() if m.reaches(inv, s)]
    if args.print_cert:
        app.say(cert)
        return 0
    folder = paths.expand(args.out) if args.out else ca.bundle_dir(name)
    bundle = ca.write_bundle(folder, ca=cfg, machine_name=name, cert=cert, ships=ships,
                             identity=args.identity, valid_before=until,
                             hostnames=_hostnames(app, ships), user=bundle_user(inv, m))
    app.say(f"Certified {name} for {', '.join(m.scope)} until {ca.stamp(until)}, to log in "
            f"as {ca.logins_said(m.logins)} ({len(ships)} Ship(s): "
            f"{', '.join(bundle.ships) or 'none yet'}).")
    app.say(f"Bundle: {bundle.folder}")
    unsigned = [s.name for s in ships if not _has_cert(s)]
    if unsigned:
        app.say(f"Not certified yet: {', '.join(unsigned)}; run `ship-fleet ca sign` first.")
    if args.install:
        script = bundle.folder / "install.sh"
        argv = ["sh", str(script)] + (["--no-herdr"] if args.no_herdr else [])
        proc = subprocess.run(argv, cwd=bundle.folder)
        return proc.returncode
    app.say("On that machine, copy the folder over and run:  sh install.sh")
    app.say(f"Renew before it lapses: ship-fleet trust {name} --print-cert "
            f"> ~/.ssh/shipshape/{name}-cert.pub (on that machine)")
    return 0


LOGINS_EXPLAINED = (
    "Your own login on Ships now takes certificates of its own (owner:*), apart from "
    "the agent's (agent:*), so a machine trusted only to attach to the agent cannot log "
    "in as you. Machines trusted before keep what they had, both: {names}. Their next "
    "certificate carries both; until they renew it, a Ship already updated lets their "
    "current one log in as the agent only. A Ship not updated yet still lets any agent "
    "certificate log in as you: update every Ship (ship-fleet update) before relying on "
    "ship-fleet trust NAME --logins agent, which lets a machine only attach to the agent.")


def migrate_logins(store, say) -> list[str]:
    """Machines trusted before logins were recorded: record both (what their
    certificates opened then), and say so, once: afterwards every machine has it.

    Their certificates still out there name agent:<scope> only, but on the Ships of
    their day that opened your login too. So each is recorded as having held
    owner:<scope> as well: narrowing the machine to the agent later revokes them."""
    inv = store.load()
    machines = [m for m in ca.machines(inv) if not m.logins_recorded and not m.revoked]
    if not machines:
        return []
    with store.edit() as ed:
        for m in machines:
            ed.set_machine_field(m.name, "logins", list(ca.LOGINS))
            if m.issued:
                ed.set_machine_field(m.name, "issued", [_as_both(i) for i in m.issued])
    say(LOGINS_EXPLAINED.format(names=", ".join(m.name for m in machines)))
    return [m.name for m in machines]


def _as_both(entry: dict) -> dict:
    """An issued certificate from before: what it opened, agent:<s> and owner:<s>."""
    principals = list(entry.get("principals", []))
    owner = [f"owner:{p[len('agent:'):]}" for p in principals if p.startswith("agent:")]
    return {**entry, "principals": list(dict.fromkeys([*principals, *owner]))}


def set_logins(app, store, name: str, logins: list[str], say) -> None:
    """What a trusted machine may log in as; narrowing revokes its wider certificates
    on every Ship at once. This computer's bundle is rewritten to match."""
    migrate_logins(store, say)
    inv = store.load()
    m = ca.machine(inv, name)
    if m is None or m.revoked:
        raise FleetError(f"No trusted machine called {name}.")
    if list(m.logins) == list(logins):
        return
    failed = None
    try:
        _change_scope(app, store, inv, m, list(m.scope), say=say, logins=list(logins))
    except PushFailed as exc:
        failed = exc
    here = this_computer()
    if here and here["machine"] == name:
        trust_this_computer(app, store, say, create=False)
    if failed is not None:
        raise failed  # recorded, but not every Ship has the new revocation list


def bundle_user(inv: Inventory, m: ca.Machine) -> str:
    """The login a machine's SSH config uses: the agent's when it may, else yours."""
    return "agent" if "agent" in m.logins else inv.owner


def _sign_machine(app, store, inv: Inventory, m: ca.Machine, *, hours=None):
    """A new user certificate for a trusted machine, recorded. (certificate, until)"""
    cfg = ca.require(inv)
    cert, serial, until = ca.sign_user(_signer(app, inv), cfg, m, hours=hours)
    with store.edit() as ed:
        ed.set_machine_field(m.name, "last_serial", serial)
        ed.set_machine_field(m.name, "last_valid_before", ca.stamp(until))
        ed.set_machine_field(m.name, "issued", ca.issued_record(m, cfg, serial, until))
    return cert, until


def _hostnames(app, ships) -> dict[str, str]:
    """Each Ship's HostName in a bundle: the first of its names that resolves here."""
    return {s.name: ca.dial_name(s, app.resolves) for s in ships}


# --- this computer ------------------------------------------------------------------
# The computer FleetLedger runs on is trusted like any other machine, with your own
# key, so `ssh <ship>` and `herdr --remote <ship>` work here as soon as there is a
# Ship. Which machine entry is this computer is kept beside ships.toml (it is a fact
# about this computer, not the fleet).
THIS_COMPUTER = "this-computer.json"
MACHINE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def this_computer() -> dict | None:
    """{"machine": name, "identity": path} once this computer is trusted; else None."""
    try:
        doc = json.loads((paths.config_dir() / THIS_COMPUTER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not MACHINE_RE.match(str(doc.get("machine", ""))):
        return None
    return doc


def attaches_here(inv: Inventory, ship: Ship) -> bool:
    """Can plain ssh / herdr --remote reach this Ship from here (this computer is
    trusted, its trust reaches the Ship, and the Ship has a host certificate)?"""
    here = this_computer()
    m = ca.machine(inv, here["machine"]) if here else None
    return bool(m and not m.revoked and "agent" in m.logins and m.reaches(inv, ship)
                and _has_cert(ship))


def _local_name(inv: Inventory) -> str:
    import socket

    base = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().split(".")[0].lower()).strip("-")
    base = base[:24] or "this-computer"
    if not MACHINE_RE.match(base):
        base = "this-computer"
    taken = {m.name for m in ca.machines(inv)}
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def _your_private_key(inv: Inventory) -> Path | None:
    """The private half, on this computer, of one of your keys: one you can open
    yourself first (plain ssh needs that), else one FleetLedger made and looks after."""
    from fleetledger import keys

    managed = set(keys.managed(inv)[1])
    found = [pub.with_name(pub.name[:-4]) for pub in inv.agent_key_paths
             if pub.name.endswith(".pub") and pub.with_name(pub.name[:-4]).is_file()]
    return next((k for k in found if k not in managed), found[0] if found else None)


def _plain_ssh(inv: Inventory, key_path: Path) -> tuple[bool, str | None]:
    """Can plain ssh open your key here? A key you look after: yes. One FleetLedger made
    (its passphrase only in your keyring): only while your own ssh-agent holds it, so
    FleetLedger loads it there, if you have one running. (works, what to say)"""
    from fleetledger import keys

    if key_path not in keys.managed(inv)[1]:
        return True, None
    if keys.users_agent_answers():
        keys.ensure_loaded(inv, [key_path])
        return True, ("Your key is one FleetLedger made: plain ssh and herdr use it from your "
                      "ssh-agent, which holds it until you log out; FleetLedger loads it "
                      "there again whenever it runs.")
    return False, ("Plain ssh cannot open your key here: FleetLedger made it, its passphrase "
                   "is only in your keyring, and no ssh-agent of yours is running. Attach "
                   "with ship-fleet ssh, or start ssh-agent and run ship-fleet trust "
                   "--this-computer again.")


RENEW_WITHIN_S = 24 * 3600


def renew_this_computer_if_due(app, store) -> bool:
    """Renew this computer's certificate (and its bundle) in its last day, so plain ssh
    keeps working: called from the web UI's poll and ship-fleet status."""
    here = this_computer()
    if here is None:
        return False
    inv = store.load()
    m = ca.machine(inv, here["machine"]) if ca.config(inv) else None
    if m is None or m.revoked:
        return False
    until = ca.parse_stamp(m.last_valid_before)
    if until is not None and (until - ca.utc_now()).total_seconds() > RENEW_WITHIN_S:
        return False
    return trust_this_computer(app, store, lambda line: None, create=False)


def this_computer_until(inv: Inventory) -> str | None:
    """When this computer's certificate lapses (for the Ship page), if it is trusted."""
    here = this_computer()
    m = ca.machine(inv, here["machine"]) if here and ca.config(inv) else None
    return m.last_valid_before if m and not m.revoked else None


def trust_this_computer(app, store, say, *, create: bool = True) -> bool:
    """Trust this computer (your own key, every Ship) and install its bundle here: an
    SSH config entry per Ship (HostName: the first of its names that resolves here),
    its certificate and the CA line, and herdr saved machines. With create=False, only
    refresh it if it is trusted already (after each enrolment). Returns whether this
    computer can now attach."""
    inv = store.load()
    cfg = ca.config(inv)
    if cfg is None:
        if create:
            say("This computer is not set up for plain ssh / herdr --remote: the fleet has "
                "no certificate authority yet (ship-fleet ca init).")
        return False
    here = this_computer()
    m = ca.machine(inv, here["machine"]) if here else None
    if m is None or m.revoked:
        if not create:
            return False
        key_path = _your_private_key(inv)
        if key_path is None:
            say("This computer is not set up for plain ssh / herdr --remote: your key's "
                "private half is not here. Attach with ship-fleet ssh.")
            return False
        from fleetledger import keys

        key = keys.public_line(key_path)
        m = next((x for x in ca.machines(inv)
                  if not x.revoked and ca._blob(x.public_key) == ca._blob(key)), None)
        if m is None:
            name = _local_name(inv)
            with store.edit() as ed:
                ed.set_machine(name, {"public_key": key, "added": now_timestamp(),
                                      "scope": ["all"], "logins": list(ca.LOGINS)})
            inv = store.load()
            m = ca.machine(inv, name)
        here = {"machine": m.name, "identity": _tilde(key_path)}
        paths.write_private(paths.config_dir() / THIS_COMPUTER,
                            (json.dumps(here, indent=2) + "\n").encode("utf-8"))
    ships = [s for s in inv.ships() if m.reaches(inv, s)]
    for ship in ships:
        if not _has_cert(ship):
            try:
                certify(app, inv, ship)
            except FleetError:
                pass  # said below: plain ssh needs it
    cert, until = _sign_machine(app, store, inv, m)
    inv = store.load()
    ships = [s for s in inv.ships() if m.reaches(inv, s)]
    bundle = ca.write_bundle(ca.bundle_dir(m.name), ca=cfg, machine_name=m.name, cert=cert,
                             ships=ships, identity=here["identity"], valid_before=until,
                             hostnames=_hostnames(app, ships), user=bundle_user(inv, m))
    argv = ["sh", str(bundle.folder / "install.sh")]
    if not getattr(app, "herdr", True):
        argv.append("--no-herdr")
    proc = subprocess.run(argv, cwd=bundle.folder, capture_output=True, text=True,
                          timeout=120, check=False)
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout or "").strip().splitlines()
        say("Could not set this computer up for plain ssh" + (f": {why[-1]}" if why else "."))
        return False
    works, note = _plain_ssh(inv, paths.expand(here["identity"]))
    if note:
        say(note)
    ready = [s.name for s in ships if _has_cert(s)] if works else []
    if ready:
        say(f"This computer ({m.name}) can attach: ssh {ready[0]}, herdr --remote "
            f"{ready[0]} (certificate until {ca.stamp(until)}; FleetLedger renews it in its "
            "last day whenever it runs: the web UI, or ship-fleet status).")
    unsigned = [s.name for s in ships if not _has_cert(s)]
    if unsigned:
        say(f"Not certified yet: {', '.join(unsigned)}; run `ship-fleet ca sign`.")
    return bool(ready)


def _tilde(path: Path) -> str:
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _change_scope(app, store, inv: Inventory, m: ca.Machine, scope: list[str], *,
                  say, logins: list[str] | None = None) -> Inventory:
    """Set a new scope (and logins); certificates wider than them go into the
    revocation list.

    A certificate names its principals, so after narrowing, the old one would
    still open the Ships the machine no longer reaches, or the login it may no
    longer use, until it lapsed. Its serial is revoked instead, and the list is
    pushed to every Ship now.
    """
    for s in scope:
        ca.principal_for(s)
    cfg = ca.require(inv)
    issued = ca.unlapsed(m.issued)
    if not issued and m.last_serial and m.last_valid_before:
        # Issued before certificates were recorded: the last one had the old scope.
        issued = ca.unlapsed([{"serial": m.last_serial, "valid_before": m.last_valid_before,
                               "principals": m.principals,
                               "ca": ca.fingerprint(cfg.user_public)}])
    before = ca.revoked_serials(inv)
    with store.edit() as ed:
        ed.set_machine_field(m.name, "scope", scope)
        if logins is not None:
            ed.set_machine_field(m.name, "logins", logins)
        ed.set_machine_field(m.name, "issued", issued or None)
    inv = store.load()
    now = ca.machine(inv, m.name)
    outlived = now.outlived()
    if logins is not None and logins != list(m.logins):
        say(f"{m.name} may now log in as {ca.logins_said(now.logins)}.")
    if outlived:
        say(f"{m.name} now has scope {', '.join(scope)} and logs in as "
            f"{ca.logins_said(now.logins)}; {len(outlived)} certificate(s) issued for more "
            "are now in the revocation list.")
    if ca.revoked_serials(inv) != before:
        failed = _push_to_ships(app, inv, say)
        if failed:
            say(f"NOT YET REVOKED on {', '.join(failed)} (not reached). Until they are "
                f"updated, {m.name}'s older certificates still work there for what it had. "
                "Run `ship-fleet ca sign` when they are back.")
            raise PushFailed(failed)
    return inv


class PushFailed(FleetError):
    """The change is recorded, but some Ships did not get the new revocation list."""

    def __init__(self, ships: list[str]):
        super().__init__(f"Not yet revoked on {', '.join(ships)}.")
        self.ships = ships


def _push_to_ships(app, inv: Inventory, say=None) -> list[str]:
    """Send every Ship the current trust document; returns those not updated."""
    say = say or app.say
    failed = []
    for ship in inv.ships():
        try:
            for message in certify(app, inv, ship):
                say(message)
        except FleetError as exc:
            failed.append(ship.name)
            app.note(f"ship-fleet: {exc.message}")
    return failed


def _has_cert(ship: Ship) -> bool:
    doc = status.read_cache(ship.name)
    return bool(doc and (doc.get("ssh") or {}).get("host_certificate"))


def cmd_untrust(args, app) -> int:
    store = app.store_factory()
    migrate_logins(store, app.say)
    inv = store.load()
    ca.require(inv)
    m = ca.machine(inv, args.machine)
    if m is None:
        raise FleetError(f"No machine named '{args.machine}'.")
    if not m.revoked:
        with store.edit() as ed:
            ed.set_machine_field(m.name, "revoked", now_timestamp())
        inv = store.load()
        app.say(f"{m.name} is untrusted; its key is now in the revocation list.")
    failed = _push_to_ships(app, inv)
    if failed:
        app.note(f"Not updated yet: {', '.join(failed)}. Until they are, {m.name}'s current "
                 f"certificate still works there (until {m.last_valid_before or 'it expires'}). "
                 "Run `ship-fleet ca sign` when they are back.")
        return 1
    return 0


def after_enrol(app, store, name: str, say) -> None:
    """Certify a newly enrolled Ship when FleetLedger has a CA (best effort)."""
    inv = store.load()
    if ca.config(inv) is None:
        return
    try:
        for message in certify(app, inv, inv.ship(name)):
            say(message)
    except FleetError as exc:
        say(f"Certificates not installed yet ({exc.message}); run `ship-fleet ca sign {name}`.")
    try:  # this computer, if trusted: its bundle now lists the new Ship
        trust_this_computer(app, store, say, create=False)
    except FleetError as exc:
        say(f"This computer's SSH config was not refreshed ({exc.message}); run "
            "`ship-fleet trust --this-computer`.")
