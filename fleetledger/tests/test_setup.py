"""ship-fleet setup: a guided first run that skips what is done and can be run again.

Nothing here reaches a real Proxmox, keyring, ssh-agent or terminal: the Proxmox
API, the certificate fetch, ssh-add and the keyring are fakes, and every answer
is scripted.
"""

from __future__ import annotations

import io
import stat
import subprocess

import pytest
from fleetledger.drivers.proxmox_api import ProxmoxError
from fleetledger.overlay import secret_store
from fleetledger.store import TomlFileStore
from test_cli import env  # noqa: F401  (the fixture, for the first-Ship test)
from test_secret_store import fake_backend

from fleetledger import cli, proxmox_setup

TOKEN = "0a1b2c3d-0000-4000-8000-SETUPSECRET1"
TOKEN_ID = "shipshape@pve!build"
FPR = ":".join(["5E"] * 32)


class FakePVE:
    """The Proxmox API as a token with the runbook's grants sees it."""

    def __init__(self, *, storages=None, perms=None, templates=None, refuse=False, pools=None):
        self.storages = storages if storages is not None else [
            {"storage": "local", "content": "iso,vztmpl,backup", "enabled": 1, "active": 1},
            {"storage": "local-lvm", "content": "images,rootdir", "enabled": 1, "active": 1},
            {"storage": "shipshape-seeds", "content": "iso", "enabled": 1, "active": 1},
            {"storage": "shipshape-images", "content": "import", "enabled": 1, "active": 1},
        ]
        vm = dict.fromkeys(proxmox_setup.ROLES["ShipShapeVM"], 1)
        self.perms = perms if perms is not None else {
            "/pool/shipshape": vm,
            "/storage/shipshape-seeds": dict.fromkeys(proxmox_setup.ROLES["ShipShapeSeeds"], 1),
            "/storage/local-lvm": dict.fromkeys(proxmox_setup.ROLES["ShipShapeDisks"], 1),
            "/sdn/zones/localnetwork/vmbr0": {"SDN.Use": 1},
        }
        self.templates = templates if templates is not None else [
            {"vmid": 9000, "name": "shipshape-base", "node": "pve", "template": 1},
            {"vmid": 101, "name": "some-vm", "node": "pve", "template": 0},
        ]
        self.pools = pools if pools is not None else [{"poolid": "shipshape"}]
        self.refuse = refuse
        self.refused: set[str] = set()  # secrets answered with 401
        self.version = "9.2.11"
        self.secrets_seen: list[str] = []
        self.configs: list[dict] = []
        self.paths: list[str] = []

    def factory(self, config, secret=None):
        """Like ProxmoxAPI: without a secret given, read it from where config says."""
        from fleetledger.drivers.proxmox_api import token_secret

        self.configs.append(dict(config))
        self.secret = secret or (lambda: token_secret(config))
        return self

    def get(self, path, **params):
        self.paths.append(path)
        self.secrets_seen.append(self.secret())
        if self.refuse or self.secret() in self.refused:
            raise ProxmoxError("Proxmox GET /version failed: 401 authentication failure",
                               status=401)
        routes = {
            "/version": {"version": self.version},
            "/nodes": [{"node": "pve", "status": "online"}],
            "/pools": self.pools,
            "/nodes/pve/storage": self.storages,
            "/access/permissions": self.perms,
            "/cluster/resources": self.templates,
        }
        if path.startswith("/nodes/pve/qemu/") and path.endswith("/config"):
            return {"name": "shipshape-base", "template": 1}
        if path not in routes:
            raise AssertionError(path)
        return routes[path]


