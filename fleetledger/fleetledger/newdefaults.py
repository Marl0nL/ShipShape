"""What a new Ship gets for each option `ship-fleet new` leaves out: `ship-fleet defaults`.

One read for a person or a panel that reads `--json`
(`contracts/fleetledger-defaults.schema.json`): ships.toml's [defaults] as `new` applies
them (the driver, base image, tags, size, Claude's default mode and bypass, tools and
profiles), and this computer's time zone, which `new` gives a Ship without --timezone.
The choices themselves are other reads: `tools ls --json` and `image ls --json`.

It only reads ships.toml, the repo's catalogue and this computer's time zone: it reaches
no Ship and reads no secret.
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA = "shipshape.fleetledger-defaults/1"
SHIP_DEFAULT_MODE = "auto"  # ship.toml.schema.json claude.default_mode's default


def add_parsers(sub, add) -> None:
    p = add(sub, "defaults", "show what a new Ship gets for each option ship-fleet new leaves out",
            "What ship-fleet new uses for an option you leave out: ships.toml's [defaults]\n"
            "(driver, base image, tags, size, default mode, tools, profiles) and this\n"
            "computer's time zone. It only reads.\n\n"
            "  ship-fleet defaults [--json]")
    p.add_argument("--json", action="store_true", help="print JSON")


def document(inv, repo) -> dict[str, Any]:
    """The defaults as `new` applies them (commands._new, seed/shiptoml.py)."""
    from fleetledger.catalogue import Catalogue
    from fleetledger.inventory import Ship
    from fleetledger.seed.shiptoml import local_timezone, profiles_for, tools_for

    blank = Ship(name="new", id="new", driver="manual", names=(), host_key="")
    claude = inv.effective_claude(blank)
    mode = claude.get("default_mode", SHIP_DEFAULT_MODE)
    return {
        "schema": SCHEMA,
        "driver": inv.defaults.get("driver", "manual"),
        "image": inv.pinned_image,
        "tags": list(inv.effective_tags(blank)),
        "size": inv.effective_size(),
        "mode": mode,
        "allow_bypass": bool(claude.get("allow_bypass", False)) or mode == "bypassPermissions",
        "tools": tools_for(inv, blank, Catalogue.from_repo(repo)),
        "tools_from": "defaults" if "tools" in inv.defaults else "catalogue",
        "profiles": profiles_for(inv, blank),
        "timezone": local_timezone(),
    }


def cmd_defaults(args, app) -> int:
    doc = document(app.store_factory().load(), app.repo_factory())
    if args.json:
        app.say(json.dumps(doc, indent=2))
        return 0
    size = doc["size"]
    rows = [
        ("driver", doc["driver"]),
        ("image", doc["image"]),
        ("tags", ", ".join(doc["tags"]) or "-"),
        ("size", f"{size['cores']} vCPUs, {size['memory_mb']} MB, {size['disk_gb']} GB disk"),
        ("mode", doc["mode"] + (" (bypassPermissions allowed)" if doc["allow_bypass"] else "")),
        ("tools", (", ".join(doc["tools"]) or "-")
         + (" (ships.toml defaults.tools)" if doc["tools_from"] == "defaults"
            else " (the catalogue's defaults)")),
        ("profiles", ", ".join(doc["profiles"]) or "-"),
        ("time zone", doc["timezone"] + " (this computer's)"),
    ]
    app.say("A new Ship gets, unless ship-fleet new says otherwise:")
    for key, value in rows:
        app.say(f"  {key:<10} {value}")
    return 0
