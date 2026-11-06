"""Sending files to a Ship's inbox: `ship-fleet put` and `ship-fleet inbox`, the
shared operation the web UI runs too, and SSH.send, which streams a file to a
command's stdin. The Ship is a fake that answers like ship-ctl inbox."""

from __future__ import annotations

import copy
import io
import json
import sys
from pathlib import Path

import pytest
from fleetledger.errors import FleetError
from fleetledger.ssh import SSH, Result, Unreachable
from fleetledger.web import security
from test_cli import VERSION, FakeShip, result

from fleetledger import cli, inbox

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ship" / "lib"))
from shipctl import inbox as ship_inbox  # noqa: E402


class InboxShip(FakeShip):
    """The CLI tests' fake Ship, plus ship-ctl inbox (as ship/lib/shipctl/inbox.py)."""

    def __init__(self):
        super().__init__()
        self.files: dict[str, dict[str, bytes]] = {}  # ship -> name -> bytes
        self.contract = True
        self.refuse: set[str] = set()  # names put refuses (as for a full disk)
        self.sent: list[tuple[str, list[str], bytes]] = []

    def __call__(self, ship, command):
        if command[:4] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "version"]:
            self.calls.append((ship.name, command))
            doc = copy.deepcopy(VERSION)
            if self.contract:
                doc["data"]["contracts"]["inbox"] = 1
            return 0, json.dumps(doc), ""
        if command[:4] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "inbox"]:
            self.calls.append((ship.name, command))
            args = command[4:-1]
            box = self.files.setdefault(ship.name, {})
            if args[0] == "list":
                files = [{"name": n, "size": len(b), "modified": "2026-01-02T03:04:05Z"}
                         for n, b in box.items()]
                return 0, json.dumps(result("inbox", data={
                    "path": "/home/agent/inbox", "exists": True, "files": files, "others": 0,
                    "truncated": False, "max_bytes": inbox.MAX_BYTES, "free_bytes": 10**10})), ""
            if args[0] == "remove":
                gone = [n for n in args[1:] if box.pop(n, None) is not None]
                return 0, json.dumps(result("inbox", data={
                    "removed": gone, "missing": [n for n in args[1:] if n not in gone],
                    "refused": []})), ""
            if args[0] == "notify":
                return 0, json.dumps(result("inbox", data={
                    "count": int(args[2]), "sent": ["main"],
                    "skipped": [{"name": "grinder", "reason": "not-running"}]})), ""
        return super().__call__(ship, command)

    def put(self, ship, command, data: bytes):
        args = command[4:-1]
        name, size = args[args.index("--name") + 1], int(args[args.index("--size") + 1])
        self.sent.append((ship.name, args, data))
        if name in self.refuse:
            err = {"code": "invalid-input", "message": f"Not enough disk space for {name}."}
            return 1, json.dumps(result("inbox", ok=False, error=err))
        box = self.files.setdefault(ship.name, {})
        stored, n = name, 1
        while stored in box:
            n += 1
            stored = ship_inbox.numbered(name, n)
        box[stored] = data[:size]
        return 0, json.dumps(result("inbox", changed=[f"/home/agent/inbox/{stored}"], data={
            "name": stored, "given": name, "size": size,
            "path": f"/home/agent/inbox/{stored}", "renamed": stored != name}))


class InboxSSH(SSH):
    def __init__(self, inventory, ship_side):
        super().__init__(inventory)
        self.ship_side = ship_side

    def run(self, ship, command, *, input=None, timeout=60):
        code, out, err = self.ship_side(ship, list(command))
        return Result(code, out.encode(), err, ship.names[0])

    def send(self, ship, command, source, *, on_progress=None, timeout=600):
        if ship.name in self.ship_side.down:
            raise Unreachable(f"Cannot reach {ship.name}")
        data = b""
        while chunk := source.read(4):
            data += chunk
            if on_progress:
                on_progress(len(data))
        code, out = self.ship_side.put(ship, list(command), data)
        return Result(code, out.encode(), "", ship.names[0])


@pytest.fixture
def env(store, fake_repo):
    side = InboxShip()
    out, err = io.StringIO(), io.StringIO()
    app = cli.App(out=out, err=err, store_factory=lambda: store, repo_factory=lambda: fake_repo,
                  ssh_factory=lambda inv: InboxSSH(inv, side), sleep=lambda s: None)

    class Env:
        pass

    e = Env()
    e.side, e.app, e.out, e.err = side, app, out, err
    e.run = lambda *argv: cli.main(list(argv), app)
    return e


# --- the same rules on both sides -----------------------------------------------------


@pytest.mark.parametrize("name", [
    "report.pdf", "../../etc/passwd", ".hidden", "-rf", "a‮b.txt", "tab\tname",
    "Résumé (final) [v2].pdf", "x" * 400 + ".tar.gz", "$(id)`x`;|&.sh", "", "~/a", "日本語.txt",
    "A\u202e\u0301B.txt", "report v1.2.pdf",
])
def test_fleetledger_cleans_names_exactly_as_the_ship_does(name):
    cleaned = inbox.clean_name(name)
    assert cleaned == ship_inbox.clean_name(name)
    assert inbox.clean_name(cleaned) == cleaned  # what is sent is what the Ship keeps
    assert inbox.numbered(cleaned, 3) == ship_inbox.numbered(cleaned, 3)