@pytest.fixture
def fresh(home, fake_repo, monkeypatch, tmp_path):
    """A computer with nothing set up yet: a home with only your own SSH key."""
    user_home = tmp_path / "userhome"
    (user_home / ".ssh").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("HOME", str(user_home))
    from conftest import keygen

    keygen(user_home / ".ssh" / "id_ed25519", "you")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/nonexistent/agent.sock")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    store = TomlFileStore(home / "config" / "ships.toml")

    class Setup:
        pass

    s = Setup()
    s.home, s.user_home, s.store = home, user_home, store
    s.out, s.err = io.StringIO(), io.StringIO()
    s.answers: list[str] = []
    s.asked: list[str] = []
    s.tty: list[list[str]] = []  # commands run on the terminal
    s.agent = set()  # fingerprints ssh-agent holds
    s.pve = FakePVE()
    s.keyring = fake_backend()
    monkeypatch.setattr(secret_store, "get_keyring", lambda: s.keyring)

    s.prompts: list[tuple[str, str]] = []  # (question, everything printed before it)

    def confirm(prompt):
        s.asked.append(prompt)
        s.prompts.append((prompt, s.out.getvalue()))
        if not s.answers:
            raise AssertionError(f"unexpected question: {prompt}")
        return s.answers.pop(0)

    def run_cmd(argv, **kw):
        if argv[:2] == ["ssh-keygen", "-lf"]:
            return subprocess.CompletedProcess(argv, 0, f"256 SHA256:{argv[2]} x (ED25519)\n", "")
        if argv[:2] == ["ssh-add", "-l"]:
            lines = "".join(f"256 SHA256:{f} x (ED25519)\n" for f in sorted(s.agent))
            return subprocess.CompletedProcess(argv, 0 if lines else 1, lines, "")
        raise AssertionError(argv)

    def run_tty(argv):
        s.tty.append(list(argv))
        if argv[0] == "ssh-add":
            s.agent.update(a + ".pub" for a in argv[1:])
        return 0

    s.app = cli.App(out=s.out, err=s.err, store_factory=lambda: store,
                    repo_factory=lambda: fake_repo, confirm=confirm,
                    stdin_isatty=lambda: True)
    s.app.run_cmd, s.app.run_tty = run_cmd, run_tty
    s.app.ask_secret = lambda prompt: s.asked.append(prompt) or TOKEN
    s.app.proxmox_api = s.pve.factory
    s.app.cert_fingerprint = lambda host, port: FPR

    def run(*argv, answers=()):
        s.answers[:] = list(answers)
        s.out.seek(0), s.out.truncate(), s.err.seek(0), s.err.truncate()
        code = cli.main(["setup", "--text", "--no-passphrase", *argv], s.app)
        return code, s.out.getvalue(), s.err.getvalue()

    s.run = run
    return s


def everything(s) -> str:
    text = s.store.path.read_text() if s.store.exists() else ""
    files = [p.read_bytes().decode("utf-8", "replace") for p in s.home.rglob("*") if p.is_file()]
    return text + s.out.getvalue() + s.err.getvalue() + "".join(files)


# Answers, in order, for a fresh computer that goes manual and skips the options.
MANUAL = ["",        # your key: the default
          "yes",     # load the admin key into ssh-agent
          "yes",     # set up the CA
          "no",      # CA keys into the agent
          "manual",  # hypervisor
          "",        # overlay: skip (the default)
          "skip"]    # first Ship


def test_setup_needs_a_terminal(fresh):
    fresh.app.stdin_isatty = lambda: False
    code, out, err = fresh.run()
    assert code != 0 and "terminal" in err and "ship-fleet doctor" in err
    assert not fresh.store.exists()


def test_a_fresh_computer_goes_from_nothing_to_set_up(fresh):
    code, out, err = fresh.run(answers=MANUAL)
    assert code == 0, err
    inv = fresh.store.load()
    assert inv.agent_key_paths == [fresh.user_home / ".ssh" / "id_ed25519.pub"]
    admin = fresh.user_home / ".ssh" / "shipshape_fleet_ed25519"
    assert inv.admin_key == admin and stat.S_IMODE(admin.stat().st_mode) == 0o600
    assert ["ssh-add", str(admin)] in fresh.tty  # loaded on the terminal, asked once
    assert set(inv.doc["ca"]) == {"host_key", "user_key"}
    assert inv.defaults["driver"] == "manual"
    assert "== 7/7" in out and "Left for later:" in out
    assert "ship-fleet overlay setup" in out and "ship-fleet new ship-01" in out
    assert fresh.answers == []  # every scripted answer was used, no more questions


def test_running_it_again_changes_nothing_and_asks_little(fresh):
    assert fresh.run(answers=MANUAL)[0] == 0
    before = fresh.store.path.read_bytes()
    fresh.tty.clear()
    code, out, err = fresh.run(answers=["", "skip"])  # overlay, Ship: nothing else asked
    assert code == 0, err
    assert fresh.store.path.read_bytes() == before
    assert fresh.tty == []  # no key made, nothing loaded again
    assert fresh.answers == [] and "Manual: FleetLedger prints the steps" in out
    for done in ("Already set up:", "ssh-agent already holds", "Already set up (host and user"):
        assert done in out


