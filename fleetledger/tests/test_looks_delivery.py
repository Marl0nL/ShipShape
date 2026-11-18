"""The fleet's Desk looks, delivered to the machines opted in (ship-fleet trust NAME --looks
on): the bundle's `looks` field (machinebundle.py, contracts/machine-bundle.schema.json) and the
refresher that checks it and writes Herdr's desk-looks folder (refresher/refresh.sh).

The bundle side runs FleetLedger in-process (caenv). The refresher side reuses
test_refresher's sandbox computers and real sshd: nothing touches this computer's real
~/.ssh, Herdr folder or FleetLedger.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
from fl_harness import keygen
from fleetledger.concierge import audit
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import env  # noqa: F401  (a fixture)
from test_refresher import AWKS, SCRIPT, Machine, fl, laptop, serve, serve_doc  # noqa: F401

from fleetledger import ca, looks, machinebundle, schemas

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "herdr" / "desk-looks.example"
V1 = Path(__file__).with_name("fixtures") / "refresher" / "refresh-v1.sh"
# The fields of a bundle for a machine not opted in, in their order: as before looks.
FIELDS = ["schema", "machine", "at", "certificate", "scope", "logins", "ssh", "known_hosts",
          "fleetledger", "ships", "idle_limit", "refresher"]


def digest(files: dict[str, bytes]) -> str:
    h = hashlib.sha256()
    for name, data in sorted(files.items()):
        h.update(f"{name}\n{len(data)}\n".encode())
        h.update(data)
    return h.hexdigest()


def example_files() -> dict[str, bytes]:
    """The placeholder folder as FleetLedger keeps it (its JSON rewritten compactly)."""
    raw = {str(p.relative_to(EXAMPLE)): p.read_bytes()
           for p in sorted(EXAMPLE.rglob("*")) if p.is_file()}
    return looks.read_folder(looks.from_files(raw)).files


def publish(e, folder: Path = EXAMPLE) -> None:
    code, out, err = e.run("looks", "publish", str(folder))
    assert code == 0, out + err


def trust(e, tmp_path, name="laptop", *more):
    keygen(tmp_path / name, name)
    code, out, err = e.run("trust", name, "--key", str(tmp_path / f"{name}.pub"), *more)
    assert code == 0, err


def bundle_text(e, name="laptop") -> str:
    code, out, err = e.run("machine-bundle", name)
    assert code == 0, err
    assert schemas.errors("machine-bundle", json.loads(out)) == []
    return out


def audited() -> list[str]:
    return [a["outcome"] for a in audit.read() if a["action"] == "machine-bundle"]


def frozen(monkeypatch):
    now = ca.utc_now()
    monkeypatch.setattr(ca, "utc_now", lambda: now)


# --- the bundle ---------------------------------------------------------------------------------
def test_a_machine_not_opted_in_gets_the_same_bundle_with_looks_kept_or_not(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    trust(caenv, tmp_path)
    frozen(monkeypatch)
    before = bundle_text(caenv)
    assert list(json.loads(before)) == FIELDS
    publish(caenv)
    assert bundle_text(caenv) == before
    # On and off again: the record is as it was, and so is the bundle.
    assert caenv.run("trust", "laptop", "--looks", "on")[0] == 0
    assert "looks" in json.loads(bundle_text(caenv))
    assert caenv.run("trust", "laptop", "--looks", "off")[0] == 0
    assert "looks" not in caenv.store.load().doc["machine"]["laptop"]
    assert bundle_text(caenv) == before
    assert all(";" not in x for x in audited()[:2])  # nothing said of looks either


def test_looks_on_sends_the_kept_folder_after_at(caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path)
    publish(caenv)
    code, out, err = caenv.run("trust", "laptop", "--looks", "on")
    assert code == 0, err
    assert "also reads the fleet's looks" in out
    assert "refresh --version" in out and "shipshape-setup" in out
    assert '"an unknown field, looks"' in out
    assert caenv.store.load().doc["machine"]["laptop"]["looks"] is True
    doc = json.loads(bundle_text(caenv))
    assert list(doc)[:4] == ["schema", "machine", "at", "looks"]
    assert [k for k in doc if k != "looks"] == FIELDS
    kept = looks.load().folder
    sent = {f["name"]: base64.b64decode(f["data"]) for f in doc["looks"]["files"]}
    assert sent == kept.files == example_files()
    assert [f["size"] for f in doc["looks"]["files"]] == [len(b) for b in kept.files.values()]
    assert doc["looks"]["digest"] == kept.sha256 == digest(sent)
    assert audited()[-1] == f"returned; looks {kept.sha256[:12]}"
    entries = [(a["action"], a["service"]) for a in audit.read()]
    assert ("trust-looks-on", "laptop") in entries
    # Said where FleetLedger shows its machines and its looks.
    code, out, _ = caenv.run("ca", "show")
    assert "gets the fleet's looks" in out
    code, out, _ = caenv.run("ca", "show", "--json")
    ca_doc = json.loads(out)
    assert schemas.errors("fleetledger-ca", ca_doc) == []
    assert [m["looks"] for m in ca_doc["machines"]] == [True]
    code, out, _ = caenv.run("looks", "show")
    assert "sent to: laptop" in out


def test_none_kept_tells_an_opted_in_machine_so(caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path)
    assert caenv.run("trust", "laptop", "--looks", "on")[0] == 0
    doc = json.loads(bundle_text(caenv))
    assert doc["looks"] is None
    assert audited()[-1] == "returned; looks: none kept"
    code, out, _ = caenv.run("looks", "show")
    assert "laptop: told there are none" in out


def test_a_kept_folder_that_fails_its_checks_is_not_sent_and_the_bundle_still_comes(
        caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path)
    publish(caenv)
    assert caenv.run("trust", "laptop", "--looks", "on")[0] == 0
    store = looks.store_path()
    kept = json.loads(store.read_text())
    kept["files"]["logos/example.png"] = base64.b64encode(b"#!/bin/sh\necho hi\n").decode()
    store.write_text(json.dumps(kept))
    doc = json.loads(bundle_text(caenv))
    assert "looks" not in doc and list(doc) == FIELDS
    assert audited()[-1] == "returned; looks not sent: logos/example.png is not a PNG picture"
    code, out, _ = caenv.run("looks", "show")
    assert code == 1 and "not a PNG picture" in out and "not sent to laptop" in out


def test_a_bundle_that_would_be_over_1_mib_goes_without_the_looks(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    trust(caenv, tmp_path)
    publish(caenv)
    assert caenv.run("trust", "laptop", "--looks", "on")[0] == 0
    small = len(bundle_text(caenv)) - 10
    monkeypatch.setattr(machinebundle, "MAX_BUNDLE", small)
    doc = json.loads(bundle_text(caenv))
    assert "looks" not in doc
    assert audited()[-1] == "returned; looks not sent: the bundle would be over 1 MiB with them"


def test_looks_changes_only_the_record(caenv, tmp_path):  # noqa: F811
    trust(caenv, tmp_path)
    for more in (["--scope", "all"], ["--print-cert"], ["--hours", "2"], ["--install"]):
        code, _, err = caenv.run("trust", "laptop", "--looks", "on", *more)
        assert code == 1 and "change only the record" in err
    code, _, err = caenv.run("trust", "--sync-keys", "--looks", "on")
    assert code == 1 and "takes no machine and no other option" in err
    code, _, err = caenv.run("trust", "tablet", "--looks", "on")
    assert code == 1 and "No trusted machine called tablet" in err
    assert "looks" not in caenv.store.load().doc["machine"]["laptop"]
    code, out, err = caenv.run("trust", "laptop", "--looks", "off")
    assert code == 0 and "stay on laptop as they are" in out


def test_the_forced_key_still_gets_only_its_own_bundle_with_looks_on(
        caenv, tmp_path, monkeypatch):  # noqa: F811
    trust(caenv, tmp_path, "laptop")
    trust(caenv, tmp_path, "desk")
    publish(caenv)
    assert caenv.run("trust", "desk", "--looks", "on")[0] == 0

    class Untouchable(io.StringIO):
        def read(self, *a):
            raise AssertionError("stdin read")

        readline = read

    monkeypatch.setattr(sys, "stdin", Untouchable())
    for asked in ("ship-fleet machine-bundle desk", "ship-fleet looks show --json",
                  "ship-fleet looks clear"):
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", asked)
        doc = json.loads(bundle_text(caenv, "laptop"))
        assert doc["machine"] == "laptop" and "looks" not in doc
    assert looks.load() is not None  # nothing cleared


def test_the_schema_takes_only_names_the_format_gives():
    doc = json.loads((ROOT / "contracts/examples/machine-bundle/looks.json").read_text())
    assert schemas.errors("machine-bundle", doc) == []
    for name in ("../looks.json", "palettes/../../x.json", "/etc/passwd", "logos/a.sh",
                 "logos/A.png", "palettes/theme.json", "looks.json/x"):
        bad = copy.deepcopy(doc)
        bad["looks"]["files"][0]["name"] = name
        assert schemas.errors("machine-bundle", bad), name


# --- the refresher ------------------------------------------------------------------------------
def herdr(m: Machine) -> Path:
    h = m.home / ".config" / "herdr-app"
    h.mkdir(parents=True, exist_ok=True)
    return h


def desk_files(m: Machine, folder: Path | None = None) -> dict[str, bytes]:
    folder = folder or herdr(m) / "desk-looks"
    return {str(p.relative_to(folder)): p.read_bytes()
            for p in sorted(folder.rglob("*")) if p.is_file()}


def opt_in(e, name="laptop"):
    code, out, err = e.run("trust", name, "--looks", "on")
    assert code == 0, err


def stray(m: Machine) -> list[str]:
    return sorted(p.name for p in herdr(m).iterdir() if p.name.startswith(".shipshape-looks"))


def applied(m: Machine) -> list[str]:
    return (m.d / "looks-applied").read_text().split()


def newer_folder(m: Machine, label: str) -> Path:
    other = m.root / f"looks-{label.lower()}"
    shutil.copytree(EXAMPLE, other)
    (other / "looks.json").write_text((other / "looks.json").read_text().replace(
        '"Example"', f'"{label}"'))
    return other


@pytest.mark.local_sshd
def test_an_opted_in_machine_gets_the_looks_in_herdrs_folder_and_only_once(fl, laptop):  # noqa: F811
    herdr(laptop)
    fleet_before = json.loads((laptop.d / "fleet.json").read_text())
    publish(fl)
    opt_in(fl)
    doc = serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert "Wrote the fleet's looks to" in proc.stdout
    assert desk_files(laptop) == example_files()
    assert (laptop.d / "looks-applied").read_text().split() == [doc["looks"]["digest"]]
    # fleet.json is the bundle as checked, without the refresher and without the looks.
    fleet = json.loads((laptop.d / "fleet.json").read_text())
    assert "looks" not in fleet and "refresher" not in fleet
    assert {k: v for k, v in fleet.items() if k != "at"} == {
        k: v for k, v in fleet_before.items() if k != "at"}
    assert laptop.state_doc()["refresher"] == 2 and laptop.state_doc()["warning"] is None
    # Unchanged: nothing written again.
    target = herdr(laptop) / "desk-looks"
    ino = (target.stat().st_ino, (target / "looks.json").stat().st_mtime_ns)
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0 and "looks" not in proc.stdout
    assert (target.stat().st_ino, (target / "looks.json").stat().st_mtime_ns) == ino
    assert stray(laptop) == []


@pytest.mark.local_sshd
def test_new_looks_replace_the_ones_it_wrote_and_none_removes_them(fl, laptop, tmp_path):  # noqa: F811
    herdr(laptop)
    publish(fl)
    opt_in(fl)
    serve(fl)
    assert laptop.refresh().returncode == 0
    other = tmp_path / "other"
    shutil.copytree(EXAMPLE, other)
    (other / "looks.json").write_text((other / "looks.json").read_text().replace(
        '"Example"', '"Another"'))
    publish(fl, other)
    doc = serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0 and "Wrote the fleet's looks" in proc.stdout, proc.stderr
    assert desk_files(laptop) == looks.load().folder.files
    assert b"Another" in desk_files(laptop)["looks.json"]
    assert applied(laptop)[0] == doc["looks"]["digest"]
    assert stray(laptop) == []
    assert fl.run("looks", "clear")[0] == 0
    assert serve(fl)["looks"] is None
    proc = laptop.refresh()
    assert proc.returncode == 0 and "Removed the fleet's looks" in proc.stdout
    assert not (herdr(laptop) / "desk-looks").exists()
    assert stray(laptop) == []
    assert laptop.refresh().returncode == 0  # and again: nothing to do


@pytest.mark.local_sshd
@pytest.mark.parametrize("change", ["own folder", "edited", "a file added", "a link"])
def test_a_desk_looks_folder_of_your_own_is_never_changed_or_removed(fl, laptop, change):  # noqa: F811
    h = herdr(laptop)
    target = h / "desk-looks"
    publish(fl)
    opt_in(fl)
    if change == "own folder":
        shutil.copytree(EXAMPLE, target)
        (target / "looks.json").write_text((target / "looks.json").read_text().replace(
            '"Example"', '"Mine"'))
    elif change == "a link":
        mine = laptop.home / "mine"
        shutil.copytree(EXAMPLE, mine)
        target.symlink_to(mine)
    else:
        serve(fl)
        assert laptop.refresh().returncode == 0
        if change == "edited":
            (target / "looks.json").write_bytes((target / "looks.json").read_bytes() + b" ")
        else:
            (target / "logos" / "mine.png").write_bytes(b"\x89PNG\r\n\x1a\nmine")
    before = desk_files(laptop)
    # A newer folder: not written over yours.
    other = laptop.root / "other"
    shutil.copytree(EXAMPLE, other)
    (other / "looks.json").write_text((other / "looks.json").read_text().replace(
        '"Example"', '"Newer"'))
    publish(fl, other)
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert "is a folder of your own" in proc.stderr
    assert "is a folder of your own" in laptop.state_doc()["warning"]
    assert desk_files(laptop) == before
    # None kept: yours is not removed either, and nothing is said of it.
    assert fl.run("looks", "clear")[0] == 0
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0 and proc.stderr == "", proc.stderr
    assert desk_files(laptop) == before
    assert target.exists()
    if change == "a link":
        assert target.is_symlink()
    assert stray(laptop) == []


@pytest.mark.local_sshd
def test_a_refresh_stopped_between_the_renames_is_mended_by_the_next(fl, laptop):  # noqa: F811
    """Stopped after the old folder went aside and before the new one came in: the next
    refresh puts the old one back, even when its bundle has no looks; what a stopped refresh
    left is removed after 15 minutes, and only the names the refresher gives its own."""
    h = herdr(laptop)
    publish(fl)
    opt_in(fl)
    serve(fl)
    assert laptop.refresh().returncode == 0
    before = desk_files(laptop)
    (h / "desk-looks").rename(h / ".shipshape-looks-old.4242")
    for name in (".shipshape-looks-new.abcdef", ".desk-looks.old.backup"):
        (h / name).mkdir()
        (h / name / "mine.txt").write_text("mine")
        os.utime(h / name, (time.time() - 3600, time.time() - 3600))
    assert fl.run("trust", "laptop", "--looks", "off")[0] == 0
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert desk_files(laptop) == before
    assert stray(laptop) == []
    assert (h / ".desk-looks.old.backup" / "mine.txt").read_text() == "mine"


@pytest.mark.local_sshd
def test_a_folder_written_under_another_herdr_folder_stays_its_own(fl, laptop):  # noqa: F811
    """HERDR_APP_HOME set for one run (the Fleet panel's, say) and not for the timer's."""
    herdr(laptop)
    elsewhere = laptop.home / "herdr-elsewhere"
    elsewhere.mkdir()
    publish(fl)
    opt_in(fl)
    serve(fl)
    assert laptop.refresh().returncode == 0
    publish(fl, newer_folder(laptop, "Newer"))
    serve(fl)
    plain = laptop.env
    laptop.env = lambda: {**plain(), "HERDR_APP_HOME": str(elsewhere)}
    assert laptop.refresh().returncode == 0
    laptop.env = plain
    proc = laptop.refresh()
    assert proc.returncode == 0 and proc.stderr == "", proc.stderr
    assert desk_files(laptop) == desk_files(laptop, elsewhere / "desk-looks")
    assert b"Newer" in desk_files(laptop)["looks.json"]
    assert fl.run("looks", "clear")[0] == 0
    serve(fl)
    assert laptop.refresh().returncode == 0
    assert not (herdr(laptop) / "desk-looks").exists()


@pytest.mark.local_sshd
def test_a_desk_looks_that_appears_during_the_write_is_left_as_it_is(fl, laptop):  # noqa: F811
    """mv onto a folder moves into it: one made between the check and the rename is yours."""
    h = herdr(laptop)
    real = shutil.which("mv")
    (laptop.bin / "mv").unlink()
    shim = laptop.bin / "mv"
    shim.write_text(f"""#!/bin/sh
for last in "$@"; do :; done
case "$1 $last" in
  *.shipshape-looks-new.*" {h}/desk-looks") mkdir -p "$last" && echo mine >"$last/looks.json" ;;
