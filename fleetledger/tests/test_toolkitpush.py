"""FleetLedger sends your toolkits to Ships (toolkitpush.py, toolkitpack.py): end to end.

Two Ships, each a sandbox Ship running the real ship-ctl with the fake herdr, claude
and npm (ship/tests/ship_harness.py): ship-01 is tagged work, ship-02 personal. SSH is
replaced by a router that runs the upload script and ship-ctl locally, so what reaches
each Ship, and what each Ship does with it, is real.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from fleetledger.catalogue import Catalogue
from fleetledger.errors import FleetError
from fleetledger.seed.bundle import Bundle
from fleetledger.ssh import SHIP_CTL, Result

from fleetledger import cli, ops, toolkitpack, toolkitpush, toolkits

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ship" / "tests"))
import ship_harness  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "toolkits" / "examplekit"
SHIP_02_TOML = ship_harness.BASIC_TOML.replace('name = "ship-01"', 'name = "ship-02"').replace(
    'id = "7c9e6679-7425-40de-944b-e07fc1f90ae7"', 'id = "2f1b8a54-3c6e-4d1f-9a2b-5e8c7d6f4a31"'
).replace('tags = ["work"]', 'tags = ["personal"]')
OTHERKIT = """\
schema = 1
id = "otherkit"
version = "{version}"
description = "A second placeholder toolkit."
"""
SCRIPTED = """\
schema = 1
id = "scripted"
description = "A script tool (a placeholder)."
kind = "script"
script = "scripted.sh"
check = "cat ~/scripted"
"""


class Router:
    """Stands in for fleetledger.ssh.SSH: each Ship is a sandbox; uploads land in its
    own incoming folder, and ship-ctl runs there for real."""

    def __init__(self, ships: dict[str, ship_harness.Ship], incoming: Path):
        self.ships = ships
        self.incoming = incoming
        self.calls: list[tuple[str, list[str]]] = []
        self.old: set[str] = set()  # Ships whose tooling predates toolkits

    def forget(self, ship) -> None:
        pass

    def local(self, name: str, path: str) -> str:
        return path.replace(ops.INCOMING, str(self.incoming / name))

    def run(self, ship, command, *, input=None, timeout=60):
        self.calls.append((ship.name, list(command)))
        sandbox = self.ships[ship.name]
        if command[:2] == ["bash", "-c"]:
            local = [self.local(ship.name, a) for a in command]
            proc = subprocess.run(local, input=input or b"", capture_output=True)
            return Result(proc.returncode, proc.stdout, proc.stderr.decode(), ship.names[0])
        assert command[:3] == ["sudo", "-n", SHIP_CTL], command
        args = [self.local(ship.name, a) for a in command[3:]]
        if ship.name in self.old and args[0] == "toolkit":
            return Result(2, json.dumps(_usage("argument COMMAND: invalid choice: 'toolkit'")),
                          "", ship.names[0])
        proc = subprocess.run([sys.executable, "-I", str(sandbox.bundle / "bin/ship-ctl"), *args],
                              env=sandbox.env, input=input, capture_output=True, timeout=120)
        out = proc.stdout
        if ship.name in self.old and args[0] == "version":
            doc = json.loads(out)
            doc["data"]["contracts"].pop("toolkit")
            out = json.dumps(doc).encode()
        return Result(proc.returncode, out, proc.stderr.decode(), ship.names[0])

    def sent(self, name: str) -> list[list[str]]:
        """ship-ctl toolkit commands and uploads that reached this Ship."""
        return [c for n, c in self.calls if n == name and (
            c[:2] == ["bash", "-c"] or c[3:4] == ["toolkit"])]


def _usage(message: str) -> dict:
    return {"schema": "shipshape.ship-ctl-result/1", "command": "status", "ok": False,
            "changed": [], "messages": [], "error": {"code": "usage", "message": message}}


def make_examplekit(where: Path, version: str = "1.4.0") -> Path:
    root = where / f"examplekit-{version}"
    shutil.copytree(FIXTURE, root)
    lint = root / "tools/example-lint.toml"  # the fake npm names it after its last part
    lint.write_text(lint.read_text().replace("example-lint --version", "lint --version"))
    toml = root / "toolkit.toml"
    toml.write_text(toml.read_text().replace('version = "1.4.0"', f'version = "{version}"'))
    return root


def make_otherkit(where: Path, version: str = "2.0.0") -> Path:
    root = where / f"otherkit-{version}"
    (root / "tools").mkdir(parents=True)
    (root / "toolkit.toml").write_text(OTHERKIT.format(version=version))
    (root / "tools/scripted.toml").write_text(SCRIPTED)
    (root / "tools/scripted.sh").write_text('echo "scripted 1.0" > ~/scripted\n')
    return root


@pytest.fixture
def fleet(store, fake_repo, tmp_path):
    ships = {}
    for name, toml in (("ship-01", ship_harness.BASIC_TOML), ("ship-02", SHIP_02_TOML)):
        sandbox = ship_harness.build_ship(tmp_path / name, toml)
        (sandbox.bundle / "tools/tasks-axi.toml").write_text(
            (fake_repo.root / "ship/tools/tasks-axi.toml").read_text())
        ships[name] = sandbox
    router = Router(ships, tmp_path / "incoming")
    src = tmp_path / "src"
    src.mkdir()
    with store.edit() as ed:
        ed.set_toolkit("examplekit", {"path": str(make_examplekit(src)), "only_tags": ["work"]})
        ed.set_toolkit("otherkit", {"path": str(make_otherkit(src))})
    out, err = io.StringIO(), io.StringIO()
    app = cli.App(out=out, err=err, store_factory=lambda: store,
                  repo_factory=lambda: fake_repo, ssh_factory=lambda inv: router,
                  stdin_isatty=lambda: False)

    def run(*argv):
        out.seek(0), out.truncate(), err.seek(0), err.truncate()
        code = cli.main(list(argv), app)
        return code, out.getvalue(), err.getvalue()

    class Fleet:
        pass

    f = Fleet()
    f.ships, f.router, f.app, f.run, f.store, f.src, f.repo = (ships, router, app, run, store,
                                                               src, fake_repo)
    f.ops = ops.Fleet(store=store, ssh_factory=lambda inv: router,
                      repo_factory=lambda: fake_repo)
    return f


def toolkits_on(sandbox) -> dict:
    code, doc, _ = sandbox.ctl("status", "--json")
    return {k["id"]: k["release"] for k in doc["toolkits"]}


# --- push -------------------------------------------------------------------------------


def test_push_sends_a_toolkit_only_to_ships_its_only_tags_allow(fleet):
    code, out, err = fleet.run("toolkit", "push", "examplekit")
    assert code == 0, out + err
    assert "ship-01" in out and "toolkit examplekit 1.4.0 applied" in out
    assert "skipped: toolkit examplekit is only for Ships tagged work" in out
    assert "and ship-02 is tagged personal" in out
    assert list(toolkits_on(fleet.ships["ship-01"])) == ["examplekit"]
    assert toolkits_on(fleet.ships["ship-02"]) == {}
    assert fleet.router.sent("ship-02") == []  # not one byte of it went there
    assert not (fleet.router.incoming / "ship-02").exists()
    assert list((fleet.router.incoming / "ship-01").iterdir()) == []  # upload cleaned up

    # Again: the Ship has this release; nothing is uploaded.
    before = len(fleet.router.sent("ship-01"))
    code, out, err = fleet.run("toolkit", "push", "examplekit")
    assert code == 0 and "already there" in out
    assert len(fleet.router.sent("ship-01")) == before

    # A toolkit with no only_tags goes everywhere; --tag narrows it.
    code, out, err = fleet.run("toolkit", "push", "otherkit", "--tag", "personal")
    assert code == 0, out + err
    assert list(toolkits_on(fleet.ships["ship-02"])) == ["otherkit"]
    assert "otherkit" not in toolkits_on(fleet.ships["ship-01"])
    code, out, err = fleet.run("toolkit", "push", "nope")
    assert code != 0 and "No toolkit 'nope'" in err


def test_tools_add_sends_the_toolkit_then_installs_its_tool(fleet):
    code, out, err = fleet.run("tools", "add", "ship-01", "example-lint")
    assert code == 0, out + err
    assert "Toolkit examplekit 1.4.0 applied." in out
    _, status, _ = fleet.ships["ship-01"].ctl("status", "--json")
    assert status["tools"]["examplekit/example-lint"]["state"] == "ok"
    assert "examplekit/example-lint" in fleet.store.load().ship("ship-01").tools

    code, out, err = fleet.run("tools", "add", "ship-02", "examplekit/example-lint")
    assert code == 1
    assert ("toolkit examplekit is only for Ships tagged work (ships.toml only_tags), and "
            "ship-02 is tagged personal") in out + err
    assert fleet.router.sent("ship-02") == []
    assert fleet.store.load().ship("ship-02").tools is None or "examplekit/example-lint" not in \
        fleet.store.load().ship("ship-02").tools


def test_a_ship_whose_tooling_predates_toolkits_is_told_to_update(fleet):
    fleet.router.old.add("ship-01")
    code, out, err = fleet.run("toolkit", "push", "examplekit")
    assert code == 1
    assert "ship-01's tooling predates toolkits, so it cannot take one yet" in out + err
    assert "ship-fleet update ship-01" in out + err
    code, out, err = fleet.run("tools", "add", "ship-01", "examplekit/example-lint")
    assert code == 1 and "predates toolkits" in out + err
    assert fleet.router.sent("ship-01") == []  # never half-sent
    assert "examplekit/example-lint" not in (fleet.store.load().ship("ship-01").tools or ())


def test_a_lower_version_needs_force(fleet):
    assert fleet.run("toolkit", "push", "examplekit")[0] == 0
    older = make_examplekit(fleet.src / "older", "1.3.0")
    with fleet.store.edit() as ed:
        ed.set_toolkit("examplekit", {"path": str(older), "only_tags": ["work"]})
    code, out, err = fleet.run("toolkit", "push", "examplekit")
    assert code == 1
    assert "examplekit 1.3.0 is older than the 1.4.0 on this Ship" in out + err
    assert "ship-fleet toolkit push examplekit --force" in out + err
    code, out, err = fleet.run("toolkit", "push", "examplekit", "--force")
    assert code == 0, out + err
    assert toolkits_on(fleet.ships["ship-01"])["examplekit"].startswith("1.3.0-")


def test_allow_root_goes_with_the_toolkit(fleet):
    kit = make_otherkit(fleet.src / "rooted")
    (kit / "tools/scripted.toml").write_text(SCRIPTED + 'run_as = "root"\n')
    with fleet.store.edit() as ed:
        ed.set_toolkit("otherkit", {"path": str(kit), "allow_root": True})
    code, out, err = fleet.run("toolkit", "push", "otherkit", "--tag", "work")
    assert code == 0, out + err
    applied = [c for c in fleet.router.sent("ship-01") if c[3:5] == ["toolkit", "apply"]]
    assert applied and "--allow-root" in applied[0]
    _, listed, _ = fleet.ships["ship-01"].ctl("toolkit", "list", "--json")
    assert listed["data"]["toolkits"][0]["allow_root"] is True

    # allow_root taken away (from a toolkit with no root recipe left): the same release
    # is applied again, without it, not skipped as already there.
    (kit / "tools/scripted.toml").write_text(SCRIPTED)
    with fleet.store.edit() as ed:
        ed.set_toolkit("otherkit", {"path": str(kit), "allow_root": True})
    assert fleet.run("toolkit", "push", "otherkit", "--tag", "work")[0] == 0
    with fleet.store.edit() as ed:
        ed.set_toolkit("otherkit", {"path": str(kit)})
    code, out, err = fleet.run("toolkit", "push", "otherkit", "--tag", "work")
    assert code == 0 and "already there" not in out, out + err
    applied = [c for c in fleet.router.sent("ship-01") if c[3:5] == ["toolkit", "apply"]]
    assert len(applied) == 3 and "--allow-root" not in applied[2]
    _, listed, _ = fleet.ships["ship-01"].ctl("toolkit", "list", "--json")
    assert listed["data"]["toolkits"][0]["allow_root"] is False


def test_update_sends_the_toolkits_each_ship_may_have(fleet, monkeypatch):
    def updated(fleet_, ship, bundle, event, **kw):
        return ops.Outcome(ship.name, True, "already on 0.1.0-test")
    monkeypatch.setattr(ops, "update_one", updated)
    outcomes = ops.update(fleet.ops, [fleet.store.load().ship("ship-01"),
                                      fleet.store.load().ship("ship-02")],
                          bundle=Bundle(b"", {"dirty": False}))
    by = {o.ship: o for o in outcomes}
    assert by["ship-01"].ok and by["ship-02"].ok, outcomes
    assert sorted(toolkits_on(fleet.ships["ship-01"])) == ["examplekit", "otherkit"]
    assert sorted(toolkits_on(fleet.ships["ship-02"])) == ["otherkit"]
    assert "Toolkit examplekit 1.4.0 applied." in by["ship-01"].details
    assert not any("examplekit" in c for cmd in fleet.router.sent("ship-02") for c in cmd)

    # A toolkit that stops loading is said, not sent.
    kit = Path(fleet.store.load().doc["toolkit"]["otherkit"]["path"])
    (kit / "tools/scripted.toml").write_text(SCRIPTED + "default = true\n")
    outcomes = ops.update(fleet.ops, [fleet.store.load().ship("ship-02")],
                          bundle=Bundle(b"", {"dirty": False}))
    assert any("Toolkit otherkit was not sent: it does not load" in d
               for d in outcomes[0].details)


def test_update_sends_no_toolkit_to_a_ship_it_left_rebooting(fleet, monkeypatch):
    def rebooting(fleet_, ship, bundle, event, **kw):
        return ops.Outcome(ship.name, True, "updated; reboot started", data={"rebooted": True})
    monkeypatch.setattr(ops, "update_one", rebooting)
    outcomes = ops.update(fleet.ops, [fleet.store.load().ship("ship-01")], wait=False,
                          bundle=Bundle(b"", {"dirty": False}))
    assert outcomes[0].ok and "Toolkits were not sent: it is rebooting." in " ".join(
        outcomes[0].details)
    assert fleet.router.sent("ship-01") == []


def test_a_new_ship_gets_its_toolkits_after_enrolling(fleet):
    said: list[str] = []
    toolkitpush.after_enrol(fleet.app, fleet.store, "ship-02", said.append)
    assert said == ["Toolkit otherkit 2.0.0 applied."]
    assert sorted(toolkits_on(fleet.ships["ship-02"])) == ["otherkit"]
    fleet.router.old.add("ship-01")
    said.clear()
    toolkitpush.after_enrol(fleet.app, fleet.store, "ship-01", said.append)
    assert len(said) == 1 and said[0].startswith("Toolkits not sent yet (ship-01's tooling")


# --- the archive ------------------------------------------------------------------------


def test_the_archive_is_reproducible_kept_and_repacked_when_the_toolkit_changes(fleet):
    kit = toolkits.load(make_examplekit(fleet.src / "again"),
                        base=Catalogue.from_repo(fleet.repo))
    first = toolkitpack.pack(kit, created_at="2026-03-01T00:00:00Z")
    assert toolkitpack.pack(kit, created_at="2026-03-01T00:00:00Z").data == first.data
    with tarfile.open(fileobj=io.BytesIO(first.data), mode="r:gz") as tar:
        names = tar.getnames()
        assert names[0] == "files" and "toolkit.json" in names
        assert all(m.uid == 0 and m.uname == "root" for m in tar.getmembers())
        assert all(m.isfile() or m.isdir() for m in tar.getmembers())
        manifest = json.load(tar.extractfile("toolkit.json"))
    assert manifest["content_sha256"] == first.manifest["content_sha256"]
    assert first.release == f"1.4.0-{manifest['content_sha256'][:12]}"

    kept = toolkitpack.archive(kit)
    assert (toolkitpack.cache_dir() / "examplekit.tar.gz").read_bytes() == kept.data
    assert toolkitpack.cache_dir().stat().st_mode & 0o077 == 0
    assert toolkitpack.archive(kit).data == kept.data  # unchanged: the same bytes
    (kit.root / "instructions/examplekit.md").write_text("Changed.\n")
    changed = toolkitpack.archive(kit)
    assert changed.release != kept.release


def test_a_toolkit_too_big_to_send_is_refused(fleet, monkeypatch):
    monkeypatch.setattr(toolkitpack, "MAX_BYTES", 200)
    code, out, err = fleet.run("toolkit", "push", "examplekit")
    assert code != 0 and "packs to more than" in err
    assert fleet.router.sent("ship-01") == []


def test_a_vendored_file_changed_after_checking_is_not_sent(fleet):
    kit = toolkits.load(make_examplekit(fleet.src / "late"),
                        base=Catalogue.from_repo(fleet.repo))
    (kit.root / "files/example-cli.txt").write_text("swapped\n")
    with pytest.raises(FleetError, match="no longer matches its SHA-256"):
        toolkitpack.pack(kit)
