"""Stop the gpg-agents the test run started, and fail the run if one survives.

Generating a key in a throwaway gnupg home starts a gpg-agent for that home. The
agent outlives the test, keeps the temp folder busy and leaves a socket folder under
/run/user/$UID/gnupg behind. At the end of the session this plugin finds every
gnupg home under the run's base temp folder, stops its agent and removes its socket
folder. Only this run's base temp folder is touched, so test runs going on at the
same time on the same machine are left alone.

Loaded with `-p` from pytest.ini rather than from a conftest.py: both test folders
already have a conftest module, and a third would add to that name clash.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
