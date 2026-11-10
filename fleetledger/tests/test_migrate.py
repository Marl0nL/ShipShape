"""ship-fleet migrate (migrate.py): moving FleetLedger to another computer.

Two computers in one test, each with a home of its own (HOME and FleetLedger's
folders, at different paths), its own fake keyring and its own ssh-agent, and one
checkout at one commit. Copy A is made the way the first run makes a fleet (managed
keys, a CA), with Ships, trusted machines, a narrowed scope and an untrusted machine.

The Ships are fakes behind FleetLedger's real SSH code: a fake runner stands in for
ssh and the network. Like a real Ship, each answers only when FleetLedger's own
known_hosts pins the host key it really has, and only to the admin key it was seeded
with, once that key is opened on the calling computer (in its ssh-agent). Each keeps
the trust `ship-fleet ca sign` gave it (host certificate, user CA keys, revocation
list) and records every command that would change it.

Nothing here touches a real home, keyring, agent or network.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import threading
import tomllib
from pathlib import Path

import pytest
from fl_harness import REPO_ROOT, keygen
from fleetledger.drivers import proxmox_api
from fleetledger.errors import FleetError
from fleetledger.overlay import secret_store
from fleetledger.ssh import SHIP_CTL
from fleetledger.store import TomlFileStore
from test_cli import RUNNING, result
from test_secret_store import fake_backend

from fleetledger import ca, cli, handover, inuse, keys, migrate, paths, schemas

pytestmark = pytest.mark.skipif(
    not (shutil.which("ssh-keygen") and shutil.which("ssh-agent") and shutil.which("ssh-add")),
    reason="needs OpenSSH's ssh-keygen, ssh-agent and ssh-add")

VERSION = result("version", data={"tooling": "0.1.0", "commit": "0123456789ab", "contracts": {
    "ship-status": 1, "ship-toml": 1, "ship-ctl-result": 1, "bundle": 1, "seed": 1,
    "runtime": 1, "ssh-trust": 1}})
CERT_ALGO = "ssh-ed25519-cert-v01@openssh.com"


# --- the Ships ----------------------------------------------------------------------------


def cert_facts(cert_line: str) -> dict:
    """What a Ship reports about its host certificate (contracts/ship-status), read
    from the certificate itself with ssh-keygen -L."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "c-cert.pub"
        f.write_text(cert_line + "\n")
        text = subprocess.run(["ssh-keygen", "-L", "-f", str(f)], capture_output=True,
                              text=True, check=True, env={**os.environ, "TZ": "UTC"}).stdout
    valid = re.search(r"Valid: from (\S+) to (\S+)", text)
    principals = text.split("Principals:", 1)[1].split("Critical Options:", 1)[0].split()
    return {"serial": int(re.search(r"Serial: (\d+)", text).group(1)),
            "key_id": re.search(r'Key ID: "(.*)"', text).group(1),
            "principals": principals,
            "valid_after": valid.group(1) + "Z", "valid_before": valid.group(2) + "Z",
            "ca_fingerprint": re.search(r"Signing CA: \S+ (SHA256:\S+)", text).group(1),
            "subject": re.search(r"Public key: \S+ (SHA256:\S+)", text).group(1)}


class FakeShip:
    def __init__(self, name, ship_id, names, tags, host_key):
        self.name, self.id, self.names, self.tags = name, ship_id, tuple(names), list(tags)
        self.host_key = host_key  # its real host key, pinned in the seed
        self.admin = None  # the authorized admin key cloud-init wrote (type blob)
        self.cert = None
        self.trust = None  # the last ssh-trust document it took
        self.changes = []  # every command that would change it

    def state(self):
        return (self.admin, self.cert, json.dumps(self.trust, sort_keys=True),
                tuple(map(tuple, self.changes)))

    def ssh_block(self):
        if self.trust is None:
            return {"host_certificate": None, "user_ca": [], "agent_principals": [],
                    "revoked_keys_sha256": None}
        facts = cert_facts(self.cert) if self.cert else None
        if facts:
            facts.pop("subject")
        return {"host_certificate": facts,
                "user_ca": [ca.fingerprint(k) for k in self.trust["user_ca_keys"]],
                "agent_principals": ["agent:all"] + [f"agent:tag:{t}" for t in self.tags],
                "revoked_keys_sha256": hashlib.sha256(
                    base64.b64decode(self.trust["revoked_keys"])).hexdigest()}

    def answer(self, argv, input):
        if argv[:3] != ["sudo", "-n", SHIP_CTL]:
            return (0, "", "") if argv == ["true"] else (127, "", "unknown command")
        verb = argv[3]
        if verb == "version":
            return 0, json.dumps(VERSION), ""
        if verb == "status":
            doc = copy.deepcopy(RUNNING)
            doc["ship"].update(name=self.name, id=self.id, tags=self.tags)
            doc["ssh"] = self.ssh_block()
            return 0, json.dumps(doc), ""
        self.changes.append(argv)
        if verb == "ssh-trust":
            doc = json.loads(input)
            schemas.check("ssh-trust", doc, "ssh-trust")
            if doc.get("host_certificate"):
                facts = cert_facts(doc["host_certificate"])
                assert facts["subject"] == ca.fingerprint(self.host_key), "not my key"
                self.cert = doc["host_certificate"]
            self.trust = doc
        return 0, json.dumps(result(verb, changed=["/etc/ssh/x"])), ""

    def presents_certificate(self, host, known_hosts: Path) -> bool:
        if not self.cert:
            return False
        facts = cert_facts(self.cert)
        for line in known_hosts.read_text().splitlines():
            parts = line.split()
            if parts[:1] == ["@cert-authority"] and host in parts[1].split(",") and \
                    ca.fingerprint(" ".join(parts[2:4])) == facts["ca_fingerprint"]:
                return host in facts["principals"]
        return False


def done(argv, code, out="", err=""):
    out = out.encode() if isinstance(out, str) else out
    return subprocess.CompletedProcess(argv, code, out, err.encode())


class World:
    """The network and the Ships, as FleetLedger's ssh meets them."""

    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir()
        self.ships: dict[str, FakeShip] = {}
        self.calls = []  # (ship, argv) of every command that reached a Ship
        self.down: set[str] = set()

    def make(self, name, ship_id, names, tags) -> str:
        keygen(self.folder / name, name)
        host_key = " ".join((self.folder / f"{name}.pub").read_text().split()[:2])
        self.ships[name] = FakeShip(name, ship_id, names, tags, host_key)
        return host_key

    def seed_admin(self, admin_pub: Path) -> None:
        for ship in self.ships.values():
            ship.admin = keys.public_line(admin_pub.with_suffix(""))

    def state(self):
        return {n: s.state() for n, s in self.ships.items()}

    def runner(self, argv, input=b"", **_kw):
        opts, identity, user, i = {}, None, None, 1
        while argv[i] != "--":
            if argv[i] == "-o":
                key, value = argv[i + 1].split("=", 1)
                opts.setdefault(key, value)  # ssh keeps the first value it reads
                i += 2
            elif argv[i] in ("-i", "-l", "-p"):
                identity = argv[i + 1] if argv[i] == "-i" else identity
                user = argv[i + 1] if argv[i] == "-l" else user
                i += 2
            else:
                i += 1
        host, command = argv[i + 1], argv[i + 2:]
        ship = next((s for s in self.ships.values() if host in s.names), None)
        if ship is None or ship.name in self.down:
            return done(argv, 255, err=f"ssh: connect to host {host} port 22: No route to host")
        known = Path(opts["UserKnownHostsFile"])
        if opts.get("HostKeyAlgorithms") == CERT_ALGO:
            if not ship.presents_certificate(host, known):
                return done(argv, 255, err="Host key verification failed.")
        else:
            alias = opts["HostKeyAlias"]
            pinned = {line.split(None, 1)[1] for line in known.read_text().splitlines()
                      if line.startswith(alias + " ")}
            if ship.host_key not in pinned:
                return done(argv, 255, err="Host key verification failed.")
        if user != "fleet" or identity is None or not self._opens(Path(identity), ship):
            return done(argv, 255, err="fleet@ship: Permission denied (publickey).")
        remote = shlex.split(command[0]) if command else []
        self.calls.append((ship.name, remote))
        code, out, err = ship.answer(remote, input)
        return done(argv, code, out, err)

    @staticmethod
    def _opens(identity: Path, ship: FakeShip) -> bool:
        """The admin key it was seeded with, usable here: unencrypted, or held by this
        computer's ssh-agent (so this computer's store opened it)."""
        if keys.public_line(identity) != ship.admin:
            return False
        if not keys.needs_passphrase(identity):
            return True
        held = subprocess.run(["ssh-add", "-L"], capture_output=True, text=True).stdout
        return ship.admin in {" ".join(line.split()[:2]) for line in held.splitlines()}


# --- two computers ------------------------------------------------------------------------