def test_without_an_agent_it_carries_on_and_says_how(fresh, monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK")
    code, out, err = fresh.run(answers=["", "yes", "manual", "", ])
    assert code == 0, err
    assert 'eval "$(ssh-agent)" && ssh-add' in out
    assert "needs the admin key in ssh-agent" in out  # no Ship offered
    assert not any(c[0] == "ssh-add" for c in fresh.tty)


def test_it_stops_on_a_broken_computer(fresh, monkeypatch):
    from fleetledger.errors import FleetError

    def no_repo():
        raise FleetError("Cannot find the ShipShape checkout.")

    fresh.app.repo_factory = no_repo
    code, out, err = fresh.run()
    assert code != 0 and "FAIL" in out and "Fix what is marked FAIL" in err
    assert not fresh.store.exists()


# --- Proxmox ------------------------------------------------------------------------------


def to_proxmox(s, *answers):
    """Run setup up to the hypervisor on a fresh computer, then these answers."""
    return s.run(answers=["", "yes", "no",  # your key, ssh-add, no CA
                          "proxmox", *answers])


START = ["pve.example.com",  # address
         "yes",              # the fingerprint matches
         TOKEN_ID]           # token id (the secret comes from ask_secret)
FOUND = ["", "", "", "", "", "", ""]  # node, pool, disk, seed, image, bridge, template:
#                                      each found through the API, Enter confirms it
PROXMOX_OK = [*START,
              "",                # store: keyring (the default)
              *FOUND,
              "",                # VM ids: any
              "", "skip"]        # overlay, Ship


def test_proxmox_from_three_answers(fresh):
    code, out, err = to_proxmox(fresh, *PROXMOX_OK)
    assert code == 0, err
    assert fresh.answers == []
    inv = fresh.store.load()
    assert inv.defaults["driver"] == "proxmox"
    assert dict(inv.driver_config("proxmox")) == {
        "api_url": "https://pve.example.com:8006/api2/json", "node": "pve",
        "pool": "shipshape", "template_vmid": 9000, "seed_storage": "shipshape-seeds",
        "disk_storage": "local-lvm", "bridge": "vmbr0", "image_storage": "shipshape-images",
        "token_id": TOKEN_ID, "token_store": "keyring",
        "keyring_item": "proxmox-token:" + TOKEN_ID, "tls_fingerprint": FPR}
    assert fresh.keyring.items == {("fleetledger", "proxmox-token:" + TOKEN_ID): TOKEN}
    assert set(fresh.pve.secrets_seen) == {TOKEN}
    assert fresh.pve.configs[0]["tls_fingerprint"] == FPR  # the API went through the pin
    assert TOKEN not in everything(fresh)
    assert all(TOKEN not in " ".join(c) for c in fresh.tty)  # never in argv
    for what in ("Node", "Pool for Ships", "Storage for seed ISOs", "Template VM"):
        assert any(q.startswith(what) for q in fresh.asked), what  # confirmed, even alone


def test_the_fingerprint_is_shown_before_it_is_confirmed(fresh):
    assert to_proxmox(fresh, *PROXMOX_OK)[0] == 0
    prompt, shown = next((q, o) for q, o in fresh.prompts if q.startswith("Type yes"))
    assert FPR in shown and "openssl x509" in shown


def test_proxmox_secret_in_a_file_when_chosen(fresh):
    code, out, err = to_proxmox(fresh, *START, "file", *FOUND, "", "", "skip")
    assert code == 0, err
    config = fresh.store.load().driver_config("proxmox")
    assert config["token_file"].endswith("proxmox-token") and "token_store" not in config
    token = fresh.home / "config" / "proxmox-token"
    assert token.read_text() == TOKEN + "\n" and stat.S_IMODE(token.stat().st_mode) == 0o600
    assert fresh.keyring.sets == []
    assert TOKEN not in fresh.store.path.read_text() + out + err


def test_without_a_usable_keyring_the_file_is_used_without_asking(fresh, monkeypatch):
    monkeypatch.setattr(secret_store, "get_keyring", lambda: fake_backend(locked=True))
    code, out, err = to_proxmox(fresh, *START, *FOUND, "", "", "skip")  # no store question
    assert code == 0, err
    assert "cannot be used here" in out
    assert "token_file" in fresh.store.load().driver_config("proxmox")


def test_a_fingerprint_not_confirmed_sends_nothing(fresh):
    code, out, err = to_proxmox(fresh, "pve.example.com", "no")
    assert code != 0 and "Not pinned" in err
    assert fresh.pve.paths == [] and fresh.keyring.sets == []
    assert fresh.store.load().driver_config("proxmox") is None


@pytest.mark.parametrize("store", ["", "file"])
def test_a_refused_token_saves_nothing(fresh, store):
    fresh.pve.refuse = True
    code, out, err = to_proxmox(fresh, *START, store)
    assert code != 0 and "refused the token" in err and "Nothing was saved" in err
    assert fresh.keyring.sets == [] and fresh.store.load().driver_config("proxmox") is None
    assert not (fresh.home / "config" / "proxmox-token").exists()
    assert TOKEN not in out + err


@pytest.mark.parametrize("typed", ["has space", "two\nlines", ""])
def test_an_implausible_token_secret_is_refused(fresh, typed):
    fresh.app.ask_secret = lambda prompt: typed
    code, out, err = to_proxmox(fresh, *START)
    assert code != 0 and "does not look like" in err
    assert fresh.pve.paths == []


def test_a_keyring_that_fails_to_store_points_at_redo(fresh, monkeypatch):
    backend = fake_backend()

    def refuse(service, item, value):
        raise RuntimeError("no")

    backend.set_password = refuse
    monkeypatch.setattr(secret_store, "get_keyring", lambda: backend)
    code, out, err = to_proxmox(fresh, *START, "")
    assert code != 0 and "ship-fleet setup --redo hypervisor" in err
    assert "overlay" not in err


def test_no_token_yet_prints_the_root_commands(fresh):
    code, out, err = to_proxmox(fresh, "pve.example.com", "yes", "")
    assert code == 1
    assert "pveum user token add shipshape@pve build --privsep 1" in out
    assert "pveum acl modify /pool/shipshape --tokens 'shipshape@pve!build' --roles ShipShapeVM" \
        in out
    assert "DISK_STORAGE=CHANGE-ME" in out and "run ship-fleet setup again" in out
    assert fresh.pve.paths == []


def test_missing_pieces_print_exact_commands_and_keep_the_token(fresh):
    fresh.pve.storages = [s for s in fresh.pve.storages if s["storage"] != "shipshape-seeds"]
    del fresh.pve.perms["/storage/shipshape-seeds"]
    code, out, err = to_proxmox(fresh, *START, "", "", "", "", "", "")  # store, node, pool,
    assert fresh.answers == []                                          # disk, image, bridge
    assert code == 1, err
    assert "a storage only for seed ISOs" in out
    assert "pvesm add dir shipshape-seeds --path /var/lib/shipshape-seeds --content iso" in out
    assert ("pveum acl modify /storage/shipshape-seeds --tokens 'shipshape@pve!build' "
            "--roles ShipShapeSeeds") in out
    assert fresh.store.load().driver_config("proxmox") is None  # nothing half-saved
    assert "driver" not in fresh.store.load().defaults  # still not chosen: asked next time
    assert fresh.keyring.items  # the checked token is kept for the next run

    # Next run: the keyring item is offered (and checked), never typed again.
    fresh.pve = FakePVE()
    fresh.app.proxmox_api = fresh.pve.factory
    fresh.asked.clear()
    code, out, err = fresh.run(answers=["no", "proxmox",
                                        "", "",  # address and token id: from the last run
                                        "yes",   # use the secret in the keyring
                                        *FOUND, "", "", "skip"])
    assert code == 0, err
    assert not any("Token secret" in q for q in fresh.asked)
    assert not any("Type yes" in q for q in fresh.asked)  # the pin, confirmed last run
    assert "the one you confirmed in your last setup run" in out
    assert not (fresh.home / "config" / "proxmox-setup-progress.json").exists()
    assert fresh.pve.secrets_seen and set(fresh.pve.secrets_seen) == {TOKEN}
    assert fresh.store.load().driver_config("proxmox")["seed_storage"] == "shipshape-seeds"


def test_missing_rights_print_the_grants(fresh):
    fresh.pve.perms["/pool/shipshape"] = {"VM.Audit": 1, "Pool.Audit": 1}
    code, out, err = to_proxmox(fresh, *START, "", *FOUND[:6])
    assert fresh.answers == []
    assert code == 1, err
    assert "rights on /pool/shipshape" in out and "VM.Clone" in out
    assert "pveum role add ShipShapeVM --privs" in out
    assert "pveum acl modify /pool/shipshape --users shipshape@pve --roles ShipShapeVM" in out


def test_proxmox_8_reads_the_guest_agent_with_vm_monitor(fresh):
    fresh.pve.version = "8.4.1"
    vm = fresh.pve.perms["/pool/shipshape"]
    del vm["VM.GuestAgent.Audit"]
    vm["VM.Monitor"] = 1
    code, out, err = to_proxmox(fresh, *PROXMOX_OK)
    assert code == 0, err  # nothing reported missing
    fresh.pve.perms["/pool/shipshape"] = {"VM.Audit": 1}
    code, out, err = fresh.run("--redo", "hypervisor", answers=[
        "no", "proxmox", "", "", "yes", *FOUND[:6]])
    assert code == 1 and "VM.Monitor" in out and "VM.GuestAgent.Audit" not in out


def test_no_template_prints_the_pinned_image_commands(fresh, fake_repo):
    fresh.pve.templates = []
    code, out, err = to_proxmox(fresh, *START, "", *FOUND[:6])
    assert code == 1, err
    image = fake_repo.versions["ubuntu"]["images"]["amd64"]
    assert f'echo "{image["sha256"]}  $IMG" | sha256sum -c' in out
    assert 'bridge=vmbr0' in out and '"local-lvm:0,import-from=$PWD/$IMG"' in out
    assert "DISK_STORAGE=CHANGE-ME" not in out  # the names are known by now


def test_only_usable_things_are_offered(fresh):
    """Templates on other nodes and containers, disabled or inactive storages, and
    bridges the token may not use are never offered."""
    fresh.pve.templates = [
        {"vmid": 9000, "name": "shipshape-base", "node": "pve", "template": 1, "type": "qemu"},
        {"vmid": 9001, "name": "elsewhere", "node": "pve2", "template": 1, "type": "qemu"},
        {"vmid": 9002, "name": "a-container", "node": "pve", "template": 1, "type": "lxc"}]
    fresh.pve.storages += [
        {"storage": "off-disk", "content": "images", "enabled": 0, "active": 1},
        {"storage": "down-seeds", "content": "iso", "enabled": 1, "active": 0}]
    fresh.pve.perms["/sdn/zones/localnetwork/vmbr9"] = {"SDN.Audit": 1}
    code, out, err = to_proxmox(fresh, *PROXMOX_OK)
    assert code == 0, err
    for name in ("9001", "9002", "off-disk", "down-seeds", "vmbr9"):
        assert name not in out


def test_a_bad_vmid_range_is_asked_again(fresh):
    code, out, err = to_proxmox(fresh, *START, "", *FOUND, "9200", "50-60", "9200-9249",
                                "", "skip")
    assert code == 0, err
    assert fresh.store.load().driver_config("proxmox")["vmid_range"] == [9200, 9249]
    assert err.count("Give two numbers") == 2


def test_the_snapshot_rights_are_mentioned_only_when_missing(fresh):
    assert "VM.Snapshot.Rollback" in to_proxmox(fresh, *PROXMOX_OK)[1]
    fresh.pve.perms["/pool/shipshape"].update({"VM.Snapshot": 1, "VM.Snapshot.Rollback": 1})
    code, out, err = fresh.run("--redo", "hypervisor", answers=[
        "no", "proxmox", "", "", "yes", *FOUND, "", "", "skip"])
    assert code == 0, err
    assert "VM.Snapshot.Rollback" not in out


def test_redo_hypervisor_keeps_the_pin_when_it_is_unchanged(fresh):
    assert to_proxmox(fresh, *PROXMOX_OK)[0] == 0
    fresh.asked.clear()
    code, out, err = fresh.run("--redo", "hypervisor", answers=[
        "no",           # CA, still not wanted
        "proxmox", "",  # same address (the default)
        "",             # same token id
        "yes",          # reuse the keyring secret
        *FOUND, "", "", "skip"])
    assert code == 0, err
    assert "the one you confirmed before" in out
    assert not any("Type yes" in q for q in fresh.asked)


def test_a_changed_certificate_is_said_plainly_and_must_be_confirmed(fresh):
    assert to_proxmox(fresh, *PROXMOX_OK)[0] == 0
    before = fresh.store.path.read_bytes()
    fresh.app.cert_fingerprint = lambda host, port: ":".join(["66"] * 32)
    fresh.pve.paths.clear()
    code, out, err = fresh.run("--redo", "hypervisor", answers=["no", "proxmox", "", ""])
    assert code != 0 and "DIFFERENT from the one you confirmed" in out
    assert FPR in out and ":".join(["66"] * 32) in out  # the old one and the new one
    assert "intercepting" in out and "default no" in fresh.asked[-1]
    assert fresh.store.path.read_bytes() == before and fresh.pve.paths == []


def test_redo_reuses_the_token_file_named_in_ships_toml(fresh, tmp_path):
    assert to_proxmox(fresh, *START, "file", *FOUND, "", "", "skip")[0] == 0
    moved = tmp_path / "elsewhere" / "token"
    moved.parent.mkdir(mode=0o700)
    (fresh.home / "config" / "proxmox-token").rename(moved)
    with fresh.store.edit() as ed:
        ed.doc["drivers"]["proxmox"]["token_file"] = str(moved)
    fresh.asked.clear()
    code, out, err = fresh.run("--redo", "hypervisor", answers=[
        "no", "proxmox", "", "", "yes", *FOUND, "", "", "skip"])
    assert code == 0, err
    assert not any("Token secret" in q for q in fresh.asked)
    assert fresh.store.load().driver_config("proxmox")["token_file"] == str(moved)


def test_no_ship_is_offered_while_proxmox_is_failing(fresh):
    assert to_proxmox(fresh, *PROXMOX_OK)[0] == 0
    fresh.pve.refuse = True
    code, out, err = fresh.run(answers=["no", ""])  # CA, overlay; no Ship question
    assert code == 0, err
    assert fresh.answers == []
    assert "needs a working hypervisor" in out


def test_a_bad_ship_name_is_asked_again(fresh):
    code, out, err = fresh.run(answers=[*MANUAL[:-1], "-rf", "skip"])
    assert code == 0, err
    assert fresh.answers == []
    assert "A Ship name is" in err


def test_server_fingerprint_connects_to_a_bare_ipv6_address(monkeypatch):
    import socket

    from fleetledger.drivers import proxmox_api

    seen = []

    def connect(address, timeout=None):
        seen.append(address)
        raise OSError("refused")

    monkeypatch.setattr(socket, "create_connection", connect)
    with pytest.raises(ProxmoxError):
        proxmox_api.server_fingerprint("[2001:db8::1]", 8006)
    assert seen == [("2001:db8::1", 8006)]


def test_server_fingerprint_reads_a_real_certificate(tmp_path):
    import shutil
    import socket
    import ssl
    import threading

    from fleetledger.drivers import proxmox_api

    if not shutil.which("openssl"):
        pytest.skip("no openssl")
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                    "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", str(key), "-out",
                    str(cert), "-days", "1", "-subj", "/CN=pve.example.com"],
                   check=True, capture_output=True)
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        try:
            with context.wrap_socket(conn, server_side=True) as tls:
                tls.recv(1)
        except (OSError, ssl.SSLError):
            pass

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        assert proxmox_api.server_fingerprint("127.0.0.1", port) == \
            proxmox_api.cert_fingerprint(der)
    finally:
        server.close()


