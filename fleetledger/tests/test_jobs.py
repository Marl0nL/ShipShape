"""The web UI's background-job runner (web/jobs.py)."""

from __future__ import annotations

import threading
import time

import pytest
from fleetledger.errors import FleetError
from fleetledger.web import jobs as jobs_mod
from fleetledger.web.jobs import BrowserApp, Job, Jobs, run_cli
from test_cli import env  # noqa: F401  (a fixture: the CLI's fake Ship and store)


def wait(job: Job, timeout: float = 10) -> Job:
    end = time.monotonic() + timeout
    while not job.done and time.monotonic() < end:
        time.sleep(0.01)
    assert job.done, "the job never finished"
    return job


# --- the registry ---------------------------------------------------------------------


def test_two_starts_on_the_same_ship_at_once_exactly_one_wins():
    """The busy check and the insert are one critical section."""
    registry = Jobs(app=None)
    release = threading.Event()
    gate = threading.Barrier(8)
    won, refused = [], []

    def go():
        gate.wait()
        try:
            won.append(registry.start("update", ["ship-01"], lambda job: release.wait(5)))
        except FleetError as exc:
            refused.append(exc.message)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(won) == 1 and len(refused) == 7
    assert all("already running" in m for m in refused)
    other = registry.start("update", ["ship-02"], lambda job: None)  # another Ship: fine
    release.set()
    wait(won[0]), wait(other)
    assert not registry.busy(["ship-01"])
    wait(registry.start("reboot", ["ship-01"], lambda job: None))  # free again


class CountingLock:
    """A lock that counts how often it is taken."""

    def __init__(self):
        self.lock, self.taken = threading.Lock(), 0

    def __enter__(self):
        self.lock.acquire()
        self.taken += 1
        return self

    def __exit__(self, *exc):
        self.lock.release()


def test_start_checks_and_inserts_under_one_lock():
    """The race above rarely loses even with two locks taken (check, then insert), so
    say the property itself: starting on a free Ship takes the registry's lock once."""
    registry = Jobs(app=None)
    registry._lock = counting = CountingLock()
    release = threading.Event()
    job = registry.start("update", ["ship-01"], lambda job: release.wait(5))
    assert counting.taken == 1
    release.set()
    wait(job)


def test_finished_jobs_are_pruned_to_the_last_twenty():
    registry = Jobs(app=None)
    for n in range(25):
        wait(registry.start("update", [f"ship-{n:02d}"], lambda job: None))
    release = threading.Event()
    last = registry.start("update", ["ship-99"], lambda job: release.wait(5))  # prunes
    finished = [j for j in registry.recent() if j.done]
    assert len(finished) == jobs_mod.KEEP_FINISHED
    assert finished[-1].ships == ["ship-05"]  # the five oldest went
    assert registry.recent()[0] is last
    release.set()
    wait(last)


def test_versions_move_and_stop_refuses_new_jobs():
    registry = Jobs(app=None)
    release = threading.Event()
    before = registry.version
    said = threading.Event()
    job = registry.start("update", ["ship-01"],
                         lambda job: (said.wait(5), job.say("step"), release.wait(5)))
    job_before = job.version
    said.set()
    for _ in range(100):
        if job.snapshot():
            break
        time.sleep(0.01)
    assert job.version > job_before  # a line moves the job's own version: the card redraws
    assert registry.version > before
    assert registry.stop() == [job]  # cut short: the server says so
    with pytest.raises(FleetError, match="stopping"):
        registry.start("update", ["ship-02"], lambda job: None)
    release.set()
    wait(job)


def test_every_way_a_job_ends_frees_its_ships():
    registry = Jobs(app=None)
    finished = []
    registry.on_finished = finished.append

    def boom(job):
        raise FleetError("Proxmox refused it.", hint="Check the token.")

    job = wait(registry.start("update", ["ship-01"], boom))
    assert job.ok is False and job.error == "Proxmox refused it. Check the token."
    job = wait(registry.start("update", ["ship-01"], lambda j: (_ for _ in ()).throw(
        SystemExit(2))))
    assert job.ok is False and job.error == jobs_mod.REFUSED_ARGV
    job = wait(registry.start("update", ["ship-01"], lambda j: 1 / 0))
    assert job.ok is False and "ZeroDivisionError" in job.error
    job = wait(registry.start("update", ["ship-01"], lambda j: 3))  # an exit code
    assert job.ok is False
    job = wait(registry.start("update", ["ship-01"], lambda j: 0))
    assert job.ok is True
    assert wait(registry.start("update", ["ship-01"], lambda j: True)).ok is True
    assert wait(registry.start("update", ["ship-01"], lambda j: False)).ok is False
    assert finished == [["ship-01"]] * 7 and not registry.busy(["ship-01"])


def test_job_output_is_cleaned_of_terminal_codes():
    job = Job(1, "update", ["ship-01"], "Update")
    job.write("\x1b[31mred\x1b[0m\r\nplain\n\n")
    assert job.snapshot() == ["red", "plain"]


# --- commands from the browser ---------------------------------------------------------


def test_run_cli_hoists_tags_like_the_cli(env):  # noqa: F811
    """`tools add --tag work ID` only parses through cli.main's tag hoist."""
    job = Job(1, "tools", ["ship-01"], "Tools")
    code = run_cli(env.app, ["tools", "add", "--tag", "work", "tasks-axi"], job)
    assert code == 0, job.snapshot()
    assert ["tools", "add", "tasks-axi", "--json"] in [c[1][3:] for c in env.ship.calls]
    assert env.store.load().ship("ship-01").tools[-1] == "tasks-axi"


def test_run_cli_bad_argv_and_fleet_errors(env):  # noqa: F811
    job = Job(1, "x", ["ship-01"], "X")
    assert run_cli(env.app, ["tools", "frobnicate"], job) == 2
    assert jobs_mod.REFUSED_ARGV in job.snapshot()
    job = Job(2, "x", ["ship-01"], "X")
    assert run_cli(env.app, ["status", "no-such-ship"], job) != 0
    assert any(line.startswith("ship-fleet: ") for line in job.snapshot())


def test_a_command_from_the_browser_prints_only_to_its_job(env):  # noqa: F811
    job = Job(1, "x", ["ship-01"], "X")
    env.app.out.seek(0), env.app.out.truncate(), env.app.err.seek(0), env.app.err.truncate()
    assert run_cli(env.app, ["tools", "ls"], job) == 0
    assert any("gh-axi" in line for line in job.snapshot())
    assert env.app.out.getvalue() == "" and env.app.err.getvalue() == ""  # not the terminal


def test_browser_app_keeps_the_apps_hooks_and_has_no_terminal(env):  # noqa: F811
    env.app.proxmox_api = marker = object()  # a hook code reads with getattr
    job = Job(1, "x", [], "X")
    wrapped = BrowserApp(env.app, job)
    assert getattr(wrapped, "proxmox_api", None) is marker
    assert wrapped.store_factory is env.app.store_factory
    assert wrapped.stdin_isatty() is False and wrapped.confirm("Type yes: ") == ""
    wrapped.say("said")
    wrapped.note("noted")
    assert job.snapshot() == ["said", "noted"]
