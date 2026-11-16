"""ship-fleet about (about.py): FleetLedger itself, for a person and as JSON
(contracts/fleetledger-about.schema.json).

The checkout is the fake one from conftest, committed and pushed to a bare "origin"
here (test_appliance's fixtures); systemd is test_service's fake, and the systemd
user folder a temporary one; a running UI is a control socket answering in the test.
Nothing reaches a network.
"""

from __future__ import annotations

import io
import json

import pytest
from fleetledger.web import control
from test_appliance import _advance, checkout, fetchable, git  # noqa: F401 - fixtures
from test_service import FakeSystem, runtime, units  # noqa: F401 - fixtures

from fleetledger import about, backup, cli, handover, repo, schemas, vault

REMOTE = "https://fleetledger.example-tailnet.ts.net"


def run(store, checkout_, *argv, system=None):  # noqa: F811
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store,
                  repo_factory=lambda: checkout_, stdin_isatty=lambda: False)
    app.system = system or FakeSystem()
    code = cli.main(["about", *argv], app)
    return code, out.getvalue()


def as_json(store, checkout_, *argv, system=None):  # noqa: F811
    code, said = run(store, checkout_, "--json", *argv, system=system)
    assert code == 0, said
    doc = json.loads(said)
    assert schemas.errors("fleetledger-about", doc) == []
    return doc


@pytest.fixture
def gits(monkeypatch):
    """Every git command about runs, recorded."""
    seen: list[tuple[str, ...]] = []
    real = repo.git

    def recording(root, *args, **kw):
        seen.append(args)
        return real(root, *args, **kw)

    monkeypatch.setattr(repo, "git", recording)
    return seen


def test_about_says_what_fleetledger_is_in_text_and_json(store, home, checkout, units):  # noqa: F811
    head = git(checkout.root, "rev-parse", "HEAD")
    code, said = run(store, checkout)
    assert code == 0, said
    assert f"FleetLedger {about.__version__}, commit {head[:12]} on main" in said
    assert "Newer on origin/main: not checked" in said
    assert "Service: not installed" in said and "Running UI: none answers here." in said
    assert "Vault: off" in said and "Backups: none made or scheduled here." in said
    assert "This copy: active." in said
    assert "Update FleetLedger: ship-fleet self-update (to origin/main" in said
    assert "Could not read" not in said
    doc = as_json(store, checkout)
    assert doc["schema"] == "shipshape.fleetledger-about/1"
    assert doc["checkout"] == {"path": str(checkout.root), "commit": head, "branch": "main",
                               "dirty": False}
    assert doc["upstream"] == {"checked": False, "ref": "origin/main", "commit": None,
                               "newer": None, "behind": None}
    assert doc["ui"] == {"local": None, "remote": None, "running": False, "as_service": None,
                         "jobs": None, "stopping": None}
    assert doc["service"] == {"installed": False, "active": None, "enabled": None}
    assert doc["vault"] == {"state": "off"} and doc["copy"] == "active"
    assert doc["backup"] == {"scheduled": False, "last": None}
    assert doc["self_update"] == {"command": "ship-fleet self-update", "ref": "origin/main"}
    assert doc["unread"] == []


def test_upstream_is_never_fetched_unless_asked(store, home, fetchable, tmp_path,  # noqa: F811
                                                units, gits):  # noqa: F811
    newer = _advance(fetchable, tmp_path, "newer")
    doc = as_json(store, fetchable)
    assert doc["upstream"]["checked"] is False and doc["upstream"]["newer"] is None
    run(store, fetchable)
    assert not [a for a in gits if a[0] in ("fetch", "pull", "remote", "ls-remote")]
    assert git(fetchable.root, "rev-parse", "origin/main") != newer  # nothing fetched
    doc = as_json(store, fetchable, "--check")
    assert [a for a in gits if a[0] == "fetch"] == [("fetch", "--tags", "--quiet", "origin")]
    assert doc["upstream"] == {"checked": True, "ref": "origin/main", "commit": newer,
                               "newer": True, "behind": 1}
    code, said = run(store, fetchable, "--check")
    assert code == 0, said
    assert f"Newer on origin/main: yes, {newer[:12]}, 1 commit(s) on: ship-fleet self-update " \
           "moves to it." in said
    head = git(fetchable.root, "rev-parse", "HEAD")
    assert head != newer  # it only looked: self-update moves the checkout


def test_check_says_when_this_is_origin_and_when_the_fetch_fails(store, home, fetchable,  # noqa: F811
                                                                  units):  # noqa: F811
    code, said = run(store, fetchable, "--check")
    assert code == 0 and "Newer on origin/main: no, this is it." in said
    git(fetchable.root, "remote", "set-url", "origin", str(fetchable.bare) + "-gone")
    doc = as_json(store, fetchable, "--check")
    assert doc["upstream"]["checked"] is False
    assert [u["what"] for u in doc["unread"]] == ["upstream"]
    code, said = run(store, fetchable, "--check")
    assert code == 0 and "Could not read upstream: git fetch origin:" in said