@pytest.mark.parametrize("text, expected", [
    ("pve.example.com", ("pve.example.com", 8006)),
    ("pve.example.com:8443", ("pve.example.com", 8443)),
    ("https://pve.example.com:8006/api2/json", ("pve.example.com", 8006)),
    ("https://pve.example.com:8006/#v1:0:18", ("pve.example.com", 8006)),
    ("192.0.2.10", ("192.0.2.10", 8006)),
    ("[2001:db8::1]:8006", ("[2001:db8::1]", 8006)),
])
def test_addresses(text, expected):
    host, port, url = proxmox_setup.parse_address(text)
    assert (host, port) == expected and url == f"https://{host}:{port}/api2/json"


@pytest.mark.parametrize("text", ["", "pve example", "pve.example.com:99999", "a;b"])
def test_bad_addresses(text):
    from fleetledger.errors import FleetError

    with pytest.raises(FleetError):
        proxmox_setup.parse_address(text)


def test_permissions_propagate_from_parents():
    perms = {"/": {"Datastore.Audit": 1}, "/storage": {"Datastore.AllocateSpace": 1}}
    assert proxmox_setup.effective(perms, "/storage/local-lvm") == {
        "Datastore.Audit", "Datastore.AllocateSpace"}


def test_a_privilege_that_does_not_propagate_still_counts_where_it_is_granted():
    """/access/permissions maps each privilege to its propagate flag: 0 means granted
    here, just not below."""
    perms = {"/storage": {"Datastore.Audit": 0},
             "/sdn/zones/localnetwork/vmbr1": {"SDN.Use": 0}}
    assert proxmox_setup.effective(perms, "/storage") == {"Datastore.Audit"}
    assert proxmox_setup.effective(perms, "/storage/local-lvm") == set()
    assert proxmox_setup.bridges_from(perms) == ["vmbr1"]


