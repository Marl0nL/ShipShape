"""Stop the gpg-agents the test run started, and fail the run if one survives.

Generating a key in a throwaway gnupg home starts a gpg-agent for that home. The
agent outlives the test, keeps the temp folder busy and leaves a socket folder under
/run/user/$UID/gnupg behind. At the end of the session this plugin finds every
gnupg home under the run's base temp folder, stops its agent and removes its socket
folder. Only this run's base temp folder is touched, so test runs going on at the
same time on the same machine are left alone.

A run ended by SIGTERM (a timeout, an agent's tool limit) would skip the end of the
session, so the plugin turns SIGTERM into an interrupt, as Ctrl-C is: pytest then
still finishes the session. A run killed outright (SIGKILL) still leaks, so at the
start of a session the plugin also stops the agents earlier runs left: this user's
gpg-agents whose home is in a `pytest-N` folder of this user's pytest root whose
`.lock` names a process that has gone. A folder with no lock counts as live: pytest
writes none when it keeps no folders, and removes it at session finish, while the
run is still going.

Loaded with `-p` from pytest.ini rather than from a conftest.py: both test folders
already have a conftest module, and a third would add to that name clash.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

# Files only a gnupg home has.
GNUPG_MARKERS = ("pubring.kbx", "trustdb.gpg", "private-keys-v1.d")


def gnupg_homes(root: Path) -> set[Path]:
    homes = set()
    for here, dirs, files in os.walk(root):
        if any(m in files or m in dirs for m in GNUPG_MARKERS):
            homes.add(Path(here))
            dirs.clear()
    return homes


def agents_under(root: Path) -> dict[int, Path]:
    """Running gpg-agents whose --homedir is inside root: {pid: homedir}."""
    found = {}
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or os.path.basename(argv[0]) != b"gpg-agent":
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
        except OSError:
            continue
        try:
            home = Path(os.fsdecode(argv[argv.index(b"--homedir") + 1]))
        except (ValueError, IndexError):
            continue
        if home == root or root in home.parents:
            found[int(entry.name)] = home
    return found


def _gpgconf(home: Path, *args: str) -> None:
    subprocess.run(["gpgconf", "--homedir", str(home), *args],
                   capture_output=True, check=False, timeout=30)


def stop_agents(root: Path, wait: float = 10.0) -> dict[int, Path]:
    """Stop every gpg-agent for a gnupg home under root and remove its socket
    folder. Returns the agents still running afterwards."""
    root = root.resolve()
    live = agents_under(root)
    homes = gnupg_homes(root) | set(live.values())
    for home in homes:
        _gpgconf(home, "--kill", "all")
    # --kill returns before the agent has gone, and the socket folder can only be
    # removed once its sockets are.
    deadline = time.monotonic() + wait
    while live and time.monotonic() < deadline:
        time.sleep(0.05)
        live = {pid: home for pid, home in live.items() if Path(f"/proc/{pid}").exists()}
    for home in homes:
        _gpgconf(home, "--remove-socketdir")
    return agents_under(root)


def temp_root(config) -> Path | None:
    """This user's pytest root, the folder pytest makes its numbered `pytest-N` base
    temp folders in (computed as pytest does, without making one). None with
    --basetemp, where there is no such root."""
    if config.getoption("basetemp", None):
        return None
    from_env = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    temproot = Path(from_env or tempfile.gettempdir()).resolve()
    try:
        import getpass

        user = getpass.getuser()
    except (ImportError, OSError, KeyError):
        user = "unknown"
    return temproot / f"pytest-of-{user}"


NUMBERED = re.compile(r"pytest-\d+")


def held_by_live_run(folder: Path) -> bool:
    """Whether a live pytest may hold this `pytest-N` folder. pytest writes its pid
    to `.lock` when it makes the folder, unless it keeps no folders, and removes the
    lock at session finish. Only a lock naming a process that has gone means the
    run is over: anything else (no lock, one that cannot be read, a pid that cannot
    be signalled) counts as held."""
    try:
        text = (folder / ".lock").read_text()
    except OSError:
        return True
    try:
        pid = int(text.strip())
    except ValueError:
        return True
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _pid_depth(pid: int | str) -> int | None:
    """How many pid namespaces deep a process is, from /proc/<pid>/status (gpg-agent
    is not dumpable, so its /proc/<pid>/ns cannot be read)."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("NSpid:"):
                return len(line.split()) - 1
    except OSError:
        pass
    return None