def test_a_locked_vault_is_shown_and_nothing_is_unlocked(store, home, checkout,  # noqa: F811
                                                         units, monkeypatch):  # noqa: F811
    monkeypatch.setattr(vault, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(vault, "in_ram", lambda path: True)
    vault.create("correct horse battery staple")
    vault.lock()
    assert vault.state() == "locked"
    code, said = run(store, checkout)
    assert code == 0, said
    assert "Vault: locked (it locks at every boot)" in said and "ship-fleet unlock" in said
    assert as_json(store, checkout)["vault"] == {"state": "locked"}
    assert vault.state() == "locked"  # still
    out = io.StringIO()  # the rest of FleetLedger still refuses
    assert cli.main(["ls"], cli.App(out=out, err=out, store_factory=lambda: store)) == 1
    assert "vault is locked" in out.getvalue()


def test_a_service_that_is_installed_but_not_running(store, home, checkout, units):  # noqa: F811
    units.mkdir(parents=True)
    (units / "fleetledger.service").write_text(
        "# Written by ship-fleet service install\n[Service]\n"
        "ExecStart=/home/fleetledger/.local/bin/ship-fleet ui --service --port 8766 "
        "--interval 30\n")
    code, said = run(store, checkout, system=FakeSystem(active=False))
    assert code == 0, said
    assert "Service: installed, inactive, disabled." in said
    assert "Running UI: none answers here." in said
    assert "http://127.0.0.1:8766 on this computer" in said
    doc = as_json(store, checkout, system=FakeSystem(active=False))
    assert doc["service"] == {"installed": True, "active": "inactive", "enabled": "disabled"}
    assert doc["ui"]["local"] == "http://127.0.0.1:8766" and doc["ui"]["running"] is False


def test_a_running_service_with_a_remote_address(store, home, checkout, units, runtime):  # noqa: F811
    units.mkdir(parents=True)
    (units / "fleetledger.service").write_text("# Written by ship-fleet service install\n")
    system = FakeSystem(active=True)
    system.enabled = True
    ctl = control.Control({"status": lambda: {
        "port": 8765, "url": "http://127.0.0.1:8765", "remote": REMOTE, "service": True,
        "stopping": False, "pid": 1, "jobs": [{"id": "j1", "title": "Update", "ships":
                                               ["ship-01"]}]}})
    assert ctl.start()
    try:
        code, said = run(store, checkout, system=system)
        doc = as_json(store, checkout, system=system)
    finally:
        ctl.stop()
    assert code == 0, said
    assert f"Web UI: http://127.0.0.1:8765 on this computer, {REMOTE} through Tailscale " \
           "Serve." in said
    assert "Service: installed, active, enabled." in said
    assert "Running UI: answers, 1 job(s) running." in said
    assert doc["ui"] == {"local": "http://127.0.0.1:8765", "remote": REMOTE, "running": True,
                         "as_service": True, "jobs": 1, "stopping": False}


def test_backups_last_result_and_schedule(store, home, checkout, units):  # noqa: F811
    units.mkdir(parents=True)
    (units / backup.TIMER).write_text("# Written by ship-fleet backup schedule.\n")
    code, said = run(store, checkout)
    assert "Backups: scheduled; none made yet." in said
    backup._note(True, home / "nas", file="fleetledger-x.flbackup")
    doc = as_json(store, checkout)
    assert doc["backup"]["scheduled"] is True
    assert doc["backup"]["last"]["ok"] is True and doc["backup"]["last"]["to"] == \
        str(home / "nas")
    code, said = run(store, checkout)
    assert "Backups: last one " in said and "; scheduled daily." in said
    backup._note(False, home / "nas", why="the vault is locked")
    code, said = run(store, checkout)
    assert "Backups: the last one failed" in said and "the vault is locked" in said


def test_about_runs_on_a_handed_over_copy_and_says_so(store, home, checkout, units):  # noqa: F811
    handover.write(store, handover.Marker("retired", handover.now(), "vm.example", 1))
    code, said = run(store, checkout)
    assert code == 0, said
    assert "This copy: retired" in said
    assert as_json(store, checkout)["copy"] == "retired"


def test_what_it_cannot_read_it_says_and_shows_the_rest(store, home, units, tmp_path):  # noqa: F811
    from fleetledger.errors import ConfigError

    def nowhere():
        raise ConfigError("Cannot find the ShipShape checkout (contracts/ and versions.toml).")

    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store, repo_factory=nowhere)
    app.system = FakeSystem()
    assert cli.main(["about", "--json", "--check"], app) == 0, out.getvalue()
    doc = json.loads(out.getvalue())
    assert schemas.errors("fleetledger-about", doc) == []
    assert doc["checkout"]["commit"] is None and doc["vault"] == {"state": "off"}
    assert [u["what"] for u in doc["unread"]] == ["checkout", "upstream"]


def test_about_writes_nothing(store, home, checkout, units):  # noqa: F811
    def snapshot():
        return sorted((str(p), p.stat().st_mtime_ns) for p in home.rglob("*")
                      if p.is_file() and "repo" not in p.parts and "origin.git" not in p.parts)

    run(store, checkout)  # the first run may make the in-use lock file, as every command
    before = snapshot()
    run(store, checkout)
    as_json(store, checkout)
    assert snapshot() == before
