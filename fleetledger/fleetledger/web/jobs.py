"""Background jobs: every long action the web UI starts (update, reboot, join, making a
Ship, and later destroy, snapshots ...) runs here, in one registry.

- One code path: a job runs the CLI's own command function or shared operation.
  `run_cli` goes through `cli.main` itself, which hoists `--tag`, turns a FleetError
  into notes and an exit code, and finds commands in every command module.
- A Ship is busy while a job on it runs. Checking that and adding the new job is one
  critical section, so two tabs posting at once cannot both start one.
- What a command prints goes to its job (redacted), never to the terminal running
  `ship-fleet ui`: BrowserApp wraps the app (a copy would drop the hooks code reads
  with getattr) and points out, err, say and note at the job.
- A job's thread catches everything (argparse's SystemExit included), so a job always
  ends and frees its Ships. Jobs live in memory only: the inventory, not a job, says
  what state a Ship is in after a restart.
"""

from __future__ import annotations

import dataclasses
import itertools
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from fleetledger.errors import FleetError

KEEP_FINISHED = 20
MAX_LINES = 400
REFUSED_ARGV = "The UI built a command the CLI refused (a bug in the UI)."


@dataclasses.dataclass
class Job:
    id: int
    kind: str  # update | reboot | join | create | ...
    ships: list[str]
    title: str
    started: float = dataclasses.field(default_factory=time.time)
    lines: deque[str] = dataclasses.field(default_factory=lambda: deque(maxlen=MAX_LINES))
    done: bool = False
    ok: bool | None = None
    error: str | None = None
    outcomes: list[Any] = dataclasses.field(default_factory=list)  # actions.Outcome
    result: dict[str, Any] = dataclasses.field(default_factory=dict)
    version: int = 0
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)

    # A file-like sink: a command's app.out / app.err can be the job.
    def write(self, text: str) -> int:
        from fleetledger.concierge.redact import clean

        with self._lock:
            for line in clean(str(text)).splitlines():
                if line.strip():
                    self.lines.append(line)
            self.version += 1
        return len(text)

    def flush(self) -> None:
        pass

    def say(self, text: str) -> None:
        self.write(text + "\n")

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.lines)

    @property
    def running(self) -> bool:
        return not self.done


class BrowserApp:
    """The app, for a command run from the browser: output to the job, no terminal
    (so a key that must be pasted, or any prompt, is refused). A wrapper, not a copy:
    every hook set on the app (an API factory, a signer ...) comes along."""

    def __init__(self, app: Any, sink: Any) -> None:
        self._app = app
        self.out = sink
        self.err = sink

    def say(self, text: str = "") -> None:
        self.out.write(text + "\n")

    def note(self, text: str) -> None:
        self.err.write(text + "\n")

    def stdin_isatty(self) -> bool:
        return False

    def confirm(self, prompt: str) -> str:
        return ""

    def __getattr__(self, name: str) -> Any:
        return getattr(self._app, name)


def run_cli(app: Any, argv: list[str], job: Any) -> int:
    """`ship-fleet ARGV` with its output in the job. A bad argv (argparse's SystemExit)
    becomes a failed job, not a thread that never ends."""
    from fleetledger import cli

    try:
        return cli.main(list(argv), BrowserApp(app, job))
    except SystemExit as exc:
        job.write(REFUSED_ARGV + "\n")
        return int(exc.code) if isinstance(exc.code, int) and exc.code else 2


class Jobs:
    """Every job the web UI started, newest first; at most KEEP_FINISHED finished ones."""

    def __init__(self, app: Any, on_finished: Callable[[list[str]], None] | None = None):
        self.app = app
        self.on_finished = on_finished or (lambda ships: None)
        self._lock = threading.Lock()
        self._jobs: list[Job] = []
        self._ids = itertools.count(1)
        self._stopping = False

    @property
    def version(self) -> int:
        with self._lock:
            return sum(j.version for j in self._jobs) + len(self._jobs)

    def recent(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs))

    def get(self, job_id: int) -> Job | None:
        with self._lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    def for_ship(self, name: str) -> list[Job]:
        return [j for j in self.recent() if name in j.ships]

    def _busy_locked(self, ships: list[str]) -> bool:
        return any(j.running and set(j.ships) & set(ships) for j in self._jobs)

    def busy(self, ships: list[str]) -> bool:
        with self._lock:
            return self._busy_locked(ships)

    def start(self, kind: str, ships: list[str], run: Callable[[Job], Any],
              title: str = "") -> Job:
        """Start run(job) in the background. Refused if one of the Ships is busy: the
        check and the insert are one critical section."""
        with self._lock:
            if self._stopping:
                raise FleetError("FleetLedger is stopping; try again once it is back.")
            if self._busy_locked(ships):
                raise FleetError("Something is already running on one of those Ships; "
                                 "wait for it to finish.")
            job = Job(next(self._ids), kind, list(ships), title or kind.capitalize())
            self._jobs.append(job)
            finished = [j for j in self._jobs if j.done]
            for old in finished[: max(0, len(finished) - KEEP_FINISHED)]:
                self._jobs.remove(old)
        threading.Thread(target=self._run, args=(job, run), name=f"job-{kind}-{job.id}",
                         daemon=True).start()
        return job

    def _run(self, job: Job, run: Callable[[Job], Any]) -> None:
        try:
            outcome = run(job)
            if job.ok is None:  # run() may decide; else an exit code or a plain return
                exit_code = isinstance(outcome, int) and not isinstance(outcome, bool)
                job.ok = outcome == 0 if exit_code else outcome is not False
        except KeyboardInterrupt:
            job.ok, job.error = False, "Interrupted."
            raise
        except FleetError as exc:
            job.ok = False
            job.error = exc.message + (f" {exc.hint}" if exc.hint else "")
        except SystemExit:
            job.ok, job.error = False, REFUSED_ARGV
        except BaseException as exc:  # noqa: BLE001 - shown on the page, never raised here
            job.ok, job.error = False, f"Internal error ({type(exc).__name__})."
        finally:
            job.done = True
            job.version += 1
            try:
                self.on_finished(job.ships)
            except Exception:  # noqa: BLE001 - a status refresh must not hide the outcome
                pass

    def stop(self) -> list[Job]:
        """At shutdown: refuse new jobs, and return those cut short (their threads die
        with the process)."""
        with self._lock:
            self._stopping = True
            return [j for j in self._jobs if j.running]


class _RecordingDriver:
    """A driver whose lifecycle answers are kept on the job, for its card."""

    def __init__(self, driver: Any, job: Any) -> None:
        self._driver = driver
        self._job = job

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._driver, name)
        if not callable(attr):
            return attr

        def call(*args: Any, **kwargs: Any) -> Any:
            result = attr(*args, **kwargs)
            from fleetledger.drivers.base import Outcome

            if isinstance(result, Outcome):
                self._job.result["outcome"] = result
                if not result.done:  # left to you: the card says "by hand", not "done"
                    self._job.result["by_hand"] = True
            return result

        return call


class RecordingApp:
    """The app for one job running a CLI command that calls a driver: the driver's
    answer (an Outcome) is kept on the job (job.result["outcome"], and "by_hand"
    when it is left to you), so _job.html shows it through _outcome.html."""

    def __init__(self, app: Any, job: Any) -> None:
        self._app = app
        self._job = job

    def driver_factory(self, inventory: Any, driver_id: str) -> Any:
        return _RecordingDriver(self._app.driver_factory(inventory, driver_id), self._job)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._app, name)
