"""The fleet as the web UI shows it: polled over SSH while someone is looking.

A background thread polls every Ship's status (status.poll, the same code
`ship-fleet ls` uses) every `interval` seconds, but only while a page has been
asked for in the last `idle_after` seconds, so an idle UI costs nothing. Pages
read the cached views and never wait on SSH. Until the first poll finishes the
last good status from `ls --offline`'s cache is shown, marked as stale.

`version` goes up whenever what the pages show may have changed, so polling
partials can answer 204 (nothing new) cheaply.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fleetledger import status
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory

POLL_INTERVAL = 30.0
IDLE_AFTER = 120.0

# Needs attention: lower sorts first.
CRITICAL, HIGH, MEDIUM, LOW = 0, 1, 2, 3
SEVERITY_LABEL = {CRITICAL: "critical", HIGH: "high", MEDIUM: "medium", LOW: "low"}

CODE_SEVERITY = {
    "signin-wrong-account": CRITICAL,
    "agent-waiting": HIGH,
    "signin-missing": HIGH,
    "signin-expired": HIGH,
    "provision-failed": HIGH,
    "unit-failed": HIGH,
    "tool-failed": HIGH,
    "signin-expiring": MEDIUM,
    "tags-differ": MEDIUM,
    "reboot-required": LOW,
    "behind-tooling": LOW,
    "tool-waiting": LOW,
}
PROBLEM_SEVERITY = {
    "host-key": CRITICAL,
    "identity": CRITICAL,
    "unreachable": HIGH,
    "invalid": HIGH,
    "error": HIGH,
    "unknown": MEDIUM,
}
# Status auth service -> concierge provider that signs it in.
SERVICE_PROVIDER = {
    "claude": "claude",
    "gcloud": "gcloud",
    "gcloud_adc": "gcloud-adc",
    "github": "github",
}
SIGNIN_CODES = ("signin-missing", "signin-expired", "signin-expiring", "signin-wrong-account")


@dataclass(frozen=True)
class Item:
    """One line in Needs attention."""

    ship: str
    code: str
    message: str
    severity: int
    subject: str | None = None
    signin: str | None = None  # provider id to offer a one-click (re-)sign-in for

    @property
    def key(self) -> str:
        """Stable identity, for browser notifications ("is this new?")."""
        return f"{self.ship}/{self.code}/{self.subject or ''}"

    @property
    def level(self) -> str:
        return SEVERITY_LABEL[self.severity]


def attention_items(views: list[status.ShipView]) -> list[Item]:
    """Everything that needs a person, most urgent first.

    Uses the shared fleetledger.ops.attention when it is installed (the CLI
    and the UI then rank the same way), else the built-in ranking below.
    """
    try:
        from fleetledger import ops  # noqa: PLC0415 - optional until day-2 operations land
    except ImportError:
        ops = None
    if ops is not None and hasattr(ops, "attention"):
        return [_from_ops(item) for item in ops.attention(views)]
    return builtin_attention(views)


def _from_ops(item) -> Item:
    code = getattr(item, "code", "unknown")
    subject = getattr(item, "subject", None)
    severity = getattr(item, "severity", MEDIUM)
    if isinstance(severity, str):
        severity = {v: k for k, v in SEVERITY_LABEL.items()}.get(severity.lower(), MEDIUM)
    signin = SERVICE_PROVIDER.get(subject) if code in SIGNIN_CODES else None
    return Item(getattr(item, "ship", "?"), code, getattr(item, "message", code),
                int(severity) if severity in SEVERITY_LABEL else MEDIUM, subject, signin)


def builtin_attention(views: list[status.ShipView]) -> list[Item]:
    items: list[Item] = []
    for view in views:
        name = view.ship.name
        if view.problem:
            items.append(
                Item(
                    name,
                    view.problem,
                    view.message or view.problem,
                    PROBLEM_SEVERITY.get(view.problem, MEDIUM),
                )
            )
            continue
        for entry in view.attention:
            code = entry.get("code", "unknown")
            subject = entry.get("subject")
            signin = None
            if code in SIGNIN_CODES and subject in SERVICE_PROVIDER:
                signin = SERVICE_PROVIDER[subject]
            items.append(
                Item(
                    name,
                    code,
                    entry.get("message", code),
                    CODE_SEVERITY.get(code, MEDIUM),
                    subject,
                    signin,
                )
            )
    items.sort(key=lambda i: (i.severity, i.ship, i.code, i.subject or ""))
    return items


@dataclass
class Snapshot:
    """What the pages read: one consistent set of views."""

    inventory: Inventory | None
    views: list[status.ShipView]
    polled_at: float | None  # wall clock of the last finished poll, None: cache only
    error: str | None = None
    by_name: dict[str, status.ShipView] = field(init=False)

    def __post_init__(self) -> None:
        self.by_name = {v.ship.name: v for v in self.views}

    def tags(self) -> list[str]:
        return self.inventory.all_tags() if self.inventory else []

    def views_for(self, tag: str | None) -> list[status.ShipView]:
        if not tag or self.inventory is None:
            return list(self.views)
        return [v for v in self.views if tag in self.inventory.effective_tags(v.ship)]

    def effective_tags(self, view: status.ShipView) -> tuple[str, ...]:
        return self.inventory.effective_tags(view.ship) if self.inventory else ()


class Fleet:
    """Cached Ship views, refreshed in the background while the UI is in use."""

    def __init__(
        self,
        app: Any,  # cli.App: store, repo and SSH factories
        *,
        interval: float = POLL_INTERVAL,
        idle_after: float = IDLE_AFTER,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self.app = app
        self.interval = interval
        self.idle_after = idle_after
        self.clock = clock
        self.wall = wall
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_seen = clock()
        self._last_poll: float | None = None
        self.version = 0
        self.snapshot = Snapshot(None, [], None)
        self._thread: threading.Thread | None = None

    # --- reading --------------------------------------------------------------
    def touch(self) -> None:
        """Someone is looking: keep polling (and poll now if it is overdue)."""
        self._last_seen = self.clock()
        if self._last_poll is None or self.clock() - self._last_poll >= self.interval:
            self._wake.set()

    def bump(self) -> None:
        with self._lock:
            self.version += 1

    def load_cached(self) -> None:
        """Show the last good status of every Ship before the first poll."""
        try:
            inv = self.app.store_factory().load()
        except FleetError as exc:
            self._publish(Snapshot(None, [], None, error=exc.message))
            return
        views = []
        for ship in inv.ships():
            doc = status.read_cache(ship.name)
            view = status.ShipView(ship, status=doc)
            if doc is None:
                view.problem, view.message = "unknown", "Not checked yet."
            else:
                view.attention = list(doc["attention"]) + status.fleet_attention(
                    inv, ship, doc, None
                )
            views.append(view)
        self._publish(Snapshot(inv, views, None))

    # --- polling ----------------------------------------------------------------
    def poll(self, names: list[str] | None = None) -> None:
        """Poll now (all Ships, or just these) and publish the result."""
        with self._poll_lock:
            try:
                inv = self.app.store_factory().load()
                ships = inv.ships()
                wanted = [s for s in ships if names is None or s.name in names]
                tooling = None
                try:
                    tooling = self.app.repo_factory().tooling_version
                except FleetError:
                    pass  # no checkout: skip the behind-tooling check
                fresh = status.poll(self.app.ssh_factory(inv), inv, wanted, tooling=tooling)
            except FleetError as exc:
                with self._lock:
                    current = self.snapshot
                self._publish(Snapshot(current.inventory, current.views, current.polled_at,
                                       error=exc.message))
                return
            with self._lock:
                previous = dict(self.snapshot.by_name)
            previous.update({v.ship.name: v for v in fresh})
            # Ships removed from the inventory drop out; new ones appear.
            views = [previous[s.name] for s in ships if s.name in previous]
            if names is None:
                self._last_poll = self.clock()
                polled_at = self.wall()
            else:  # one Ship re-checked: the fleet as a whole is as old as before
                polled_at = self.snapshot.polled_at or self.wall()
            self._publish(Snapshot(inv, views, polled_at))

    def _publish(self, snapshot: Snapshot) -> None:
        with self._lock:
            self.snapshot = snapshot
            self.version += 1

    def active(self) -> bool:
        return self.clock() - self._last_seen < self.idle_after

    def _due(self) -> bool:
        return self._last_poll is None or self.clock() - self._last_poll >= self.interval

    def run(self) -> None:
        """The polling loop (runs in its own thread until stop())."""
        while not self._stop.is_set():
            if self.active() and self._due():
                try:
                    self.poll()
                except Exception:  # noqa: BLE001 - keep polling; the next try may work
                    self._last_poll = self.clock()
            self._wake.wait(timeout=1.0)
            self._wake.clear()

    def start(self) -> None:
        self.load_cached()
        self._thread = threading.Thread(target=self.run, name="fleet-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
