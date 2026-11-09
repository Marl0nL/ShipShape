# ruff: noqa: F811  (the fixtures of test_migrate.py, imported by name)
"""Handing a fleet over (handover.py, ship-fleet migrate): standby, active, retired.

The two computers and the fake Ships of test_migrate.py. `push` reaches the new
computer through a stand-in for ssh that runs that computer's own ship-fleet in this
process, on its side (its folders, keyring and ssh-agent), with what push sends on
its stdin: so a whole push, cut-over included, runs end to end.
"""

from __future__ import annotations

import dataclasses
import io
import json
import os
import shlex
import shutil
import signal
import subprocess
import threading
import tomllib
from datetime import timedelta

import pytest
from fleetledger.errors import FleetError
from test_migrate import (  # noqa: F401  (fixtures)
    Computer,
    built,
    checkout,
    computers,
    receive,
    send_bytes,
    short,
)

from fleetledger import cli, handover, inuse, keys, migrate, schemas

pytestmark = [
    pytest.mark.skipif(
        not (shutil.which("ssh-keygen") and shutil.which("ssh-agent") and shutil.which("ssh-add")),
        reason="needs OpenSSH's ssh-keygen, ssh-agent and ssh-add"),
    # The new computer's ship-fleet runs in this process while push relays its output
    # on threads, so pexpect forks with threads alive: fine here (it execs at once).
    pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning"),
]


class Remote:
    """ssh to the new computer: runs its ship-fleet here, as that computer."""

    def __init__(self, there: Computer):
        self.there = there
        self.calls: list[list[str]] = []
        self.fail: dict[str, int] = {}  # a verb -> the exit code ssh returns for it

    def popen(self, argv, stdin=None, **_kw):
        return RemoteProcess(self, argv, stdin)


class RemoteProcess:
    def __init__(self, remote: Remote, argv, stdin):
        self.remote, self.argv = remote, argv
        self.received = b""
        self.stdin = self._reader = None
        if stdin == subprocess.PIPE:
            r, w = os.pipe()
            self.stdin, self._r = os.fdopen(w, "wb"), os.fdopen(r, "rb")
            self._reader = threading.Thread(target=self._read, daemon=True)
            self._reader.start()
        out_r, self._out = os.pipe()
        err_r, self._err = os.pipe()
        self.stdout, self.stderr = os.fdopen(out_r, "rb"), os.fdopen(err_r, "rb")

    def _read(self):
        with self._r:
            self.received = self._r.read()

    def wait(self):
        if self._reader is not None:
            self._reader.join(timeout=60)
        # sh -lc 'exec ship-fleet migrate ...'
        argv = shlex.split(shlex.split(self.argv[-1])[2])[2:]
        self.remote.calls.append(argv)
        there = self.remote.there
        code = self.remote.fail.get(argv[1])
        if code is None:
            with pytest.MonkeyPatch.context() as mp:
                there.use(mp)
                there.app.stdin_bytes = io.BytesIO(self.received)
                code = there.run(*argv)[0]
                there.app.stdin_bytes = None
        for fd, text in ((self._out, there.out.getvalue()), (self._err, there.err.getvalue())):
            with os.fdopen(fd, "wb") as fh:
                fh.write(text.encode())
        return code

    def kill(self):
        pass


def push(a: Computer, b: Computer, monkeypatch, *args, remote: Remote | None = None):
    remote = remote or Remote(b)
    a.use(monkeypatch)
    a.app.popen = remote.popen
    return (*a.run("migrate", "push", "you@vm.example", *args), remote)


def role(c: Computer):
    return handover.marker(c.store)


# --- the hand-over -------------------------------------------------------------------------


