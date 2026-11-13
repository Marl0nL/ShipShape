"""Shared fixtures: a private FleetLedger home and a small fake ShipShape repo.

Nothing here touches your real ~/.config/fleetledger, keys or network. Helpers
live in fl_harness.py: import them from there, never from conftest. The network guard
every test runs under, and its opt-in markers, are in fl_netguard.py.
"""

from __future__ import annotations

import shutil
import textwrap
import time
from pathlib import Path

import fl_netguard
import pytest
from fl_harness import (
    CONTRACTS,
    INVENTORY,
    REPO_ROOT,
    FakeSSH,
    NoKeyring,
    SSHAgents,
    keygen,
    short_folder,
)
from fleetledger.repo import Repo
from fleetledger.store import TomlFileStore


@pytest.fixture(autouse=True, scope="session")
def own_short_socket_root():
    """paths.runtime_socket's short /tmp folder, for a runtime folder too long for a
    socket, made under a private folder of this test process instead of /tmp itself:
    never your real one, and never shared with another test worker running at the
    same time (xdist's longer temp paths make that fallback likelier)."""
    from fleetledger import paths

    root = short_folder()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(paths, "SHORT_SOCKET_ROOT", root)
        yield root
    shutil.rmtree(root, ignore_errors=True)


HERE = Path(__file__).resolve().parent


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_resolver: may look names up for real (fl_netguard.py)")
    config.addinivalue_line(
        "markers", "local_sshd: may run real ssh to a loopback sshd (fl_netguard.py)")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """The network guard (fl_netguard.py) for a FleetLedger test's whole run: its
    module and session fixtures' setup and teardown too, not just the test. Only for
    tests in this folder: a conftest hook sees every test of the run."""
    if HERE not in item.path.parents:
        return (yield)
    fl_netguard.start(item.nodeid)
    try:
        return (yield)
    finally:
        fl_netguard.stop()


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """No DNS, no connection to another machine, no real ssh (fl_netguard.py): a test
    that tries fails, naming what it tried, even when a background thread swallowed
    the error. cli._resolves is a fake unless the test is marked real_resolver."""
    from fleetledger import cli

    guard = fl_netguard.active() or fl_netguard.start(request.node.nodeid)
    guard.real_resolver = request.node.get_closest_marker("real_resolver") is not None
    guard.local_sshd = request.node.get_closest_marker("local_sshd") is not None
    if not guard.real_resolver:
        monkeypatch.setattr(cli, "_resolves", fl_netguard.fake_resolves)
    yield guard
    guard.real_resolver = guard.local_sshd = False  # the opt-ins end with the test
    if guard.refused:
        pytest.fail("The test reached for the network:\n  " + "\n  ".join(guard.refused)
                    + "\n" + fl_netguard.HELP, pytrace=False)


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch):
    from fleetledger.overlay import secret_store

    monkeypatch.setattr(secret_store, "get_keyring", lambda: NoKeyring())


@pytest.fixture(autouse=True)
def fast_keys(monkeypatch):
    """Managed keys made with 1 bcrypt round instead of ssh-keygen's default, and no
    pause when a pexpect-driven ssh-keygen or ssh-add closes: the same steps, much
    faster (as test_vault.py's `fast` lowers the vault's scrypt cost).

    With no pause, a child still running at close (one asked for a passphrase it
    never gets) may not be reaped yet just after its SIGKILL on a busy machine (tests
    in parallel workers), and ptyprocess then says it could not terminate it; so a
    forced close waits for the kill to land instead."""
    from fleetledger import keys

    monkeypatch.setattr(keys, "KDF_ROUNDS", 1)
    spawn = keys._spawn

    def quick(*args, **kwargs):
        child = spawn(*args, **kwargs)
        for proc in (child, child.ptyproc):
            proc.delayafterclose = 0
            proc.delayafterterminate = 0
        pty, terminate = child.ptyproc, child.ptyproc.terminate

        def terminate_and_wait(force=False):
            if terminate(force) or not force:
                return not pty.isalive()  # done, or not asked to kill
            deadline = time.monotonic() + 10
            while pty.isalive() and time.monotonic() < deadline:
                time.sleep(0.01)
            return not pty.isalive()

        pty.terminate = terminate_and_wait
        return child

    monkeypatch.setattr(keys, "_spawn", quick)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """FleetLedger config/data/runtime dirs under tmp, with throwaway keys."""
    config = tmp_path / "config"
    data = tmp_path / "data"
    runtime = tmp_path / "run"
    keys = tmp_path / "keys"
    for d in (config, data, runtime, keys):
        d.mkdir(mode=0o700)
    monkeypatch.setenv("FLEETLEDGER_CONFIG_DIR", str(config))
    monkeypatch.setenv("FLEETLEDGER_DATA_DIR", str(data))
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(runtime))
    keygen(keys / "fleet_admin", "fleet-admin")
    keygen(keys / "you", "you")
    return tmp_path


