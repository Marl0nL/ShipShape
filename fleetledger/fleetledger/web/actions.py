"""What the web UI can change on a Ship, in one place.

The UI never reimplements an operation: settings, tags, logs, update, reboot
and joining the overlay are the shared day-2 operations (fleetledger.ops) that
`ship-fleet` runs too; sign-out is the `ship-fleet logout` command itself. So
the CLI and the UI always do exactly the same thing.

Update, reboot and join take minutes, so they run in the background
(Operations): the page that starts one returns at once, and the fleet page
shows each one's progress and outcome.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import itertools
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from fleetledger import commands, ops
from fleetledger.errors import FleetError

KEEP_OPERATIONS = 10
MAX_EVENTS = 200


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
    """`ship-fleet logout` with its output captured."""
    buf = io.StringIO()
    sub = dataclasses.replace(app, out=buf, err=buf)
    try:
        code = commands.cmd_logout(argparse.Namespace(name=ship, service=service), sub) or 0
    except FleetError as exc:
        return Outcome(ship, False, exc.message + (f" {exc.hint}" if exc.hint else ""))
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    return Outcome(ship, code == 0, lines[0] if lines else "Done.", tuple(lines[1:]))


# --- update, reboot and join, in the background ------------------------------


@dataclasses.dataclass
class Operation:
    id: int
    kind: str  # update | reboot | join
    ships: list[str]
    started: float = dataclasses.field(default_factory=time.time)
    events: deque[str] = dataclasses.field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    outcomes: list[Outcome] = dataclasses.field(default_factory=list)
    done: bool = False
    error: str | None = None
    version: int = 0

    @property
    def ok(self) -> bool:
        return self.done and self.error is None and all(o.ok for o in self.outcomes)


class _NoTerminal:
    """The app, with no terminal to paste a key into (a browser has none). A wrapper,
    not a copy: options set on the app (an API factory, say) come along."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def stdin_isatty(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._app, name)


def _join(app: Any, ships, event) -> list[ops.Outcome]:
    """Join Ships to the overlay from the browser (keys from the API: no terminal)."""
    from fleetledger import access, trustcmd

    quiet = _NoTerminal(app)
    store = app.store_factory()
    results = []
    for ship in ships:
        said: list[str] = []
        try:
            access.join_one(quiet, store, ship.name, say=said.append)
            ok, summary = True, "on the overlay"
        except FleetError as exc:
            ok, summary = False, f"not joined: {exc.message}"
        for line in said:
            event(ship.name, line.removeprefix(f"{ship.name}: "))
        results.append(ops.Outcome(ship.name, ok, summary, said))
    try:  # this computer's SSH config now prefers the overlay names
        trustcmd.trust_this_computer(quiet, store, lambda line: event("*", line), create=False)
    except FleetError:
        pass
    return results


KINDS = ("update", "reboot", "join")  # what Operations.start runs


class Operations:
    """Update, reboot and overlay-join runs started from the browser."""

    def __init__(self, app: Any, on_finished: Callable[[list[str]], None] | None = None):
        self.app = app
        self.on_finished = on_finished or (lambda ships: None)
        self._lock = threading.Lock()
        self._ops: list[Operation] = []
        self._ids = itertools.count(1)

    @property
    def version(self) -> int:
        with self._lock:
            return sum(o.version for o in self._ops) + len(self._ops)

    def recent(self) -> list[Operation]:
        with self._lock:
            return list(reversed(self._ops))

    def busy(self, ships: list[str]) -> bool:
        with self._lock:
            return any(not o.done and set(o.ships) & set(ships) for o in self._ops)

    def start(self, kind: str, ships: list[str]) -> Operation:
        if kind not in KINDS:
            raise FleetError(f"Unknown operation '{kind}'.")
        if self.busy(ships):
            raise FleetError("An operation is already running on one of those Ships.")
        op = Operation(next(self._ids), kind, list(ships))
        with self._lock:
            self._ops.append(op)
            finished = [o for o in self._ops if o.done]
            for old in finished[: max(0, len(finished) - KEEP_OPERATIONS)]:
                self._ops.remove(old)
        threading.Thread(target=self._run, args=(op,), name=f"{kind}-{op.id}",
                         daemon=True).start()
        return op

    def _run(self, op: Operation) -> None:
        def event(ship: str, text: str) -> None:
            op.events.append(text if ship == "*" else f"{ship}: {text}")
            op.version += 1

        try:
            fleet = _fleet(self.app)
            inv = fleet.inventory
            ships = [inv.ship(name) for name in op.ships]
            # In the background, so wait: the outcome then says whether each
            # Ship came back with its agents in their modes, as the CLI does.
            if op.kind == "join":
                results = _join(self.app, ships, event)
            elif op.kind == "update":
                results = ops.update(fleet, ships, reboot_policy="auto", wait=True,
                                     on_event=event)
            else:
                results = ops.reboot(fleet, ships, wait=True, on_event=event)
            op.outcomes = [Outcome.of(r) for r in results]
        except FleetError as exc:
            op.error = exc.message
        except Exception as exc:  # noqa: BLE001 - shown on the page, never raised in a thread
            op.error = f"Internal error ({type(exc).__name__})."
        finally:
            op.done = True
            op.version += 1
        try:
            self.on_finished(op.ships)
        except Exception:  # noqa: BLE001 - a status refresh must not hide the outcome
            pass