def stale_agents(root: Path) -> dict[int, Path]:
    """This user's gpg-agents whose home is inside a `pytest-N` folder of root that
    no live pytest holds: {pid: homedir}. Agents in another pid namespace (a
    sandboxed run) are left alone: the pid in their run's lock means nothing here.
    /proc shows only our namespace and those below it, so one deeper than ours is
    another."""
    stale = {}
    ours = _pid_depth("self")
    for pid, home in agents_under(root).items():
        if ours is None or _pid_depth(pid) != ours:
            continue
        top = home.relative_to(root).parts[:1]
        if top and NUMBERED.fullmatch(top[0]) and not held_by_live_run(root / top[0]):
            stale[pid] = home
    return stale


def stop_stale_agents(root: Path, wait: float = 10.0) -> tuple[dict[int, Path], dict[int, Path]]:
    """Stop the agents earlier runs left under root (see stale_agents) and remove
    their socket folders. Their homes may be half deleted by pytest's own cleanup, so
    each agent is sent SIGTERM (it then shuts down as `gpgconf --kill` asks it to).
    Returns ({pid: home} stopped, {pid: home} still running)."""
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
        return {}, {}
    root = root.resolve()
    stale = stale_agents(root)
    if not stale:
        return {}, {}
    for pid, home in stale.items():
        # Only if it is still the same agent (pids are reused).
        if agents_under(home).get(pid) == home:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    deadline = time.monotonic() + wait
    live = dict(stale)
    while live and time.monotonic() < deadline:
        time.sleep(0.05)
        live = {pid: home for pid, home in live.items() if Path(f"/proc/{pid}").exists()}
    if shutil.which("gpgconf") is not None:
        for home in set(stale.values()):
            _gpgconf(home, "--remove-socketdir")
    left = {pid: home for pid, home in agents_under(root).items() if pid in stale}
    return {pid: home for pid, home in stale.items() if pid not in left}, left


def _interrupt_on_sigterm(signum, frame):
    # A second SIGTERM ends the run at once; the next run's start stops what it leaks.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    raise KeyboardInterrupt("SIGTERM")


_previous_sigterm = None


def pytest_configure(config):
    global _previous_sigterm
    # Only where nobody else handles SIGTERM, and only the main thread may set it.
    if threading.current_thread() is not threading.main_thread():
        return
    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        return
    _previous_sigterm = signal.signal(signal.SIGTERM, _interrupt_on_sigterm)


def pytest_unconfigure(config):
    global _previous_sigterm
    if _previous_sigterm is not None:
        if signal.getsignal(signal.SIGTERM) is _interrupt_on_sigterm:
            signal.signal(signal.SIGTERM, _previous_sigterm)
        _previous_sigterm = None


def pytest_sessionstart(session):
    root = temp_root(session.config)
    if root is None:
        return
    stopped, left = stop_stale_agents(root)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    if stopped:
        reporter.write_line(
            f"leftover_daemons: stopped {len(stopped)} gpg-agent(s) earlier runs left", yellow=True)
    for pid, home in sorted(left.items()):
        reporter.write_line(
            f"leftover_daemons: could not stop pid {pid}, --homedir {home}", red=True)


# tryfirst: before pytest's own sessionfinish, which may delete the base temp folder.
@pytest.hookimpl(tryfirst=True)
def pytest_sessionfinish(session):
    if shutil.which("gpgconf") is None:
        return
    # Only when a test asked for a temp folder: getbasetemp() would make one.
    factory = getattr(session.config, "_tmp_path_factory", None)
    if factory is None or getattr(factory, "_basetemp", None) is None:
        return
    left = stop_agents(factory.getbasetemp())
    if left:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(
                f"leftover_daemons: {len(left)} gpg-agent(s) still running after the tests:",
                red=True)
            for pid, home in sorted(left.items()):
                reporter.write_line(f"  pid {pid}, --homedir {home}", red=True)
        if session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