def test_a_push_hands_over_and_the_old_copy_refuses_to_act(computers, monkeypatch):
    a, b, world = computers
    ships_before = world.state()
    a_files = a.files()
    code, out, err, remote = push(a, b, monkeypatch)
    assert code == 0, out + err
    assert [c[:2] for c in remote.calls] == [["migrate", "receive"], ["migrate", "activate"]]
    assert "vm.example: This computer has the fleet on standby and reached every Ship" in out
    assert "you@vm.example is now the active FleetLedger (epoch 1)" in out

    # The new copy is the active one, at the next epoch, and works.
    b.use(monkeypatch)
    assert role(b) is None
    assert tomllib.loads(b.store.path.read_text())["fleet"]["epoch"] == 1
    code, out, err = b.run("ca", "sign")
    assert code == 0 and out.count("up to date") == 2, err

    # The old one: retired, naming where the fleet went; its files are as they were.
    a.use(monkeypatch)
    m = role(a)
    assert (m.role, m.other, m.epoch, m.wiped) == ("retired", "you@vm.example", 1, None)
    assert {k: v for k, v in a.files().items() if k != "config/retired.json"} == a_files
    for argv in (("ls",), ("status", "ship-01"), ("ca", "sign"), ("trust", "laptop"),
                 ("update", "all", "--yes"), ("reboot", "ship-01"), ("set", "ship-01",
                 "remote_control=off"), ("ui", "--no-browser"), ("setup",),
                 ("untrust", "laptop")):
        code, out, err = a.run(*argv)
        assert code == 1 and "retired on" in err and "you@vm.example" in err, argv
    code, out, err = a.run("migrate", "push", "you@vm.example")
    assert code == 1 and "this copy: This FleetLedger was retired" in out
    code, out, err = a.run("doctor", "--offline")
    assert "this copy" in out and "retired on" in out
    assert world.state() == ships_before  # nothing changed on any Ship, from either copy


def test_receive_by_hand_waits_on_standby_until_activated(computers, monkeypatch):
    a, b, world = computers
    assert receive(b, monkeypatch, send_bytes(a, monkeypatch)) == migrate.VERIFIED
    assert "on the old computer, ship-fleet migrate retire --to vm; then here, " \
        "ship-fleet migrate activate" in b.out.getvalue()
    m = role(b)
    assert (m.role, m.other, m.epoch) == ("standby", "desktop", 1)
    for argv in (("ls",), ("ca", "sign"), ("ui", "--no-browser")):
        code, out, err = b.run(*argv)
        assert code == 1 and "on standby" in err, argv
    assert b.run("migrate", "verify")[0] == 0  # allowed: it checks, never acts

    a.use(monkeypatch)
    assert a.run("ls")[0] == 0  # the old copy is still the active one
    b.use(monkeypatch)
    code, out, err = b.run("migrate", "activate")
    assert code == 1 and "add --yes" in err  # asks first (no terminal here)
    b.app.stdin_isatty = lambda: True
    b.app.confirm = lambda question: "yes"
    code, out, err = b.run("migrate", "activate")
    assert code == 0 and "now the active one (epoch 1)" in out
    assert b.run("ls")[0] == 0


def test_activate_refuses_while_a_check_fails(computers, monkeypatch):
    a, b, world = computers
    assert receive(b, monkeypatch, send_bytes(a, monkeypatch)) == migrate.VERIFIED
    world.down.add("ship-01")
    code, out, err = b.run("migrate", "activate", "--yes")
    assert code == 1 and "Not made active" in err and role(b).role == "standby"


def test_a_ship_it_cannot_check_keeps_the_old_copy_active(computers, monkeypatch):
    a, b, world = computers
    world.down.add("ship-02")
    code, out, err, remote = push(a, b, monkeypatch)
    assert code == migrate.UNVERIFIED and "still the active one" in err
    assert [c[1] for c in remote.calls] == ["receive"]  # no hand-over
    assert role(a) is None and a.run("ls")[0] == 0
    b.use(monkeypatch)
    assert role(b).role == "standby"

    # Fixed: push again, and it replaces the copy on standby.
    world.down.clear()
    code, out, err, remote = push(a, b, monkeypatch, remote=remote)
    assert code == 0, out + err
    assert "Replacing the copy on standby here" in out
    b.use(monkeypatch)
    assert role(b) is None and b.run("ls")[0] == 0


