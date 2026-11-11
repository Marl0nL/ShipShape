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
    # No lock (a run that keeps no folders, or one past its session finish): may be
    # live, left alone.
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
        assert "stopped 1 gpg-agent(s) earlier runs left" in r.stdout

        assert set(leftover_daemons.agents_under(tmp_path.resolve()).values()) == {
            (unlocked / "gnupg0").resolve(), (live / "gnupg0").resolve(),
            (other / "gnupg").resolve(), outside.resolve()}
    finally:
        leftover_daemons.stop_agents(tmp_path)


def test_a_live_runs_folder_is_held(tmp_path):
    folder = tmp_path / "pytest-1"
    folder.mkdir()
    assert leftover_daemons.held_by_live_run(folder)  # no lock: may be live
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


INNER = '''
import subprocess
from pathlib import Path

import pytest


def start_agent(tmp_path, name):
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    subprocess.run(["gpg", "--batch", "--homedir", str(home), "--passphrase", "",
                    "--quick-gen-key", "Test <test@example.com>", "ed25519", "sign", "never"],
                   check=True, capture_output=True)
    socketdir = subprocess.run(["gpgconf", "--homedir", str(home), "--list-dirs", "socketdir"],
                               check=True, capture_output=True, text=True).stdout.strip()
    (Path(OUT) / name).write_text(socketdir)


def test_passes(tmp_path):
    start_agent(tmp_path, "passes")


def test_fails(tmp_path):
    start_agent(tmp_path, "fails")
    assert False


@pytest.fixture
def broken(tmp_path):
    start_agent(tmp_path, "setup-error")
    raise RuntimeError("setup fails after starting an agent")


def test_setup_error(broken):
    pass
'''


def test_a_run_keeps_only_failed_tests_folders_and_leaves_no_socket_folders(tmp_path):
    """With the repo's pytest.ini, a passing test's tmp_path is deleted at its teardown
    and a failing test's is kept; none leaves its agent or socket folder, not even
    one whose fixture fails after starting an agent."""
    repo = Path(__file__).resolve().parents[2]
    out, basetemp = tmp_path / "out", tmp_path / "basetemp"
    out.mkdir()
    inner = tmp_path / "inner" / "test_inner.py"
    inner.parent.mkdir()
    inner.write_text(f"OUT = {str(out)!r}\n" + INNER)
    r = subprocess.run([sys.executable, "-m", "pytest", "-c", str(repo / "pytest.ini"),
                        "--rootdir", str(repo), "--basetemp", str(basetemp),
                        "-p", "no:cacheprovider", "-q", str(inner)],
                       capture_output=True, text=True, cwd=repo, timeout=120)
    assert "1 failed, 1 passed, 1 error" in r.stdout, r.stdout + r.stderr

    assert [p.name for p in basetemp.iterdir() if not p.is_symlink()] == ["test_fails0"]
    assert (basetemp / "test_fails0" / "gnupg").is_dir()
    assert leftover_daemons.agents_under(basetemp.resolve()) == {}
    for name in ("passes", "fails", "setup-error"):
        socketdir = Path((out / name).read_text())
        if not socketdir.is_relative_to(basetemp):  # gpg keeps sockets in /run/user when it can
            assert not socketdir.exists(), name
