"""What the web UI can change on a Ship, in one place.

The UI never reimplements an operation: each function here calls the shared
operations module (fleetledger.ops, day-2 operations) when it is installed, and
until then the same `ship-fleet` command function the CLI runs, with its output
captured. Either way the CLI and the UI do exactly the same thing.

Every function returns an Outcome: ok, and a short summary safe to show.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import io
from typing import Any

from fleetledger import commands
from fleetledger.errors import FleetError


@dataclasses.dataclass(frozen=True)
class Outcome:
    ship: str
    ok: bool
    summary: str
    details: tuple[str, ...] = ()


def _ops() -> Any | None:
    try:
        return importlib.import_module("fleetledger.ops")
    except ImportError:
        return None


def available(name: str) -> bool:
    """Whether an operation exists yet (update and reboot arrive with fleetledger.ops)."""
    ops = _ops()
    if ops is not None and hasattr(ops, name):
        return True
    return hasattr(commands, f"cmd_{name}")


def run_command(app: Any, ship: str, command: str, **kwargs: Any) -> Outcome:
    """Run a ship-fleet command function with its output captured."""
    buf = io.StringIO()
    sub = dataclasses.replace(app, out=buf, err=buf)
    handler = getattr(commands, f"cmd_{command}")
    try:
        code = handler(argparse.Namespace(**kwargs), sub) or 0
    except FleetError as exc:
        return Outcome(ship, False, exc.message + (f" {exc.hint}" if exc.hint else ""))
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    summary = lines[0] if lines else ("Done." if code == 0 else "Failed.")
    return Outcome(ship, code == 0, summary, tuple(lines[1:]))


def _from_ops(result: Any, ship: str) -> Outcome:
    """fleetledger.ops Outcome (ship, ok, summary, details, data) -> ours."""
    details = getattr(result, "details", ()) or ()
    if isinstance(details, str):
        details = tuple(details.splitlines())
    return Outcome(
        getattr(result, "ship", ship),
        bool(getattr(result, "ok", False)),
        str(getattr(result, "summary", "") or ("Done." if getattr(result, "ok", False)
                                                 else "Failed.")),
        tuple(str(d) for d in details),
    )


def _first(results: Any, ship: str) -> Outcome:
    if isinstance(results, list | tuple):
        return _from_ops(results[0], ship) if results else Outcome(ship, True, "No change.")
    return _from_ops(results, ship)


def _guard(ship: str, call) -> Outcome:
    try:
        return call()
    except FleetError as exc:
        return Outcome(ship, False, exc.message + (f" {exc.hint}" if exc.hint else ""))


def set_settings(app: Any, ship: str, assignments: list[str]) -> Outcome:
    ops = _ops()
    if ops is not None and hasattr(ops, "set_settings"):
        return _guard(ship, lambda: _first(
            ops.set_settings(app, [_ship(app, ship)], assignments), ship))
    return run_command(app, ship, "set", name=ship, assignments=assignments)


def set_tags(app: Any, ship: str, tags: list[str]) -> Outcome:
    changes = ["none", *(f"+{t}" for t in dict.fromkeys(tags))]
    ops = _ops()
    if ops is not None and hasattr(ops, "set_tags"):
        return _guard(ship, lambda: _first(ops.set_tags(app, _ship(app, ship), changes), ship))
    return run_command(app, ship, "tag", name=ship, changes=changes, local=False)


def logout(app: Any, ship: str, service: str) -> Outcome:
    return run_command(app, ship, "logout", name=ship, service=service)


def logs(app: Any, ship: str, source: str, lines: int = 300) -> tuple[bool, list[str]]:
    ops = _ops()
    if ops is not None and hasattr(ops, "logs"):
        try:
            return True, [str(x) for x in ops.logs(app, _ship(app, ship), source, lines)]
        except FleetError as exc:
            return False, [exc.message]
    out = run_command(app, ship, "logs", name=ship, source=source, lines=lines)
    return out.ok, [out.summary, *out.details] if out.ok else [out.summary]


def update(app: Any, ships: list[str], reboot: str = "auto") -> list[Outcome]:
    """ship-fleet update (fleetledger.ops). Not available until that lands."""
    ops = _ops()
    if ops is None or not hasattr(ops, "update"):
        return [Outcome(s, False, "Updating from the UI arrives with ship-fleet update.")
                for s in ships]
    try:
        results = ops.update(app, [_ship(app, s) for s in ships], reboot=reboot)
    except FleetError as exc:
        return [Outcome(s, False, exc.message) for s in ships]
    return [_from_ops(r, "?") for r in results]


def reboot(app: Any, ships: list[str]) -> list[Outcome]:
    """ship-fleet reboot (fleetledger.ops). Not available until that lands."""
    ops = _ops()
    if ops is None or not hasattr(ops, "reboot"):
        return [Outcome(s, False, "Rebooting from the UI arrives with ship-fleet reboot.")
                for s in ships]
    try:
        results = ops.reboot(app, [_ship(app, s) for s in ships], wait=False)
    except FleetError as exc:
        return [Outcome(s, False, exc.message) for s in ships]
    return [_from_ops(r, "?") for r in results]


def _ship(app: Any, name: str):
    return app.store_factory().load().ship(name)
