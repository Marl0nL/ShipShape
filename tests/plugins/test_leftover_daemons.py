"""The plugin that stops the gpg-agents tests leave behind."""

import getpass
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import leftover_daemons
import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("gpg") and shutil.which("gpgconf")), reason="needs gpg and gpgconf")


def start_agent(home: Path) -> Path:
    """Make a gnupg home with a key in it (which starts its agent); returns its
    socket folder."""
    home.mkdir(mode=0o700, parents=True)
    subprocess.run(["gpg", "--batch", "--homedir", str(home), "--passphrase", "",
                    "--quick-gen-key", "Test <test@example.com>", "ed25519", "sign", "never"],
                   check=True, capture_output=True)
    return Path(subprocess.run(["gpgconf", "--homedir", str(home), "--list-dirs", "socketdir"],
                               check=True, capture_output=True, text=True).stdout.strip())


def test_stops_the_agents_under_the_folder_and_removes_their_socket_folders(tmp_path):
    root = tmp_path / "run"
    socketdir = start_agent(root / "test0" / "gpg")
    socketdir2 = start_agent(root / "deeper" / "test1" / "gnupg")
    assert set(leftover_daemons.agents_under(root.resolve()).values()) == {
        (root / "test0" / "gpg").resolve(), (root / "deeper" / "test1" / "gnupg").resolve()}

    assert leftover_daemons.stop_agents(root) == {}
    assert leftover_daemons.agents_under(root.resolve()) == {}
    if socketdir != root / "test0" / "gpg":  # gpg keeps sockets in /run/user when it can
        assert not socketdir.exists() and not socketdir2.exists()


def test_leaves_agents_outside_the_folder_alone(tmp_path):
    outside = tmp_path / "other-run" / "gpg"
    start_agent(outside)
    try:
        leftover_daemons.stop_agents(tmp_path / "this-run-is-empty")
        assert list(leftover_daemons.agents_under(outside.resolve()).values()) == [
            outside.resolve()]
    finally:
        leftover_daemons.stop_agents(outside)


def test_a_folder_with_no_gnupg_home_is_fine(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert leftover_daemons.gnupg_homes(tmp_path) == set()
    assert leftover_daemons.stop_agents(tmp_path) == {}


# A test file for a pytest run in a subprocess: it makes a key (which starts an agent
# under the run's base temp folder), says so, then waits to be killed.
KEY_THEN_WAIT = '''
import subprocess, time

def test_key_then_wait(tmp_path):
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    subprocess.run(["gpg", "--batch", "--homedir", str(home), "--passphrase", "",
                    "--quick-gen-key", "Test <test@example.com>", "ed25519", "sign", "never"],
                   check=True, capture_output=True)
    open("{ready}", "w").close()
    time.sleep(120)
'''

NOTHING = "def test_nothing():\n    pass\n"


def inner_run(tmp_path: Path, test_source: str) -> tuple[list[str], dict, Path]:
    """argv, env and pytest root for a pytest run in a subprocess, isolated in
    tmp_path: its own pytest.ini (with this plugin) and its own TMPDIR."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pytest.ini").write_text(
        "[pytest]\naddopts = -p leftover_daemons -p no:cacheprovider\n")
    (project / "test_inner.py").write_text(test_source)
    temproot = tmp_path / "tmp"
    temproot.mkdir(exist_ok=True)
    env = dict(os.environ, TMPDIR=str(temproot),
               PYTHONPATH=str(Path(leftover_daemons.__file__).parent))
    env.pop("PYTEST_DEBUG_TEMPROOT", None)
    env.pop("PYTEST_ADDOPTS", None)
    argv = [sys.executable, "-m", "pytest", "-q", str(project / "test_inner.py")]
    return argv, env, temproot.resolve() / f"pytest-of-{getpass.getuser()}"


def test_a_run_ended_by_sigterm_still_stops_its_agents(tmp_path):
    ready = tmp_path / "ready"
    argv, env, root = inner_run(tmp_path, KEY_THEN_WAIT.replace("{ready}", str(ready)))
    proc = subprocess.Popen(argv, env=env, cwd=tmp_path / "project",
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 120
        while not ready.exists():
            assert proc.poll() is None, proc.stdout.read().decode()
            assert time.monotonic() < deadline, "the inner run never made its key"
            time.sleep(0.1)
        assert leftover_daemons.agents_under(root), "making the key started no agent"

        proc.send_signal(signal.SIGTERM)
        out = proc.communicate(timeout=60)[0].decode()
        assert proc.returncode == pytest.ExitCode.INTERRUPTED, out
        assert leftover_daemons.agents_under(root) == {}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        leftover_daemons.stop_agents(tmp_path)


def dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_the_next_run_stops_agents_an_earlier_run_left(tmp_path):
    argv, env, root = inner_run(tmp_path, NOTHING)
    # Killed outright: its lock names a process that has gone.
    killed = root / "pytest-3"
    start_agent(killed / "test_a0" / "gnupg")
    (killed / ".lock").write_text(str(dead_pid()))
    # Gone after pytest removed its lock (as at a normal exit).
    unlocked = root / "pytest-4"
    start_agent(unlocked / "gnupg0")
    # A run still going (this one): left alone.
    live = root / "pytest-5"
    start_agent(live / "gnupg0")
    (live / ".lock").write_text(str(os.getpid()))
    # Not a pytest-N folder, and not under the root: left alone.
    other = root / "elsewhere"
    start_agent(other / "gnupg")
    outside = tmp_path / "outside" / "gnupg"
    start_agent(outside)
    try:
        homes = set(leftover_daemons.agents_under(tmp_path.resolve()).values())
        assert len(homes) == 5

        r = subprocess.run(argv, env=env, cwd=tmp_path / "project",
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stdout + r.stderr
        assert "stopped 2 gpg-agent(s) earlier runs left" in r.stdout

        assert set(leftover_daemons.agents_under(tmp_path.resolve()).values()) == {
            (live / "gnupg0").resolve(), (other / "gnupg").resolve(), outside.resolve()}
    finally:
        leftover_daemons.stop_agents(tmp_path)


def test_a_live_runs_folder_is_held(tmp_path):
    folder = tmp_path / "pytest-1"
    folder.mkdir()
    assert not leftover_daemons.held_by_live_run(folder)
    (folder / ".lock").write_text(str(os.getpid()))
    assert leftover_daemons.held_by_live_run(folder)
    (folder / ".lock").write_text(str(dead_pid()))
    assert not leftover_daemons.held_by_live_run(folder)
    (folder / ".lock").write_text("")  # being written: counts as held
    assert leftover_daemons.held_by_live_run(folder)


def test_the_pytest_root_is_where_pytest_makes_its_folders(request, tmp_path_factory):
    root = leftover_daemons.temp_root(request.config)
    if root is None:
        pytest.skip("--basetemp: no pytest root")
    assert root == tmp_path_factory.getbasetemp().parent
