"""Running `ship-fleet` commands from the web UI, beside web.jobs.

- RecordingApp: the app for one job, whose drivers keep their answer (an Outcome)
  on the job (`job.result["outcome"]`), so the job's card can show it with
  _outcome.html: a by-hand driver's steps, for instance.
- run_now: a quick command (one that only edits the inventory, such as
  `image use`) run straight away, its output (redacted) returned for a flash. It
  goes through jobs.run_cli, so it is the CLI's own command, checks and messages.
"""

from __future__ import annotations

from typing import Any

from fleetledger.drivers.base import Outcome


class _Recording:
    """A driver whose answers are kept on the job."""

    def __init__(self, driver: Any, job: Any) -> None:
        self._driver = driver
        self._job = job

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._driver, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            if isinstance(result, Outcome):
                self._job.result["outcome"] = result
                if not result.done:  # left to you: the card says "by hand", not "done"
                    self._job.result["by_hand"] = True
            return result

        return call


class RecordingApp:
    """The app for one job: every driver it makes records its answers on the job."""

    def __init__(self, app: Any, job: Any) -> None:
        self._app = app
        self._job = job

    def driver_factory(self, inventory: Any, driver_id: str) -> Any:
        return _Recording(self._app.driver_factory(inventory, driver_id), self._job)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._app, name)


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
