"""ship-fleet overlay (phase 5): join Ships to the overlay network, leave it, show it.

See overlay.py for how keys are handled and docs/runbooks/remote-access.md for
the whole setup, including your laptop.
"""

from __future__ import annotations

from fleetledger import overlay
from fleetledger.commands import table
from fleetledger.errors import FleetError
from fleetledger.shipctl import ShipCtl


def add_parsers(sub, add) -> None:
    p = add(sub, "overlay", "join Ships to the overlay network (access off the LAN)",
            "  ship-fleet overlay setup           guided, once: provider, key source, checks\n"
            "  ship-fleet overlay check [--policy FILE] [--probe]\n"
            "  ship-fleet overlay join ship-01 [ship-02 ...] | --tag work\n"
            "  ship-fleet overlay status [ship-01 ...]\n"
            "  ship-fleet overlay leave ship-01\n"
            "  ship-fleet overlay policy          access rules to paste into the admin console\n\n"
            "Each join uses a one-off auth key, pasted (not shown) or minted through the\n"
            "Tailscale API ([overlay] key_source). Keys never go into seeds or logs.\n"
            "See docs/runbooks/remote-access.md.")
    p.add_argument("action", choices=("setup", "check", "join", "leave", "status", "policy"))
    p.add_argument("names", nargs="*", metavar="SHIP")
    p.add_argument("--tag", help="every Ship with this tag")
    p.add_argument("--force", action="store_true", help="setup: replace an existing [overlay]")
    p.add_argument("--policy", metavar="FILE",
                   help="check: a policy file exported from the admin console")
    p.add_argument("--probe", action="store_true",
                   help="check: from each Ship, try to open a connection to this computer "
                   "and to another Ship (both must fail)")


def cmd_overlay(args, app) -> int:
    from fleetledger import overlay_setup

    if args.action == "setup":
        return overlay_setup.setup(args, app)
    if args.action == "check":
        return overlay_setup.check(args, app)
    store = app.store_factory()
    inv = store.load()
    config = overlay.settings(inv)
    if args.action == "policy":
        app.say(overlay.policy(config).rstrip())
        return 0
    if args.action == "status":
        ships = inv.select(args.names, args.tag) if (args.names or args.tag) else inv.ships()
        rows = []
        for ship in ships:
            try:
                doc = overlay.status(ShipCtl(app.ssh_factory(inv), ship))
            except FleetError as exc:
                rows.append([ship.name, "?", "-", "-", exc.message[:60]])
                continue
            if not doc:
                rows.append([ship.name, "not joined", "-", "-", ""])
                continue
            rows.append([ship.name, doc["state"] if doc["joined"] else "not joined",
                         doc["name"] or "-", doc["ssh_listen"], ", ".join(doc["addresses"])])
        app.say(table(rows, ["SHIP", "OVERLAY", "NAME", "SSH", "ADDRESSES"]) if rows
                else "No Ships.")
        return 0
    if not args.names and not args.tag:
        raise FleetError(f"overlay {args.action}: which Ship? Give names or --tag.")
    ships = inv.select(args.names, args.tag)
    failed = 0
    if args.action == "join":
        keys = _keys(app, config)
        for ship in ships:
            try:
                join_one(app, store, ship.name, keys, app.say)
            except FleetError as exc:
                app.say(f"{ship.name}: not joined: {exc.message}")
                failed += 1
        if failed < len(ships):  # this computer's SSH config now prefers the overlay names
            from fleetledger import trustcmd

            try:
                trustcmd.trust_this_computer(app, store, app.say, create=False)
            except FleetError as exc:
                app.say(f"This computer's SSH config was not refreshed ({exc.message}).")
        if not failed:
            app.say("Only SSH with the pinned host key is allowed in, over the overlay as on "
                    "the LAN. To refuse LAN logins: ship-fleet set SHIP ssh_listen=overlay")
        return 1 if failed else 0
    for ship in ships:  # leave
        ctl = ShipCtl(app.ssh_factory(inv), ship)
        try:
            before = overlay.status(ctl)
            messages = overlay.leave(ctl)
        except FleetError as exc:
            app.say(f"{ship.name}: {exc.message}")
            failed += 1
            continue
        with store.edit() as ed:
            if before and before.get("name"):
                ed.set_field(ship.name, "names",
                             overlay.names_without(store.load().ship(ship.name), before["name"]))
            ed.set_field(ship.name, "overlay_node", None)
        for message in messages:
            app.say(f"{ship.name}: {message}")
    return 1 if failed else 0


def _keys(app, config):
    return getattr(app, "overlay_keys", None) or overlay.key_source(
        config, isatty=app.stdin_isatty)


def join_one(app, store, name: str, keys=None, say=None) -> overlay.Joined:
    """Join one Ship to the overlay, record its overlay name (first of its names) and
    node, and re-sign its host certificate so the overlay name is in it. Raises
    FleetError if the join fails; a certificate that cannot be re-signed is said."""
    from fleetledger import ca, trustcmd

    say = say or app.say
    inv = store.load()
    config = overlay.settings(inv)
    ship = inv.ship(name)
    joined = overlay.join(ShipCtl(app.ssh_factory(inv), ship), ship, config,
                          keys or _keys(app, config))
    host = joined.name if joined.name and overlay.HOST_RE.match(joined.name) else None
    if joined.name and host is None:
        say(f"{name}: its overlay name {joined.name!r} is not a usable host name; add one "
            "by hand to its names in ships.toml.")
    with store.edit() as ed:
        ed.set_field(name, "names", overlay.names_with(store.load().ship(name), host))
        ed.set_field(name, "overlay_node", joined.node_id)
    say(f"{name}: on the overlay as {joined.name or '?'} "
        f"({', '.join(joined.addresses) or 'no address yet'}).")
    inv = store.load()
    if host and ca.config(inv) is not None:
        try:
            trustcmd.certify(app, inv, inv.ship(name), force=True)
            say(f"{name}: host certificate re-signed with its overlay name.")
        except FleetError as exc:
            say(f"{name}: host certificate not re-signed ({exc.message}); run "
                f"`ship-fleet ca sign {name} --force`.")
    return joined


def join_after_enrol(app, store, name: str, say) -> None:
    """With an overlay set up, a newly enrolled Ship joins it (best effort: a key that
    must be pasted with no terminal, or a failed join, is said with the command)."""
    inv = store.load()
    if not inv.overlay:
        return
    try:
        join_one(app, store, name, say=say)
    except FleetError as exc:
        say(f"{name}: not on the overlay yet ({exc.message}); join it with "
            f"`ship-fleet overlay join {name}` (or its page in the web UI).")
