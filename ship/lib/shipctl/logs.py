"""`ship-ctl logs SOURCE`: recent log lines, by source name rather than unit name."""

from __future__ import annotations

from collections import deque

from .context import Context
from .errors import CtlError
from .homefs import UnsafePath
from .record import LOG_TAG

SOURCES = ("provision", "herdr", "core", "resume", "tools", "status", "system")

# herdr-core has no log command. It runs itself as transient user units
# (hc-serve-*, hc-hold-*) whose stderr systemd appends to files in its state folder
# (default session), so only the boot unit that starts it is in the journal.
CORE_STATE = ".local/state/herdr-core/default"
CORE_LOGS = ("core.log", "holder.log")
CORE_LOG_BYTES = 4 << 20


def _tail(path, lines: int) -> list[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in deque(fh, maxlen=lines)]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CtlError("internal", f"cannot read {path.name}: {exc.strerror}") from exc


def _home_tail(ctx: Context, rel: str, lines: int) -> list[str]:
    """The last lines of a file in the agent's home, read without following links."""
    try:
        got = ctx.home.read_tail(rel, CORE_LOG_BYTES)
    except (UnsafePath, OSError):
        return []
    if got is None:
        return []
    raw, cut = got
    text = raw.decode("utf-8", "replace").splitlines()
    if cut:
        text = text[1:]  # read from the middle of a line
    return text[-lines:]


def _core(ctx: Context, journal: list[str], lines: int) -> list[str]:
    """ship-core.service's journal (the core starting at boot), then the end of the
    core's and its holders' own logs, each line prefixed with its file. `lines`
    applies to each of the three sources (contracts/ship-ctl.md)."""
    out = list(journal)
    for name in CORE_LOGS:
        out += [f"{name}: {line}" for line in _home_tail(ctx, f"{CORE_STATE}/{name}", lines)]
    return out


def read(ctx: Context, source: str, lines: int) -> list[str]:
    if source not in SOURCES:
        raise CtlError("usage", f"unknown log source {source!r}; one of: {', '.join(SOURCES)}")
    if lines < 1 or lines > 10000:
        raise CtlError("usage", "--lines must be between 1 and 10000")
    if source == "provision":
        return _tail(ctx.paths.logs / "provision.log", lines)
    if source == "tools":
        return _tail(ctx.paths.logs / "tools.log", lines)
    base = ["journalctl", "--no-pager", "-o", "short-iso", "-n", str(lines)]
    # As root, --user-unit would match root's own user units (it adds _UID of the
    # caller), so the agent's units are selected by explicit journal fields.
    ids = ctx.runner.agent_ids()
    agent = [f"_UID={ids[0]}"] if ids else []
    if source == "resume":
        # Agents are resumed (and started again after a crash) by the status refresh,
        # from the agent record: its lines in that unit's journal.
        base[-1] = str(min(10000, lines * 20))
    selector = {
        "herdr": ["_SYSTEMD_USER_UNIT=herdr.service", *agent],
        "core": ["_SYSTEMD_USER_UNIT=ship-core.service", *agent],
        "resume": ["-u", "ship-status.service"],
        "status": ["-u", "ship-status.service"],
        "system": [],
    }[source]
    r = ctx.runner.run([*base, *selector], timeout=15)
    if not r.ok:
        raise CtlError("internal", f"journalctl failed (exit {r.rc})")
    out = [line for line in r.out.splitlines() if not line.startswith("-- No entries --")]
    if source == "resume":
        out = [line for line in out if f"ship-ctl: {LOG_TAG}:" in line][-lines:]
    if source == "core":
        out = _core(ctx, out, lines)
    return out