def test_a_hand_over_cut_short_leaves_no_copy_active(computers, monkeypatch):
    a, b, world = computers
    remote = Remote(b)
    remote.fail["activate"] = 1
    code, out, err, _ = push(a, b, monkeypatch, remote=remote)
    assert code == migrate.NOT_ACTIVATED
    assert "no copy acts on the fleet now" in err and "ship-fleet migrate activate" in err
    assert a.run("ls")[0] == 1  # retired
    b.use(monkeypatch)
    assert b.run("ls")[0] == 1  # on standby
    assert b.run("migrate", "activate", "--yes")[0] == 0  # the way on, as it said


def test_push_without_the_cut_over_leaves_a_trial_copy(computers, monkeypatch):
    a, b, world = computers
    code, out, err, remote = push(a, b, monkeypatch, "--no-cutover")
    assert code == 0 and "this copy is still the active one" in out
    assert role(a) is None
    b.use(monkeypatch)
    assert role(b).role == "standby"


# --- rollback and the epoch ------------------------------------------------------------------


def test_rollback_retire_there_then_unretire_here(computers, monkeypatch):
    a, b, world = computers
    assert push(a, b, monkeypatch)[0] == 0
    b.use(monkeypatch)
    code, out, err = b.run("migrate", "retire", "--to", "desktop", "--yes")
    assert code == 0 and role(b).epoch == 2
    a.use(monkeypatch)
    code, out, err = a.run("migrate", "unretire")
    assert code == 1 and "add --yes" in err
    code, out, err = a.run("migrate", "unretire", "--yes")
    assert code == 0 and "acts on the fleet again (epoch 2)" in out
    assert tomllib.loads(a.store.path.read_text())["fleet"]["epoch"] == 2
    assert a.run("ca", "sign")[0] == 0
    b.use(monkeypatch)
    assert b.run("ls")[0] == 1


def test_receive_refuses_an_older_fleet_than_it_handed_on(computers, monkeypatch):
    a, b, world = computers
    old = send_bytes(a, monkeypatch)  # epoch 0: a backup, say
    assert push(a, b, monkeypatch)[0] == 0  # B is the active copy, epoch 1

    a.use(monkeypatch)  # A, retired: never written over, not even by a newer fleet
    with pytest.raises(FleetError, match="has a retired FleetLedger"):
        receive(a, monkeypatch, send_bytes(b, monkeypatch))
    assert a.run("migrate", "wipe", "--now", "--yes")[0] == 0

    with pytest.raises(FleetError, match="from epoch 0: an older state"):
        receive(a, monkeypatch, old)
    assert not a.store.path.exists()
    # The fleet as B has it now comes back (B retires, A activates at epoch 2).
    assert push(b, a, monkeypatch)[0] == 0
    a.use(monkeypatch)
    assert role(a) is None
    assert tomllib.loads(a.store.path.read_text())["fleet"]["epoch"] == 2
    assert a.run("ca", "sign")[0] == 0


# --- wipe ------------------------------------------------------------------------------------


