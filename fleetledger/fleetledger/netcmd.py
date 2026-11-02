"""ship-fleet net: network visibility from the command line (see net.py)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from fleetledger import net


def _table(rows: list[list[str]], headers: list[str]) -> str:
    from fleetledger.commands import table

    return table(rows, headers)


def cmd_net(args, app) -> int:
    inv = app.store_factory().load()
    if args.name and (args.new or args.who):
        app.note("ship-fleet: give a Ship, or --new, or --who; not both.")
        return 2
    ships = inv.select([args.name] if args.name else [], args.tag) if inv.ships() else []
    problems: dict[str, str] = {}
    if not args.offline and ships:
        fetched = net.refresh(app.ssh_factory(inv), inv, ships)
        problems = {n: f.problem for n, f in fetched.items() if f.problem}
        for name, problem in problems.items():
            app.note(f"{name}: {problem}" + (" (showing the last summary)"
                                             if net.cached_summary(name) else ""))
    now = datetime.now(UTC)
    if args.who:
        rows = net.who_talked(inv, args.who, args.tag)
        if args.json:
            app.say(json.dumps(rows, indent=2))
            return 0
        if not rows:
            app.say(f"No Ship reached {args.who} in the last 7 days.")
            return 0
        app.say(_table([[r["ship"], r["name"], net.since(now, r["last_seen"]),
                         str(r["connections_24h"]), str(r["connections_7d"]),
                         ",".join(r["programs"][:3])] for r in rows],
                       ["SHIP", "DESTINATION", "LAST SEEN", "24H", "7D", "PROGRAMS"]))
        return 0
    if args.new:
        rows = net.new_destinations(inv, args.tag)
        if args.json:
            app.say(json.dumps(rows, indent=2))
            return 0
        if not rows:
            app.say("No new destinations in the last 24 hours.")
            return 0
        app.say(_table([[r["ship"], r["name"] + (" (new to the fleet)" if r["new_to_fleet"]
                                                  else ""),
                         net.since(now, r["first_seen"]), str(r["connections_24h"]),
                         ",".join(r["programs"][:3])] for r in rows],
                       ["SHIP", "DESTINATION", "FIRST SEEN", "24H", "PROGRAMS"]))
        return 0
    if args.name:
        ship = inv.ship(args.name)
        view = net.ship_view(inv, ship, net.cached_summary(ship.name), now=now)
        if args.json:
            app.say(json.dumps(view, indent=2))
            return 0 if view["available"] else 1
        return _show_ship(app, view, now, week=args.week)
    views = [net.ship_view(inv, s, net.cached_summary(s.name), now=now) for s in ships]
    if args.json:
        app.say(json.dumps(views, indent=2))
        return 0
    rows = []
    for v in views:
        if v["collector"] is None:
            rows.append([v["ship"], problems.get(v["ship"], "no data")[:40], "-", "-", "-", "-"])
            continue
        t = v["totals"]
        rows.append([v["ship"], v["collector"]["state"], f"{t['rx_mb_24h']:,.1f}",
                     f"{t['tx_mb_24h']:,.1f}", str(len(v["top_24h"])), str(len(v["new"]))])
    app.say(_table(rows, ["SHIP", "COLLECTOR", "IN MB 24H", "OUT MB 24H", "DESTINATIONS",
                          "NEW"]))
    return 0


def _show_ship(app, view, now, *, week: bool) -> int:
    if not view["available"]:
        app.say(f"{view['ship']}: no network visibility "
                f"({(view['collector'] or {}).get('note') or 'no summary yet'}).")
        return 1
    c, t = view["collector"], view["totals"]
    app.say(f"{view['ship']}: collector {c['state']}"
            + (f" ({c['source']})" if c["source"] else "")
            + (f"; {c['note']}" if c["note"] else ""))
    app.say(f"Traffic: {t['rx_mb_24h']:,.1f} MB in, {t['tx_mb_24h']:,.1f} MB out (24 h); "
            f"{t['rx_mb_7d']:,.1f} MB in, {t['tx_mb_7d']:,.1f} MB out (7 days)")
    rows = view["top_7d"] if week else view["top_24h"]
    key = "7d" if week else "24h"
    app.say(f"Top destinations ({'7 days' if week else '24 hours'}):")
    if not rows:
        app.say("  none")
    else:
        app.say(_table(
            [[("* " if r["new"] else "  ") + r["name"], r["scope"],
              str(r[f"connections_{key}"]),
              net.human_bytes(r[f"rx_bytes_{key}"]), net.human_bytes(r[f"tx_bytes_{key}"]),
              ",".join(p["name"] for p in r["programs"][:3])] for r in rows],
            ["DESTINATION", "SCOPE", "CONNS", "IN", "OUT", "PROGRAMS"]))
    if view["new"]:
        app.say("* first seen in the last 24 hours: "
                + ", ".join(r["name"] for r in view["new"][:10]))
    if view["programs"]:
        app.say("Programs: " + ", ".join(f"{p['name']} ({p['connections_7d']})"
                                         for p in view["programs"][:8]))
    return 0
