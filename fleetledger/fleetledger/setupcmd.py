"""ship-fleet doctor: a new user's map of what is set up.

doctor is read-only: it reports what is set up, what is missing, and the exact
command for each missing piece (firstrun.py holds the checks).
"""

from __future__ import annotations

from fleetledger import firstrun
from fleetledger.commands import table

COMMANDS = ("doctor",)

MARK = {"ok": "ok", "warn": "WARN", "missing": "MISSING", "fail": "FAIL", "skip": "-"}


def add_parsers(sub, add) -> None:
    p = add(sub, "doctor", "show what is set up, what is missing, and the next command",
            "Check this computer, FleetLedger's keys and inventory, the hypervisor, the\n"
            "overlay and the Ships. Read-only: it changes nothing. Each missing piece\n"
            "comes with the exact command that fixes it.")
    p.add_argument("--offline", action="store_true",
                   help="do not contact the Proxmox API")


def cmd_doctor(args, app) -> int:
    items = firstrun.everything(app, offline=args.offline)
    rows = [[MARK[i.state], i.area, i.name, i.detail] for i in items]
    app.say(table(rows, ["", "AREA", "CHECK", "DETAIL"]))
    todo = [i for i in items if i.state in ("missing", "fail", "warn") and i.next]
    if todo:
        app.say("\nNext:")
        for item in dict.fromkeys(i.next for i in todo):
            app.say(f"  {item}")
    else:
        app.say("\nEverything is in place.")
    return 1 if any(i.state in ("missing", "fail") for i in items) else 0
