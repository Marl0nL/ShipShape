"""The fleet as the web UI shows it: polled over SSH while someone is looking.

A background thread polls every Ship's status (status.poll, the same code
`ship-fleet ls` uses) every `interval` seconds, but only while a page has been
asked for in the last `idle_after` seconds, so an idle UI costs nothing. Pages
read the cached views and never wait on SSH. Until the first poll finishes the
last good status from `ls --offline`'s cache is shown, marked as stale.

`version` goes up whenever what the pages show may have changed, so polling
partials can answer 204 (nothing new) cheaply.

With a `history` (fleetledger.history.History), every poll is also sampled for
the dashboard's charts.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fleetledger import ops, status
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory

POLL_INTERVAL = 30.0
IDLE_AFTER = 120.0

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
    """One line in Needs attention: ops.attention's item plus a sign-in offer."""

    ship: str
    code: str
    message: str
    level: str  # high | medium | low (ops.SEVERITY)
    subject: str | None = None
    signin: str | None = None  # provider id to offer a one-click (re-)sign-in for

    @property
    def key(self) -> str:
        """Stable identity, for browser notifications ("is this new?")."""
        return f"{self.ship}/{self.code}/{self.subject or ''}"


def attention_items(views: list[status.ShipView]) -> list[Item]:
    """Needs attention, ranked by the shared ops.attention (as `ship-fleet attention`)."""
    items = []
    for entry in ops.attention(views):
        signin = None
        if entry.code in SIGNIN_CODES and entry.subject in SERVICE_PROVIDER:
            signin = SERVICE_PROVIDER[entry.subject]
        elif entry.code == "agent-repo-waiting":  # its clone needs the GitHub sign-in
            signin = SERVICE_PROVIDER["github"]
        items.append(Item(entry.ship, entry.code, entry.message, entry.severity,
                          entry.subject, signin))
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
        history: Any = None,  # fleetledger.history.History, or None: keep no history
    ) -> None:
        self.app = app
        self.interval = interval
        self.idle_after = idle_after
        self.clock = clock
        self.wall = wall
        self.history = history
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
        views = [status.cached_view(inv, ship, "Not checked yet.") for ship in inv.ships()]
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
            self._record(fresh)
        if names is None:
            self._renew_this_computer()

    def _record(self, views: list[status.ShipView]) -> None:
        if self.history is None:
            return
        try:
            self.history.record(views, self.wall())
        except (OSError, sqlite3.Error):
            pass  # charts are a convenience; never fail a poll for them

    def _renew_this_computer(self) -> None:
        """This computer's certificate, in its last day: renewed, so plain ssh and herdr
        --remote keep working (trustcmd.renew_this_computer_if_due)."""
        from fleetledger import trustcmd

        try:
            trustcmd.renew_this_computer_if_due(self.app, self.app.store_factory())
        except Exception:  # noqa: BLE001 - a renewal must never stop the poller
            pass

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
