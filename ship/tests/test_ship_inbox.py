"""ship-ctl inbox: files a person sends to the Ship land in ~/inbox, whole, never
overwriting, never through a symlink the agent planted."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unicodedata

import pytest
from ship_harness import SHIP, assert_valid

sys.path.insert(0, str(SHIP / "lib"))
from shipctl import inbox  # noqa: E402


def put(ship, name: str, data: bytes, size: int | None = None):
    """`ship-ctl inbox put` with bytes on stdin (the harness's ctl is text only)."""
    size = len(data) if size is None else size
    proc = subprocess.run(
        [sys.executable, "-I", str(ship.bundle / "bin/ship-ctl"), "inbox", "put",
         "--name", name, "--size", str(size), "--json"],
        env=ship.env, input=data, capture_output=True, timeout=60)
    return proc.returncode, json.loads(proc.stdout), proc.stderr.decode()


def box(ship):
    return ship.p("/home/agent/inbox")


def staging(ship):
    return ship.p("/home/agent/.local/state/shipshape/incoming")


def test_a_file_lands_whole_private_and_not_executable(ship):
    rc, doc, err = put(ship, "notes.txt", b"hello\n")
    assert rc == 0, err
    assert_valid("ship-ctl-result", doc)
    assert doc["command"] == "inbox"
    assert doc["data"] == {"name": "notes.txt", "given": "notes.txt", "size": 6,
                           "path": "/home/agent/inbox/notes.txt", "renamed": False}
    assert doc["changed"] == ["/home/agent/inbox/notes.txt"]
    stored = box(ship) / "notes.txt"
    assert stored.read_bytes() == b"hello\n"
    assert oct(stored.stat().st_mode & 0o7777) == "0o600"
    assert oct(box(ship).stat().st_mode & 0o7777) == "0o700"
    assert list(staging(ship).iterdir()) == []  # the staging copy is gone
    logged = ship.scenario()["logged"]
    assert logged[-1]["message"] == "inbox: notes.txt (6 bytes)"


def test_a_taken_name_gets_a_number_and_nothing_is_overwritten(ship):
    assert put(ship, "report.pdf", b"one")[0] == 0
    rc, doc, _ = put(ship, "report.pdf", b"two")
    assert rc == 0
    assert doc["data"]["name"] == "report-2.pdf" and doc["data"]["renamed"] is True
    assert "report.pdf was taken, so it is report-2.pdf" in doc["messages"][0]
    assert put(ship, "report.pdf", b"three")[1]["data"]["name"] == "report-3.pdf"
    assert (box(ship) / "report.pdf").read_bytes() == b"one"
    assert (box(ship) / "report-2.pdf").read_bytes() == b"two"
    assert put(ship, "a.tar.gz", b"x")[0] == 0
    assert put(ship, "a.tar.gz", b"y")[1]["data"]["name"] == "a-2.tar.gz"


@pytest.mark.parametrize("given, stored", [
    ("../../etc/passwd", "passwd"),
    ("..\\..\\boot.ini", "boot.ini"),
    (".bashrc", "bashrc"),
    ("-rf --no-preserve-root", "rf --no-preserve-root"),
    ("$(reboot)`id`;x|y.sh", "_(reboot)_id__x_y.sh"),
    ("evil‮gnp.exe", "evilgnp.exe"),  # a bidi override is dropped
    ("tab\there\nnewline.txt", "tab here newline.txt"),
    ("Résumé 2026 (final).pdf", "Résumé 2026 (final).pdf"),
    ("  ..  ", "file"),
    ("", "file"),
    ("~/notes", "notes"),
    ("trailing dots...", "trailing dots"),
])
def test_names_are_cleaned_but_still_read_like_the_original(given, stored):
    assert inbox.clean_name(given) == stored


@pytest.mark.parametrize("name, second", [
    ("report.pdf", "report-2.pdf"),
    ("report v1.2.pdf", "report v1.2-2.pdf"),
    ("2026.11.06 notes.md", "2026.11.06 notes-2.md"),
    ("archive.tar.gz", "archive-2.tar.gz"),
    ("backup.TAR.ZST", "backup-2.TAR.ZST"),
    ("README", "README-2"),
    ("odd.extension-that-is-far-too-long", "odd.extension-that-is-far-too-long-2"),
])
def test_a_taken_name_is_numbered_before_its_extension(name, second):
    assert inbox.numbered(name, 2) == second


@pytest.mark.parametrize("name", [
    "A\u202e\u0301B.txt",  # a bidi override between a letter and its accent
    "e\u200b\u0301\u00e9.md", "Résumé (final).pdf", "../$(x)\u202e.txt", "x" * 300 + ".tar.gz",
    "\u0301leading mark.txt", "  -.~#name. .",
    "=\u202e\u0338.txt",  # = and a combining long solidus: they compose into ≠
])
def test_cleaning_a_clean_name_changes_nothing(name):
    once = inbox.clean_name(name)
    assert inbox.clean_name(once) == once
    assert once == unicodedata.normalize("NFC", once)


def test_put_sweeps_staging_files_a_dead_transfer_left(ship, tmp_path):
    stage = staging(ship)
    stage.mkdir(parents=True)
    old, fresh = stage / f".{'a' * 16}.part", stage / f".{'b' * 16}.part"
    other = stage / "notes.txt"
    for f in (old, fresh, other):
        f.write_bytes(b"x" * 10)
    os.utime(old, (1_700_000_000, 1_700_000_000))
    os.utime(other, (1_700_000_000, 1_700_000_000))
    link = stage / f".{'c' * 16}.part"
    link.symlink_to(tmp_path / "target")
    (tmp_path / "target").write_text("keep")
    os.utime(link, (1_700_000_000, 1_700_000_000), follow_symlinks=False)
    assert put(ship, "a.txt", b"hi")[0] == 0
    assert not old.exists()
    assert fresh.exists() and other.exists() and link.is_symlink()
    assert (tmp_path / "target").read_text() == "keep"


def test_a_full_disk_is_cleared_of_dead_transfers_before_the_space_check(ship, monkeypatch):
    """A stale staging file can be what fills the disk: put sweeps it before it
    refuses for lack of space, so the next try can succeed."""
    from shipctl.context import Context
    from shipctl.errors import CtlError
    from shipctl.paths import Paths

    stage = staging(ship)
    stage.mkdir(parents=True)
    dead = stage / f".{'d' * 16}.part"
    dead.write_bytes(b"x" * 100)
    os.utime(dead, (1_700_000_000, 1_700_000_000))
    seen = []

    def free(_ctx):
        seen.append(dead.exists())
        return inbox.KEEP_FREE  # nothing to spare: refused
    monkeypatch.setattr(inbox, "_free_bytes", free)
    ctx = Context(Paths(root=ship.root, bundle=ship.bundle), quiet=True)
    with pytest.raises(CtlError, match="Not enough disk space"):
        inbox.put(ctx, "a.bin", 10, open(os.devnull, "rb"))
    assert seen == [False]  # swept before free space was measured
    assert not dead.exists()


def test_a_long_name_is_cut_and_keeps_its_extension():
    name = inbox.clean_name("é" * 300 + ".pdf")
    assert name.endswith(".pdf") and len(name.encode()) <= inbox.NAME_BYTES
    assert name.encode().decode() == name  # never half a character


def test_a_hostile_name_cannot_leave_the_inbox(ship):
    rc, doc, _ = put(ship, "../../../../etc/shipshape/ship.toml", b"pwned")
    assert rc == 0
    assert doc["data"]["name"] == "ship.toml"
    assert (box(ship) / "ship.toml").read_bytes() == b"pwned"
    assert b"pwned" not in ship.p("/etc/shipshape/ship.toml").read_bytes()


def test_a_file_over_the_limit_is_refused_before_anything_is_read(ship):
    rc, doc, _ = put(ship, "big.iso", b"", size=inbox.MAX_BYTES + 1)
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "invalid-input"
    assert "up to 256 MB each" in doc["error"]["message"]
    assert not box(ship).exists() or list(box(ship).iterdir()) == []


@pytest.mark.parametrize("sent, size, why", [
    (b"abc", 10, "cut short"), (b"abcdef", 3, "More than --size bytes")])
def test_a_transfer_of_the_wrong_length_keeps_nothing(ship, sent, size, why):
    rc, doc, _ = put(ship, "partial.bin", sent, size=size)
    assert rc == 1
    assert doc["error"]["code"] == "invalid-input" and why in doc["error"]["message"]
    assert not (box(ship) / "partial.bin").exists()
    assert list(staging(ship).iterdir()) == []


@pytest.mark.parametrize("rel", ["inbox", ".local/state/shipshape/incoming"])
def test_a_symlink_in_the_way_is_refused_and_its_target_untouched(ship, tmp_path, rel):
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = ship.p("/home/agent") / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists():
        link.rmdir()
    link.symlink_to(target)
    rc, doc, _ = put(ship, "x.txt", b"data")
    assert rc == 1
    assert doc["error"]["code"] == "invalid-input"
    assert "symlink" in doc["error"]["message"]
    assert list(target.iterdir()) == []


def test_the_list_shows_plain_files_newest_first_and_counts_the_rest(ship, tmp_path):
    put(ship, "old.txt", b"1")
    put(ship, "new.txt", b"22")
    os.utime(box(ship) / "old.txt", (1_700_000_000, 1_700_000_000))
    (box(ship) / "a-folder").mkdir()
    (box(ship) / "a-link").symlink_to(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("a file elsewhere, " * 100)
    (box(ship) / "file-link.txt").symlink_to(secret)  # listed by neither name nor size
    rc, doc, _ = ship.ctl("inbox", "list", "--json")
    assert rc == 0
    assert_valid("ship-ctl-result", doc)
    data = doc["data"]
    assert [f["name"] for f in data["files"]] == ["new.txt", "old.txt"]
    assert data["files"][1] == {"name": "old.txt", "size": 1, "modified": "2023-11-14T22:13:20Z"}
    assert data["others"] == 3 and data["exists"] is True and data["truncated"] is False
    assert data["path"] == "/home/agent/inbox" and data["max_bytes"] == inbox.MAX_BYTES


def test_an_inbox_that_is_not_there_yet_lists_as_empty(ship):
    rc, doc, _ = ship.ctl("inbox", "list", "--json")
    assert rc == 0 and doc["data"]["exists"] is False and doc["data"]["files"] == []


def test_remove_takes_plain_files_only_and_missing_is_not_an_error(ship, tmp_path):
    put(ship, "done.txt", b"x")
    (box(ship) / "link").symlink_to(tmp_path / "victim")
    (tmp_path / "victim").write_text("keep me")
    rc, doc, _ = ship.ctl("inbox", "remove", "done.txt", "gone.txt", "link", "--json")
    assert rc == 0
    assert_valid("ship-ctl-result", doc)
    assert doc["data"] == {"removed": ["done.txt"], "missing": ["gone.txt"], "refused": ["link"]}
    assert not (box(ship) / "done.txt").exists()
    assert (tmp_path / "victim").read_text() == "keep me"


@pytest.mark.parametrize("name", ["../x", "a/b", "..", "."])
def test_remove_refuses_a_path(ship, name):
    rc, doc, _ = ship.ctl("inbox", "remove", name, "--json")
    assert rc == 2 and doc["error"]["code"] == "usage"


def test_put_needs_a_name_and_size(ship):
    rc, doc, _ = ship.ctl("inbox", "put", "--json")
    assert rc == 2 and "--name and --size" in doc["error"]["message"]


def test_notify_tells_the_core_agents_a_count_and_no_names(ship):
    ship.ctl("autostart", "--json")
    s = ship.scenario()
    # A Claude that is not an [[autostart]] agent: it is told nothing.
    worker = {**ship.terminal("main"), "address": "t9", "name": "worker-1",
              "labels": {"herdr.space": "w"}, "session": "sess-worker-1",
              "status": {"app": "claude-code", "state": "idle"}}
    s["hc"]["terminals"].append(worker)
    s["hc"]["sent"] = []
    ship.set(hc=s["hc"])
    rc, doc, out = ship.ctl("inbox", "notify", "--count", "2", "--json")
    assert rc == 0, out
    assert_valid("ship-ctl-result", doc)
    assert doc["data"] == {"count": 2, "sent": ["main", "grinder"], "skipped": []}
    line = inbox.notice_text(2)
    assert line.startswith("ShipShape: 2 new files in your inbox (~/inbox)")
    assert ship.prompts() == [["main", line], ["grinder", line]]
    assert inbox.notice_text(1).startswith("ShipShape: 1 new file in")


def test_version_says_the_ship_has_an_inbox(ship):
    rc, doc, _ = ship.ctl("version", "--json")
    assert rc == 0 and doc["data"]["contracts"]["inbox"] == 1


def test_a_nearly_full_disk_refuses_the_file(ship, monkeypatch):
    from shipctl.context import Context
    from shipctl.errors import CtlError
    from shipctl.paths import Paths

    ctx = Context(Paths(root=ship.root, bundle=ship.bundle), quiet=True)
    monkeypatch.setattr(inbox, "_free_bytes", lambda _ctx: inbox.KEEP_FREE + 10)
    with pytest.raises(CtlError) as exc:
        inbox.put(ctx, "a.bin", 11, open(os.devnull, "rb"))
    assert exc.value.code == "invalid-input" and "Not enough disk space" in exc.value.message
