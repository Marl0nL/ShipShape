"""What the web UI can change on a Ship, in one place.

The UI never reimplements an operation: settings, tags, logs, update, reboot
and joining the overlay are the shared day-2 operations (fleetledger.ops) that
`ship-fleet` runs too; sign-out is the `ship-fleet logout` command itself. So
the CLI and the UI always do exactly the same thing.

Update, reboot and join take minutes, so they run in the background as jobs
(web.jobs): the page that starts one returns at once, and the Fleet page's Activity
and the Ship's page show each one's progress and outcome.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
from collections.abc import Callable
from typing import Any

from fleetledger import commands, ops
from fleetledger.errors import FleetError


@dataclasses.dataclass(frozen=True)
class Outcome:
    ship: str
    ok: bool
    summary: str
    details: tuple[str, ...] = ()

    @classmethod
    def of(cls, outcome: ops.Outcome) -> Outcome:
        return cls(outcome.ship, outcome.ok, outcome.summary, tuple(outcome.details))


def _fleet(app: Any) -> ops.Fleet:
    return ops.Fleet.from_app(app)


def _guard(ship: str, call: Callable[[], list[ops.Outcome]]) -> Outcome:
    """One Ship's outcome; a refusal before any Ship was touched becomes a failure."""
    try:
        results = call()
    except FleetError as exc:
        return Outcome(ship, False, exc.message + (f" {exc.hint}" if exc.hint else ""))
    return Outcome.of(results[0]) if results else Outcome(ship, True, "No change.")


def set_settings(app: Any, ship: str, assignments: list[str]) -> Outcome:
    fleet = _fleet(app)
    return _guard(ship, lambda: ops.set_settings(fleet, [fleet.inventory.ship(ship)],
                                                 assignments))


def set_tags(app: Any, ship: str, tags: list[str]) -> Outcome:
    changes = ["none", *(f"+{t}" for t in dict.fromkeys(tags))]
    fleet = _fleet(app)
    return _guard(ship, lambda: ops.set_tags(fleet, [fleet.inventory.ship(ship)], changes))


def logs(app: Any, ship: str, source: str, lines: int = 300) -> tuple[bool, list[str]]:
    fleet = _fleet(app)
    try:
        return True, ops.logs(fleet, fleet.inventory.ship(ship), source, lines)
    except FleetError as exc:
        return False, [exc.message]


def logout(app: Any, ship: str, service: str) -> Outcome:
    """`ship-fleet logout` with its output captured (never on the server's stdout)."""
    from fleetledger.web.jobs import BrowserApp

    buf = io.StringIO()
    sub = BrowserApp(app, buf)
    try:
        code = commands.cmd_logout(argparse.Namespace(name=ship, service=service), sub) or 0
    except FleetError as exc:
        return Outcome(ship, False, exc.message + (f" {exc.hint}" if exc.hint else ""))
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    return Outcome(ship, code == 0, lines[0] if lines else "Done.", tuple(lines[1:]))


# --- update, reboot and join, in the background (web.jobs) -------------------


KINDS = ("update", "reboot", "join")  # what start_operation runs
TITLES = {"update": "Update", "reboot": "Reboot", "join": "Overlay join"}


def _join(app: Any, ships, event) -> list[ops.Outcome]:
    """Join Ships to the overlay from the browser (keys from the API: no terminal)."""
    from fleetledger import access, trustcmd

    store = app.store_factory()
    results = []
    for ship in ships:
        said: list[str] = []
        try:
            access.join_one(app, store, ship.name, say=said.append)
            ok, summary = True, "on the overlay"
        except FleetError as exc:
            ok, summary = False, f"not joined: {exc.message}"
        for line in said:
            event(ship.name, line.removeprefix(f"{ship.name}: "))
        results.append(ops.Outcome(ship.name, ok, summary, said))
    try:  # this computer's SSH config now prefers the overlay names
        trustcmd.trust_this_computer(app, store, lambda line: event("*", line), create=False)
    except FleetError:
        pass
    return results


def start_operation(jobs: Any, app: Any, kind: str, ships: list[str]) -> Any:
    """Update, reboot or join these Ships as a background job (refused if one of
    them is busy)."""
    from fleetledger.web.jobs import BrowserApp

    if kind not in KINDS:
        raise FleetError(f"Unknown operation '{kind}'.")

    def run(job) -> None:
        def event(ship: str, text: str) -> None:
            job.write(text if ship == "*" else f"{ship}: {text}")

        quiet = BrowserApp(app, job)
        fleet = _fleet(quiet)
        inv = fleet.inventory
        chosen = [inv.ship(name) for name in ships]
        # In the background, so wait: the outcome then says whether each Ship came
        # back with its agents in their modes, as the CLI does.
        if kind == "join":
            results = _join(quiet, chosen, event)
        elif kind == "update":
            results = ops.update(fleet, chosen, reboot_policy="auto", wait=True,
                                 on_event=event)
        else:
            results = ops.reboot(fleet, chosen, wait=True, on_event=event)
        job.outcomes = [Outcome.of(r) for r in results]
        job.ok = all(o.ok for o in job.outcomes)

    return jobs.start(kind, list(ships), run, TITLES[kind])
