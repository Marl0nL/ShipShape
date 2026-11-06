"""A small, bounded history: each Ship's status (the dashboard's charts) and the
operations FleetLedger finished (the Activity page).

The web UI's poller (web/fleet.py) records one sample per Ship per poll: whether
it answered, CPU, memory and disk use, and how many agents it runs. Samples go
into one SQLite file in FleetLedger's data folder (`history.sqlite3`, mode 0600)
and are kept for `KEEP` (seven days); a Ship gets at most one sample every
`MIN_GAP` seconds however often it is polled, so the file stays small (about
2,000 rows per Ship).

There are samples only while the UI is open (it polls only while someone is
looking), so charts show gaps for the hours nobody was. Samples hold no secrets,
accounts or messages: only numbers and Ship names.

Operations (the web UI's jobs: update, reboot, make a Ship ...) are saved in the
same file once they end: kind, title, Ships, start and end time, outcome, error
and log, newest `OPS_KEEP_COUNT` for `OPS_KEEP` (30 days). The log and error are
passed through redact() as they are saved (long token-like strings, email
addresses and link queries are replaced), so a record holds no secret even if a
command printed one; the title loses addresses and link queries; Ship names and
the kind are identifiers and are kept as they are. Whether its card was dismissed
is kept with it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field, replace
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
CREATE TABLE IF NOT EXISTS operation (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  title TEXT NOT NULL,
  ships TEXT NOT NULL,
  started REAL NOT NULL,
  ended REAL NOT NULL,
  outcome TEXT NOT NULL,
  error TEXT,
  lines INTEGER NOT NULL,
  log TEXT NOT NULL,
  dismissed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS operation_ended ON operation (ended);
PRAGMA user_version = 2;
"""
FIELDS = ("ship", "at", "up", "cpu", "mem", "disk", "agents", "working", "waiting")


OPS_KEEP = 30 * 86400.0
OPS_KEEP_COUNT = 200
OPS_MAX_LINES = 2000  # per operation; more are cut, with a line saying how many
OPS_MAX_LINE = 1000  # characters per line
OUTCOMES = ("ok", "failed", "by-hand", "stopped")


def history_path() -> Path:
    return paths.data_dir() / "history.sqlite3"