def test_doctor_reads_a_keyring_token(fresh):
    assert to_proxmox(fresh, *PROXMOX_OK)[0] == 0
    fresh.out.seek(0), fresh.out.truncate()
    fresh.pve.secrets_seen.clear()
    cli.main(["doctor"], fresh.app)
    out = fresh.out.getvalue()
    assert "readable in the keyring (item proxmox-token:shipshape@pve!build)" in out
    assert fresh.pve.secrets_seen and set(fresh.pve.secrets_seen) == {TOKEN}  # from the keyring
    assert TOKEN not in out


def test_the_proxmox_keyring_names_are_stable():
    assert secret_store.proxmox_item(TOKEN_ID) == "proxmox-token:shipshape@pve!build"


# --- the first Ship -------------------------------------------------------------------------


def test_the_first_ship_is_offered_once_the_rest_is_done(env, monkeypatch):  # noqa: F811
    """With ssh-agent holding the admin key, setup offers a Ship and makes it."""
    with env.store.edit() as ed:
        for ship in env.store.load().ships():
            ed.remove_ship(ship.name)
    admin_pub = env.store.load().admin_public_key_path
    monkeypatch.setenv("SSH_AUTH_SOCK", "/nonexistent/agent.sock")

    def run_cmd(argv, **kw):
        line = "256 SHA256:admin x (ED25519)\n"
        if argv[:2] in (["ssh-keygen", "-lf"], ["ssh-add", "-l"]):
            return subprocess.CompletedProcess(argv, 0, line, "")
        raise AssertionError(argv)

    answers = ["no",       # CA
               "manual",   # hypervisor
               "",         # overlay: skip
               "ship-05",  # first Ship
               "work"]     # its tag
    env.app.stdin_isatty = lambda: True
    env.app.confirm = lambda prompt: answers.pop(0)
    env.app.run_cmd = run_cmd
    env.app.run_tty = lambda argv: pytest.fail(f"ran {argv}")
    code, out, err = env.run("setup", "--text")
    assert code == 0, err
    assert answers == []
    ship = env.store.load().ship("ship-05")
    assert ship.tags == ("work",) and admin_pub.exists()
    assert "ship-fleet status ship-05 --wait, then ship-fleet login ship-05 all" in out


