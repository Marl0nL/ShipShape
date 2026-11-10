"""ship-ctl toolkit: your own toolkits on a Ship (contracts/toolkit.md, "On a Ship").

Two toolkits, packed by FleetLedger's own packer (fleetledger/toolkitpack.py) from the
placeholder toolkit in fleetledger/tests/fixtures/toolkits: examplekit (a script tool
needing its own sign-in, an npm tool, a skill and a profile) and otherkit (a tool that
runs as root, and a script tool that says how to uninstall itself). Everything runs in
the sandbox Ship with the fake herdr, claude, npm and example-cli.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import tarfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from ship_harness import REPO, assert_valid

from fleetledger import toolkitpack, toolkits

FIXTURE = REPO / "fleetledger" / "tests" / "fixtures" / "toolkits" / "examplekit"
CREATED = "2026-03-01T00:00:00Z"
TASKS_AXI = """\
schema = 1
id = "tasks-axi"
description = "Task backlog tool."
kind = "npm"
package = "tasks-axi"
version = "1.0.0"
check = "tasks-axi --version"
"""
OTHER_TOOLKIT = """\
schema = 1
id = "otherkit"
version = "{version}"
description = "A second placeholder toolkit."
"""
ROOT_TOOL = """\
schema = 1
id = "root-tool"
description = "Needs root (a placeholder)."
kind = "script"
script = "root-tool.sh"
run_as = "root"
check = 'cat "$HOME/root-tool-ran"'
"""
SCRIPTED = """\
schema = 1
id = "scripted"
description = "A script tool that can be uninstalled (a placeholder)."
kind = "script"
script = "scripted.sh"
uninstall = "rm -f ~/.local/share/scripted/marker"
check = "cat ~/.local/share/scripted/marker"
"""
SIGNED_IN = {"out": json.dumps({"signed_in": True, "account": {"email": "you@example.com"}})}
SIGNED_OUT = {"rc": 1}


def _pin(root: Path) -> None:
    """Pin every file under files/ in toolkit.toml again (after a test changed one)."""
    text = (root / "toolkit.toml").read_text().split("[files]")[0]
    pins = "".join(f'"{p.relative_to(root).as_posix()}" = '
                   f'"{hashlib.sha256(p.read_bytes()).hexdigest()}"\n'
                   for p in sorted((root / "files").rglob("*")) if p.is_file())
    (root / "toolkit.toml").write_text(text + ("[files]\n" + pins if pins else ""))


def make_examplekit(where: Path, version: str = "1.4.0") -> Path:
    root = where / f"examplekit-{version}"
    shutil.copytree(FIXTURE, root)
    # Keep the fake example-cli first on PATH: install the release file elsewhere.
    (root / "tools/example-cli.sh").write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\n'
        'here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\n'
        'install -m 0644 -D "$here/files/example-cli.txt" '
        '"$HOME/.local/share/example-cli/release.txt"\n')
    (root / "tools/example-cli.sh").chmod(0o755)
    # The fake npm names a scoped package's command after its last part.
    lint = (root / "tools/example-lint.toml").read_text()
    (root / "tools/example-lint.toml").write_text(lint.replace("example-lint --version",
                                                               "lint --version"))
    toml = (root / "toolkit.toml").read_text().replace('version = "1.4.0"',
                                                     f'version = "{version}"')
    (root / "toolkit.toml").write_text(toml)
    return root


def make_otherkit(where: Path, version: str = "2.0.0") -> Path:
    root = where / f"otherkit-{version}"
    (root / "tools").mkdir(parents=True)
    (root / "toolkit.toml").write_text(OTHER_TOOLKIT.format(version=version))
    (root / "tools/root-tool.toml").write_text(ROOT_TOOL)
    # Root's HOME in the sandbox is the test's folder (ship_harness), not the agent's.
    (root / "tools/root-tool.sh").write_text('echo "ran as root" > "$HOME/root-tool-ran"\n')
    (root / "tools/scripted.toml").write_text(SCRIPTED)
    (root / "tools/scripted.sh").write_text(
        'mkdir -p ~/.local/share/scripted && echo "scripted 1.0" > '
        '~/.local/share/scripted/marker\n')
    return root


def pack(root: Path, out: Path, allow_root: bool = False) -> Path:
    from fleetledger.catalogue import Catalogue
    from fleetledger.repo import Repo

    kit = toolkits.load(root, allow_root=allow_root, base=Catalogue.from_repo(Repo(REPO)))
    archive = toolkitpack.pack(kit, created_at=CREATED)
    path = out / f"{kit.id}-{kit.version}-{archive.manifest['content_sha256'][:6]}.tar.gz"
    path.write_bytes(archive.data)
    return path


@pytest.fixture
def kits(ship, tmp_path):
    """The sandbox Ship with tasks-axi in its catalogue and both toolkits packed."""
    (ship.bundle / "tools/tasks-axi.toml").write_text(TASKS_AXI)
    src, out = tmp_path / "src", tmp_path / "archives"
    src.mkdir()
    out.mkdir()
    ship.set(**{"example-cli": {"auth status --json": SIGNED_OUT, "setup hooks": {},
                                "--version": {"out": "example-cli 1.2.0"}}})

    class Kits:
        pass

    k = Kits()
    k.ship, k.src, k.out = ship, src, out
    k.example = pack(make_examplekit(src), out)
    k.other = pack(make_otherkit(src), out, allow_root=True)
    return k


def apply(ship, archive: Path, *extra: str):
    rc, doc, out = ship.ctl("toolkit", "apply", "--archive", str(archive), *extra, "--json")
    assert_valid("ship-ctl-result", doc)
    return rc, doc, out


def ship_toml(ship) -> dict:
    return tomllib.loads(ship.p("/etc/shipshape/ship.toml").read_text())


def add_tools(ship, *ids: str, profiles: tuple[str, ...] = ()) -> None:
    with ship.p("/etc/shipshape/ship.toml").open("a") as fh:
        fh.write(f"\n[tools]\ninstall = {json.dumps(list(ids))}\n"
                 f"profiles = {json.dumps(list(profiles))}\n")


def release_of(archive: Path) -> str:
    with tarfile.open(archive, "r:gz") as tar:
        manifest = json.load(tar.extractfile("toolkit.json"))
    return f"{manifest['version']}-{manifest['content_sha256'][:12]}"


# --- apply, lookup, status ------------------------------------------------------------


def test_version_says_the_ship_takes_toolkits(ship):
    rc, doc, _ = ship.ctl("version", "--json")
    assert rc == 0 and doc["data"]["contracts"]["toolkit"] == 1


def test_apply_unpacks_records_and_installs_its_tools(kits):
    ship = kits.ship
    # The seed asked for them before the toolkit arrived: they fail, nothing runs.
    add_tools(ship, "examplekit/example-lint", "examplekit/example-guide", "gh-axi-free")
    (ship.bundle / "tools/gh-axi-free.toml").write_text(
        TASKS_AXI.replace("tasks-axi", "gh-axi-free"))
    rc, doc, _ = ship.ctl("tools", "run", "--json")
    assert rc == 1
    assert doc["data"]["installed"]["examplekit/example-lint"]["state"] == "failed"
    assert any("toolkit examplekit is not on this Ship" in m for m in doc["messages"])
    assert doc["data"]["installed"]["gh-axi-free"]["state"] == "ok"

    rc, doc, out = apply(ship, kits.example)
    assert rc == 0, out
    release = release_of(kits.example)
    assert doc["data"]["toolkit"] == {"id": "examplekit", "version": "1.4.0", "release": release,
                                      "previous": None, "allow_root": False}
    assert doc["messages"][0] == f"Toolkit examplekit: now on 1.4.0 ({release})."
    folder = ship.p("/opt/shipshape/toolkits/examplekit")
    assert os.readlink(folder / "current") == release
    assert (folder / release / "tools/example-cli.toml").is_file()
    assert (folder / release / "toolkit.json").is_file()
    assert oct((folder / release / "tools/example-cli.sh").stat().st_mode & 0o777) == "0o755"
    assert oct((folder / release / "tools/example-cli.toml").stat().st_mode & 0o777) == "0o644"
    assert ship_toml(ship)["toolkits"] == {"examplekit": release}
    # Its tools the Ship asked for were installed from it.
    assert doc["data"]["installed"]["examplekit/example-lint"]["state"] == "ok"
    assert ["@example/lint@2.0.1"] in [c[-1:] for c in ship.scenario()["npm"]["installs"]]
    assert ship.p("/home/agent/.claude/skills/example-guide/SKILL.md").is_file()

    _, status, _ = ship.ctl("status", "--json")
    assert_valid("ship-status", status)
    assert status["toolkits"] == [{"id": "examplekit", "version": "1.4.0", "release": release,
                                   "state": "ok", "allow_root": False}]
    assert status["tools"]["examplekit/example-lint"]["state"] == "ok"
    assert "exampledocs" in status["auth"]  # its sign-in joins the Ship's
    rc, doc, _ = ship.ctl("tools", "list", "--json")
    assert {"examplekit/example-cli", "examplekit/example-lint", "tasks-axi"} <= set(
        doc["data"]["available"])
    rc, doc, _ = ship.ctl("toolkit", "list", "--json")
    assert rc == 0 and doc["data"]["toolkits"][0]["tools"] == [
        "example-cli", "example-guide", "example-lint"]

    rc, doc, _ = apply(ship, kits.example)  # the same again: nothing new
    assert rc == 0 and doc["messages"][0].startswith("Toolkit examplekit: already on 1.4.0")


def test_a_toolkit_tool_waits_for_the_toolkit_s_sign_in(kits):
    ship = kits.ship
    rc, _, out = apply(ship, kits.example)
    assert rc == 0, out
    rc, doc, out = ship.ctl("tools", "add", "examplekit/example-cli", "--json")
    assert rc == 0, out
    assert doc["data"]["installed"]["examplekit/example-cli"]["waiting_for"] == ["exampledocs"]
    assert ship.p("/home/agent/.local/share/example-cli/release.txt").read_text().startswith(
        "example-cli 1.2.0")
    assert [a for a in ship.calls("example-cli") if a[1:] == ["setup", "hooks"]] == []

    ship.ctl("refresh")  # records exampledocs as missing
    s = ship.scenario()
    s["example-cli"]["auth status --json"] = SIGNED_IN
    ship.set(**{"example-cli": s["example-cli"]})
    shutil.rmtree(ship.p("/run/shipshape"), ignore_errors=True)  # no kept check result
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert [a for a in ship.calls("example-cli") if a[1:] == ["setup", "hooks"]]
    _, status, _ = ship.ctl("status", "--json")
    assert status["tools"]["examplekit/example-cli"]["state"] == "ok"
    assert status["auth"]["exampledocs"]["account"] == "you@example.com"
    rc, doc, _ = ship.ctl("signin", "check", "exampledocs", "--json")
    assert rc == 0 and doc["data"]["auth"]["exampledocs"]["state"] == "ok"


def test_a_toolkit_profile_runs_with_its_own_and_catalogue_tools(kits):
    ship = kits.ship
    add_tools(ship, profiles=("examplekit/dev",))
    with ship.p("/etc/shipshape/ship.toml").open("a") as fh:
        fh.write('\n[profile_options."examplekit/dev"]\n'
                 'workspace = "https://example.com/workspace.git"\n')
    rc, doc, out = apply(ship, kits.example)
    assert rc == 0, out
    assert doc["data"]["profiles"] == {"examplekit/dev": "ok"}
    _, status, _ = ship.ctl("status", "--json")
    assert set(status["tools"]) == {"examplekit/example-cli", "examplekit/example-guide",
                                    "tasks-axi"}
    assert "workspace: https://example.com/workspace.git" in ship.p(
        "/var/log/shipshape/tools.log").read_text()


# --- the allow-list ----------------------------------------------------------------------


def test_nothing_runs_from_a_toolkit_not_in_the_allow_list(kits):
    ship = kits.ship
    rc, _, out = apply(ship, kits.other, "--allow-root")
    assert rc == 0, out
    # Files of a toolkit on disk, with no [toolkits] entry: copied there by hand, say.
    release = release_of(kits.other)
    shutil.copytree(ship.p(f"/opt/shipshape/toolkits/otherkit/{release}"),
                    ship.p(f"/opt/shipshape/toolkits/sneaky/{release}"))
    toml = ship.p("/etc/shipshape/ship.toml")
    toml.write_text(toml.read_text() + '\n[tools]\ninstall = ["sneaky/scripted"]\n')
    rc, doc, out = ship.ctl("tools", "run", "--json")
    assert rc == 1
    assert doc["data"]["installed"]["sneaky/scripted"]["state"] == "failed"
    assert "toolkit sneaky is not on this Ship" in out
    assert not ship.p("/home/agent/.local/share/scripted/marker").exists()
    rc, doc, out = ship.ctl("tools", "add", "sneaky/root-tool", "--json")
    assert rc == 2 and "not on this Ship: toolkit sneaky" in doc["error"]["message"]
    assert not (ship.root.parent / "root-tool-ran").exists()

    # A release folder the allow-list does not name is never used either, nor `current`.
    folder = ship.p("/opt/shipshape/toolkits/otherkit")
    planted = folder / "9.9.9-000000000000"
    shutil.copytree(folder / release, planted)
    (planted / "tools/scripted.sh").write_text('echo planted > ~/planted\n')
    os.unlink(folder / "current")
    os.symlink(planted.name, folder / "current")
    toml.write_text(toml.read_text().replace('"sneaky/scripted"', '"otherkit/scripted"'))
    rc, doc, out = ship.ctl("tools", "run", "--json")
    assert rc == 0, out
    assert not ship.p("/home/agent/planted").exists()
    assert ship.p("/home/agent/.local/share/scripted/marker").exists()

    # A sign-in of a toolkit not in the list is not the Ship's.
    shutil.copytree(FIXTURE / "signins", ship.p(f"/opt/shipshape/toolkits/sneaky/{release}/"
                                                "signins"))
    _, status, _ = ship.ctl("status", "--json")
    assert "exampledocs" not in status["auth"]
    assert [k["id"] for k in status["toolkits"]] == ["otherkit"]


def test_root_recipes_run_only_when_applied_with_allow_root(kits):
    ship = kits.ship
    rc, _, out = apply(ship, kits.other)
    assert rc == 0, out
    rc, doc, out = ship.ctl("tools", "add", "otherkit/root-tool", "--json")
    assert rc == 1
    assert "runs as root, and toolkit otherkit was not sent with allow_root" in out
    assert not (ship.root.parent / "root-tool-ran").exists()
    assert ship.calls("bash") == [] or all("root-tool" not in " ".join(c)
                                           for c in ship.calls("bash"))

    rc, doc, out = apply(ship, kits.other, "--allow-root")
    assert rc == 0, out
    assert doc["data"]["toolkit"]["allow_root"] is True
    assert doc["data"]["installed"]["otherkit/root-tool"]["state"] == "ok"
    assert (ship.root.parent / "root-tool-ran").read_text() == "ran as root\n"
    _, listed, _ = ship.ctl("toolkit", "list", "--json")
    assert listed["data"]["toolkits"][0]["allow_root"] is True

    # Applied again without it: root is refused again.
    (ship.root.parent / "root-tool-ran").unlink()
    rc, doc, out = apply(ship, kits.other)
    assert rc == 1 and doc["error"]["code"] == "tool-failed"
    assert "is applied, but its tools did not all install" in doc["error"]["message"]
    assert not (ship.root.parent / "root-tool-ran").exists()


# --- versions ----------------------------------------------------------------------------


def test_a_lower_version_is_not_applied_without_force(kits):
    ship = kits.ship
    assert apply(ship, kits.example)[0] == 0
    newer = release_of(kits.example)
    older = pack(make_examplekit(kits.src, "1.3.0"), kits.out)
    before = ship.p("/etc/shipshape/ship.toml").read_text()
    rc, doc, _ = apply(ship, older)
    assert rc == 1 and doc["error"]["code"] == "toolkit-rollback"
    assert "examplekit 1.3.0 is older than the 1.4.0 on this Ship" in doc["error"]["message"]
    assert ship.p("/etc/shipshape/ship.toml").read_text() == before
    assert sorted(p.name for p in ship.p("/opt/shipshape/toolkits/examplekit").iterdir()) == \
        sorted(["current", newer])

    rc, doc, out = apply(ship, older, "--force")
    assert rc == 0, out
    assert doc["messages"][0].startswith("Toolkit examplekit: rolled back to 1.3.0")
    assert ship_toml(ship)["toolkits"]["examplekit"] == release_of(older)
    # A pre-release is lower than its release; the same version with other content is not.
    pre = pack(make_examplekit(kits.src, "1.3.0-rc.1"), kits.out)
    assert apply(ship, pre)[1]["error"]["code"] == "toolkit-rollback"
    same = make_examplekit(kits.src / "again", "1.3.0")
    (same / "instructions/examplekit.md").write_text("Changed.\n")
    assert apply(ship, pack(same, kits.out))[0] == 0


# --- removing ----------------------------------------------------------------------------


def test_remove_uninstalls_its_tools_and_forgets_it(kits):
    ship = kits.ship
    add_tools(ship, "examplekit/example-lint", "examplekit/example-guide", "otherkit/scripted",
              "tasks-axi", profiles=("examplekit/dev",))
    with ship.p("/etc/shipshape/ship.toml").open("a") as fh:
        fh.write('\n[profile_options."examplekit/dev"]\n'
                 'workspace = "https://example.com/workspace.git"\n')
    assert apply(ship, kits.example)[0] == 0
    assert apply(ship, kits.other, "--allow-root")[0] == 0
    assert ship.p("/home/agent/.local/share/scripted/marker").exists()

    rc, doc, out = ship.ctl("toolkit", "remove", "otherkit", "--json")
    assert rc == 0, out
    assert_valid("ship-ctl-result", doc)
    assert doc["data"]["uninstalled"] == ["otherkit/scripted"]
    assert not ship.p("/home/agent/.local/share/scripted/marker").exists()
    assert not ship.p("/opt/shipshape/toolkits/otherkit").exists()

    rc, doc, out = ship.ctl("toolkit", "remove", "examplekit", "--json")
    assert rc == 0, out
    assert set(doc["data"]["uninstalled"]) == {"examplekit/example-lint",
                                               "examplekit/example-guide"}
    assert ["@example/lint"] in ship.scenario()["npm"]["uninstalls"]
    assert not ship.p("/home/agent/.claude/skills/example-guide").exists()
    assert not ship.p("/opt/shipshape/toolkits/examplekit").exists()
    toml = ship_toml(ship)
    assert "toolkits" not in toml and "profile_options" not in toml
    assert toml["tools"] == {"install": ["tasks-axi"], "profiles": []}
    assert not [t for t in ship.json("/var/lib/shipshape/tools.json") if "/" in t]
    assert ship.json("/var/lib/shipshape/toolkits.json") == {}
    _, status, _ = ship.ctl("status", "--json")
    assert status["toolkits"] == [] and set(status["tools"]) == {"tasks-axi"}
    assert "exampledocs" not in status["auth"]
    rc, doc, _ = ship.ctl("toolkit", "remove", "examplekit", "--json")
    assert rc == 2 and "there is no toolkit examplekit" in doc["error"]["message"]


def test_set_file_keeps_the_allow_list(kits):
    ship = kits.ship
    assert apply(ship, kits.example)[0] == 0
    kept = ship_toml(ship)["toolkits"]
    from ship_harness import BASIC_TOML

    rc, doc, out = ship.ctl("set", "--file", "-", "--json", input=BASIC_TOML)
    assert rc == 0, out
    assert ship_toml(ship)["toolkits"] == kept
    rc, doc, _ = ship.ctl("set", "--file", "-", "--json", input=BASIC_TOML + (
        '\n[toolkits]\nexamplekit = "9.9.9-000000000000"\n'))
    assert rc == 2 and "[toolkits] changes only through ship-ctl toolkit" in doc["error"][
        "message"]


# --- refused archives --------------------------------------------------------------------


def _repack(src: Path, dest: Path, change) -> Path:
    """The archive's members, changed by change(name, data) -> (name, data) or None (drop),
    plus whatever extra members change.extra lists."""
    out = io.BytesIO()
    with tarfile.open(src, "r:gz") as tar, gzip.GzipFile(fileobj=out, mode="wb") as gz, \
            tarfile.open(fileobj=gz, mode="w") as new:
        for m in tar.getmembers():
            data = tar.extractfile(m).read() if m.isfile() else None
            got = change(m.name, data)
            if got is None:
                continue
            m.name, data = got
            if data is not None:
                m.size = len(data)
            new.addfile(m, io.BytesIO(data) if data is not None else None)
        for extra in getattr(change, "extra", []):
            new.addfile(extra)
    dest.write_bytes(out.getvalue())
    return dest


def _refused(ship, archive: Path, said: str) -> None:
    before = ship.p("/etc/shipshape/ship.toml").read_text()
    rc, doc, _ = apply(ship, archive)
    assert rc == 1 and doc["error"]["code"] == "invalid-toolkit", doc
    assert said in doc["error"]["message"], doc["error"]["message"]
    assert ship.p("/etc/shipshape/ship.toml").read_text() == before
    kits = ship.p("/opt/shipshape/toolkits")
    assert not kits.exists() or [p.name for p in kits.iterdir()] == []


def test_a_tampered_archive_is_refused(kits):
    ship = kits.ship

    def swap(name, data):
        if name == "tools/example-cli.sh":
            return name, data + b"echo tampered > ~/tampered\n"
        return name, data
    _refused(ship, _repack(kits.example, kits.out / "tampered.tar.gz", swap),
             "do not match the content hash")

    def drop(name, data):
        return None if name == "tools/example-lint.toml" else (name, data)
    _refused(ship, _repack(kits.example, kits.out / "dropped.tar.gz", drop),
             "do not match the content hash")

    def no_manifest(name, data):
        return None if name == "toolkit.json" else (name, data)
    _refused(ship, _repack(kits.example, kits.out / "bare.tar.gz", no_manifest),
             "no toolkit.json")

    sidecar = kits.out / (kits.example.name + ".sha256")
    sidecar.write_text("0" * 64 + "  toolkit.tar.gz\n")
    _refused(ship, kits.example, "does not match its .sha256 file")
    sidecar.write_text(hashlib.sha256(kits.example.read_bytes()).hexdigest() + "  x\n")
    assert apply(ship, kits.example)[0] == 0


@pytest.mark.parametrize("bad", ["symlink", "hardlink", "escape", "absolute", "device",
                                 "fifo"])
def test_links_and_path_escapes_are_refused(kits, bad):
    ship = kits.ship
    info = tarfile.TarInfo({"escape": "../outside", "absolute": "/etc/evil"}.get(bad, "tools/x"))
    info.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                 "device": tarfile.CHRTYPE, "fifo": tarfile.FIFOTYPE}.get(bad, tarfile.REGTYPE)
    info.linkname = "/etc/shadow" if bad in ("symlink", "hardlink") else ""

    def keep(name, data):
        return name, data
    keep.extra = [info]
    said = "unsafe path" if bad in ("escape", "absolute") else "links and devices"
    _refused(ship, _repack(kits.example, kits.out / f"{bad}.tar.gz", keep), said)
    assert not (ship.root.parent / "outside").exists()


def test_an_archive_over_the_size_limit_is_refused(kits):
    big = kits.out / "big.tar.gz"
    with big.open("wb") as fh:  # sparse: no disk used
        fh.truncate(100 * 1024 * 1024 + 1)
    _refused(kits.ship, big, "a toolkit archive may be 100 MB at most")


def test_a_toolkit_whose_sign_in_clashes_is_refused(kits):
    ship = kits.ship
    clash = make_otherkit(kits.src / "clash")
    (clash / "signins").mkdir()
    shutil.copy(REPO / "ship/signins/github.toml", clash / "signins/github.toml")
    # FleetLedger refuses such a toolkit; packed here as if it had been bypassed.
    kit = SimpleNamespace(id="otherkit", version="2.0.0", root=clash, files={})
    archive = kits.out / "clash.tar.gz"
    archive.write_bytes(toolkitpack.pack(kit, created_at=CREATED).data)
    _refused(ship, archive, "sign-in github is also in")


def test_remove_leaves_what_a_kept_tool_shares(kits):
    """A second toolkit's skill folder of the same name lost to the first one: removing
    the second leaves the first's folder (and so for an MCP server of one name)."""
    ship = kits.ship
    clash = make_otherkit(kits.src / "clash")
    (clash / "toolkit.toml").write_text(OTHER_TOOLKIT.format(version="2.0.0").replace(
        'id = "otherkit"', 'id = "clashkit"'))
    for f in ("root-tool.toml", "root-tool.sh"):
        (clash / "tools" / f).unlink()
    (clash / "skills/mine").mkdir(parents=True)
    (clash / "skills/mine/SKILL.md").write_text("---\nname: mine\n---\nNot examplekit's.\n")
    (clash / "tools/example-guide.toml").write_text(
        'schema = 1\nid = "example-guide"\ndescription = "Same skill name."\n'
        'kind = "skill-dir"\nskill_dir = "skills/mine"\ncheck = "true"\n')
    add_tools(ship, "examplekit/example-guide", "clashkit/example-guide")
    assert apply(ship, kits.example)[0] == 0
    skill = ship.p("/home/agent/.claude/skills/example-guide/SKILL.md")
    first = skill.read_text()
    rc, doc, _ = apply(ship, pack(clash, kits.out))
    assert rc == 1 and "is examplekit/example-guide's" in " ".join(doc["messages"])
    assert skill.read_text() == first
    rc, doc, out = ship.ctl("toolkit", "remove", "clashkit", "--json")
    assert rc == 0, out
    assert skill.read_text() == first  # still examplekit's