class Computer:
    def __init__(self, name: str, root: Path, short: Path, checkout, world: World):
        self.name, self.root, self.checkout, self.world = name, root, checkout, world
        self.home = root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.env = {"HOME": str(self.home),
                    "FLEETLEDGER_CONFIG_DIR": str(root / "config"),
                    "FLEETLEDGER_DATA_DIR": str(root / "data"),
                    "FLEETLEDGER_RUNTIME_DIR": str(short / f"{name}-run")}
        self.ring = fake_backend()
        self.sock = short / f"{name}.sock"
        out = subprocess.run(["ssh-agent", "-a", str(self.sock)], capture_output=True,
                             text=True, check=True, timeout=10).stdout
        self.agent_pid = int(out.split("SSH_AGENT_PID=")[1].split(";")[0])
        self.store = TomlFileStore(root / "config" / "ships.toml")
        self.out, self.err = io.StringIO(), io.StringIO()
        self.app = cli.App(
            out=self.out, err=self.err, store_factory=lambda: self.store,
            repo_factory=lambda: self.checkout, ssh_factory=self.ssh,
            stdin_isatty=lambda: False, resolves=lambda host: True, sleep=lambda s: None,
            keyscan=lambda host: (_ for _ in ()).throw(AssertionError("keyscan")),
            herdr=False)

    def ssh(self, inv):
        """What ship-fleet itself does (cli._ssh_with_managed_keys), over the World."""
        from fleetledger.ssh import SSH

        keys.ensure_loaded(inv)
        return SSH(inv, runner=self.world.runner)

    def use(self, monkeypatch) -> Computer:
        for key, value in self.env.items():
            monkeypatch.setenv(key, value)
        for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "SHIPSHAPE_REPO",
                    "PYTHONPATH"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("SSH_AUTH_SOCK", str(self.sock))
        monkeypatch.setattr(secret_store, "get_keyring", lambda: self.ring)
        monkeypatch.setattr(keys, "_ADDED", {})
        monkeypatch.setattr(socket, "gethostname", lambda: self.name)
        return self

    def run(self, *argv):
        for stream in (self.out, self.err):
            stream.seek(0)
            stream.truncate()
        code = cli.main(list(argv), self.app)
        return code, self.out.getvalue(), self.err.getvalue()

    def files(self) -> dict[str, bytes]:
        """Every file this computer holds, by path under its root (locks aside)."""
        found = {}
        for base in (self.root, Path(self.env["FLEETLEDGER_RUNTIME_DIR"])):
            for f in sorted(base.rglob("*")):
                if f.is_file() and not f.name.endswith(".lock"):
                    found[str(f.relative_to(base))] = f.read_bytes()
        return found


def commit_all(root: Path, message: str = "fleet") -> str:
    git = ["git", "-C", str(root), "-c", "user.name=Test", "-c", "user.email=test@example.com"]
    if not (root / ".git").exists():
        subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", message, "--allow-empty"], check=True)
    return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def checkout(fake_repo):
    commit_all(fake_repo.root)
    return fake_repo