def test_a_working_proxmox_is_left_alone(fresh):
    assert to_proxmox(fresh, *PROXMOX_OK)[0] == 0
    fresh.asked.clear()
    before = fresh.store.path.read_bytes()
    code, out, err = fresh.run(answers=["no", "", "skip"])  # CA, overlay, Ship
    assert code == 0, err
    assert "Proxmox, already set up and working" in out
    assert fresh.store.path.read_bytes() == before and fresh.answers == []


def test_a_keyring_token_never_falls_back_to_a_file(monkeypatch, tmp_path):
    """A locked keyring is an error that says how to unlock it or switch, even with
    a readable token file named next to it."""
    from fleetledger.drivers.proxmox_api import token_secret
    from fleetledger.errors import ConfigError

    stray = tmp_path / "proxmox-token"
    stray.write_text("other-secret\n")
    stray.chmod(0o600)
    monkeypatch.setattr(secret_store, "get_keyring", lambda: fake_backend(locked=True))
    config = {"token_store": "keyring", "keyring_item": "proxmox-token:" + TOKEN_ID,
              "token_file": str(stray)}
    with pytest.raises(ConfigError) as exc:
        token_secret(config)
    assert "locked" in exc.value.message and "Unlock it" in exc.value.hint
    assert "ship-fleet setup --redo hypervisor" in exc.value.hint
    assert "other-secret" not in str(exc.value) + exc.value.hint


