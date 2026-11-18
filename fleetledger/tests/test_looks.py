"""ship-fleet looks: the fleet's Desk looks (looks.py, lookscmd.py; contracts/ship-fleet.md,
"ship-fleet looks"). The folder read is the repo's placeholder, herdr/desk-looks.example/,
and the contract's own examples; a test runs the Desk's own checks (lookfolder.js, in node)
on the same cases."""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest
from fleetledger.concierge import audit
from test_authkeys import block, seed, trust, vm  # noqa: F401  (vm is a fixture)
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import env  # noqa: F401  (a fixture)

from fleetledger import cli, handover, looks, paths, schemas

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "herdr" / "desk-looks.example"
EXAMPLES = ROOT / "contracts" / "examples"
DESK = ROOT / "herdr" / "mods" / "desk"


def example_files() -> dict[str, bytes]:
    return {"looks.json": (EXAMPLE / "looks.json").read_bytes(),
            "palettes/looks-example-dark.json":
                (EXAMPLE / "palettes" / "looks-example-dark.json").read_bytes(),
            "logos/example.png": (EXAMPLE / "logos" / "example.png").read_bytes()}


def example_doc() -> dict:
    return json.loads((EXAMPLE / "looks.json").read_text())


def tar_of(members: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as t:
        for info, data in members:
            t.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def reg(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, data


def folder_dir(name: str = "./") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    return info, None


def example_tar(*extra, files: dict[str, bytes] | None = None) -> bytes:
    files = example_files() if files is None else files
    return tar_of([folder_dir("./"), folder_dir("./palettes"), folder_dir("./logos"),
                   *[reg(f"./{n}", b) for n, b in files.items()], *extra])


def publish_stream(e, data: bytes):
    e.app.stdin_bytes = io.BytesIO(data)
    try:
        return e.run("looks", "publish", "-")
    finally:
        e.app.stdin_bytes = None


def show(e) -> dict:
    code, out, err = e.run("looks", "show", "--json")
    assert code == 0, err
    doc = json.loads(out)
    assert schemas.errors("fleetledger-looks", doc) == []
    return doc


def audited() -> list[tuple[str, str, str]]:
    return [(a["action"], a["service"], a["outcome"]) for a in audit.read()
            if a["action"].startswith("looks-")]


# --- publish, show, clear ------------------------------------------------------------------


def test_nothing_is_stored_at_first(env):  # noqa: F811
    assert show(env) == {"schema": looks.SHOW_SCHEMA, "stored": False}
    code, out, _ = env.run("looks", "show")
    assert code == 0 and "No Desk looks are stored" in out
    code, out, _ = env.run("looks", "clear")
    assert code == 0 and "nothing to clear" in out
    assert audited() == []


def test_a_folder_here_is_published_shown_and_cleared(env, tmp_path):  # noqa: F811
    code, out, err = env.run("looks", "publish", str(EXAMPLE))
    assert code == 0, err
    assert "Published: 1 preset, 1 tag looks, 3 files" in out
    path = looks.store_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    doc = show(env)
    assert doc["stored"] is True and doc["problem"] is None
    assert doc["from"] == {"how": "folder", "folder": paths.tilde(EXAMPLE), "address": None,
                           "actor": None, "actor_key": None}
    assert doc["presets"] == ["example"]
    assert doc["palettes"] == [{"id": "looks-example-dark", "dark": True}]
    assert doc["tag_looks"] == [{"tag": "work", "preset": "example"}]
    assert [f["name"] for f in doc["files"]] == [
        "logos/example.png", "looks.json", "palettes/looks-example-dark.json"]
    # The logo as it came; the JSON files rewritten from their checked values.
    logo = next(f for f in doc["files"] if f["name"] == "logos/example.png")
    assert logo["bytes"] == len(example_files()["logos/example.png"])
    # Never a file's contents: no label, no colour, no logo bytes.
    text = json.dumps(doc)
    assert "Example" not in text and "#1f6fb2" not in text
    code, out, _ = env.run("looks", "show")
    assert code == 0 and "presets:   example" in out and "work -> example" in out

    code, out, _ = env.run("looks", "publish", str(EXAMPLE))
    assert code == 0 and "Published again, unchanged" in out
    code, out, _ = env.run("looks", "clear")
    assert code == 0 and "Cleared" in out
    assert not path.exists()
    assert show(env)["stored"] is False
    digest = doc["sha256"][:12]
    assert audited() == [("looks-publish", digest, "ok"), ("looks-publish", digest, "ok"),
                         ("looks-clear", "-", "ok")]


def test_a_tar_stream_on_stdin_is_published_with_where_it_came_from(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.10 50000 192.0.2.1 22")
    real = subprocess.run(["tar", "-cf", "-", "-C", str(EXAMPLE), "."], capture_output=True,
                          check=True).stdout  # the command the contract gives
    code, out, err = publish_stream(env, real)
    assert code == 0, err
    doc = show(env)
    assert doc["from"]["how"] == "stdin" and doc["from"]["address"] == "192.0.2.10"
    assert doc["from"]["folder"] is None
    # The same folder, either way in: the same files, the same digest.
    env.run("looks", "clear")
    env.run("looks", "publish", str(EXAMPLE))
    assert show(env)["sha256"] == doc["sha256"]


def test_publish_takes_one_folder_and_only_publish_does(env):  # noqa: F811
    assert env.run("looks", "publish")[0] == 1
    assert env.run("looks", "show", str(EXAMPLE))[0] == 1
    assert env.run("looks", "clear", "--json")[0] == 1
    with pytest.raises(SystemExit):
        env.run("looks", "publish", str(EXAMPLE), str(EXAMPLE))
    env.app.stdin_isatty = lambda: True
    code, _, err = env.run("looks", "publish", "-")
    assert code == 1 and "tar stream on its input" in err


def test_what_is_stored_is_rewritten_from_checked_values(env, tmp_path):  # noqa: F811
    folder = tmp_path / "desk-looks"
    shutil.copytree(EXAMPLE, folder)
    doc = example_doc()
    doc["presets"][0]["brand"]["w"] = 20.0  # a whole number written as one with a point
    (folder / "looks.json").write_text(json.dumps(doc, indent=8) + "\n")
    assert env.run("looks", "publish", str(folder))[0] == 0
    stored = looks.load().folder
    kept = json.loads(stored.files["looks.json"])
    w = kept["presets"][0]["brand"]["w"]
    assert w == 20 and isinstance(w, int)
    assert len(stored.files["looks.json"]) < len((folder / "looks.json").read_bytes())
    assert stored.files["logos/example.png"] == (EXAMPLE / "logos" / "example.png").read_bytes()


# --- refused whole, and what was there stays ------------------------------------------------


def _bad_folder_cases():
    def edit(f):
        def make(folder: Path):
            doc = example_doc()
            f(doc)
            (folder / "looks.json").write_text(json.dumps(doc))
        return make

    def put(name, data):
        def make(folder: Path):
            (folder / name).write_bytes(data)
        return make

    def link(name, to):
        def make(folder: Path):
            (folder / name).unlink()
            (folder / name).symlink_to(to)
        return make

    def fifo(name):
        def make(folder: Path):
            (folder / name).unlink()
            os.mkfifo(folder / name)
        return make

    def swap_dir(name):
        def make(folder: Path):
            shutil.rmtree(folder / name)
            (folder / name).symlink_to(EXAMPLE / name)
        return make

    return {
        "an unknown field": (edit(lambda d: d.update(extra=1)), "unknown field"),
        "a newer version": (edit(lambda d: d.update(schema="shipshape.desk-looks/2")),
                            "is not shipshape.desk-looks/1"),
        "a logo given as a path": (edit(lambda d: d["presets"][0]["brand"].update(
            logo="../../.ssh/id_ed25519")), "brand.logo"),
        "a null list": (edit(lambda d: d.update(palettes=None)), "palettes is not a list"),
        "no looks.json": (lambda folder: (folder / "looks.json").unlink(), "no looks.json"),
        "looks.json is not JSON": (put("looks.json", b"{"), "is not JSON"),
        "looks.json is not UTF-8": (put("looks.json", b'{"schema": "\xff"}'), "not UTF-8"),
        "a field named twice": (put("looks.json", b'{"schema":"shipshape.desk-looks/1",'
                                    b'"schema":"shipshape.desk-looks/1"}'), "twice"),
        "NaN": (put("looks.json", b'{"schema":NaN}'), "is not JSON"),
        "looks.json too big": (put("looks.json", b" " * (looks.LOOKS_BYTES + 1)), "over 64 KiB"),
        "a logo that is a script": (put("logos/example.png", b"#!/bin/sh\nrm -rf ~\n"),
                                    "not a PNG picture"),
        "a logo too big": (put("logos/example.png", b"\x89PNG\r\n\x1a\n" + b"0" * 65536),
                           "over 64 KiB"),
        "a palette missing": (lambda folder: (folder / "palettes/looks-example-dark.json")
                              .unlink(), "is not there"),
        "looks.json a link": (link("looks.json", EXAMPLE / "looks.json"), "a link is never"),
        "a logo a link": (link("logos/example.png", "/etc/hostname"), "a link is never"),
        "palettes/ a link": (swap_dir("palettes"), "a link is never"),
        "a logo a FIFO": (fifo("logos/example.png"), "not a regular file"),
        "a palette a folder": (lambda folder: ((folder / "palettes/looks-example-dark.json")
                                               .unlink(), (folder / "palettes/looks-example-dark"
                                                           ".json").mkdir()),
                               "not a regular file"),
    }


BAD_FOLDERS = _bad_folder_cases()


@pytest.mark.parametrize("case", sorted(BAD_FOLDERS))
def test_a_bad_folder_is_refused_whole_and_the_stored_one_stays(env, tmp_path, case):  # noqa: F811
    assert env.run("looks", "publish", str(EXAMPLE))[0] == 0
    before = looks.store_path().read_bytes()
    folder = tmp_path / "desk-looks"
    shutil.copytree(EXAMPLE, folder)
    make, why = BAD_FOLDERS[case]
    make(folder)
    code, out, err = env.run("looks", "publish", str(folder))
    assert code == 1 and out == ""
    assert "The looks folder is refused, and nothing changed" in err and why in err, err
    assert looks.store_path().read_bytes() == before
    assert audited()[-1] == ("looks-publish", "-", "refused")


def test_files_looks_json_does_not_name_are_never_read(env, tmp_path):  # noqa: F811
    folder = tmp_path / "desk-looks"
    shutil.copytree(EXAMPLE, folder)
    (folder / "README").write_text("notes")
    (folder / "palettes" / "looks-unused.json").write_text("not even JSON")
    (folder / "logos" / "unused.png").symlink_to("/etc/hostname")
    os.mkfifo(folder / "logos" / "pipe.png")  # read, it would hang
    assert env.run("looks", "publish", str(folder))[0] == 0
    assert sorted(looks.load().folder.files) == sorted(example_files())


def test_where_it_came_from_is_kept_as_one_short_line(env, tmp_path):  # noqa: F811
    folder = tmp_path / "desk\nlooks\x7f"
    shutil.copytree(EXAMPLE, folder)
    env.app.actor = lambda: {"actor": "desk\ttop", "actor_key": "SHA256:" + "A" * 43}
    assert env.run("looks", "publish", str(folder))[0] == 0
    src = show(env)["from"]
    assert src["folder"].endswith("desk?looks?") and src["actor"] == "desk?top"
    assert src["actor_key"] == "SHA256:" + "A" * 43


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_a_file_it_may_not_read_is_said_as_such(env, tmp_path):  # noqa: F811
    folder = tmp_path / "desk-looks"
    shutil.copytree(EXAMPLE, folder)
    (folder / "looks.json").chmod(0)
    code, _, err = env.run("looks", "publish", str(folder))
    assert code == 1 and "looks.json could not be opened (Permission denied)" in err
    (folder / "looks.json").chmod(0o600)
    (folder / "logos").chmod(0)
    try:
        code, _, err = env.run("looks", "publish", str(folder))
        assert code == 1 and "logos could not be opened (Permission denied)" in err
    finally:
        (folder / "logos").chmod(0o700)


def test_a_missing_folder_is_said_plainly(env, tmp_path):  # noqa: F811
    code, _, err = env.run("looks", "publish", str(tmp_path / "nowhere"))
    assert code == 1 and "is not there" in err
    (tmp_path / "file").write_text("x")
    code, _, err = env.run("looks", "publish", str(tmp_path / "file"))
    assert code == 1 and "is not a folder" in err


def _big_header(name: str, size: int) -> bytes:
    """A tar whose one member claims `size` bytes and holds a few (a size bomb)."""
    info = tarfile.TarInfo(name)
    info.size = size
    head = info.tobuf(format=tarfile.USTAR_FORMAT if size < 8 ** 11 else tarfile.GNU_FORMAT)
    return head + b"x" * 512 + b"\0" * 1024


def _link(name, target, kind=tarfile.SYMTYPE):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info, None


def _special(name, kind):
    info = tarfile.TarInfo(name)
    info.type = kind
    return info, None


def _cut_in_the_logo() -> bytes:
    data = example_tar()
    with tarfile.open(fileobj=io.BytesIO(data)) as t:
        logo = t.getmember("./logos/example.png")
    return data[:logo.offset_data + 100]


def _bad_stream_cases():
    files = example_files()
    many_palettes = [reg(f"./palettes/looks-p{i}.json", b"{}") for i in range(17)]
    fill = [reg(f"./logos/l{i}.png", b"\x89PNG\r\n\x1a\n" + b"0" * 60000) for i in range(9)]
    sparse = tarfile.TarInfo("./logos/example.png")
    sparse.type = tarfile.GNUTYPE_SPARSE
    sparse.size = 0
    return {
        "a symlink": (example_tar(_link("./logos/x.png", "/etc/passwd")), "not a regular file"),
        "a hard link": (example_tar(_link("./logos/x.png", "looks.json", tarfile.LNKTYPE)),
                        "not a regular file"),
        "a device": (example_tar(_special("./logos/x.png", tarfile.CHRTYPE)),
                     "not a regular file"),
        "a FIFO": (example_tar(_special("./logos/x.png", tarfile.FIFOTYPE)),
                   "not a regular file"),
        "a sparse file": (tar_of([(sparse, None)]), "not a regular file"),
        "an absolute path": (example_tar(reg("/etc/cron.d/x", b"x")), "not part of a looks"),
        "a dot-dot path": (example_tar(reg("../looks.json", b"x")), "not part of a looks"),
        "a dot-dot inside": (example_tar(reg("./palettes/../../x.json", b"x")),
                             "not part of a looks"),
        "a folder of its own": (example_tar(folder_dir("./other")), "not part of a looks"),
        "another file": (example_tar(reg("./README", b"notes")), "not part of a looks"),
        "macOS's ._ file": (example_tar(reg("./._looks.json", b"x")), "not part of a looks"),
        "a name twice": (example_tar(reg("./looks.json", files["looks.json"])), "twice"),
        "too many palettes": (example_tar(*many_palettes), "more than 16 files in palettes"),
        "too many members": (tar_of([folder_dir("./")] * 37), "more than 36 members"),
        "a member over its limit": (example_tar(reg("./logos/big.png", b"0" * 65537)),
                                    "over 64 KiB"),
        "a size bomb": (_big_header("looks.json", 10 * 1024 ** 3), "over 64 KiB"),
        "more bytes in all than allowed": (example_tar(*fill), "over 512 KiB of files"),
        "a stream over 1 MiB": (b"\0" * (looks.STREAM_BYTES + 1), "over 1024 KiB"),
        "not a tar": (b"hello, this is not a tar stream" * 40, "not a tar stream"),
        "a compressed tar": (__import__("gzip").compress(example_tar()), "not a tar stream"),
        "a tar cut short": (_cut_in_the_logo(), "cut short"),
        "a bad folder inside it": (example_tar(files={**files, "logos/example.png": b"GIF89a"}),
                                   "not a PNG picture"),
    }


BAD_STREAMS = _bad_stream_cases()


@pytest.mark.parametrize("case", sorted(BAD_STREAMS))
def test_a_bad_tar_stream_is_refused_whole_and_the_stored_one_stays(env, case):  # noqa: F811
    assert publish_stream(env, example_tar())[0] == 0
    before = looks.store_path().read_bytes()
    data, why = BAD_STREAMS[case]
    code, out, err = publish_stream(env, data)
    assert code == 1 and out == ""
    assert "The looks folder is refused, and nothing changed" in err and why in err, err
    assert looks.store_path().read_bytes() == before
    assert sorted(p.name for p in looks.store_path().parent.iterdir()) == [looks.STORE]


def _empty_pax_headers(n: int) -> bytes:
    info = tarfile.TarInfo("././@PaxHeader")
    info.type = tarfile.XHDTYPE
    return info.tobuf(format=tarfile.USTAR_FORMAT) * n


def _bad_sparse(headers: dict[str, str], body: bytes) -> bytes:
    info = tarfile.TarInfo("looks.json")
    info.size = len(body)
    info.pax_headers = headers
    return tar_of([(info, body)])


CRAFTED = {  # each once made tarfile raise something other than TarError
    "many pax headers in a row": _empty_pax_headers(1500) + example_tar(),
    "many GNU long names in a row": b"".join(
        tarfile.TarInfo("x" * 200).tobuf(format=tarfile.GNU_FORMAT)[:1024] for _ in range(900))
    + example_tar(),
    "a sparse map that is not numbers": _bad_sparse(
        {"GNU.sparse.major": "1", "GNU.sparse.minor": "0", "GNU.sparse.name": "looks.json"},
        b"zz\nqq\n"),
    "a sparse size that is not a number": _bad_sparse(
        {"GNU.sparse.map": "a,b,c", "GNU.sparse.size": "q"}, b"x"),
}


@pytest.mark.parametrize("case", sorted(CRAFTED))
def test_a_crafted_header_is_a_refusal_never_a_traceback(env, case):  # noqa: F811
    assert len(CRAFTED[case]) <= looks.STREAM_BYTES
    code, out, err = publish_stream(env, CRAFTED[case])
    assert code == 1 and out == ""
    assert "The looks folder is refused, and nothing changed" in err, err
    assert audited() == [("looks-publish", "-", "refused")]


def test_the_stream_is_read_with_a_hard_limit(env):  # noqa: F811
    class Endless(io.RawIOBase):
        def __init__(self):
            self.given = 0

        def readable(self):
            return True

        def readinto(self, b):
            n = len(b)
            b[:n] = b"\0" * n
            self.given += n
            return n

    source = io.BufferedReader(Endless())
    with pytest.raises(looks.Refused, match="over 1024 KiB"):
        looks.read_stream(source)
    assert source.raw.given <= looks.STREAM_BYTES + 1 + io.DEFAULT_BUFFER_SIZE


def test_a_stream_with_a_leading_dot_slash_or_none_reads_the_same(env):  # noqa: F811
    files = example_files()
    plain = tar_of([reg(n, b) for n, b in files.items()])
    assert looks.tar_files(plain) == looks.tar_files(example_tar()) == files


def test_a_failed_write_leaves_the_stored_one(env, monkeypatch):  # noqa: F811
    assert env.run("looks", "publish", str(EXAMPLE))[0] == 0
    before = looks.store_path().read_bytes()
    doc = example_doc()
    doc["tag_looks"] = []
    real = os.replace

    def cut_short(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", cut_short)
    files = {**example_files(), "looks.json": json.dumps(doc).encode()}
    with pytest.raises(OSError):
        looks.save(looks.read_folder(looks.from_files(files)),
                   dict.fromkeys(looks.SOURCE_FIELDS) | {"how": "stdin"}, "now")
    monkeypatch.setattr(os, "replace", real)
    assert looks.store_path().read_bytes() == before
    assert sorted(p.name for p in looks.store_path().parent.iterdir()) == [looks.STORE]


def test_a_hand_edited_store_is_a_problem_never_looks(env):  # noqa: F811
    assert env.run("looks", "publish", str(EXAMPLE))[0] == 0
    path = looks.store_path()
    good = json.loads(path.read_text())

    def stored(doc_or_bytes) -> dict:
        path.write_bytes(doc_or_bytes if isinstance(doc_or_bytes, bytes)
                         else json.dumps(doc_or_bytes).encode())
        return show(env)

    bad = copy.deepcopy(good)
    bad["files"]["../../evil"] = "eA=="
    assert "cannot hold" in stored(bad)["problem"]
    bad = copy.deepcopy(good)
    bad["files"]["logos/example.png"] = "IyEvYmluL3NoCg=="  # #!/bin/sh
    assert "not a PNG" in stored(bad)["problem"]
    bad = copy.deepcopy(good)
    bad["sha256"] = "0" * 64
    assert "digest" in stored(bad)["problem"]
    bad = copy.deepcopy(good)
    bad["files"]["palettes/looks-other.json"] = good["files"]["palettes/looks-example-dark.json"]
    assert "other than" in stored(bad)["problem"]
    assert "not JSON" in stored(b"{")["problem"]
    code, out, _ = env.run("looks", "show")
    assert code == 1 and "not usable" in out
    path.unlink()
    path.symlink_to(EXAMPLE / "looks.json")
    assert "not a regular file" in show(env)["problem"]


@pytest.mark.parametrize("field,value,why", [
    ("published_at", "\x1b]0;pwn\x07", "published_at"),
    ("published_at", "x\nship-fleet: all good", "published_at"),
    ("version", True, "version"),
    ("version", 1.0, "version"),
    ("how", "carrier pigeon", "how"),
    ("how", "stdin\u202e", "how"),
    ("actor", "desk\x9btop", "actor"),
    ("actor_key", "not a fingerprint", "actor_key"),
    ("address", "\u2066192.0.2.1", "address"),
    ("address", "somewhere", "address"),
    ("folder", "/etc", "folder"),  # a folder for a stream
])
def test_where_a_store_came_from_is_checked_too(env, monkeypatch, field, value, why):  # noqa: F811
    publish_stream(env, example_tar())
    path = looks.store_path()
    doc = json.loads(path.read_text())
    (doc["from"] if field in looks.SOURCE_FIELDS else doc)[field] = value
    path.write_text(json.dumps(doc))
    shown = show(env)
    assert why in shown["problem"]
    code, out, _ = env.run("looks", "show")
    assert code == 1 and "\x1b" not in out and "all good" not in out.split("\n", 1)[1]


def test_something_else_in_the_stores_place_is_said_not_thrown(env):  # noqa: F811
    looks.store_path().mkdir(parents=True)
    assert "not a regular file" in show(env)["problem"]
    code, _, err = env.run("looks", "publish", str(EXAMPLE))
    assert code == 1 and "Couldn't store the looks" in err and "Traceback" not in err
    assert [a[2] for a in audited()] == ["failed"]
    code, _, err = env.run("looks", "clear")
    assert code == 1 and "Couldn't clear the looks" in err


# --- who may, and when ----------------------------------------------------------------------


def test_show_answers_while_the_vault_is_locked_and_changes_wait(env, monkeypatch):  # noqa: F811
    from fleetledger import vault

    assert env.run("looks", "publish", str(EXAMPLE))[0] == 0
    before = looks.store_path().read_bytes()
    monkeypatch.setattr(vault, "state", lambda: "locked")
    assert show(env)["stored"] is True
    for argv in (["publish", str(EXAMPLE)], ["clear"]):
        code, _, err = env.run("looks", *argv)
        assert code == 1 and "vault is locked" in err
    assert looks.store_path().read_bytes() == before


def test_a_copy_that_does_not_act_refuses_all_three(env, store):  # noqa: F811
    assert env.run("looks", "publish", str(EXAMPLE))[0] == 0
    before = looks.store_path().read_bytes()
    handover.write(store, handover.Marker("standby", handover.now(), "vm.example", 1))
    for argv in (["show"], ["show", "--json"], ["publish", str(EXAMPLE)], ["clear"]):
        code, out, err = env.run("looks", *argv)
        assert code == 1 and out == "" and "standby" in err
    assert looks.store_path().read_bytes() == before


def test_the_command_is_listed_and_documented():
    assert "looks" in cli.COMMANDS
    contract = (ROOT / "contracts" / "ship-fleet.md").read_text()
    assert "## `ship-fleet looks`" in contract
    assert "`looks show --json` | one document | `shipshape.fleetledger-looks/1`" in contract


# --- the same rules as the Desk's (lookfolder.js) ---------------------------------------------


NODE = shutil.which("node")
JS = """
import { checkLooks, checkPalette } from __LOOKFOLDER__;
import { builtinParts } from __LOOKSJS__;
const cases = __CASES__;
const out = {};
const b = builtinParts();
for (const [id, [kind, doc]] of Object.entries(cases)) {
  try { kind === "looks" ? checkLooks(doc, b) : checkPalette(doc); out[id] = null; }
  catch (e) { out[id] = String(e.message); }
}
out.__builtin__ = { palettes: Object.fromEntries(b.palettes.map((p) => [p.id, p.dark])),
  presets: Object.keys(b.presets), finishes: Object.keys(b.finishes), types: Object.keys(b.types),
  densities: Object.keys(b.densities), fonts: b.fonts };
console.log(JSON.stringify(out));
"""


def desk_says(tmp_path: Path, cases: dict[str, tuple[str, object]]) -> dict:
    """What lookfolder.js says of each case: None (accepted) or why it refused."""
    if NODE is None:
        if os.environ.get("CI"):
            pytest.fail("node is needed for this test (CI has it)")
        pytest.skip("needs node")
    script = tmp_path / "desk-says.mjs"
    script.write_text(JS.replace("__LOOKFOLDER__", json.dumps((DESK / "lookfolder.js").as_uri()))
                      .replace("__LOOKSJS__", json.dumps((DESK / "looks.js").as_uri()))
                      .replace("__CASES__", json.dumps(cases)))
    run = subprocess.run([NODE, str(script)], capture_output=True, text=True, timeout=60,
                         cwd=tmp_path)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def fleetledger_says(kind: str, doc: object) -> str | None:
    try:
        looks.check_looks(doc) if kind == "looks" else looks.check_palette(doc)
        return None
    except looks.Refused as exc:
        return exc.why


def _contract_cases() -> dict[str, tuple[str, object]]:
    cases = {}
    for name, kind in (("desk-looks", "looks"), ("desk-palette", "palette")):
        for sub in ("", "invalid"):
            for f in sorted((EXAMPLES / name / sub).glob("*.json")):
                cases[f"{name}/{sub}/{f.name}"] = (kind, json.loads(f.read_text()))
    return cases


def _edits() -> dict[str, tuple[str, object]]:
    """More cases, each a small change to the placeholder's looks.json or palette."""
    base = example_doc()
    palette = json.loads((EXAMPLE / "palettes" / "looks-example-dark.json").read_text())

    def looks_with(f):
        d = copy.deepcopy(base)
        f(d)
        return ("looks", d)

    def palette_with(f):
        d = copy.deepcopy(palette)
        f(d)
        return ("palette", d)

    p0 = lambda d: d["presets"][0]  # noqa: E731
    return {
        "placeholder": ("looks", base),
        "brand w as 20.0": looks_with(lambda d: p0(d)["brand"].update(w=20.0)),
        "brand w true": looks_with(lambda d: p0(d)["brand"].update(w=True)),
        "brand w 65": looks_with(lambda d: p0(d)["brand"].update(w=65)),
        "brand w 7.5": looks_with(lambda d: p0(d)["brand"].update(w=7.5)),
        "brand round 0": looks_with(lambda d: p0(d)["brand"].update(round=0)),
        "brand font": looks_with(lambda d: p0(d)["brand"].update(font="Lato")),
        "brand font unknown": looks_with(lambda d: p0(d)["brand"].update(font="Comic Sans")),
        "brand no name": looks_with(lambda d: p0(d)["brand"].pop("name")),
        "logo jpg": looks_with(lambda d: p0(d)["brand"].update(logo="a-b.jpg")),
        "logo gif": looks_with(lambda d: p0(d)["brand"].update(logo="a.gif")),
        "logo upper": looks_with(lambda d: p0(d)["brand"].update(logo="A.png")),
        "label 40": looks_with(lambda d: p0(d).update(label="é" * 40)),
        "label 41": looks_with(lambda d: p0(d).update(label="x" * 41)),
        "label newline": looks_with(lambda d: p0(d).update(label="a\nb")),
        "label empty": looks_with(lambda d: p0(d).update(label="")),
        "label emoji": looks_with(lambda d: p0(d).update(label="\U0001f30a" * 40)),
        "label a number": looks_with(lambda d: p0(d).update(label=5)),
        "id upper": looks_with(lambda d: p0(d).update(id="Example")),
        "id 32 long": looks_with(lambda d: p0(d).update(id="a" * 32)),
        "id trailing newline": looks_with(lambda d: p0(d).update(id="example\n")),
        "id a desk preset": looks_with(lambda d: p0(d).update(id="slate")),
        "preset twice": looks_with(lambda d: d["presets"].append(copy.deepcopy(p0(d)))),
        "palette light as dark": looks_with(lambda d: p0(d).update(palette="paper")),
        "palette unknown": looks_with(lambda d: p0(d).update(palette="nope")),
        "palette a list": looks_with(lambda d: p0(d).update(palette=["graphite"])),
        "light dark one": looks_with(lambda d: p0(d).update(light="graphite")),
        "light a desk one": looks_with(lambda d: p0(d).update(light="desk-linen")),
        "finish unknown": looks_with(lambda d: p0(d).update(finish="matte")),
        "finish toString": looks_with(lambda d: p0(d).update(finish="toString")),
        "type unknown": looks_with(lambda d: p0(d).update(type="serif")),
        "density unknown": looks_with(lambda d: p0(d).update(density="airy")),
        "band one": looks_with(lambda d: p0(d).update(band=["#000000"])),
        "band alpha": looks_with(lambda d: p0(d).update(band=["#00000000", "#000000"])),
        "no band": looks_with(lambda d: p0(d).pop("band", None)),
        "no brand": looks_with(lambda d: p0(d).pop("brand", None)),
        "palette id twice": looks_with(lambda d: d["palettes"].append(copy.deepcopy(
            d["palettes"][0]))),
        "palette id a desk one": looks_with(lambda d: d["palettes"][0].update(id="graphite")),
        "palette dark a string": looks_with(lambda d: d["palettes"][0].update(dark="true")),
        "17 palettes": looks_with(lambda d: d.update(palettes=[
            {"id": f"looks-p{i}", "label": "P", "dark": True} for i in range(17)])),
        "16 palettes": looks_with(lambda d: d.update(palettes=[
            *d["palettes"], *[{"id": f"looks-p{i}", "label": "P", "dark": True}
                              for i in range(15)]])),
        "33 presets": looks_with(lambda d: d.update(presets=[
            {**copy.deepcopy(p0(d)), "id": f"p{i}"} for i in range(33)])),
        "17 logos": looks_with(lambda d: d.update(presets=[
            {**copy.deepcopy(p0(d)), "id": f"p{i}",
             "brand": {"name": "N", "logo": f"l{i}.png"}} for i in range(17)])),
        "65 tag looks": looks_with(lambda d: d.update(tag_looks=[
            {"tag": f"t{i}", "preset": "moss"} for i in range(65)])),
        "tag twice": looks_with(lambda d: d.update(tag_looks=[
            {"tag": "work", "preset": "moss"}, {"tag": "work", "preset": "slate"}])),
        "tag a desk preset": looks_with(lambda d: d.update(tag_looks=[
            {"tag": "home", "preset": "ember"}])),
        "tag unknown preset": looks_with(lambda d: d.update(tag_looks=[
            {"tag": "home", "preset": "nope"}])),
        "tag preset before it is listed": looks_with(lambda d: d.update(presets=[])),
        "tag preset a number": looks_with(lambda d: d.update(tag_looks=[
            {"tag": "home", "preset": 1}])),
        "tag with a space": looks_with(lambda d: d.update(tag_looks=[
            {"tag": "a b", "preset": "moss"}])),
        "no schema": looks_with(lambda d: d.pop("schema")),
        "a list": ("looks", []),
        "a string": ("looks", "looks"),
        "palette placeholder": ("palette", palette),
        "palette no colors": palette_with(lambda d: d.pop("colors", None)),
        "palette alpha": palette_with(lambda d: d.update(background="#11223344")),
        "palette short colour": palette_with(lambda d: d.update(background="#123")),
        "palette colour in a list": palette_with(lambda d: d.update(cursor=["#112233"])),
        "palette 17 ansi": palette_with(lambda d: d["ansi"].append("#000000")),
        "palette ansi a number": palette_with(lambda d: d["ansi"].__setitem__(0, 0)),
        "palette unknown colour": palette_with(lambda d: d.setdefault("colors", {}).update(
            sparkle="#000000")),
        "palette colors a list": palette_with(lambda d: d.update(colors=[])),
        "palette named colour bad": palette_with(lambda d: d.setdefault("colors", {}).update(
            accent="red")),
        "palette unknown field": palette_with(lambda d: d.update(name="x")),
    }


def test_fleetledger_and_the_desk_accept_and_refuse_the_same(tmp_path):
    cases = {**_contract_cases(), **_edits()}
    desk = desk_says(tmp_path, cases)
    differ = {case: (desk[case], fleetledger_says(*cases[case])) for case in cases
              if (desk[case] is None) != (fleetledger_says(*cases[case]) is None)}
    assert differ == {}
    # And the examples say what they say they do.
    for case in _contract_cases():
        assert (desk[case] is None) == ("/invalid/" not in case), case
    assert fleetledger_says(*cases["placeholder"]) is None
    assert sum(desk[c] is None for c in cases) > 15 and sum(desk[c] is not None for c in cases) > 30


def test_the_desk_offers_what_fleetledger_thinks_it_does(tmp_path):
    builtin = desk_says(tmp_path, {})["__builtin__"]
    assert builtin["palettes"] == looks.DESK_PALETTES
    assert builtin["presets"] == list(looks.DESK_PRESETS)
    assert builtin["finishes"] == list(looks.FINISHES)
    assert builtin["types"] == list(looks.TYPES)
    assert builtin["densities"] == list(looks.DENSITIES)
    assert sorted(builtin["fonts"]) == sorted(looks.FONTS)
    # And the contract's schema lists the same.
    schema = json.loads((ROOT / "contracts" / "desk-looks.schema.json").read_text())
    preset = schema["properties"]["presets"]["items"]["properties"]
    assert preset["finish"]["enum"] == list(looks.FINISHES)
    assert preset["type"]["enum"] == list(looks.TYPES)
    assert preset["density"]["enum"] == list(looks.DENSITIES)
    assert sorted(preset["brand"]["properties"]["font"]["enum"]) == sorted(looks.FONTS)


def test_where_fleetledger_is_stricter_than_the_desk(tmp_path):
    """Corners the Desk lets through and FleetLedger refuses: never the other way."""
    base = example_doc()
    stricter = {
        "null palettes": ("looks", {"schema": looks.SCHEMA, "palettes": None}),
        "null presets": ("looks", {"schema": looks.SCHEMA, "presets": None}),
        "null tag looks": ("looks", {**base, "tag_looks": None}),
        "a lone surrogate": ("looks", {**base, "presets": [
            {**base["presets"][0], "label": "\ud800"}]}),
    }
    desk = desk_says(tmp_path, stricter)
    for case, (kind, doc) in stricter.items():
        assert desk[case] is None, case
        assert fleetledger_says(kind, doc) is not None, case
    raw = b'{"schema":"shipshape.desk-looks/1","schema":"shipshape.desk-looks/1"}'
    with pytest.raises(looks.Refused, match="twice"):
        looks.parse_json(raw, "looks.json")


# --- the keys FleetLedger forces to one command ------------------------------------------------


class Untouchable(io.BytesIO):
    def read(self, *a):
        raise AssertionError("stdin read")

    readline = read


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_the_keys_forced_to_one_command_never_reach_looks(vm, monkeypatch):  # noqa: F811
    """A trusted machine's bundle key and the pairing key run their own command whatever the
    client asks for (sshd puts what it asked in SSH_ORIGINAL_COMMAND), so neither can
    publish or clear looks, with a valid folder on stdin or not."""
    import re
    import shlex
    import sys

    from fl_harness import keygen

    from fleetledger import authkeys

    seed()
    trust(vm, "laptop")
    assert vm.run("pair", "on")[0] == 0
    forced = [shlex.split(re.search(r'command="([^"]*)"', line).group(1)) for line in block()]
    assert sorted(argv[1:] for argv in forced) == [["machine-bundle", "laptop"],
                                                   ["machine-pair"]]
    assert all(argv[0] == authkeys.SHIP_FLEET for argv in forced)
    # A machine's name is one word: it can never add one to its forced command.
    key = keygen(vm.tmp / "other", "other").with_name("other.pub").read_text()
    for name in ("laptop looks publish -", "laptop;looks", "laptop\nlooks", "looks publish"):
        assert authkeys.line_for(name, key) is None

    assert vm.run("looks", "publish", str(EXAMPLE))[0] == 0  # a shell on FleetLedger may
    before = looks.store_path().read_bytes()
    monkeypatch.setattr(sys, "stdin", Untouchable())
    for argv in forced:
        for asked in ("ship-fleet looks publish -", "looks publish -", "looks clear",
                      "machine-pair; ship-fleet looks clear", f"{argv[1]} looks clear",
                      f"ship-fleet looks publish {EXAMPLE}"):
            monkeypatch.setenv("SSH_ORIGINAL_COMMAND", asked)
            vm.app.stdin_bytes = Untouchable(example_tar())
            vm.run(*argv[1:])
            assert looks.store_path().read_bytes() == before, (argv, asked)
    looks.clear()
    for argv in forced:
        monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "looks publish -")
        vm.app.stdin_bytes = Untouchable(example_tar())
        vm.run(*argv[1:])
        assert not looks.store_path().exists(), argv
    vm.app.stdin_bytes = None