@pytest.fixture
def store(home):
    path = home / "config" / "ships.toml"
    path.write_text(INVENTORY.format(keys=home / "keys"), encoding="utf-8")
    path.chmod(0o600)
    return TomlFileStore(path)


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    path.chmod(mode)


@pytest.fixture
def fake_repo(tmp_path):
    """A minimal ShipShape checkout: real contracts and versions, a small ship/ tree."""
    root = tmp_path / "repo"
    root.mkdir()
    shutil.copytree(CONTRACTS, root / "contracts")
    shutil.copy(REPO_ROOT / "versions.toml", root / "versions.toml")
    ship = root / "ship"
    _write(ship / "provision" / "ship-provision", "#!/usr/bin/env bash\necho provision\n", 0o755)
    _write(ship / "bin" / "ship-ctl", "#!/usr/bin/env python3\nprint('ship-ctl')\n", 0o755)
    _write(ship / "files" / "herdr.service", "[Unit]\nDescription=herdr\n")
    _write(ship / "tests" / "test_ignored.py", "assert True\n")
    _write(ship / "axi" / "node_modules" / "dep" / "index.js", "module.exports = 1\n")
    _write(ship / "bin" / "__pycache__" / "x.pyc", "junk")
    _write(
        ship / "tools" / "gh-axi.toml",
        """\
        schema = 1
        id = "gh-axi"
        description = "GitHub CLI for agents."
        kind = "npm"
        package = "gh-axi"
        version = "0.1.47"
        needs = ["github"]
        check = "gh-axi --version"
        default = true
        """,
    )
    _write(
        ship / "tools" / "ship-axi.toml",
        """\
        schema = 1
        id = "ship-axi"
        description = "Tells agents about their Ship."
        kind = "script"
        script = "ship-axi.sh"
        check = "ship-axi --version"
        default = true
        """,
    )
    _write(
        ship / "tools" / "tasks-axi.toml",
        """\
        schema = 1
        id = "tasks-axi"
        description = "Task backlog tool."
        kind = "npm"
        package = "tasks-axi"
        version = "1.0.0"
        check = "tasks-axi --version"
        """,
    )
    _write(
        ship / "profiles" / "orchestrator.toml",
        """\
        schema = 1
        id = "orchestrator"
        description = "An orchestration repo and its tools."
        tools = ["tasks-axi"]

        [options.repo_url]
        description = "Repository to clone."
        required = true
        pattern = "^https://"
        """,
    )
    return Repo(root)


@pytest.fixture
def fake_ssh():
    return FakeSSH


@pytest.fixture
def short_dir():
    """A private folder under /tmp short enough for Unix sockets, removed afterwards."""
    folder = short_folder()
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def ssh_agent(short_dir, monkeypatch):
    """Start a private ssh-agent: ssh_agent(sock=None) -> its socket, which
    SSH_AUTH_SOCK then points at. The socket goes in short_dir unless given; every
    agent started is stopped when the test ends."""
    agents = SSHAgents()

    def start(sock: Path | None = None) -> Path:
        sock = agents.start(sock or short_dir / f"agent{len(agents.procs)}.sock")
        monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
        return sock

    yield start
    agents.stop_all()