def test_wipe_waits_two_weeks_then_says_what_goes_and_what_stays(computers, monkeypatch):
    a, b, world = computers
    assert push(a, b, monkeypatch)[0] == 0
    a.use(monkeypatch)
    code, out, err = a.run("migrate", "wipe")
    assert code == 1 and "kept two weeks for a rollback (14 more day(s))" in err

    _retired_long_ago(a)
    code, out, err = a.run("migrate", "wipe")
    assert code == 1 and "add --yes" in err and a.store.path.exists()  # asks first
    a.app.stdin_isatty = lambda: True
    a.app.confirm = lambda question: "wipe"
    ssh = a.home / ".ssh"
    items_before = set(a.ring.items)
    code, out, err = a.run("migrate", "wipe")
    assert code == 0, out + err
    goes, stays = out.split("Keeps:")
    assert "shipshape_fleet_ed25519" in goes and "keyring item key-passphrase:" in goes

    # Gone: the admin and CA keys, their passphrases, the inventory, the history.
    for name in ("shipshape_fleet_ed25519", "shipshape_host_ca_ed25519",
                 "shipshape_user_ca_ed25519"):
        assert not (ssh / name).exists() and not (ssh / f"{name}.pub").exists()
    assert not a.store.path.exists() and not (a.root / "data").exists()
    assert not (a.root / "config" / "this-computer.json").exists()
    # Kept: your own key and its passphrase (this computer still attaches with it).
    assert (ssh / "shipshape_ed25519").exists() and "shipshape_ed25519" in stays
    left = set(a.ring.items)
    assert left < items_before and len(left) == 1 and "shipshape_ed25519" in next(iter(left))[1]
    assert os.listdir(a.root / "config") == ["retired.json"]
    assert role(a).wiped

    code, out, err = a.run("ls")
    assert code == 1 and "wiped on" in err and "retired.json first" in err
    assert a.run("doctor", "--offline")[0] == 0


