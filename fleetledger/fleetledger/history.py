"""A small, bounded history of each Ship's status, for the dashboard's charts.

The web UI's poller (web/fleet.py) records one sample per Ship per poll: whether
it answered, CPU, memory and disk use, and how many agents it runs. Samples go
into one SQLite file in FleetLedger's data folder (`history.sqlite3`, mode 0600)
and are kept for `KEEP` (seven days); a Ship gets at most one sample every
`MIN_GAP` seconds however often it is polled, so the file stays small (about
2,000 rows per Ship).

There are samples only while the UI is open (it polls only while someone is
looking), so charts show gaps for the hours nobody was. No secrets, accounts or
messages are stored: only numbers and Ship names.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleetledger import paths

KEEP = 7 * 86400.0
MIN_GAP = 290.0  # just under the 5-minute mark, so a 30 s poll lands every 5 minutes
PRUNE_EVERY = 3600.0
SCHEMA = """
CREATE TABLE IF NOT EXISTS sample (
  ship TEXT NOT NULL,
  at REAL NOT NULL,
  up INTEGER NOT NULL,
  cpu REAL, mem REAL, disk REAL,
  agents INTEGER, working INTEGER, waiting INTEGER,
  PRIMARY KEY (ship, at)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS sample_at ON sample (at);
PRAGMA user_version = 1;
"""
FIELDS = ("ship", "at", "up", "cpu", "mem", "disk", "agents", "working", "waiting")


def history_path() -> Path:
    return paths.data_dir() / "history.sqlite3"


@dataclass(frozen=True)
class Sample:
    """One Ship at one moment. Measures are None when the Ship did not answer."""

    ship: str
    at: float  # unix seconds
    up: bool
    cpu: float | None = None  # load1 / cpus, as a percentage (capped at 100)
    mem: float | None = None  # percent used
    disk: float | None = None  # percent used
    agents: int | None = None
    working: int | None = None
    waiting: int | None = None


def sample_of(view: Any, at: float) -> Sample:
    """A Sample from a status.ShipView (a Ship that did not answer is `up=False`)."""
    doc = view.status
    if view.problem or doc is None:
        return Sample(view.ship.name, at, up=False)
    host = doc["host"]
    agents = doc["herdr"]["agents"]
    return Sample(
        view.ship.name, at, up=True,
        cpu=min(100.0, 100.0 * float(host["load1"]) / max(1, int(host["cpus"]))),
        mem=float(host["mem_used_pct"]),
        disk=float(host["disk_used_pct"]),
        agents=len(agents),
        working=sum(1 for a in agents if a["state"] == "working"),
        waiting=sum(1 for a in agents if a["state"] == "blocked"),
    )


class History:
    """The sample store. Safe to share between threads; every call opens its own
    connection. Errors reading or writing are the caller's to ignore (charts are
    a convenience: a broken file must never stop a poll or a page)."""

    def __init__(self, path: Path | None = None, *, keep: float = KEEP,
                 min_gap: float = MIN_GAP, wall=time.time) -> None:
        self.path = path or history_path()
        self.keep = keep
        self.min_gap = min_gap
        self.wall = wall
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._pruned = 0.0

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        # Every open checks the file and its table, so deleting the file while
        # FleetLedger runs just starts a new history (docs/web-ui.md).
        if not self.path.exists():
            paths.private_dir(self.path.parent)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.executescript(SCHEMA)
            with conn:
                yield conn
        finally:
            conn.close()

    # --- writing -----------------------------------------------------------
    def record(self, views: Iterable[Any], at: float | None = None) -> int:
        """Sample these Ship views (at most one per Ship per min_gap); returns how
        many were stored."""
        at = self.wall() if at is None else at
        with self._lock:
            due = [v for v in views
                   if at - self._last.get(v.ship.name, float("-inf")) >= self.min_gap]
            if not due:
                return 0
            stored = self.add([sample_of(v, at) for v in due])
            for v in due:
                self._last[v.ship.name] = at
            return stored

    def add(self, samples: Iterable[Sample]) -> int:
        rows = [tuple(getattr(s, f) for f in FIELDS) for s in samples]
        if not rows:
            return 0
        now = self.wall()
        with self._db() as conn:
            conn.executemany(
                f"INSERT OR REPLACE INTO sample ({', '.join(FIELDS)}) "
                f"VALUES ({', '.join('?' * len(FIELDS))})", rows)
            if now - self._pruned >= PRUNE_EVERY:
                conn.execute("DELETE FROM sample WHERE at < ?", (now - self.keep,))
                self._pruned = now
        return len(rows)

    # --- reading -----------------------------------------------------------
    def since(self, start: float, ships: Iterable[str] | None = None
              ) -> dict[str, list[Sample]]:
        """{ship: samples oldest first} from `start` on (only these Ships, if given)."""
        query = f"SELECT {', '.join(FIELDS)} FROM sample WHERE at >= ?"
        args: list[Any] = [start]
        if ships is not None:
            names = sorted(set(ships))
            if not names:
                return {}
            query += f" AND ship IN ({', '.join('?' * len(names))})"
            args += names
        found: dict[str, list[Sample]] = {}
        if not self.path.exists():
            return found
        with self._db() as conn:
            for row in conn.execute(query + " ORDER BY at", args):
                sample = Sample(*row[:2], bool(row[2]), *row[3:])
                found.setdefault(sample.ship, []).append(sample)
        return found