def test_a_missing_keyring_token_says_so(monkeypatch):
    from fleetledger.drivers.proxmox_api import token_secret
    from fleetledger.errors import ConfigError

    monkeypatch.setattr(secret_store, "get_keyring", lambda: fake_backend())
    with pytest.raises(ConfigError, match="not in the keyring"):
        token_secret({"token_store": "keyring", "keyring_item": "proxmox-token:x@pve!y"})


def test_the_api_reads_a_keyring_token_for_each_request(monkeypatch):
    from fleetledger.drivers.proxmox_api import ProxmoxAPI

    backend = fake_backend()
    backend.items[("fleetledger", "proxmox-token:" + TOKEN_ID)] = TOKEN
    monkeypatch.setattr(secret_store, "get_keyring", lambda: backend)
    sent = []

    class Conn:
        def __init__(self, *a):
            pass

        def request(self, method, url, body=None, headers=None):
            sent.append(headers["Authorization"])

        def getresponse(self):
            class R:
                status, reason = 200, "OK"

                def read(self):
                    return b'{"data": {"version": "9.2.11"}}'
            return R()

        def close(self):
            pass

    api = ProxmoxAPI({"api_url": "https://pve.example.com:8006/api2/json", "token_id": TOKEN_ID,
                      "tls_fingerprint": FPR, "token_store": "keyring",
                      "keyring_item": "proxmox-token:" + TOKEN_ID}, connection_factory=Conn)
    assert api.get("/version") == {"version": "9.2.11"}
    assert sent == [f"PVEAPIToken={TOKEN_ID}={TOKEN}"]
    assert TOKEN not in repr(api)


