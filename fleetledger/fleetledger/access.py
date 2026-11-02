"""ship-fleet overlay (phase 5): join Ships to the overlay network, leave it, show it.

See overlay.py for how keys are handled and docs/runbooks/remote-access.md for
the whole setup, including your laptop.
"""

from __future__ import annotations

from fleetledger import overlay
from fleetledger.commands import table
from fleetledger.errors import FleetError
from fleetledger.shipctl import ShipCtl

COMMANDS = ("overlay",)


def add_parsers(sub, add) -> None:
    p = add(sub, "overlay", "join Ships to the overlay network (access off the LAN)",
            "  ship-fleet overlay join ship-01 [ship-02 ...] | --tag work\n"
            "  ship-fleet overlay status [ship-01 ...]\n"
            "  ship-fleet overlay leave ship-01\n"
            "  ship-fleet overlay policy          access rules to paste into the admin console\n\n"
            "Each join uses a one-off auth key, pasted (not shown) or minted through the\n"
            "Tailscale API ([overlay] key_source). Keys never go into seeds or logs.\n"
            "See docs/runbooks/remote-access.md.")
    p.add_argument("action", choices=("join", "leave", "status", "policy"))
    p.add_argument("names", nargs="*", metavar="SHIP")
    p.add_argument("--tag", help="every Ship with this tag")


def cmd_overlay(args, app) -> int:
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
        keys = getattr(app, "overlay_keys", None) or overlay.key_source(
            config, isatty=app.stdin_isatty)
        for ship in ships:
            try:
                joined = overlay.join(ShipCtl(app.ssh_factory(inv), ship), ship, config, keys)
            except FleetError as exc:
                app.say(f"{ship.name}: not joined: {exc.message}")
                failed += 1
                continue
            with store.edit() as ed:
                ed.set_field(ship.name, "names",
                             overlay.names_with(store.load().ship(ship.name), joined.name))
            app.say(f"{ship.name}: on the overlay as {joined.name or '?'} "
                    f"({', '.join(joined.addresses) or 'no address yet'}).")
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
        if before and before.get("name"):
            with store.edit() as ed:
                ed.set_field(ship.name, "names",
                             overlay.names_without(store.load().ship(ship.name), before["name"]))
        for message in messages:
            app.say(f"{ship.name}: {message}")
    return 1 if failed else 0
