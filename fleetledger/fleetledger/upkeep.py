"""What FleetLedger brings up to date by itself after an update: `ship-fleet self-update`
runs it once the checkout has moved (in a new process, so it is the new version's own
list), and so does the service each time it starts (so an update made by an older
self-update, or by git pull and a restart, is caught up too). It asks nothing and is
safe to run any number of times: each step writes only when something differs.

Only what FleetLedger's own user can do, on the copy that acts:

- the trusted machines' bundle keys in ~/.ssh/authorized_keys, rewritten to match them
  (what `ship-fleet trust --sync-keys` does, with all of authkeys.sync's rules: only
  inside FleetLedger's block, only on its own VM for a new line, never a line of yours).

What needs another account (sshd's ExposeAuthInfo, vmadmin's) is not a step here:
nothing needs it, and `ship-fleet doctor` mentions it once, as optional
(firstrun.auth_info_items).
"""

from __future__ import annotations

from fleetledger.errors import FleetError

STEPS = ("bundle keys",)


def catch_up(store) -> list[str]:
    """Run every step; what changed, said, one line each. Raises FleetError (with the
    step named) for the first that fails, after trying the rest."""
    from fleetledger import authkeys, handover

    if handover.marker(store) is not None:
        return []  # a copy on standby or retired changes nothing (migrate empties its block)
    said: list[str] = []
    failed: list[str] = []
    try:
        said += authkeys.synced_said(authkeys.sync(store))
    except FleetError as exc:
        failed.append(f"bundle keys: {exc.message}" + (f" {exc.hint}" if exc.hint else ""))
    if failed:
        raise FleetError("Bringing FleetLedger's own state up to date failed: "
                         + "; ".join(failed),
                         hint="Fix that, then run ship-fleet self-update again (it catches "
                              "up even when there is nothing new to fetch).")
    return said


def at_service_start(app) -> None:
    """The service's catch-up: said on stderr (the journal), never a reason not to
    start."""
    try:
        for line in catch_up(app.store_factory()):
            app.note(f"ship-fleet: {line}")
    except FleetError as exc:
        app.note(f"ship-fleet: {exc.message}")
    except OSError as exc:  # a folder that cannot be read: doctor says more
        app.note(f"ship-fleet: could not bring FleetLedger's own state up to date: {exc}")