def _retired_long_ago(a):
    m = role(a)
    old = (handover._parse(m.when) - timedelta(days=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    handover.write(a.store, dataclasses.replace(m, when=old))


def test_a_locked_keyring_stops_the_wipe_before_anything_goes(computers, monkeypatch):
    from test_secret_store import fake_backend

    a, b, world = computers
    assert a.run("migrate", "retire", "--yes")[0] == 0
    before = a.files()
    a.ring = fake_backend(locked=True)
    code, out, err = a.run("migrate", "wipe", "--now", "--yes")
    assert code == 1 and "Cannot reach the keyring" in err and "nothing was deleted" in err
    assert a.files() == before
    assert (a.home / ".ssh" / "shipshape_fleet_ed25519").exists()


def test_a_key_you_look_after_yourself_is_kept_and_said(computers, monkeypatch):
    a, b, world = computers
    admin = a.home / ".ssh" / "shipshape_fleet_ed25519"
    with a.store.edit() as ed:  # the admin key is yours, not FleetLedger's
        keys_list = ed.doc["fleet"]["managed_keys"]["keys"]
        keys_list.remove("~/.ssh/shipshape_fleet_ed25519")
    assert a.run("migrate", "retire", "--yes")[0] == 0
    code, out, err = a.run("migrate", "wipe", "--now", "--yes")
    assert code == 0, err
    goes, stays = out.split("Keeps:")
    assert "shipshape_fleet_ed25519" not in goes
    assert "the fleet admin key, which you look after yourself" in stays
    assert admin.exists() and admin.with_name(admin.name + ".pub").exists()
    assert not (a.home / ".ssh" / "shipshape_host_ca_ed25519").exists()  # FleetLedger's: gone


def test_a_file_store_wipe_keeps_your_own_keys_passphrase_file(computers, monkeypatch, tmp_path,
                                                               short):
    from fleetledger.web import setup

    a, b, world = computers
    c = Computer("laptop", tmp_path / "c", short, a.checkout, world)
    try:
        c.use(monkeypatch)
        setup._new_fleet(c.app, {"store": ["file"], "your_key": ["new"]})
        own = c.home / ".ssh" / "shipshape_ed25519"
        own_passphrase = keys.passphrase_file(own)
        admin_passphrase = keys.passphrase_file(c.home / ".ssh" / "shipshape_fleet_ed25519")
        assert own_passphrase.exists() and admin_passphrase.exists()
        assert c.run("migrate", "retire", "--yes")[0] == 0
        code, out, err = c.run("migrate", "wipe", "--now", "--yes")
        assert code == 0, err
        assert "the fleet admin key's passphrase" in out.split("Keeps:")[0]
        assert own.exists() and own_passphrase.exists()  # this computer still attaches with it
        assert keys.public_of(own, own_passphrase.read_text().strip()) == keys.public_line(own)
        assert not admin_passphrase.exists()
        assert not (c.home / ".ssh" / "shipshape_fleet_ed25519").exists()
    finally:
        os.kill(c.agent_pid, signal.SIGTERM)


def test_handing_over_waits_for_every_other_ship_fleet(computers, monkeypatch):
    a, b, world = computers
    with inuse.using(a.store):  # the web UI, say: its jobs could act under a retired copy
        for argv in (("migrate", "retire", "--yes"), ("migrate", "unretire", "--yes"),
                     ("migrate", "wipe", "--now", "--yes")):
            code, out, err = a.run(*argv)
            assert code == 1 and "Another ship-fleet is running" in err, argv
    assert role(a) is None


def test_an_active_copy_is_never_wiped(computers, monkeypatch):
    a, b, world = computers
    code, out, err = a.run("migrate", "wipe", "--now", "--yes")
    assert code == 1 and "the active one" in err and a.store.path.exists()


def test_a_copy_on_standby_can_be_dropped_at_once(computers, monkeypatch):
    a, b, world = computers
    assert receive(b, monkeypatch, send_bytes(a, monkeypatch)) == migrate.VERIFIED
    code, out, err = b.run("migrate", "retire", "--yes")
    assert code == 1 and "nothing to retire" in err
    assert b.run("migrate", "wipe", "--yes")[0] == 0
    assert role(b) is None and not b.store.path.exists()
    assert not (b.home / ".ssh" / "shipshape_fleet_ed25519").exists()
    assert receive(b, monkeypatch, send_bytes(a, monkeypatch)) == migrate.VERIFIED  # afresh


# --- the markers -----------------------------------------------------------------------------


@pytest.mark.parametrize("name,text", [
    ("standby.json", "{not json"),
    ("retired.json", json.dumps({"schema": handover.SCHEMA, "role": "retired",
                                 "when": "2026-11-06 10:00", "other": None, "epoch": 1})),
    ("retired.json", json.dumps({"schema": handover.SCHEMA, "role": "retired",
                                 "when": "2026-11-06T10:00:00Z", "other": None,
                                 "epoch": 1 << 62}))])
def test_a_marker_that_cannot_be_read_stops_everything_and_says_which(computers, monkeypatch,
                                                                     name, text):
    a, b, world = computers
    marker = a.root / "config" / name
    marker.write_text(text)
    doc = a.store.path.read_text()
    for argv in (("ls",), ("migrate", "unretire", "--yes"), ("migrate", "wipe", "--now", "--yes"),
                 ("migrate", "activate", "--yes"), ("migrate", "retire", "--yes")):
        code, out, err = a.run(*argv)
        assert code == 1 and f"{name} cannot be read" in err and "Traceback" not in err, argv
    code, out, err = a.run("migrate", "check")
    assert code == 1 and f"{name} cannot be read" in out
    with pytest.raises(FleetError, match="cannot be read"):
        receive(a, monkeypatch, b"")
    assert a.store.path.read_text() == doc and marker.read_text() == text  # nothing acted on
    assert "cannot be read" in a.run("doctor", "--offline")[1]


def test_the_epoch_is_part_of_the_inventory_contract(store):
    doc = tomllib.loads(store.path.read_text())
    doc["fleet"]["epoch"] = 3
    assert schemas.errors("ships.toml", doc) == []
    doc["fleet"]["epoch"] = -1
    assert schemas.errors("ships.toml", doc)


def test_every_command_but_migrate_and_doctor_is_refused_when_handed_over(store, home):
    handover.write(store, handover.Marker("retired", handover.now(), "vm.example", 1))
    allowed = {c for c in cli.COMMANDS if c in handover.ALLOWED}
    assert allowed == {"migrate", "doctor", "service"}
    for command in cli.COMMANDS:
        if command in allowed:
            continue
        with pytest.raises(FleetError, match="retired"):
            handover.gate(command, store)
    assert json.loads((home / "config" / "retired.json").read_text())["schema"] == \
        handover.SCHEMA
