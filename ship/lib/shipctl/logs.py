"""`ship-ctl logs SOURCE`: recent log lines, by source name rather than unit name."""

from __future__ import annotations

from collections import deque

from .context import Context
from .errors import CtlError

SOURCES = ("provision", "herdr", "resume", "tools", "status", "system")


def _tail(path, lines: int) -> list[str]:
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            return [line.rstrip("\n") for line in deque(fh, maxlen=lines)]
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise CtlError("internal", f"cannot read {path.name}: {exc.strerror}") from exc


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
    selector = {
        "herdr": ["--user-unit", "herdr.service"],
        "resume": ["--user-unit", "ship-resume.service"],
        "status": ["-u", "ship-status.service"],
        "system": [],
    }[source]
    r = ctx.runner.run([*base, *selector], timeout=15)
    if not r.ok:
        raise CtlError("internal", f"journalctl failed (exit {r.rc})")
    return [line for line in r.out.splitlines() if not line.startswith("-- No entries --")]