@pytest.fixture
def short():
    """A short folder for agent sockets (Unix socket paths are limited)."""
    folder = Path(tempfile.mkdtemp(prefix="flm-", dir="/tmp"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Copy A, made once (it takes a few seconds) and put back exactly as it was, at
    the same paths, before each test."""
    from fleetledger.repo import Repo

    root = tmp_path_factory.mktemp("fleet")
    sockets = Path(tempfile.mkdtemp(prefix="flm-", dir="/tmp"))
    world = World(root / "world")
    with pytest.MonkeyPatch.context() as mp:
        a = Computer("desktop", root / "a", sockets, Repo(REPO_ROOT), world)
        try:
            make_fleet(a, mp)
        finally:
            os.kill(a.agent_pid, signal.SIGTERM)
    snapshot = root / "snapshot"
    shutil.copytree(a.root, snapshot, symlinks=True)
    yield a.root, snapshot, dict(a.ring.items), world
    shutil.rmtree(sockets, ignore_errors=True)


@pytest.fixture
def computers(built, tmp_path, short, checkout, monkeypatch):
    a_root, snapshot, ring, built_world = built
    shutil.rmtree(a_root)
    shutil.copytree(snapshot, a_root, symlinks=True)
    world = copy.deepcopy(built_world)
    a = Computer("desktop", a_root, short, checkout, world)
    a.ring.items.update(ring)
    b = Computer("vm", tmp_path / "b" / "elsewhere", short, checkout, world)
    a.use(monkeypatch)
    yield a, b, world
    for c in (a, b):
        os.kill(c.agent_pid, signal.SIGTERM)


SHIPS = [("ship-01", "7c9e6679-7425-40de-944b-e07fc1f90ae7", ["ship-01.local"], ["work"]),
         ("ship-02", "2f1b8a54-3c6e-4d1f-9a2b-5e8c7d6f4a31",
          ["ship-02.local", "ship-02.example.net"], ["personal"])]


def make_fleet(a: Computer, monkeypatch) -> None:
    """Copy A, as the first run and a few weeks of use leave it."""
    from fleetledger.web import setup

    a.use(monkeypatch)
    setup._new_fleet(a.app, {"store": ["keyring"], "your_key": ["new"]})
    with a.store.edit() as ed:
        ed.set_default("driver", "manual")
        for name, ship_id, names, tags in SHIPS:
            host_key = a.world.make(name, ship_id, names, tags)
            ed.add_ship(name, {"id": ship_id, "tags": tags, "driver": "manual", "names": names,
                               "host_key": host_key, "enrolled": "2026-01-01T00:00:00Z"})
    a.world.seed_admin(a.store.load().admin_key.with_name("shipshape_fleet_ed25519.pub"))
    machines = a.root / "machines"
    machines.mkdir()
    for name in ("laptop", "old-laptop", "phone"):
        keygen(machines / name, name)
    you = a.home / ".ssh" / "shipshape_ed25519.pub"
    steps = [("trust", "desktop", "--key", str(you)),
             ("trust", "laptop", "--key", str(machines / "laptop.pub")),
             ("trust", "phone", "--key", str(machines / "phone.pub")),
             ("trust", "phone", "--scope", "tag:work"),  # narrowed: its wider certificate revoked
             ("trust", "old-laptop", "--key", str(machines / "old-laptop.pub")),
             ("untrust", "old-laptop"),
             ("ca", "sign")]
    for argv in steps:
        code, out, err = a.run(*argv)
        assert code == 0, (argv, out, err)
    (a.root / "config" / "this-computer.json").write_text(json.dumps(
        {"machine": "desktop", "identity": "~/.ssh/shipshape_ed25519"}))
    data = a.root / "data"
    (data / "audit.log").write_text("2026-01-01T00:00:00Z ship-01 claude ok work@example.com\n")
    (data / "attempts.json").write_text("[]\n")
    (data / "net").mkdir()
    (data / "net" / "ship-01.json").write_text("{}\n")
    db = sqlite3.connect(data / "history.sqlite3")
    db.execute("create table samples (ship text, at real)")
    db.execute("insert into samples values ('ship-01', 1.0)")
    db.commit()
    db.close()
    (data / "inbox-spool").mkdir()
    (data / "inbox-spool" / "half-sent").write_bytes(b"x")
    (data / "toolkit-archives").mkdir()  # a packed toolkit (toolkitpack.py)
    (data / "toolkit-archives" / "examplekit.tar.gz").write_bytes(b"packed")
    (data / "images" / "base").mkdir(parents=True)
    (data / "images" / "base" / "noble.img").write_bytes(b"ubuntu")
    (a.root / "config" / "proxmox-setup-progress.json").write_text("{}\n")
    from fleetledger.web import devices, remote

    remote.save(remote.Remote("https://desktop.example-tailnet.ts.net", 8765))  # a piece-1 setting
    store = devices.Devices()  # the remote web UI's paired browsers and pairing links
    with store._locked():
        for f in (store.devices_file, store.pairings_file):
            paths.write_private(f, json.dumps({"schema": 1, "items": []}).encode())
    assert a.run("ls")[0] == 0  # the status cache, and FleetLedger's known_hosts


def send_bytes(a: Computer, monkeypatch, **kw) -> bytes:
    a.use(monkeypatch)
    out = io.BytesIO()
    migrate.send(a.app, out, **kw)
    return out.getvalue()


def receive(b: Computer, monkeypatch, stream: bytes, **kw) -> int:
    b.use(monkeypatch)
    for s in (b.out, b.err):
        s.seek(0)
        s.truncate()
    return migrate.receive(b.app, io.BytesIO(stream), **kw)


def invariant(doc):
    return migrate._invariant(doc)


# --- the whole move -----------------------------------------------------------------------


def test_ships_made_by_one_copy_keep_working_from_the_other_with_nothing_changed(
        computers, monkeypatch):
    a, b, world = computers
    a_files, a_doc = a.files(), tomllib.loads(a.store.path.read_text())
    a_krl = (a.root / "data" / "ca" / "revoked-keys.krl").read_bytes()
    ships_before = world.state()
    phone = a_doc["machine"]["phone"]
    assert phone["issued"] and a_doc["machine"]["old-laptop"]["revoked"]  # what must not be lost

    stream = send_bytes(a, monkeypatch)
    world.calls.clear()
    code = receive(b, monkeypatch, stream)
    out = b.out.getvalue()
    assert code == migrate.VERIFIED, out + b.err.getvalue()
    assert "no remote branch" not in out

    # Every Ship answered B on its pin with the same admin key, and nothing on any changed.
    assert world.state() == ships_before
    assert world.calls and all(argv[3:4] in (["status"], ["version"]) for _s, argv in world.calls)
    assert "FAIL" not in out and "WARN" not in out
    for name, *_ in SHIPS:
        assert f"{name} certificate" in out and "signed by this CA; same revocation list" in out
    for label in ("fleet admin key", "host CA key", "user CA key"):
        assert re.search(rf"ok +{label} +opens with its passphrase from your OS keyring", out)
    assert "ship-fleet ui remote https://<this computer's name> --port 8765 (" in out

    # The whole inventory came: every Ship and pin, machine, issued certificate, revocation.
    b_doc = tomllib.loads(b.store.path.read_text())
    assert invariant(b_doc) == invariant(a_doc)
    assert b_doc["machine"]["phone"]["issued"] == phone["issued"]
    assert (b.root / "elsewhere").exists() is False  # nothing written outside its folders
    b_krl = (b.root / "data" / "ca" / "revoked-keys.krl").read_bytes()
    assert b_krl == a_krl and ca.build_krl(b.store.load()) == a_krl  # byte for byte

    # The keys: same files at the same place under B's home; passphrases stored again
    # under B's own item names (a passphrase's item depends on the key's full path).
    for name in ("shipshape_fleet_ed25519", "shipshape_host_ca_ed25519",
                 "shipshape_user_ca_ed25519"):
        for f in (name, name + ".pub"):
            assert (b.home / ".ssh" / f).read_bytes() == (a.home / ".ssh" / f).read_bytes()
        assert stat.S_IMODE((b.home / ".ssh" / name).stat().st_mode) == 0o600
        item_b = keys.item(b.home / ".ssh" / name)
        assert item_b != keys.item(a.home / ".ssh" / name)
        assert b.ring.items[("fleetledger", item_b)] == \
            a.ring.items[("fleetledger", keys.item(a.home / ".ssh" / name))]
    assert b_doc["fleet"]["managed_keys"] == {"store": "keyring", "keys": [
        "~/.ssh/shipshape_fleet_ed25519", "~/.ssh/shipshape_host_ca_ed25519",
        "~/.ssh/shipshape_user_ca_ed25519"]}

    # Your own key FleetLedger made stays on your machines: only its .pub moves.
    assert not (b.home / ".ssh" / "shipshape_ed25519").exists()
    own = b.root / "config" / "agent-keys" / "shipshape_ed25519.pub"
    assert own.read_bytes() == (a.home / ".ssh" / "shipshape_ed25519.pub").read_bytes()
    assert [paths.expand(k) for k in b_doc["fleet"]["agent_keys"]] == [own]
    assert not any("shipshape_ed25519-" in item for _svc, item in b.ring.items)

    # What never moves; known_hosts is written afresh from the pins.
    assert not (b.root / "config" / "this-computer.json").exists()
    for gone in ("config/proxmox-setup-progress.json", "config/remote-ui.json",
                 "data/inbox-spool", "data/images/base", "data/web"):
        assert not (b.root / gone).exists(), gone
    assert "known_hosts" not in b_doc["fleet"]
    assert (b.root / "config" / "known_hosts").read_text() == \
        (a.root / "config" / "known_hosts").read_text()
    # What does: history and caches.
    for kept in ("audit.log", "attempts.json", "net/ship-01.json", "status/ship-01.json",
                 "history.sqlite3"):
        assert stat.S_IMODE((b.root / "data" / kept).stat().st_mode) == 0o600, kept
    for kept in ("trust/laptop", "trust/phone"):
        assert (b.root / "data" / kept).exists(), kept
    rows = sqlite3.connect(b.root / "data" / "history.sqlite3").execute(
        "select * from samples").fetchall()
    assert rows == [("ship-01", 1.0)]

    # B waits on standby until it is handed the fleet: A retires, then B activates.
    code, out, err = b.run("ls")
    assert code == 1 and "on standby" in err
    a.use(monkeypatch)
    assert a.run("migrate", "retire", "--to", "vm", "--yes")[0] == 0
    a_files["config/retired.json"] = (a.root / "config" / "retired.json").read_bytes()
    b.use(monkeypatch)
    code, out, err = b.run("migrate", "activate", "--yes")
    assert code == 0 and "now the active one (epoch 1)" in out, out + err

    # Copy B now acts on the fleet as A did: ca sign finds every Ship up to date, a
    # renewal is recorded beside A's certificates, and still nothing changed on the Ships.
    code, out, err = b.run("ca", "sign")
    assert code == 0 and out.count("up to date") == 2, out + err
    code, out, err = b.run("trust", "laptop", "--print-cert")
    assert code == 0 and out.startswith("ssh-ed25519-cert-v01@openssh.com "), err
    laptop = tomllib.loads(b.store.path.read_text())["machine"]["laptop"]
    before = [i["serial"] for i in a_doc["machine"]["laptop"]["issued"]]
    assert [i["serial"] for i in laptop["issued"]] == [*before, laptop["last_serial"]]
    code, out, err = b.run("ls")
    assert code == 0 and "ship-01" in out and "ship-02" in out
    assert world.state() == ships_before

    # Nothing on the old computer changed but its marker, and it refuses to act.
    a.use(monkeypatch)
    assert a.files() == a_files
    code, out, err = a.run("ca", "sign")
    assert code == 1 and "retired" in err and world.state() == ships_before


def test_the_file_store_keeps_every_secret_in_0600_files(computers, monkeypatch):
    a, b, world = computers
    code = receive(b, monkeypatch, send_bytes(a, monkeypatch), store_kind="file")
    assert code == migrate.VERIFIED, b.out.getvalue()
    doc = tomllib.loads(b.store.path.read_text())
    assert doc["fleet"]["managed_keys"]["store"] == "file"
    for name in ("shipshape_fleet_ed25519", "shipshape_host_ca_ed25519",
                 "shipshape_user_ca_ed25519"):
        secret = keys.passphrase_file(b.home / ".ssh" / name)
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert b.ring.sets == []
    assert b.run("migrate", "activate", "--yes")[0] == 0
    b_files = b.files()
    code, out, err = b.run("migrate", "check")  # and it can move on again from here
    assert code == 0 and "Ready to move" in out, out + err
    assert b.files() == b_files


def test_the_proxmox_token_and_oauth_secret_are_stored_again_and_checked(
        computers, monkeypatch):
    a, b, world = computers
    a.use(monkeypatch)
    token_item = secret_store.proxmox_item("fleet@pve!ledger")
    oauth_item = secret_store.oauth_item("k123EXAMPLE")
    a.ring.set_password("fleetledger", token_item, "11111111-2222-3333-4444-555555555555")
    a.ring.set_password("fleetledger", oauth_item, "tskey-client-k123EXAMPLE-SECRET")
    with a.store.edit() as ed:
        ed.set_driver("proxmox", {
            "api_url": "https://pve.example.com:8006/api2/json", "node": "pve",
            "pool": "shipshape", "template_vmid": 9000, "seed_storage": "local",
            "disk_storage": "local-lvm", "bridge": "vmbr0", "token_id": "fleet@pve!ledger",
            "token_store": "keyring", "keyring_item": token_item,
            "tls_fingerprint": ":".join(["AB"] * 32)})
        ed.set_overlay({"provider": "tailscale", "tags": ["tag:ship"],
                        "key_source": "tailscale-api",
                        "api": {"tailnet": "-", "oauth_client_id": "k123EXAMPLE",
                                "secret_store": "keyring", "keyring_item": oauth_item}})
    stream = send_bytes(a, monkeypatch)
    holding = [i.name for i, data in _members(stream) if b"11111111-2222" in data]
    assert holding == ["secrets.json"]  # in no other member, the manifest included

    seen = {}

    class PVE:
        def __init__(self, config):
            seen["token"] = proxmox_api.token_secret(config)

        def get(self, path):
            return {"version": "8.2"}

    class Tailnet:
        granted = frozenset({"auth_keys"})

        def __init__(self, api):
            seen["oauth"] = secret_store.load(api)

        def token(self):
            return "t"

    b.app.proxmox_api = PVE
    b.app.overlay_api = Tailnet
    code = receive(b, monkeypatch, stream, store_kind="file")
    out = b.out.getvalue()
    assert code == migrate.VERIFIED, out
    assert seen == {"token": "11111111-2222-3333-4444-555555555555",
                    "oauth": "tskey-client-k123EXAMPLE-SECRET"}
    doc = tomllib.loads(b.store.path.read_text())
    pve = doc["drivers"]["proxmox"]
    assert "token_store" not in pve and "keyring_item" not in pve
    token_file = paths.expand(pve["token_file"])
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert doc["overlay"]["api"]["secret_store"] == "file"
    assert "works through the pinned certificate" in out and "scopes the keys level" in out

    # Into a keyring, under the names this computer works out.
    b.root.joinpath("config").rename(b.root / "config-file")
    shutil.rmtree(b.root / "data")
    shutil.rmtree(b.home / ".ssh")
    code = receive(b, monkeypatch, stream, store_kind="keyring")
    assert code == migrate.VERIFIED, b.out.getvalue()
    assert b.ring.items[("fleetledger", token_item)].startswith("11111111")
    assert b.ring.items[("fleetledger", oauth_item)].startswith("tskey-client")


# --- what moves, what stays, what stops it --------------------------------------------------


def test_check_lists_what_moves_what_stays_and_why(computers, monkeypatch):
    a, b, world = computers
    code, out, err = a.run("migrate", "check")
    assert code == 0, out + err
    moves, stays = out.split("Stays here:")
    for what in ("inventory: 2 Ship(s), 4 trusted machine(s) (1 untrusted)", "fleet admin key",
                 "host CA key", "user CA key", "your key for Ships (public half)", "audit log",
                 "last status", "charts and Activity history", "failed Ship attempts",
                 "network summaries", "revocation list", "trust bundle",
                 "secrets, stored again there", "3 key passphrase(s)"):
        assert what in moves, what
    assert "revoked host key" in moves and "issued certificate(s) on record" in moves
    for path, why in (("this-computer.json", "a fact about this computer"),
                      ("known_hosts", "rebuilt from the inventory"),
                      ("proxmox-setup-progress.json", "part-finished Proxmox setup"),
                      ("remote-ui.json", "the remote web UI setting: this computer's own name"),
                      ("web/devices.json", "paired browsers and pairing links"),
                      ("web/pairings.json", "pair each device again"),
                      ("web/.lock", "bound to this computer's address"),
                      ("inbox-spool/half-sent", "half-way to a Ship"),
                      ("toolkit-archives/examplekit.tar.gz", "rebuilt from the toolkit's folder"),
                      ("images/base/noble.img", "Ubuntu's cloud image"),
                      ("shipshape_ed25519", "stay on your machines")):
        line = next((x for x in stays.splitlines() if path in x), "")
        assert why in line, (path, stays)
    assert "Ready to move" in out
    assert "is on no remote branch yet: push it" in out  # said here, not on the new computer
    assert a.run("migrate", "check")[0] == 0  # read-only: the same again


@pytest.mark.parametrize("where", ["config/notes.txt", "data/backup/ships.toml.bak",
                                   "data/ca/old.krl", "data/web/future-piece.json"])
def test_a_file_fleetledger_does_not_know_stops_the_move(computers, monkeypatch, where):
    a, b, world = computers
    path = a.root / where
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("mine\n")
    code, out, err = a.run("migrate", "check")
    assert code == 1 and "Stops the move" in out and "does not know this file" in out
    with pytest.raises(FleetError, match="Not moving"):
        send_bytes(a, monkeypatch)


def test_a_ship_not_enrolled_yet_or_a_seed_stops_the_move(computers, monkeypatch):
    a, b, world = computers
    with a.store.edit() as ed:
        ed.set_field("ship-02", "enrolled", None)
    seeds = a.root / "data" / "seeds"
    seeds.mkdir()
    (seeds / "ship-02-1.iso").write_bytes(b"seed")
    code, out, err = a.run("migrate", "check")
    assert code == 1
    assert "Ship ship-02: is not enrolled yet" in out and "seed holds a Ship's private host" in out


def test_another_ship_fleet_running_stops_the_move_and_waits_for_none(computers, monkeypatch):
    a, b, world = computers
    with inuse.using(a.store):  # the web UI, or a sign-in in another terminal
        code, out, err = a.run("migrate", "check")
        assert code == 1 and "Another ship-fleet is running on this computer" in out
        code, out, err = a.run("migrate", "push", "you@vm.example")
        assert code == 1 and "Another ship-fleet is running" in err
        with pytest.raises(FleetError, match="Another ship-fleet"):
            send_bytes(a, monkeypatch)
    with inuse.exclusive(a.store):  # a migrate copying the fleet: nothing else starts
        code, out, err = a.run("ls")
        assert code == 1 and "being moved to or from another computer" in err
    assert a.run("ls")[0] == 0


def test_a_locked_keyring_stops_the_move_before_anything_is_sent(computers, monkeypatch):
    a, b, world = computers
    a.ring = fake_backend(locked=True)
    code, out, err = a.run("migrate", "check")
    assert code == 1 and "the fleet admin key's passphrase" in out and "locked" in out
    with pytest.raises(FleetError):
        send_bytes(a, monkeypatch)


def test_no_secret_is_ever_printed(computers, monkeypatch):
    a, b, world = computers
    plan = migrate.check(a.app)
    secrets = list(plan.secrets["passphrases"].values())
    assert len(secrets) == 3 and all(len(x) > 20 for x in secrets)
    said = "".join(a.run("migrate", "check")[1:])
    a.app.popen = lambda argv, **kw: pytest.fail("ssh ran")
    with inuse.using(a.store):  # a blocker: push prints the plan, never runs ssh
        said += "".join(a.run("migrate", "push", "vm.example")[1:])
    assert receive(b, monkeypatch, send_bytes(a, monkeypatch)) == migrate.VERIFIED
    said += b.out.getvalue() + b.err.getvalue() + "".join(b.run("migrate", "verify")[1:])
    for secret in secrets:
        assert secret not in said


def test_other_commands_run_beside_each_other(computers, monkeypatch):
    a, b, world = computers
    with inuse.using(a.store), inuse.using(a.store):  # the web UI, and a sign-in, say
        code, out, err = a.run("ls")
        assert code == 0, err
        code, out, err = a.run("doctor", "--offline")
        assert "being moved" not in err


def test_uncommitted_changes_in_the_checkout_stop_the_move(computers, monkeypatch):
    a, b, world = computers
    (a.checkout.root / "ship" / "bin" / "ship-ctl").write_text("#!/bin/sh\necho changed\n")
    code, out, err = a.run("migrate", "check")
    assert code == 1 and "uncommitted changes" in out


def test_send_writes_only_into_a_pipe(computers, monkeypatch, tmp_path):
    a, b, world = computers
    with open(tmp_path / "fleet.tar", "wb") as fh, pytest.raises(FleetError, match="never into"):
        migrate._pipe_only(fh)
    assert not (tmp_path / "fleet.tar").read_bytes()
    r, w = os.pipe()
    with os.fdopen(w, "wb") as fh:
        migrate._pipe_only(fh)  # a pipe is fine
    os.close(r)


def test_send_stops_cleanly_when_the_other_side_stops_reading(computers, monkeypatch):
    a, b, world = computers

    class Gone(io.RawIOBase):
        def writable(self):
            return True

        def write(self, data):
            raise BrokenPipeError

    a.app.stdout_bytes = Gone()
    before = a.files()
    code, out, err = a.run("migrate", "send")
    assert code == 1 and "stopped reading" in err and out == ""
    assert a.files() == before


# --- receive: refusals, and nothing written ---------------------------------------------------


def test_receive_never_writes_over_a_fleetledger(computers, monkeypatch):
    a, b, world = computers
    stream = send_bytes(a, monkeypatch)
    b.use(monkeypatch)
    (b.root / "data").mkdir(parents=True)
    (b.root / "data" / "audit.log").write_text("another fleet\n")
    with pytest.raises(FleetError, match="already has a FleetLedger"):
        receive(b, monkeypatch, stream)
    assert not b.store.path.exists() and not (b.home / ".ssh").exists()


def test_receive_wants_the_same_commit(computers, monkeypatch, tmp_path):
    a, b, world = computers
    stream = send_bytes(a, monkeypatch)
    other = tmp_path / "other-checkout"
    shutil.copytree(a.checkout.root, other)
    (other / "ship" / "files" / "herdr.service").write_text("[Unit]\nDescription=newer\n")
    commit_all(other, "newer")
    from fleetledger.repo import Repo

    b.checkout = Repo(other)
    with pytest.raises(FleetError, match="both must run the same commit"):
        receive(b, monkeypatch, stream)
    assert not b.store.path.exists()


def test_receive_wants_a_clean_checkout_too(computers, monkeypatch):
    a, b, world = computers
    stream = send_bytes(a, monkeypatch)
    (a.checkout.root / "versions.toml").write_text("# changed here\n")  # B's checkout too
    with pytest.raises(FleetError, match="uncommitted changes"):
        receive(b, monkeypatch, stream)
    assert not b.store.path.exists()


def test_receive_refuses_while_another_ship_fleet_runs_there(computers, monkeypatch):
    a, b, world = computers
    stream = send_bytes(a, monkeypatch)
    b.use(monkeypatch)
    with inuse.using(b.store), pytest.raises(FleetError, match="Another ship-fleet is running"):
        receive(b, monkeypatch, stream)
    assert not b.store.path.exists()


def _members(stream: bytes) -> list[tuple[tarfile.TarInfo, bytes]]:
    with tarfile.open(fileobj=io.BytesIO(stream), mode="r|") as tar:
        return [(m, tar.extractfile(m).read()) for m in tar]


def _pack(members) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        for info, data in members:
            info = copy.copy(info)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _tamper(stream: bytes, how: str) -> bytes:
    members = _members(stream)
    if how == "flipped":
        members = [(i, d[:-2] + bytes([d[-2] ^ 1]) + d[-1:]) if i.name == "keys/admin.pub"
                   else (i, d) for i, d in members]
    elif how == "cut short":
        members = [m for m in members if m[0].name != "data/audit.log"]
    elif how == "extra":
        extra = tarfile.TarInfo("data/net/extra.json")
        members.insert(2, (extra, b"{}"))
    elif how == "manifest":
        manifest = json.loads(members[0][1])
        manifest["files"][0]["member"] = "../../outside"
        members[0] = (members[0][0], json.dumps(manifest).encode())
    elif how == "secrets":
        manifest = json.loads(members[0][1])
        bad = b"not json"
        entry = next(f for f in manifest["files"] if f["member"] == "secrets.json")
        entry.update(size=len(bad), sha256=hashlib.sha256(bad).hexdigest())
        members[0] = (members[0][0], json.dumps(manifest).encode())
        members[-1] = (members[-1][0], bad)
    elif how == "control character":
        secrets = json.loads(members[-1][1])
        secrets["passphrases"]["admin"] += "\r"
        data = json.dumps(secrets).encode()
        manifest = json.loads(members[0][1])
        entry = next(f for f in manifest["files"] if f["member"] == "secrets.json")
        entry.update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        members[0] = (members[0][0], json.dumps(manifest).encode())
        members[-1] = (members[-1][0], data)
    elif how == "missing key":  # listed under keys, carried by neither files nor the stream
        manifest = json.loads(members[0][1])
        gone = {"keys/host-ca", "keys/host-ca.pub"}
        manifest["files"] = [f for f in manifest["files"] if f["member"] not in gone]
        members = [(members[0][0], json.dumps(manifest).encode())] + \
            [m for m in members[1:] if m[0].name not in gone]
    elif how == "pub half":  # the private half carried, its .pub not
        manifest = json.loads(members[0][1])
        manifest["files"] = [f for f in manifest["files"] if f["member"] != "keys/host-ca.pub"]
        members = [(members[0][0], json.dumps(manifest).encode())] + \
            [m for m in members[1:] if m[0].name != "keys/host-ca.pub"]
    elif how in ("root source", "folder source"):  # placed by a source that names no file
        manifest = json.loads(members[0][1])
        key = next(k for k in manifest["keys"] if k["role"] == "host-ca")
        key["source"] = "/" if how == "root source" else str(Path(key["source"]).parent) + "/"
        members[0] = (members[0][0], json.dumps(manifest).encode())
    elif how == "unclaimed key":  # carried, but no key entry says what it is
        manifest = json.loads(members[0][1])
        manifest["keys"] = [k for k in manifest["keys"] if k["role"] != "host-ca"]
        members[0] = (members[0][0], json.dumps(manifest).encode())
    elif how == "listed twice":
        manifest = json.loads(members[0][1])
        agent = next(k for k in manifest["keys"] if k["role"] == "agent")
        manifest["keys"].append(dict(agent, name="another.pub"))
        members[0] = (members[0][0], json.dumps(manifest).encode())
    elif how == "agent member":
        manifest = json.loads(members[0][1])
        next(k for k in manifest["keys"] if k["role"] == "agent")["member"] = "data/audit.log"
        members[0] = (members[0][0], json.dumps(manifest).encode())
    elif how == "ssh source":  # a harmless name, but placed by its source: ~/.ssh/config
        manifest = json.loads(members[0][1])
        key = next(k for k in manifest["keys"] if k["role"] == "user-ca")
        key["source"] = str(Path(key["source"]).with_name("config"))
        members[0] = (members[0][0], json.dumps(manifest).encode())
    elif how == "cut in the manifest":
        return stream[:700]
    elif how in ("..", "config"):
        manifest = json.loads(members[0][1])
        key = next(k for k in manifest["keys"] if k["role"] == ("agent" if how == ".." else
                                                               "host-ca"))
        key["name"] = how
        members[0] = (members[0][0], json.dumps(manifest).encode())
    elif how == "truncated":
        return stream[: len(stream) // 2]
    return _pack(members)


@pytest.mark.parametrize("how", ["flipped", "cut short", "extra", "manifest", "secrets",
                                 "control character", "..", "config", "missing key",
                                 "agent member", "ssh source", "cut in the manifest",
                                 "pub half", "root source", "folder source", "unclaimed key",
                                 "listed twice", "truncated"])
def test_a_stream_that_is_not_what_was_sent_writes_nothing(computers, monkeypatch, how):
    a, b, world = computers
    bad = _tamper(send_bytes(a, monkeypatch), how)
    b.use(monkeypatch)
    with pytest.raises(FleetError, match="nothing was written|not a FleetLedger move") as said:
        receive(b, monkeypatch, bad)
    if how in ("missing key", "pub half"):
        assert "keys/host-ca is not in the stream" in said.value.message
    if how == "unclaimed key":
        assert "no key entry says what it is" in said.value.message
    assert not b.store.path.exists() and not (b.home / ".ssh").exists()
    assert b.ring.sets == []
    assert not list((b.root / "data").glob(migrate.STAGING + "*"))
    assert receive(b, monkeypatch, send_bytes(a, monkeypatch)) == migrate.VERIFIED  # then fine


def test_a_key_already_there_and_different_is_never_overwritten(computers, monkeypatch):
    a, b, world = computers
    stream = send_bytes(a, monkeypatch)
    b.use(monkeypatch)
    (b.home / ".ssh").mkdir(mode=0o700)
    keygen(b.home / ".ssh" / "shipshape_host_ca_ed25519", "someone else's CA")
    before = (b.home / ".ssh" / "shipshape_host_ca_ed25519").read_bytes()
    with pytest.raises(FleetError, match="Not writing over files already here"):
        receive(b, monkeypatch, stream)
    assert (b.home / ".ssh" / "shipshape_host_ca_ed25519").read_bytes() == before
    assert not b.store.path.exists() and not (b.home / ".ssh" / "shipshape_fleet_ed25519").exists()


def test_the_same_key_already_there_is_kept_as_it_is(computers, monkeypatch):
    a, b, world = computers
    stream = send_bytes(a, monkeypatch)
    (b.home / ".ssh").mkdir(mode=0o700)
    for f in ("shipshape_fleet_ed25519", "shipshape_fleet_ed25519.pub"):
        shutil.copy(a.home / ".ssh" / f, b.home / ".ssh" / f)  # copied by hand before
    assert receive(b, monkeypatch, stream) == migrate.VERIFIED, b.out.getvalue()


def test_a_failure_part_way_takes_back_everything_it_wrote(computers, monkeypatch):
    a, b, world = computers
    stream = send_bytes(a, monkeypatch)
    b.use(monkeypatch)
    b.ring.set_password("fleetledger", "key-passphrase:unrelated", "keep me")
    real = b.ring.set_password
    calls = []

    def flaky(service, item, value):
        calls.append(item)
        if len(calls) == 2:
            raise RuntimeError("the keyring went away")
        real(service, item, value)

    b.ring.set_password = flaky
    with pytest.raises(FleetError, match="Could not store"):
        receive(b, monkeypatch, stream)
    assert not b.store.path.exists()
    assert not (b.home / ".ssh" / "shipshape_fleet_ed25519").exists()
    assert not list((b.root / "data").rglob("*.*"))
    assert set(b.ring.items) == {("fleetledger", "key-passphrase:unrelated")}


def test_the_keyring_asked_for_but_locked_is_refused_not_swapped(computers, monkeypatch):
    a, b, world = computers
    stream = send_bytes(a, monkeypatch)
    b.ring = fake_backend(locked=True)
    with pytest.raises(FleetError, match="Cannot keep the secrets in the keyring here"):
        receive(b, monkeypatch, stream, store_kind="keyring")
    assert not b.store.path.exists()


# --- verify --------------------------------------------------------------------------------


def test_a_ship_the_new_computer_cannot_reach_leaves_it_unverified(computers, monkeypatch):
    a, b, world = computers
    stream = send_bytes(a, monkeypatch)
    world.down.add("ship-02")
    code = receive(b, monkeypatch, stream)
    out = b.out.getvalue()
    assert code == migrate.UNVERIFIED and "FAIL" in out and "ship-02" in out
    assert "ship-fleet migrate verify" in out
    assert b.store.path.exists()  # written: fix it, then verify
    world.down.clear()
    code, out, err = b.run("migrate", "verify")
    assert code == 0 and "FAIL" not in out, out + err


def test_a_ship_behind_on_the_revocation_list_is_a_warning_not_ok(computers, monkeypatch):
    a, b, world = computers
    stream = send_bytes(a, monkeypatch)
    ship = world.ships["ship-02"]
    ship.trust = copy.deepcopy(ship.trust)
    ship.trust["revoked_keys"] = base64.b64encode(b"some other list").decode()
    assert receive(b, monkeypatch, stream) == migrate.VERIFIED  # a warning, not a failure
    out = b.out.getvalue()
    assert re.search(r"WARN +ship-02 SSH trust +its user CA keys or revocation list differ", out)
    assert "ship-fleet ca sign ship-02" in out
    assert re.search(r"ok +ship-01 certificate +signed by this CA; same revocation list", out)


def test_the_remote_web_ui_logins_are_said_not_turned_on(computers, monkeypatch):
    a, b, world = computers
    from fleetledger.web import remote

    remote.save(remote.Remote("https://desktop.example-tailnet.ts.net", 8765,
                              ("you@example.com",)))
    assert receive(b, monkeypatch, send_bytes(a, monkeypatch)) == migrate.VERIFIED
    assert "--port 8765 --login you@example.com (docs/runbooks/remote-ui.md)" in \
        b.out.getvalue()
    assert not (b.root / "config" / "remote-ui.json").exists()  # off until you turn it on


def test_a_different_time_zone_is_said(computers, monkeypatch):
    a, b, world = computers
    monkeypatch.setenv("TZ", "Europe/Berlin")
    stream = send_bytes(a, monkeypatch)
    monkeypatch.setenv("TZ", "America/New_York")
    assert receive(b, monkeypatch, stream) == migrate.VERIFIED
    assert "time zone" in b.out.getvalue() and "Europe/Berlin" in b.out.getvalue()


# --- images --------------------------------------------------------------------------------


IMAGE = "26.04-20261102-3f9a1c2e"


def _with_image(a: Computer, published: bool) -> Path:
    image = a.root / "data" / "images" / f"{IMAGE}.qcow2"
    image.write_bytes(b"qcow2" * 1000)
    (a.root / "data" / "images" / f"{IMAGE}.json").write_text("{}\n")
    entry = {"created": "2026-11-02T10:00:00Z", "sha256": "0" * 64, "file": str(image),
             "ubuntu": {"release": "26.04", "build": "20261016"}, "tooling": "0.1.0",
             "commit": "0" * 40, "recipes": []}
    if published:
        entry["published"] = {"proxmox": {"template_vmid": 9181}}
    with a.store.edit() as ed:
        ed.add_image(IMAGE, entry)
    return image


def test_baked_images_stay_unless_asked_for(computers, monkeypatch):
    a, b, world = computers
    image = _with_image(a, published=True)
    code, out, err = a.run("migrate", "check")
    assert code == 0 and "published to proxmox, so new Ships still use it" in out
    assert receive(b, monkeypatch, send_bytes(a, monkeypatch)) == migrate.VERIFIED
    doc = tomllib.loads(b.store.path.read_text())
    assert "file" not in doc["image"][IMAGE] and doc["image"][IMAGE]["published"]
    assert not (b.root / "data" / "images" / image.name).exists()


def test_with_images_moves_them_too(computers, monkeypatch):
    a, b, world = computers
    image = _with_image(a, published=False)
    code, out, err = a.run("migrate", "check")
    assert "not published anywhere" in out
    stream = send_bytes(a, monkeypatch, with_images=True)
    assert receive(b, monkeypatch, stream) == migrate.VERIFIED
    doc = tomllib.loads(b.store.path.read_text())
    moved = paths.expand(doc["image"][IMAGE]["file"])
    assert moved == b.root / "data" / "images" / image.name
    assert moved.read_bytes() == image.read_bytes()
    assert (b.root / "data" / "images" / f"{IMAGE}.json").exists()


# --- push: over SSH into receive ---------------------------------------------------------------


class FakeSSHProcess:
    """ssh to the new computer: takes the stream, answers as receive would."""

    def __init__(self, argv, code, said=b"", stdin=subprocess.PIPE):
        self.argv, self.code = argv, code
        self.stdout = io.BytesIO(said)
        self.stderr = io.BytesIO(b"")
        self.received = b""
        self.stdin = self._reader = None
        if stdin == subprocess.PIPE:
            r, w = os.pipe()
            self.stdin = os.fdopen(w, "wb")
            self._r = os.fdopen(r, "rb")
            self._reader = threading.Thread(target=self._read, daemon=True)
            self._reader.start()

    def _read(self):
        with self._r:
            self.received = self._r.read()

    def wait(self):
        if self._reader is not None:
            self._reader.join(timeout=30)
        return self.code


def test_push_streams_everything_into_receive_over_ssh(computers, monkeypatch):
    a, b, world = computers
    procs = []

    def popen(argv, stdin=None, **kw):
        procs.append(FakeSSHProcess(argv, 0, b"This computer has the fleet\n", stdin))
        return procs[-1]

    a.app.popen = popen
    code, out, err = a.run("migrate", "push", "you@vm.example", "--store", "file",
                           "-o", "Port=2222", "-o", "StrictHostKeyChecking=no")
    assert code == 0, out + err
    argv = procs[0].argv
    assert argv[:2] == ["ssh", "-T"] and argv[-2] == "you@vm.example"
    opts = [argv[i + 1] for i, x in enumerate(argv) if x == "-o"]
    assert opts.index("StrictHostKeyChecking=yes") < opts.index("StrictHostKeyChecking=no")
    assert "ForwardAgent=no" in opts and "Port=2222" in opts
    assert argv[-1] == "sh -lc 'exec ship-fleet migrate receive --pushed --store file'"
    assert "vm.example: This computer has the fleet" in out
    # Checked there, so the hand-over: this copy retires first, then vm.example activates.
    assert procs[1].argv[-1] == "sh -lc 'exec ship-fleet migrate activate --yes'"
    assert "-o" in procs[1].argv and "Port=2222" in procs[1].argv
    assert "now the active FleetLedger (epoch 1)" in out
    assert json.loads((a.root / "config" / "retired.json").read_text())["other"] == \
        "you@vm.example"
    assert receive(b, monkeypatch, procs[0].received, store_kind="file") == migrate.VERIFIED


@pytest.mark.parametrize("exit_code,says", [
    (255, "ssh could not log in to"), (127, "has no ship-fleet on PATH"),
    (migrate.UNVERIFIED, "could not check all of it"), (1, "did not take the fleet")])
def test_push_says_what_happened_there(computers, monkeypatch, exit_code, says):
    a, b, world = computers
    a.app.popen = lambda argv, stdin=None, **kw: FakeSSHProcess(argv, exit_code, stdin=stdin)
    code, out, err = a.run("migrate", "push", "vm.example")
    assert code == exit_code if exit_code not in (255, 127) else code == 1
    assert says in out + err


def test_push_refuses_a_host_that_is_not_one(computers, monkeypatch):
    a, b, world = computers
    a.app.popen = lambda argv, **kw: pytest.fail("ssh ran")
    for host in ("-oProxyCommand=evil", "vm example", "a@b@c", ""):
        code, out, err = a.run("migrate", "push", host)
        assert code != 0, host


def test_a_key_lands_in_ssh_or_fleetledgers_folders_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FLEETLEDGER_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("FLEETLEDGER_DATA_DIR", str(tmp_path / "data"))
    src = {"home": "/srv/old", "config_dir": "/srv/old/.config/fleetledger",
           "data_dir": "/srv/fleetledger"}
    home = tmp_path / "home"
    assert migrate._target_key_path("/srv/old/.ssh/sub/admin", src) == home / ".ssh/sub/admin"
    assert migrate._target_key_path("/srv/old/.config/fleetledger/k/ca", src) == \
        tmp_path / "config" / "k" / "ca"
    assert migrate._target_key_path("/srv/fleetledger/ca/host", src) == tmp_path / "data/ca/host"
    for elsewhere in ("/srv/old/.bashrc", "/srv/old/.config/autostart/x", "/etc/keys/admin",
                      "/srv/old/.ssh/../.profile"):
        assert migrate._target_key_path(elsewhere, src).parent == home / ".ssh", elsewhere


def test_the_remote_command_quotes_a_path_under_home():
    assert migrate.remote_command("~/.local/bin/ship-fleet", ["migrate", "receive"]) == \
        "sh -lc 'exec \"$HOME\"/.local/bin/ship-fleet migrate receive'"
    assert migrate.remote_command("/opt/x y/ship-fleet", ["migrate", "activate"]) == \
        "sh -lc 'exec '\"'\"'/opt/x y/ship-fleet'\"'\"' migrate activate'"


# --- FleetLedger's vault (vault.py) on either computer ---------------------------------------


@pytest.fixture
def vaults(monkeypatch):
    """The vault, quick (a lower scrypt cost) and allowed in these temporary folders."""
    from fleetledger import vault

    monkeypatch.setattr(vault, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(vault, "in_ram", lambda path: True)
    return vault


def _with_hypervisor_secrets(a: Computer, monkeypatch) -> tuple[str, str]:
    a.use(monkeypatch)
    token_item = secret_store.proxmox_item("fleet@pve!ledger")
    oauth_item = secret_store.oauth_item("k123EXAMPLE")
    a.ring.set_password("fleetledger", token_item, "11111111-2222-3333-4444-555555555555")
    a.ring.set_password("fleetledger", oauth_item, "tskey-client-k123EXAMPLE-SECRET")
    with a.store.edit() as ed:
        ed.set_driver("proxmox", {
            "api_url": "https://pve.example.com:8006/api2/json", "node": "pve",
            "pool": "shipshape", "template_vmid": 9000, "seed_storage": "local",
            "disk_storage": "local-lvm", "bridge": "vmbr0", "token_id": "fleet@pve!ledger",
            "token_store": "keyring", "keyring_item": token_item,
            "tls_fingerprint": ":".join(["AB"] * 32)})
        ed.set_overlay({"provider": "tailscale", "tags": ["tag:ship"],
                        "key_source": "tailscale-api",
                        "api": {"tailnet": "-", "oauth_client_id": "k123EXAMPLE",
                                "secret_store": "keyring", "keyring_item": oauth_item}})
    return token_item, oauth_item


class _PVE:
    def __init__(self, config):
        proxmox_api.token_secret(config)

    def get(self, path):
        return {"version": "8.2"}


class _Tailnet:
    granted = frozenset({"auth_keys"})

    def __init__(self, api):
        secret_store.load(api)

    def token(self):
        return "t"


def test_onto_a_computer_whose_vault_is_on_every_secret_goes_in_it(computers, monkeypatch,
                                                                   vaults):
    """The appliance's way: its vault first (empty, unlocked), then the fleet. Nothing
    lands in a 0600 file or an OS keyring, and a copy of its disk opens nothing."""
    a, b, world = computers
    token_item, oauth_item = _with_hypervisor_secrets(a, monkeypatch)
    secrets_ = [v for (_s, _i), v in a.ring.items.items()]
    stream = send_bytes(a, monkeypatch)
    ships_before = world.state()
    b.use(monkeypatch)
    vaults.create("correct horse battery staple")
    b.app.proxmox_api, b.app.overlay_api = _PVE, _Tailnet
    code = receive(b, monkeypatch, stream)  # no --store: the vault, as this computer's keyring
    assert code == migrate.VERIFIED, b.out.getvalue() + b.err.getvalue()
    assert world.state() == ships_before
    assert b.ring.sets == []  # not one item in the OS keyring
    doc = tomllib.loads(b.store.path.read_text())
    assert doc["fleet"]["managed_keys"]["store"] == "keyring"
    assert doc["drivers"]["proxmox"]["token_store"] == "keyring"
    assert doc["overlay"]["api"]["secret_store"] == "keyring"
    assert not (b.root / "config" / keys.PASSPHRASE_DIR).exists()
    names = vaults.backend().item_names("fleetledger")
    assert token_item in names and oauth_item in names
    assert keys.item(b.home / ".ssh" / "shipshape_fleet_ed25519") in names
    # A backup of B: every file it keeps on disk (the runtime folder is RAM).
    disk = b"".join(f.read_bytes() for f in sorted(b.root.rglob("*")) if f.is_file())
    assert secrets_ and not any(s.encode() in disk for s in secrets_)

    # Locked (a reboot), B is read-only; unlocked, it acts again with nothing else asked.
    assert b.run("migrate", "activate", "--yes")[0] == 0
    vaults.lock()
    code, out, err = b.run("ls")
    assert code == 1 and "vault is locked" in err
    assert b.run("ls", "--offline")[0] == 0
    vaults.unlock("correct horse battery staple")
    monkeypatch.setattr(keys, "_ADDED", {})
    code, out, err = b.run("ls")
    assert code == 0 and "ship-01" in out, err
    assert world.state() == ships_before

    # With the vault on, --store file is refused rather than writing a secret to disk.
    with pytest.raises(FleetError, match="never in a file"):
        migrate.choose_store("file")


@pytest.mark.parametrize("own_key", [True, False])
def test_a_copy_with_its_vault_on_moves_and_its_wipe_empties_the_vault(computers, monkeypatch,
                                                                     vaults, own_key):
    from fleetledger import vaultcmd

    a, b, world = computers
    _with_hypervisor_secrets(a, monkeypatch)
    own = a.home / ".ssh" / "shipshape_ed25519"
    if not own_key:  # your own key is yours to look after: the vault holds only the fleet's
        with a.store.edit() as ed:
            ed.doc["fleet"]["managed_keys"]["keys"].remove("~/.ssh/shipshape_ed25519")
        a.ring.delete_password("fleetledger", keys.item(own))
    vaults.create("correct horse battery staple")
    moved = vaultcmd.move_in(a.app, a.store)
    assert len(moved) == (6 if own_key else 5) and not a.ring.items
    code, out, err = a.run("migrate", "check")
    assert code == 0 and "this computer's vault: the secrets in it move as secrets" in out, \
        out + err
    stream = send_bytes(a, monkeypatch)
    b.app.proxmox_api, b.app.overlay_api = _PVE, _Tailnet
    assert receive(b, monkeypatch, stream) == migrate.VERIFIED, b.out.getvalue()
    assert b.ring.items  # B has no vault: its OS keyring, as before
    a.use(monkeypatch)
    assert a.run("migrate", "retire", "--to", "vm", "--yes")[0] == 0
    code, out, err = a.run("migrate", "wipe", "--now", "--yes")
    assert code == 0, out + err
    if own_key:  # it still opens your own key, which wipe keeps: only the fleet's items go
        assert "it still holds your own key's passphrase" in out
        assert vaults.backend().item_names("fleetledger") == [keys.item(own)]
        assert keys.public_of(own, keys.read_passphrase(own, "keyring")) == \
            keys.public_line(own)
        return
    assert "the vault (every secret in it)" in out
    assert not vaults.vault_path().exists() and not vaults.key_path().exists()
    assert vaults.state() == "off"


# --- backups (backup.py) and restoring one (migrate import) ---------------------------------


def test_a_backup_is_sealed_under_the_vault_and_restores_on_another_computer(
        computers, monkeypatch, vaults, tmp_path):
    from fleetledger import backup

    a, b, world = computers
    _with_hypervisor_secrets(a, monkeypatch)
    code = vaults.create("correct horse battery staple")
    from fleetledger import vaultcmd

    vaultcmd.move_in(a.app, a.store)
    secrets_ = list(vaults.backend()._items()["fleetledger"].values())
    folder = tmp_path / "backups"
    code_, out, err = a.run("backup", "now", "--same-disk", "--to", str(folder), "--keep", "2")
    assert code_ == 0, out + err
    [made] = list(folder.iterdir())
    assert stat.S_IMODE(made.stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    data = made.read_bytes()
    assert secrets_ and not any(s.encode() in data for s in secrets_)  # sealed
    header = json.loads(data.split(b"\n", 1)[0])
    assert header["schema"] == backup.SCHEMA and header["epoch"] == 0
    assert "ships.toml" not in data.decode("ascii", "replace")  # nothing readable inside

    # On another computer: the wrong passphrase opens nothing and writes nothing.
    b.use(monkeypatch)
    b.app.proxmox_api, b.app.overlay_api = _PVE, _Tailnet
    b.app.ask_secret = lambda prompt: "not the passphrase at all"
    code_, out, err = b.run("migrate", "import", str(made))
    assert code_ == 1 and "does not open this backup" in err
    assert not b.store.path.exists()
    # The recovery code opens it; the fleet comes on standby, every Ship checked.
    b.app.ask_secret = lambda prompt: code
    code_, out, err = b.run("migrate", "import", str(made), "--recovery", "--store", "file")
    assert code_ == migrate.VERIFIED, out + err
    assert "Opened the backup of" in out
    assert invariant(tomllib.loads(b.store.path.read_text())) == \
        invariant(tomllib.loads(a.store.path.read_text()))
    assert "on standby" in b.run("ls")[2]

    # Older ones go; never an unencrypted backup (the vault off, or locked).
    a.use(monkeypatch)
    for _ in range(2):
        assert a.run("backup", "now", "--same-disk", "--to", str(folder), "--keep", "2")[0] == 0
    kept = sorted(f.name for f in folder.iterdir())
    assert len(kept) == 2 and made.name not in kept  # the oldest went
    vaults.lock()
    code_, out, err = a.run("backup", "now", "--same-disk", "--to", str(folder))
    assert code_ == 2 and "vault is locked" in err


def test_no_backup_without_the_vault(computers, monkeypatch, tmp_path):
    a, b, world = computers
    code, out, err = a.run("backup", "now", "--same-disk", "--to", str(tmp_path / "backups"))
    assert code == 1 and "there is never an unencrypted backup" in err
    assert not (tmp_path / "backups").exists()


def test_a_backup_runs_beside_the_running_service(computers, monkeypatch, vaults, tmp_path):
    """It takes the inventory's lock while it reads, not the whole FleetLedger: the
    service (holding the shared lock) keeps running."""
    from fleetledger import inuse

    a, b, world = computers
    vaults.create("correct horse battery staple")
    from fleetledger import vaultcmd

    vaultcmd.move_in(a.app, a.store)
    with inuse.using(a.store):  # what a running ship-fleet ui holds
        code, out, err = a.run("backup", "now", "--same-disk", "--to", str(tmp_path / "b"))
    assert code == 0, out + err


def test_a_scheduled_backup_is_a_daily_user_timer(computers, monkeypatch, vaults, tmp_path):
    from test_service import FakeSystem

    a, b, world = computers
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    vaults.create("correct horse battery staple")
    ship_fleet = tmp_path / "bin" / "ship-fleet"
    ship_fleet.parent.mkdir()
    ship_fleet.write_text("#!/bin/sh\n")
    ship_fleet.chmod(0o755)
    system = FakeSystem()
    a.app.system = system
    code, out, err = a.run("backup", "schedule", "--to", str(tmp_path / "nas"), "--at", "04:15",
                           "--keep", "7", "--ship-fleet", str(ship_fleet), "--same-disk")
    assert code == 0, out + err
    units = tmp_path / "xdg" / "systemd" / "user"
    service_text = (units / "fleetledger-backup.service").read_text()
    assert f"ExecStart={ship_fleet} backup now --to {tmp_path / 'nas'} --keep 7 --same-disk" in \
        service_text
    assert f"RequiresMountsFor={tmp_path / 'nas'}" in service_text
    assert "LimitCORE=0" in service_text
    assert "Persistent=true" in (units / "fleetledger-backup.timer").read_text()
    assert "OnCalendar=*-*-* 04:15:00" in (units / "fleetledger-backup.timer").read_text()
    assert ["enable", "--now", "fleetledger-backup.timer"] in system.verbs()
    code, out, err = a.run("backup", "unschedule")
    assert code == 0 and not (units / "fleetledger-backup.timer").exists()


def test_push_to_fleetledgers_own_vm_checks_its_pin_before_any_option_of_yours(
        computers, monkeypatch):
    """The pin's options come before any -o you give (ssh keeps the first), so a later
    UserKnownHostsFile or HostKeyAlias cannot replace it."""
    import tomlkit

    a, b, world = computers
    pin = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHNlbGZzZWxmc2VsZnNlbGZzZWxmc2VsZnNlbGZzZWxm"
    with a.store.edit() as ed:
        entry = tomlkit.inline_table()
        entry.update({"name": "fleetledger", "vmid": 120, "names": ["fleetledger.local"],
                      "host_key": pin})
        ed.doc["fleet"]["self"] = entry
    procs = []

    def popen(argv, stdin=None, **kw):
        procs.append(FakeSSHProcess(argv, 255, b"", stdin))
        return procs[-1]

    a.app.popen = popen
    a.run("migrate", "push", "fleetledger@fleetledger.local", "-o", "UserKnownHostsFile=/dev/null",
          "-o", "HostKeyAlias=elsewhere", "-o", "GlobalKnownHostsFile=/etc/ssh/ssh_known_hosts")
    opts = [procs[0].argv[i + 1] for i, x in enumerate(procs[0].argv) if x == "-o"]
    first = {}
    for opt in opts:
        key, value = opt.split("=", 1)
        first.setdefault(key, value)
    assert first["StrictHostKeyChecking"] == "yes"
    assert first["HostKeyAlias"] == "fleetledger-self"
    assert first["GlobalKnownHostsFile"] == "/dev/null"
    known = Path(first["UserKnownHostsFile"])
    assert f"fleetledger-self {pin}" in known.read_text()


def test_a_backup_leaves_out_what_only_stops_a_move_and_says_so(computers, monkeypatch, vaults,
                                                               tmp_path):
    from fleetledger import backup, firstrun, vaultcmd

    a, b, world = computers
    vaults.create("correct horse battery staple")
    vaultcmd.move_in(a.app, a.store)
    (a.root / "data" / "stray.txt").write_text("a file FleetLedger does not know\n")
    seeds = a.root / "data" / "seeds"
    seeds.mkdir(exist_ok=True)
    (seeds / "appliance-fleetledger.iso").write_bytes(b"its private host key")
    code, out, err = a.run("backup", "now", "--same-disk", "--to", str(tmp_path / "b"))
    assert code == 0, out + err
    assert "Left out (it would stop a move, not a backup)" in out
    assert "stray.txt" in out and "appliance-fleetledger.iso" in out
    assert "FleetLedger's own VM's seed holds that VM's private host key" in out
    last = backup.last()
    assert last["ok"] and len(last["left_out"]) == 2
    assert [i.state for i in firstrun.backup_items()] == ["ok"]
    # A handed-over copy still refuses, and doctor says the last one failed, and why.
    assert a.run("migrate", "retire", "--to", "vm", "--yes")[0] == 0
    from fleetledger import handover

    with pytest.raises(FleetError, match="retired"):
        backup.seal(a.app)
    handover.clear(a.store, "retired")
    vaults.lock()
    code, out, err = a.run("backup", "now", "--same-disk", "--to", str(tmp_path / "b"))
    assert code == 2 and "vault is locked" in err
    items = firstrun.backup_items()
    assert items[0].state == "warn" and "vault is locked" in items[0].detail


def test_a_backup_never_goes_to_this_computers_own_disk_unasked(computers, monkeypatch, vaults,
                                                               tmp_path):
    from fleetledger import backup, vaultcmd

    a, b, world = computers
    vaults.create("correct horse battery staple")
    vaultcmd.move_in(a.app, a.store)
    folder = tmp_path / "mnt" / "nas"  # a share that is not mounted: just a folder here
    code, out, err = a.run("backup", "now", "--to", str(folder))
    assert code == 1 and "on this computer's own disk" in err and "--same-disk" in err
    assert not folder.exists()
    assert backup.last()["ok"] is False
    monkeypatch.setattr(backup, "on_this_disk", lambda folder: False)  # mounted elsewhere
    assert a.run("backup", "now", "--to", str(folder))[0] == 0


def test_prune_touches_only_fleetledgers_own_backups(tmp_path):
    from fleetledger import backup

    for name in ("fleetledger-20261101-000000-000001-e1.flbackup",
                 "fleetledger-20261102-000000-000001-e1.flbackup",
                 "fleetledger-20261103-000000-000001-e1.flbackup", "holiday.jpg",
                 "fleetledger-notes.txt"):
        (tmp_path / name).write_text("x")
    gone = backup.prune(tmp_path, 1)
    assert sorted(f.name for f in gone) == ["fleetledger-20261101-000000-000001-e1.flbackup",
                                            "fleetledger-20261102-000000-000001-e1.flbackup"]
    assert (tmp_path / "holiday.jpg").exists() and (tmp_path / "fleetledger-notes.txt").exists()


def test_a_backup_asking_for_a_huge_scrypt_cost_is_refused_unrun(monkeypatch):
    from fleetledger import backup, vault

    header = {"schema": backup.SCHEMA, "slots": [{"kind": "passphrase", "kdf": "scrypt",
                                                   "n": 2 ** 30, "r": 8, "p": 1,
                                                   "salt": "AAAAAAAAAAAAAAAAAAAAAA==",
                                                   "key": "x"}]}
    monkeypatch.setattr(vault, "derive", lambda *a: pytest.fail("scrypt must not run"))
    with pytest.raises(FleetError, match="no passphrase slot FleetLedger can open"):
        backup.open_backup(json.dumps(header).encode() + b"\ntoken", "a passphrase")


def test_a_backup_with_a_half_made_ship_that_never_answers_still_restores_and_activates(
        computers, monkeypatch, vaults, tmp_path):
    """The Ship was being made when the backup ran (its seed left out), and never came
    up: the restored copy says so, and can still become active to finish or destroy it."""
    from fleetledger import vaultcmd

    a, b, world = computers
    vaults.create("correct horse battery staple")
    vaultcmd.move_in(a.app, a.store)
    with a.store.edit() as ed:
        ed.set_field("ship-02", "enrolled", None)
    world.down.add("ship-02")
    code, out, err = a.run("backup", "now", "--same-disk", "--to", str(tmp_path / "saved"))
    assert code == 0 and "Ship ship-02: is not enrolled yet" in out, out + err
    [made] = list((tmp_path / "saved").iterdir())
    b.use(monkeypatch)
    b.app.ask_secret = lambda prompt: "correct horse battery staple"
    code, out, err = b.run("migrate", "import", str(made), "--store", "file")
    assert code == migrate.VERIFIED, out + err
    assert "not enrolled yet, not answering" in out and "FAIL" not in out
    assert "reached every enrolled Ship (not ship-02: still being made" in out
    assert "reached every Ship;" not in out
    code, out, err = b.run("migrate", "activate", "--yes")
    assert code == 0 and "now the active one" in out, out + err
    # An enrolled Ship that does not answer still fails the check, as ever.
    world.down.add("ship-01")
    checks = migrate.verify(b.app)
    assert any(c.state == "fail" and c.name == "ship-01" for c in checks)


def test_doctor_says_when_backups_are_stale_or_not_made_yet(home, tmp_path, monkeypatch):
    import time

    from fleetledger import backup, firstrun

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert firstrun.backup_items() == []  # none scheduled, none made: nothing to say
    units = tmp_path / "xdg" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / backup.TIMER).write_text("# Written by ship-fleet backup schedule\n")
    assert [i.detail for i in firstrun.backup_items()] == ["scheduled; none made yet"]
    (home / "data" / backup.LAST).write_text(json.dumps(
        {"at": "2026-01-01T03:30:00Z", "ok": True, "to": "/mnt/nas", "left_out": []}))
    later = time.mktime((2026, 1, 4, 3, 30, 0, 0, 0, 0))
    [item] = firstrun.backup_items(now=later)
    assert item.state == "warn" and "over two days ago" in item.detail


def test_a_half_made_ship_answering_with_another_host_key_still_blocks_activate(
        computers, monkeypatch, vaults, tmp_path):
    """Only a half-made Ship that does not answer (or is still provisioning) is a mere
    warning: one answering with another key than its pin says something is wrong."""
    from fleetledger import vaultcmd

    a, b, world = computers
    vaults.create("correct horse battery staple")
    vaultcmd.move_in(a.app, a.store)
    other = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    with a.store.edit() as ed:
        ed.set_field("ship-02", "enrolled", None)
        ed.set_field("ship-02", "host_key", other)  # it answers with its real key instead
    assert a.run("backup", "now", "--same-disk", "--to", str(tmp_path / "saved"))[0] == 0
    [made] = list((tmp_path / "saved").iterdir())
    b.use(monkeypatch)
    b.app.ask_secret = lambda prompt: "correct horse battery staple"
    code, out, err = b.run("migrate", "import", str(made), "--store", "file")
    assert code == migrate.UNVERIFIED, out + err
    assert "FAIL  ship-02" in out and "not enrolled yet, not answering" not in out
    code, out, err = b.run("migrate", "activate", "--yes")
    assert code == 1 and "Not made active" in err


# --- toolkits ------------------------------------------------------------------------------


TOOLKIT = Path(__file__).resolve().parent / "fixtures" / "toolkits" / "examplekit"


def test_toolkit_folders_are_named_in_the_plan_and_never_copied(computers, monkeypatch):
    a, b, world = computers
    yours = a.home / "toolkits" / "examplekit"
    shutil.copytree(TOOLKIT, yours)
    inside = a.root / "config" / "toolkits" / "otherkit"  # kept in FleetLedger's own folder
    inside.mkdir(parents=True)
    (inside / "toolkit.toml").write_text('schema = 1\nid = "otherkit"\n')
    with a.store.edit() as ed:
        ed.set_toolkit("examplekit", {"path": "~/toolkits/examplekit"})
        ed.set_toolkit("otherkit", {"path": str(inside)})
    code, out, err = a.run("migrate", "check")
    assert code == 0, out + err
    assert ("Note: Toolkit examplekit is read from ~/toolkits/examplekit, a folder of yours "
            "that migrate does not copy: bring this folder to the new computer") in out
    assert "Toolkit otherkit is read from" in out
    stays = out.split("Stays here:")[1]
    line = next(x for x in stays.splitlines() if "otherkit/toolkit.toml" in x)
    assert "part of toolkit otherkit's folder, which migrate does not copy" in line
    stream = send_bytes(a, monkeypatch)
    assert b"placeholder release file" not in stream and b'id = "otherkit"' not in stream
    assert receive(b, monkeypatch, stream) == migrate.VERIFIED
    assert "Toolkit examplekit is read from ~/toolkits/examplekit" in b.out.getvalue()
    assert tomllib.loads(b.store.path.read_text())["toolkit"]["examplekit"] == {
        "path": "~/toolkits/examplekit"}
    assert not (b.root / "config" / "toolkits").exists()

    a.use(monkeypatch)
    gone, kept = handover.wipe_plan(a.store)  # a wipe leaves a toolkit folder of yours
    assert not [g for g in gone if "otherkit" in g.where]
    assert any("toolkit otherkit's folder: yours" in why for _, why in kept)
    (inside / "files").mkdir()  # an empty folder of the toolkit's (a git checkout has some)
    handover.wipe(a.store, gone)
    assert (inside / "toolkit.toml").exists() and (inside / "files").is_dir()
    assert not a.store.path.exists()