def test_the_size_limits_agree():
    assert inbox.MAX_BYTES == ship_inbox.MAX_BYTES == security.MAX_UPLOAD
    assert inbox.human(inbox.MAX_BYTES) == ship_inbox.human(ship_inbox.MAX_BYTES) == "256 MB"


# --- ship-fleet put ------------------------------------------------------------------


def test_put_streams_each_file_with_its_cleaned_name(env, tmp_path):
    a = tmp_path / "Q3 report.pdf"
    a.write_bytes(b"%PDF-1.7 report")
    b = tmp_path / "$(id).txt"
    b.write_bytes(b"hello")
    assert env.run("put", "ship-01", str(a), str(b)) == 0
    assert env.side.files["ship-01"] == {"Q3 report.pdf": b"%PDF-1.7 report",
                                         "_(id).txt": b"hello"}
    args = env.side.sent[0][1]
    assert args == ["put", "--name", "Q3 report.pdf", "--size", "15"]
    out = env.out.getvalue()
    assert "ship-01: 2 files in ~/inbox: Q3 report.pdf, _(id).txt" in out
    assert "$(id).txt is _(id).txt there" in out


def test_put_never_overwrites_and_says_where_the_file_went(env, tmp_path):
    f = tmp_path / "notes.txt"
    f.write_text("one")
    env.run("put", "ship-01", str(f))
    f.write_text("two")
    assert env.run("put", "ship-01", str(f), "--json") == 0
    text = env.out.getvalue()
    doc = json.loads(text[text.index("\n[") + 1:])
    assert doc[0]["data"]["files"][0]["stored"] == "notes-2.txt"
    assert env.side.files["ship-01"] == {"notes.txt": b"one", "notes-2.txt": b"two"}


def test_a_refused_file_does_not_stop_the_others(env, tmp_path):
    files = []
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name)
        files.append(str(tmp_path / name))
    env.side.refuse = {"b.txt"}
    assert env.run("put", "ship-01", *files) == 1
    assert sorted(env.side.files["ship-01"]) == ["a.txt", "c.txt"]
    out = env.out.getvalue()
    assert "FAILED: 2 files in ~/inbox: a.txt, c.txt; 1 not sent" in out
    assert "b.txt: Not enough disk space for b.txt." in out


