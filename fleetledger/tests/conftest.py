"""Shared fixtures: a private FleetLedger home and a small fake ShipShape repo.

Nothing here touches your real ~/.config/fleetledger, keys or network. Helpers
live in fl_harness.py: import them from there, never from conftest.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

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


@pytest.fixture(autouse=True)
def no_real_keyring(monkeypatch):
    from fleetledger.overlay import secret_store

    monkeypatch.setattr(secret_store, "get_keyring", lambda: NoKeyring())


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