def test_a_changed_certificate_between_partial_runs_is_flagged(fresh):
    fresh.pve.templates = []
    assert to_proxmox(fresh, *START, "", *FOUND[:6])[0] == 1  # partial: no template
    fresh.app.cert_fingerprint = lambda host, port: ":".join(["77"] * 32)
    code, out, err = fresh.run(answers=["no", "proxmox", "", ""])
    assert code != 0 and "DIFFERENT from the one you confirmed in your last setup run" in out


def test_a_rotated_token_asks_for_the_new_secret_instead_of_stopping(fresh):
    assert to_proxmox(fresh, *PROXMOX_OK)[0] == 0
    fresh.pve.refused = {TOKEN}  # the old token was replaced on the host
    new = "0a1b2c3d-0000-4000-8000-NEWSECRET002"
    fresh.app.ask_secret = lambda prompt: fresh.asked.append(prompt) or new
    code, out, err = fresh.run("--redo", "hypervisor", answers=[
        "no", "proxmox", "", "",
        "",   # reuse the stored secret: the default, yes (refused: then asked)
        "",   # store: keyring
        *FOUND, "", "", "skip"])
    assert code == 0, err
    assert "refused the stored secret" in out
    assert fresh.keyring.items[("fleetledger", "proxmox-token:" + TOKEN_ID)] == new
    assert new not in out + err


@pytest.mark.parametrize("remove", ["yes", "no"])
def test_a_store_switch_on_redo_offers_to_remove_the_old_copy(fresh, remove):
    assert to_proxmox(fresh, *START, "file", *FOUND, "", "", "skip")[0] == 0
    old_file = fresh.home / "config" / "proxmox-token"
    code, out, err = fresh.run("--redo", "hypervisor", answers=[
        "no", "proxmox", "", "",
        "no",       # do not reuse the file's secret: type it again
        "keyring",  # and keep it in the keyring now
        *FOUND, "", remove, "", "skip"])
    assert code == 0, err
    config = fresh.store.load().driver_config("proxmox")
    assert config["token_store"] == "keyring" and "token_file" not in config  # nothing stale
    assert old_file.exists() is (remove == "no")


def test_a_bridge_granted_on_a_parent_path_is_typed(fresh):
    del fresh.pve.perms["/sdn/zones/localnetwork/vmbr0"]
    fresh.pve.perms["/sdn"] = {"SDN.Use": 1}
    code, out, err = to_proxmox(fresh, *START, "", *FOUND[:5], "bad bridge!", "vmbr1",
                                FOUND[6], "", "", "skip")
    assert code == 0, err
    assert fresh.store.load().driver_config("proxmox")["bridge"] == "vmbr1"
    assert "any bridge" in out


def test_a_template_outside_the_pool_can_be_added_to_it(fresh):
    fresh.pve.templates = []
    code, out, err = to_proxmox(fresh, *START, "", *FOUND[:6])
    assert code == 1
    assert "pveum pool modify shipshape --vms 9000" in out


def test_missing_rights_on_a_storage_are_reported(fresh):
    fresh.pve.perms["/storage/local-lvm"] = {"Datastore.Audit": 1}
    code, out, err = to_proxmox(fresh, *START, "", *FOUND[:6])
    assert code == 1
    assert "rights on /storage/local-lvm (Datastore.AllocateSpace)" in out
    assert "pveum acl modify /storage/local-lvm --tokens 'shipshape@pve!build' " \
        "--roles ShipShapeDisks" in out


def test_an_ssh_add_that_did_not_load_the_key_is_said(fresh):
    fresh.app.run_tty = lambda argv: fresh.tty.append(list(argv)) or 0  # loads nothing
    code, out, err = fresh.run(answers=MANUAL[:2] + ["no", "manual", "", ])
    assert code == 0, err
    assert "still not in ssh-agent" in err and "Loaded." not in out


def test_moving_the_secret_out_of_the_keyring_offers_to_remove_the_item(fresh):
    assert to_proxmox(fresh, *PROXMOX_OK)[0] == 0
    code, out, err = fresh.run("--redo", "hypervisor", answers=[
        "no", "proxmox", "", "", "no", "file", *FOUND, "", "yes", "", "skip"])
    assert code == 0, err
    assert fresh.keyring.items == {}
    assert "token_file" in fresh.store.load().driver_config("proxmox")
