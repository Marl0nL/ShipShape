"""Running a quick `ship-fleet` command from the web UI, beside web.jobs (whose
RecordingApp keeps a driver's answer on a job, for its card).

- run_now: a quick command (one that only edits the inventory, such as
  `image use`) run straight away, its output (redacted) returned for a flash. It
  goes through jobs.run_cli, so it is the CLI's own command, checks and messages.
"""

from __future__ import annotations

from typing import Any


class _Lines:
    """A sink for run_now: what the command printed, cleaned of terminal codes."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def write(self, text: str) -> int:
        from fleetledger.concierge.redact import clean

        self.lines += [line for line in clean(str(text)).splitlines() if line.strip()]
        return len(text)

    def flush(self) -> None:
        pass


def run_now(app: Any, argv: list[str]) -> tuple[int, list[str]]:
    """`ship-fleet ARGV` now (call it in a worker thread): its exit code and lines."""
    from fleetledger.web import jobs

    sink = _Lines()
    code = jobs.run_cli(app, argv, sink)
    return code, sink.lines