@contextlib.contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    """The history file, made (0600) if missing, with its tables. Every open checks
    the file and its tables, so deleting the file while FleetLedger runs just starts
    a new history (docs/web-ui.md)."""
    if not path.exists():
        paths.private_dir(path.parent)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        os.close(fd)
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.executescript(SCHEMA)
        with conn:
            yield conn
    finally:
        conn.close()


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

    def _db(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        return _connect(self.path)

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

    def forget(self, ship: str) -> None:
        """Drop every sample of a Ship (it was destroyed or forgotten)."""
        if not self.path.exists():
            return
        with self._lock:
            self._last.pop(ship, None)
        with self._db() as conn:
            conn.execute("DELETE FROM sample WHERE ship = ?", (ship,))

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


# --- operations ----------------------------------------------------------------


@dataclass(frozen=True)
class Operation:
    """One finished operation (a web UI job). `log` is empty when it was listed, not
    read whole (Operations.get reads it); `lines` says how long it is."""

    id: int
    kind: str
    title: str
    ships: tuple[str, ...]
    started: float
    ended: float
    outcome: str  # one of OUTCOMES
    error: str | None = None
    log: tuple[str, ...] = field(default=(), repr=False)
    lines: int = 0
    dismissed: bool = False

    @property
    def took(self) -> float:
        return max(0.0, self.ended - self.started)


# A Ship name or a job kind: an identifier, never a secret, so kept as it is (a long
# Ship name would otherwise read as a token). Anything else is scrubbed in full.
IDENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


# The characters redact() counts as part of a token: a name is kept in a log line
# only where it stands alone, not inside a longer run (which may be a secret).
TOKEN_CHAR = r"A-Za-z0-9_\-+/="


def _scrub(text: str, keep: tuple[str, ...] = ()) -> str:
    """text, cleaned and redacted. Each name in keep (the job's own Ship names,
    identifiers saved in clear in the same record) stays readable where it stands
    alone, however long: a 24+ character name would otherwise read as a token."""
    from fleetledger.concierge.redact import clean, redact

    text = clean(text)  # no control characters left: \0 marks a kept name below
    long = sorted((n for n in keep if len(n) >= 24), key=len, reverse=True)
    for i, name in enumerate(long):
        alone = re.compile(rf"(?<![{TOKEN_CHAR}.]){re.escape(name)}"
                           rf"(?![{TOKEN_CHAR}]|\.[{TOKEN_CHAR}.])")
        text = alone.sub(f"\0{i}\0", text)
    text = redact(text)
    for i, name in enumerate(long):
        text = text.replace(f"\0{i}\0", name)
    return text[:OPS_MAX_LINE]


def _ident(text: str) -> str:
    return text if IDENT.match(text) else _scrub(text)


def _label(text: str) -> str:
    """A title ("Take snapshot before-upgrade-2026-11-06-full"): written by FleetLedger
    from names it checked, so only addresses and link queries are taken out; long
    runs stay readable."""
    from fleetledger.concierge.redact import EMAIL, QUERY, clean

    return EMAIL.sub("[email]", QUERY.sub(r"\1?[…]", clean(text)))[:OPS_MAX_LINE]


def scrubbed(op: Operation) -> Operation:
    """op as it is saved: the log and error redacted and bounded (the job's own Ship
    names left readable in them), the title cleared of addresses and link queries,
    Ship names and the kind kept (identifiers)."""
    keep = tuple(s for s in op.ships if IDENT.match(s))
    log = [line for line in (_scrub(raw, keep).rstrip() for raw in op.log) if line.strip()]
    if len(log) > OPS_MAX_LINES:
        cut = len(log) - OPS_MAX_LINES
        log = log[:OPS_MAX_LINES] + [f"({cut} more line{'s' if cut != 1 else ''} not kept)"]
    return replace(op, kind=_ident(op.kind), title=_label(op.title),
                   ships=tuple(_ident(s) for s in op.ships),
                   outcome=op.outcome if op.outcome in OUTCOMES else "failed",
                   error=_scrub(op.error, keep) if op.error else None,
                   log=tuple(log), lines=len(log))


OP_FIELDS = ("id", "kind", "title", "ships", "started", "ended", "outcome", "error",
             "lines", "dismissed")


def _operation(row: tuple[Any, ...], log: str | None = None) -> Operation:
    values = dict(zip(OP_FIELDS, row, strict=True))
    try:
        ships = tuple(str(s) for s in json.loads(values["ships"]))
    except (TypeError, ValueError):
        ships = ()
    lines: tuple[str, ...] = ()
    if log is not None:
        try:
            lines = tuple(str(line) for line in json.loads(log))
        except (TypeError, ValueError):
            lines = ()
    return Operation(id=int(values["id"]), kind=str(values["kind"]),
                     title=str(values["title"]), ships=ships,
                     started=float(values["started"]), ended=float(values["ended"]),
                     outcome=values["outcome"] if values["outcome"] in OUTCOMES else "failed",
                     error=values["error"], log=lines, lines=int(values["lines"]),
                     dismissed=bool(values["dismissed"]))


class Operations:
    """The finished-operations store, in the history file. Safe to share between
    threads. Like History, errors are the caller's to ignore: a broken file must
    never stop a job or a page."""

    def __init__(self, path: Path | None = None, *, keep: float = OPS_KEEP,
                 keep_count: int = OPS_KEEP_COUNT, wall=time.time) -> None:
        self.path = path or history_path()
        self.keep = keep
        self.keep_count = keep_count
        self.wall = wall

    def _db(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        return _connect(self.path)

    # --- writing -----------------------------------------------------------
    def save(self, op: Operation) -> Operation:
        """Save (or replace) op, scrubbed; prune what is past keep or keep_count.
        Returns what was saved."""
        op = scrubbed(op)
        with self._db() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO operation ({', '.join(OP_FIELDS)}, log) "
                f"VALUES ({', '.join('?' * (len(OP_FIELDS) + 1))})",
                (op.id, op.kind, op.title, json.dumps(list(op.ships)), op.started,
                 op.ended, op.outcome, op.error, op.lines, int(op.dismissed),
                 json.dumps(list(op.log))))
            conn.execute("DELETE FROM operation WHERE ended < ?",
                         (self.wall() - self.keep,))
            conn.execute("DELETE FROM operation WHERE id NOT IN (SELECT id FROM operation "
                         "ORDER BY ended DESC, id DESC LIMIT ?)", (self.keep_count,))
        return op

    def dismiss(self, ids: Iterable[int]) -> None:
        ids = sorted(set(ids))
        if not ids or not self.path.exists():
            return
        with self._db() as conn:
            conn.execute(f"UPDATE operation SET dismissed = 1 WHERE id IN "
                         f"({', '.join('?' * len(ids))})", ids)

    # --- reading -----------------------------------------------------------
    def last_id(self) -> int:
        if not self.path.exists():
            return 0
        with self._db() as conn:
            (found,) = conn.execute("SELECT MAX(id) FROM operation").fetchone()
        return int(found or 0)

    def find(self, *, ship: str | None = None, outcome: str | None = None,
             before: int | None = None, limit: int = 50) -> list[Operation]:
        """Newest first (by id, which is the order they started), without logs."""
        query = f"SELECT {', '.join(OP_FIELDS)} FROM operation WHERE 1"
        args: list[Any] = []
        if ship is not None:
            query += " AND EXISTS (SELECT 1 FROM json_each(operation.ships) WHERE value = ?)"
            args.append(ship)
        if outcome is not None:
            query += " AND outcome = ?"
            args.append(outcome)
        if before is not None:
            query += " AND id < ?"
            args.append(before)
        query += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        if not self.path.exists():
            return []
        with self._db() as conn:
            return [_operation(row) for row in conn.execute(query, args)]

    def get(self, op_id: int) -> Operation | None:
        if not self.path.exists():
            return None
        with self._db() as conn:
            row = conn.execute(f"SELECT {', '.join(OP_FIELDS)}, log FROM operation "
                               "WHERE id = ?", (op_id,)).fetchone()
        return None if row is None else _operation(row[:-1], row[-1])

    def ships(self) -> list[str]:
        """Every Ship named in a saved operation, sorted."""
        if not self.path.exists():
            return []
        with self._db() as conn:
            rows = conn.execute("SELECT DISTINCT value FROM operation, "
                                "json_each(operation.ships)").fetchall()
        return sorted(str(r[0]) for r in rows)