def test_put_with_tell_announces_the_count(env, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    assert env.run("put", "ship-01", str(tmp_path / "a.txt"), "--tell") == 0
    notify = [c for _s, c in env.side.calls if c[3:5] == ["inbox", "notify"]]
    assert notify == [["sudo", "-n", "/usr/local/bin/ship-ctl", "inbox", "notify", "--count",
                       "1", "--json"]]
    assert "told main; not told: grinder (not-running)" in env.out.getvalue()


def test_a_ship_without_the_inbox_is_told_to_update(env, tmp_path):
    (tmp_path / "a.txt").write_text("x")
    env.side.contract = False
    assert env.run("put", "ship-01", str(tmp_path / "a.txt")) != 0
    err = env.err.getvalue()
    assert "tooling predates the file inbox" in err and "ship-fleet update ship-01" in err
    assert env.side.sent == []


@pytest.mark.parametrize("make, why", [
    (lambda d: [str(d / "missing.txt")], "no such file"),
    (lambda d: [str(d)], "not a file"),
    (lambda d: [str(d / "f")] * (inbox.MAX_FILES + 1), f"At most {inbox.MAX_FILES} files"),
])
def test_put_refuses_before_touching_the_ship(env, tmp_path, make, why):
    (tmp_path / "f").write_text("x")
    assert env.run("put", "ship-01", *make(tmp_path)) != 0
    assert why in env.err.getvalue()
    assert env.side.calls == []


def test_a_file_over_the_limit_is_refused_here(tmp_path, monkeypatch):
    big = tmp_path / "big.iso"
    big.write_bytes(b"x" * 11)
    monkeypatch.setattr(inbox, "MAX_BYTES", 10)
    with pytest.raises(FleetError, match="big.iso is 11 bytes; the inbox takes files of up to"):
        inbox.uploads([big])


# --- ship-fleet inbox ls | rm ---------------------------------------------------------


def test_inbox_ls_and_rm(env):
    env.side.files["ship-01"] = {"spec.pdf": b"x" * 2048, "data.csv": b"1,2"}
    assert env.run("inbox", "ls", "ship-01") == 0
    out = env.out.getvalue()
    assert "2 KB  2026-01-02 03:04:05 UTC  spec.pdf" in out
    assert "3 bytes  2026-01-02 03:04:05 UTC  data.csv" in out
    assert env.run("inbox", "rm", "ship-01", "spec.pdf", "gone.txt") == 0
    assert env.side.files["ship-01"] == {"data.csv": b"1,2"}
    assert "removed spec.pdf" in env.out.getvalue()
    assert "not there (moved already?): gone.txt" in env.out.getvalue()


def test_inbox_rm_refuses_a_path(env):
    assert env.run("inbox", "rm", "ship-01", "../etc/passwd") != 0
    assert "not a file name in the inbox" in env.err.getvalue()


def test_an_empty_inbox_says_so(env):
    assert env.run("inbox", "ls", "ship-01") == 0
    assert "ship-01: /home/agent/inbox is empty." in env.out.getvalue()


# --- SSH.send: a file streamed to a command's stdin --------------------------------------


class HungProc:
    """An ssh that never answers until it is killed."""

    def __init__(self):
        import threading

        self.killed = threading.Event()
        self.stdin = io.BytesIO()
        self.stdout = self
        self.stderr = self

    def read(self):
        self.killed.wait(10)
        return b""

    def wait(self):
        self.killed.wait(10)
        return -9

    def poll(self):
        return -9 if self.killed.is_set() else None

    def kill(self):
        self.killed.set()


def test_send_gives_up_after_its_timeout(store):
    proc = HungProc()
    ssh = SSH(store.load(), popen=lambda argv, **kw: proc)
    with pytest.raises(FleetError, match="sending timed out after 0s"):
        ssh.send(store.load().ship("ship-01"), ["x"], io.BytesIO(b"abc"), timeout=0.2)
    assert proc.killed.is_set()


class FakeProc:
    """A Popen stand-in: records stdin, answers like ship-ctl (or like a failed ssh)."""

    def __init__(self, code=0, out=b'{"ok": true}', err=b"", reads=None):
        self.stdin = self
        self.stdout = io.BytesIO(out)
        self.stderr = io.BytesIO(err)
        self.code = code
        self.got = b""
        self.reads = reads  # stop reading after this many bytes (a refusal)

    def write(self, data):
        if self.reads is not None and len(self.got) >= self.reads:
            raise BrokenPipeError
        self.got += data

    def close(self):
        pass

    def wait(self):
        return self.code

    def poll(self):
        return self.code

    def kill(self):
        pass


def test_send_streams_in_pieces_and_reports_progress(store, monkeypatch):
    monkeypatch.setattr("fleetledger.ssh.SEND_CHUNK", 4)
    procs = []

    def popen(argv, **kw):
        procs.append((argv, FakeProc()))
        return procs[-1][1]

    ssh = SSH(store.load(), popen=popen)
    seen = []
    res = ssh.send(store.load().ship("ship-01"), ["cat"], io.BytesIO(b"0123456789"),
                   on_progress=seen.append)
    assert res.returncode == 0 and res.stdout == b'{"ok": true}'
    assert procs[0][1].got == b"0123456789"
    assert seen == [4, 8, 10]
    assert procs[0][0][-1] == "cat" and "-l" in procs[0][0]


def test_send_stops_when_the_command_stops_reading(store, monkeypatch):
    monkeypatch.setattr("fleetledger.ssh.SEND_CHUNK", 4)
    proc = FakeProc(code=1, out=b'{"refused": true}', reads=4)
    ssh = SSH(store.load(), popen=lambda argv, **kw: proc)
    res = ssh.send(store.load().ship("ship-01"), ["x"], io.BytesIO(b"0123456789"))
    assert res.returncode == 1 and res.stdout == b'{"refused": true}'
    assert proc.got == b"0123"


def test_send_tries_the_next_name_from_the_start(store):
    ship = store.load().ship("ship-02")  # two names
    procs = [FakeProc(code=255, out=b"", err=b"ssh: connect to host: No route to host"),
             FakeProc()]
    ssh = SSH(store.load(), popen=lambda argv, **kw: procs.pop(0) if len(procs) > 1 else
              procs[0])
    last = procs[1]
    res = ssh.send(ship, ["x"], io.BytesIO(b"abc"))
    assert res.host == ship.names[1]
    assert last.got == b"abc"  # the whole file again, not the rest of it


def test_send_says_unreachable_when_no_name_answers(store):
    def popen(argv, **kw):
        return FakeProc(code=255, out=b"", err=b"ssh: Could not resolve hostname x")

    ssh = SSH(store.load(), popen=popen)
    with pytest.raises(Unreachable):
        ssh.send(store.load().ship("ship-01"), ["x"], io.BytesIO(b"abc"))


def test_send_runs_a_real_process(store, tmp_path):
    """The real Popen path, with a fake ssh that is `cat` into a file."""
    fake_ssh = tmp_path / "ssh"
    target = tmp_path / "got"
    fake_ssh.write_text(f"#!/bin/sh\ncat > {target}\necho '{{}}'\n")
    fake_ssh.chmod(0o755)
    ssh = SSH(store.load(), ssh_bin=str(fake_ssh))
    data = bytes(range(256)) * 4096  # 1 MiB, more than a pipe holds
    res = ssh.send(store.load().ship("ship-01"), ["x"], io.BytesIO(data))
    assert res.returncode == 0 and res.stdout == b"{}\n"
    assert target.read_bytes() == data