esac
exec {real} "$@"
""")
    shim.chmod(0o755)
    publish(fl)
    opt_in(fl)
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0
    assert "Could not write the fleet's looks" in proc.stderr
    assert desk_files(laptop) == {"looks.json": b"mine\n"}
    assert stray(laptop) == []


@pytest.mark.local_sshd
def test_without_herdrs_folder_nothing_is_written_and_it_says_so(fl, laptop):  # noqa: F811
    publish(fl)
    opt_in(fl)
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0
    assert "there is no Herdr folder here" in proc.stderr
    assert not (laptop.home / ".config" / "herdr-app").exists()
    assert not (laptop.d / "looks-applied").exists()


@pytest.mark.local_sshd
def test_herdr_app_home_is_where_the_looks_go(fl, laptop):  # noqa: F811
    elsewhere = laptop.home / "herdr-elsewhere"
    elsewhere.mkdir()
    laptop.env = lambda: {**Machine.env(laptop), "HERDR_APP_HOME": str(elsewhere)}
    publish(fl)
    opt_in(fl)
    serve(fl)
    assert laptop.refresh().returncode == 0
    assert desk_files(laptop, elsewhere / "desk-looks") == example_files()


@pytest.mark.local_sshd
def test_without_the_tools_to_check_them_the_looks_are_left(fl, laptop):  # noqa: F811
    herdr(laptop)
    (laptop.bin / "od").unlink()
    publish(fl)
    opt_in(fl)
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0
    assert "no base64, sha256sum (or shasum) or od" in proc.stderr
    assert not (herdr(laptop) / "desk-looks").exists()


@pytest.mark.local_sshd
def test_shasum_serves_where_there_is_no_sha256sum(fl, laptop, tmp_path):  # noqa: F811
    """macOS has shasum, not sha256sum."""
    herdr(laptop)
    (laptop.bin / "sha256sum").unlink()
    real = shutil.which("sha256sum")
    shim = laptop.bin / "shasum"
    shim.write_text(f'#!/bin/sh\n[ "$1 $2" = "-a 256" ] || exit 2\nexec {real}\n')
    shim.chmod(0o755)
    publish(fl)
    opt_in(fl)
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert desk_files(laptop) == example_files()


# Each refused whole: (what to change in the bundle's looks, a word the refusal says).
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 24


def _file(name: str, data: bytes) -> dict:
    return {"name": name, "size": len(data), "data": base64.b64encode(data).decode()}


def _with(files: list[dict], *, fix_digest=True) -> dict:
    out = {"digest": "0" * 64, "files": files}
    if fix_digest:
        try:
            out["digest"] = digest({f["name"]: base64.b64decode(f["data"]) for f in files})
        except (ValueError, KeyError, TypeError):
            pass
    return out


def _named(looks_doc: dict, name: str) -> dict:
    return next(f for f in looks_doc["files"] if f["name"] == name)


def _hostile():
    def rename(new):
        def go(lk):
            _named(lk, "logos/example.png")["name"] = new
            return _with(lk["files"])
        return go

    def add(*files):
        return lambda lk: _with(lk["files"] + list(files))

    def set_logo(content, **over):
        def go(lk):
            f = _named(lk, "logos/example.png")
            f.update(_file(f["name"], content))
            f.update(over)
            return _with(lk["files"])
        return go

    def set_looks_json(data):
        def go(lk):
            f = _named(lk, "looks.json")
            f.update(_file("looks.json", data))
            return _with(lk["files"])
        return go

    return {
        "a path up": (rename("../../.ssh/authorized_keys"), "a looks file's name"),
        "a path up inside palettes": (rename("palettes/../../x.json"), "a looks file's name"),
        "an absolute path": (rename("/tmp/x.png"), "a looks file's name"),
        "a script's name": (rename("logos/run.sh"), "a looks file's name"),
        "upper case": (rename("logos/Example.png"), "a looks file's name"),
        "a name too long": (rename("logos/" + "a" * 32 + ".png"), "a looks file's name"),
        "a name with a newline": (rename("logos/a\nb.png"), "a looks file's name"),
        "a theme of Herdr's": (add(_file("palettes/graphite.json", b"{}")), "a looks file's name"),
        "a name twice": (add(_file("logos/example.png", PNG)), "listed twice"),
        "no looks.json": (lambda lk: _with([f for f in lk["files"]
                                             if f["name"] != "looks.json"]), "no looks.json"),
        "no files": (lambda lk: {"digest": lk["digest"], "files": []}, "looks.files"),
        "a field it does not know": (lambda lk: {**lk, "mode": "0755"}, "an unknown field"),
        "a file field it does not know": (
            lambda lk: _with([{**f, "path": "/tmp/x"} for f in lk["files"]]), "an unknown field"),
        "looks a list": (lambda lk: [lk], "has a value out of place"),
        "looks a string": (lambda lk: "$(touch PWNED)", "has a value out of place"),
        "a digest not hex": (lambda lk: {**lk, "digest": "$(touch PWNED)"}, "looks.digest"),
        "the wrong digest": (lambda lk: {**lk, "digest": "0" * 64}, "do not match their digest"),
        "data not base64": (set_logo(PNG, data="$(touch PWNED)==="), "data"),
        "data not its size": (set_logo(PNG, size=len(PNG) + 3), "not its size"),
        "a size of zero": (set_logo(b"", size=0), "size"),
        "a logo over 64 KiB": (set_logo(PNG + b"\0" * (65537 - len(PNG))), "over its limit"),
        "a script named .png": (set_logo(b"#!/bin/sh\ntouch PWNED\n"), "not a PNG picture"),
        "a jpg that is a png": (lambda lk: _with(lk["files"] + [_file("logos/x.jpg", PNG)]),
                                "not a JPEG picture"),
        "looks.json not JSON": (set_looks_json(b"#!/bin/sh\ntouch PWNED\n"), "not JSON"),
        "looks.json another schema": (set_looks_json(b'{"schema":"shipshape.other/1"}'),
                                      "not shipshape.desk-looks/1"),
        "a control byte in a string": (set_looks_json(
            b'{"schema":"shipshape.desk-looks/1","x":"\x1b]0;title\x07"}'), "control character"),
        "a NUL in a string": (set_looks_json(
            b'{"schema":"shipshape.desk-looks/1","x":"a\x00b"}'), "control character"),
        "a palette too deep": (add(_file("palettes/looks-deep.json",
                                         b"[" * 20 + b"]" * 20)), "not JSON"),
        "17 palettes": (add(*[_file(f"palettes/looks-p{i}.json", b"{}") for i in range(17)]),
                        "more looks files"),
        "over 512 KiB": (add(*[_file(f"logos/big{i}.png", PNG + b"\0" * 60000)
                               for i in range(9)]), "over 512 KiB"),
    }


HOSTILE = _hostile()


@pytest.mark.local_sshd
@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_a_bundle_with_looks_out_of_place_is_refused_whole(fl, laptop, case):  # noqa: F811
    h = herdr(laptop)
    pwned = laptop.root / "PWNED"
    publish(fl)
    opt_in(fl)
    serve(fl)
    assert laptop.refresh().returncode == 0
    before = laptop.files()
    doc = serve(fl)
    change, says = HOSTILE[case]
    doc["looks"] = change(copy.deepcopy(doc["looks"]))
    serve_doc(fl, json.loads(json.dumps(doc).replace("PWNED", str(pwned))))
    proc = laptop.refresh()
    assert proc.returncode == 1, proc.stdout
    assert "the files here are unchanged" in proc.stderr, proc.stderr
    assert says.replace("'", "") in proc.stderr.replace("'", ""), proc.stderr
    after = laptop.files()
    assert {k for k in before if before[k] != after.get(k)} | (after.keys() - before.keys()) <= {
        ".ssh/shipshape/state.json"}
    assert stray(laptop) == [] and sorted(p.name for p in h.iterdir()) == ["desk-looks"]
    assert not pwned.exists()
    assert not (laptop.root / "tmp").exists() and not Path("/tmp/x.png").exists()


@pytest.mark.local_sshd
def test_the_refresher_before_looks_takes_a_bundle_without_them_and_refuses_one_with(
        fl, laptop):  # noqa: F811
    """Refresher 1 (as installed before this change): every bundle of a machine not opted in
    still passes it; once opted in, it refuses each bundle whole, saying so, and keeps its
    files, until shipshape-setup (or the pasted line) puts refresher 2 in place."""
    herdr(laptop)
    shutil.copyfile(V1, laptop.d / "refresh")
    publish(fl)  # kept on FleetLedger, but this machine is not opted in
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert laptop.state_doc()["refresher"] == 1
    opt_in(fl)
    serve(fl)
    before = laptop.files()
    proc = laptop.refresh()
    assert proc.returncode == 1
    assert "an unknown field, looks" in proc.stderr and "files here are unchanged" in proc.stderr
    after = laptop.files()
    assert {k for k in before if before[k] != after.get(k)} <= {".ssh/shipshape/state.json"}
    assert "an unknown field, looks" in laptop.state_doc()["last_error"]
    assert not (herdr(laptop) / "desk-looks").exists()
    # The new refresher, as setup installs it: the same bundle is taken.
    shutil.copyfile(SCRIPT, laptop.d / "refresh")
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert desk_files(laptop) == example_files()


@pytest.mark.local_sshd
@pytest.mark.parametrize("awk", AWKS)
def test_other_awks_take_and_refuse_the_looks_the_same(fl, laptop, awk):  # noqa: F811
    herdr(laptop)
    laptop.set_awk(awk)
    publish(fl)
    opt_in(fl)
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert desk_files(laptop) == example_files()
    doc = serve(fl)
    _named(doc["looks"], "logos/example.png")["name"] = "../../x.png"
    serve_doc(fl, doc)
    proc = laptop.refresh()
    assert proc.returncode == 1 and "a looks file" in proc.stderr, proc.stderr


@pytest.mark.local_sshd
@pytest.mark.parametrize("awk", ["awk", *AWKS])
def test_a_folder_at_its_limits_is_read_in_good_time(fl, laptop, tmp_path, awk):  # noqa: F811
    """512 KiB of looks: about 700 KiB of base64 in a bundle under 1 MiB."""
    herdr(laptop)
    laptop.set_awk(awk)
    big = tmp_path / "big"
    shutil.copytree(EXAMPLE, big)
    doc = json.loads((big / "looks.json").read_text())
    base = doc["presets"][0]
    for i in range(7):
        (big / "logos" / f"big{i}.png").write_bytes(
            PNG + os.urandom(65536 - len(PNG)))
        doc["presets"].append({**base, "id": f"big{i}", "label": f"Big {i}",
                               "brand": {**base["brand"], "logo": f"big{i}.png"}})
    (big / "looks.json").write_text(json.dumps(doc))
    publish(fl, big)
    assert looks.load().folder.size > 7 * 65536
    opt_in(fl)
    sent = serve(fl)
    assert "looks" in sent
    start = time.monotonic()
    proc = laptop.refresh()
    took = time.monotonic() - start
    assert proc.returncode == 0, proc.stderr
    assert desk_files(laptop) == looks.load().folder.files
    assert took < 60, took


@pytest.mark.local_sshd
def test_a_refresh_with_looks_writes_nothing_else_outside_its_own_files(fl, laptop):  # noqa: F811
    h = herdr(laptop)
    (h / "settings.json").write_text("{}")
    before = laptop.files()
    publish(fl)
    opt_in(fl)
    serve(fl)
    assert laptop.refresh().returncode == 0
    after = laptop.files()
    changed = {k for k in before if before[k] != after.get(k)} | (after.keys() - before.keys())
    own = {f".ssh/shipshape/{f}" for f in ("fleet.json", "state.json", "looks-applied")}
    looks_files = {f".config/herdr-app/desk-looks/{n}" for n in example_files()}
    assert looks_files | {".ssh/shipshape/looks-applied"} <= changed <= own | looks_files


def test_the_refresher_says_its_version():
    text = SCRIPT.read_text()
    assert "\nVERSION=2\n" in text
    assert "\nVERSION=1\n" in V1.read_text()


def test_the_contract_example_carries_the_placeholder_folder():
    doc = json.loads((ROOT / "contracts/examples/machine-bundle/looks.json").read_text())
    files = {f["name"]: base64.b64decode(f["data"]) for f in doc["looks"]["files"]}
    assert files == example_files()
    assert doc["looks"]["digest"] == digest(files)

