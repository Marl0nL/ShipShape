"""Shared fixtures: a private FleetLedger home and a small fake ShipShape repo.

Nothing here touches your real ~/.config/fleetledger, keys or network.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from fleetledger.repo import Repo
from fleetledger.store import TomlFileStore

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts"


def keygen(path: Path, comment: str) -> Path:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        check=True,
        capture_output=True,
    )
    return path


class NoKeyring:
    """What tests get by default: no usable keyring, so nothing ever reaches a real one."""

    def get_password(self, service, item):
        raise RuntimeError("no keyring in tests")

    set_password = get_password


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


INVENTORY = """\
# My fleet (a comment that must survive edits)
schema = 1

[fleet]
admin_key = "{keys}/fleet_admin"
agent_keys = ["{keys}/you.pub"]

[defaults]
driver = "manual"
tags = ["work"]
claude = {{ remote_control = "on", default_mode = "auto" }}

[tag.work]
claude = "work@example.com"
gcloud = "work@example.com"
github = "example-work"

[tag.personal]
claude = "you@example.com"
github = "example-user"

[profile_options.orchestrator]
repo_url = "https://example.com/orchestrator.git"

[ship.ship-01]
id = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
tags = ["work"]
driver = "manual"
names = ["ship-01.local"]
host_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGVob3N0a2V5ZXhhbXBsZWhvc3RrZXlleGFt"
profiles = ["orchestrator"]
enrolled = "2026-01-01T00:00:00Z"

[ship.ship-02]
id = "2f1b8a54-3c6e-4d1f-9a2b-5e8c7d6f4a31"
tags = ["personal"]
driver = "manual"
names = ["ship-02.local", "ship-02.example.net"]
host_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGVob3N0a2V5c2hpcDAyZXhhbXBsZWhvc3Rr"
claude = {{ default_mode = "bypassPermissions", allow_bypass = true }}
enrolled = "2026-01-01T00:00:00Z"
"""


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


class FakeSSH:
    """Stands in for fleetledger.ssh.SSH: answers each remote command from a handler.

    handler(ship, command, input) returns (returncode, stdout, stderr) or raises.
    """

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def run(self, ship, command, *, input=None, timeout=60):
        from fleetledger.ssh import Result

        self.calls.append((ship.name, list(command), input))
        code, out, err = self.handler(ship, list(command), input)
        if isinstance(out, str):
            out = out.encode()
        return Result(code, out, err, ship.names[0])


@pytest.fixture
def fake_ssh():
    return FakeSSH
