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
import subprocess
import tempfile
from pathlib import Path

from fleetledger import ca, paths, status
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Inventory, Ship, now_timestamp
from fleetledger.shipctl import ShipCtl, ShipCtlError


def _signer(app) -> ca.Signer:
    return getattr(app, "signer", None) or ca.Signer()


def _ssh(app, inventory):
    return app.ssh_factory(inventory)


# --- ca init --------------------------------------------------------------------
def ca_init(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    if ca.config(inv) is not None and not args.force:
        raise FleetError("FleetLedger already has a certificate authority ([ca] in ships.toml).",
                         hint="`ship-fleet ca show` lists it.")
    made = []
    for path_text, comment, what in (
        (args.host_key, ca.HOST_CA_COMMENT, "host CA"),
        (args.user_key, ca.USER_CA_COMMENT, "user CA"),
    ):
        path = paths.expand(path_text)
        if path.exists():
            app.say(f"Using the existing {what} key {path}.")
            continue
        paths.private_dir(path.parent)
        command = ["ssh-keygen", "-t", "ed25519", "-C", comment, "-f", str(path)]
        if args.no_passphrase:
            command += ["-N", ""]
        else:
            app.note(f"Choose a passphrase for the {what} key (ssh-agent can hold it).")
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise FleetError(f"ssh-keygen could not make the {what} key.") from exc
        made.append(path)
    entry = {"host_key": args.host_key, "user_key": args.user_key}
    with store.edit() as ed:
        ed.set_ca(entry)
    cfg = ca.require(store.load())
    app.say(f"Host CA {ca.fingerprint(cfg.host_public)}")
    app.say(f"User CA {ca.fingerprint(cfg.user_public)}")
    app.say("Next:")
    app.say(f"  ssh-add {args.host_key} {args.user_key}   # sign without retyping passphrases")
    app.say("  ship-fleet ca sign                 # certify every Ship")
    app.say("  ship-fleet trust <machine> --key <its .pub>")
    return 0


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
    cert = ca.sign_host(_signer(app), cfg, ship) if (force or judged.needs_cert) else None
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
    name = args.machine
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
        entry = {"public_key": key, "added": now_timestamp(), "scope": scope}
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
        if args.scope:
            for s in args.scope:
                ca.principal_for(s)
            with store.edit() as ed:
                ed.set_machine_field(name, "scope", args.scope)
            inv = store.load()
    m = ca.machine(inv, name)
    cert, serial, until = ca.sign_user(_signer(app), cfg, m, hours=args.hours)
    with store.edit() as ed:
        ed.set_machine_field(name, "last_serial", serial)
        ed.set_machine_field(name, "last_valid_before", ca.stamp(until))
    ships = [s for s in inv.ships() if m.reaches(inv, s)]
    if args.print_cert:
        app.say(cert)
        return 0
    folder = paths.expand(args.out) if args.out else ca.bundle_dir(name)
    bundle = ca.write_bundle(folder, ca=cfg, machine_name=name, cert=cert, ships=ships,
                             identity=args.identity, valid_before=until)
    app.say(f"Certified {name} for {', '.join(m.scope)} until {ca.stamp(until)} "
            f"({len(ships)} Ship(s): {', '.join(bundle.ships) or 'none yet'}).")
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


def _has_cert(ship: Ship) -> bool:
    doc = status.read_cache(ship.name)
    return bool(doc and (doc.get("ssh") or {}).get("host_certificate"))


def cmd_untrust(args, app) -> int:
    store = app.store_factory()
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
    failed = []
    for ship in inv.ships():
        try:
            for message in certify(app, inv, ship):
                app.say(message)
        except FleetError as exc:
            failed.append(ship.name)
            app.note(f"ship-fleet: {exc.message}")
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
