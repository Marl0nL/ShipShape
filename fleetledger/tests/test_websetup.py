"""The first-run setup in the browser (/setup), through the real gate.

Keys are made with the real OpenSSH tools in a scratch home; the keyring, the
Proxmox API, the certificate fetch and Tailscale are fakes. Nothing here touches
your own ~/.ssh, ~/.config/fleetledger, keyring or agent.
"""

from __future__ import annotations

import io
import re
import stat
import subprocess

import pytest
from fleetledger.overlay import secret_store
from fleetledger.store import TomlFileStore
from fleetledger.web import app as webapp
from fleetledger.web.fleet import Fleet
from fleetledger.web.flows import Concierge
from fleetledger.web.security import Gate
from starlette.testclient import TestClient
from test_cli import LEVEL_SCOPES, OAUTH_SECRET, FakeTailscale
from test_secret_store import fake_backend
from test_setup import FPR, TOKEN, TOKEN_ID, FakePVE

from fleetledger import cli, keys

PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = {"Origin": BASE}


class Tailnet(FakeTailscale):
    """The CLI tests' fake Tailscale, plus the test key setup makes and deletes."""

    def __init__(self, *a, tag_ok=True, **kw):
        super().__init__(*a, **kw)
        self.tag_ok, self.tried = tag_ok, []

    def try_key(self, tags):
        from fleetledger.errors import FleetError

        self.tried.append(list(tags))
        if not self.tag_ok:
            raise FleetError("Tailscale API: making a test key failed: 400 requested tags "
                             "[tag:ship] are invalid or not permitted")


@pytest.fixture
def ws(home, fake_repo, monkeypatch, tmp_path):
    """A computer with nothing set up: a scratch home holding only your own key."""
    user_home = tmp_path / "userhome"
    (user_home / ".ssh").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "you", "-f",
                    str(user_home / ".ssh" / "id_ed25519")], check=True)
    ring = fake_backend()
    monkeypatch.setattr(secret_store, "get_keyring", lambda: ring)
    store = TomlFileStore(home / "config" / "ships.toml")
    pve = FakePVE()
    tailnet = Tailnet(scopes=LEVEL_SCOPES["keys"])
    app = cli.App(out=io.StringIO(), err=io.StringIO(), store_factory=lambda: store,
                  repo_factory=lambda: fake_repo, stdin_isatty=lambda: False,
                  sleep=lambda s: None, ssh_factory=lambda inv: None, herdr=False)
    app.proxmox_api = pve.factory
    app.cert_fingerprint = lambda host, port: FPR
    app.overlay_api = tailnet
    gate = Gate(PORT)
    fleet = Fleet(app, interval=30)
    asgi = webapp.create_app(app, gate, fleet=fleet, concierge_=Concierge(app),
                             start_page="/setup")

    class Env:
        pass

    env = Env()
    env.home, env.user_home, env.store, env.ring, env.pve, env.tailnet = (
        home, user_home, store, ring, pve, tailnet)
    env.app, env.gate = app, gate
    env.client = TestClient(asgi, base_url=BASE, follow_redirects=False)
    env.pages = []  # every response body, to check no secret ever comes back

    def sign_in():
        assert env.client.get(f"/login?token={gate.new_token()}").status_code == 200
        env.csrf = re.search(r'name="csrf-token" content="([^"]+)"', get("/setup").text).group(1)

    def get(path):
        response = env.client.get(path)
        env.pages.append(response.text)
        return response

    def post(path, data=None):
        response = env.client.post(path, data={"csrf": env.csrf, **(data or {})},
                                   headers=ORIGIN)
        env.pages.append(response.text)
        return response

    env.sign_in, env.get, env.post = sign_in, get, post
    env.sign_in()
    return env


def page(env) -> str:
    return env.get("/setup").text


# --- getting there ------------------------------------------------------------------------


def test_a_computer_that_is_not_set_up_lands_on_setup(ws):
    response = ws.get("/")
    assert response.status_code == 303 and response.headers["location"] == "/setup"
    text = page(ws)
    assert "New fleet" in text and "recommended" in text
    assert "Connect to an existing fleet or keys" in text
    assert "~/.ssh/id_ed25519.pub" in text  # your key, found and proposed


def test_every_setup_change_needs_the_csrf_token(ws):
    response = ws.client.post("/setup/fleet/new", data={"your_key": "~/.ssh/id_ed25519.pub"},
                              headers=ORIGIN)
    assert response.status_code == 403 and not ws.store.exists()


def test_setup_needs_a_session(ws):
    fresh = TestClient(ws.client.app, base_url=BASE, follow_redirects=False)
    assert fresh.get("/setup").status_code == 401


# --- a new fleet ------------------------------------------------------------------------------


def test_a_new_fleet_makes_and_looks_after_every_key(ws):
    response = ws.post("/setup/fleet/new", {"your_key": "~/.ssh/id_ed25519.pub"})
    assert response.status_code == 303
    inv = ws.store.load()
    ssh = ws.user_home / ".ssh"
    names = ("shipshape_fleet_ed25519", "shipshape_host_ca_ed25519", "shipshape_user_ca_ed25519")
    store_kind, managed = keys.managed(inv)
    assert store_kind == "keyring" and sorted(p.name for p in managed) == sorted(names)
    for name in names:
        assert keys.needs_passphrase(ssh / name)  # encrypted
        assert stat.S_IMODE((ssh / name).stat().st_mode) == 0o600
        assert ("fleetledger", keys.item(ssh / name)) in ws.ring.items
    assert inv.agent_key_paths == [ssh / "id_ed25519.pub"]
    assert "driver" not in inv.defaults  # chosen in the next step
    text = page(ws)
    assert "FleetLedger looks after its keys" in text
    passphrases = [v for (s, i), v in ws.ring.items.items() if i.startswith("key-passphrase:")]
    assert all(p not in "".join(ws.pages) for p in passphrases)


def test_a_new_fleet_can_make_your_key_too(ws):
    (ws.user_home / ".ssh" / "id_ed25519.pub").unlink()
    assert "a new key FleetLedger makes for me" in page(ws)
    ws.post("/setup/fleet/new", {"your_key": "new"})
    inv = ws.store.load()
    yours = ws.user_home / ".ssh" / "shipshape_ed25519"
    assert inv.agent_key_paths == [yours.with_name("shipshape_ed25519.pub")]
    assert yours in keys.managed(inv)[1]


def test_the_file_store_when_chosen(ws):
    ws.post("/setup/fleet/new", {"your_key": "~/.ssh/id_ed25519.pub", "store": "file"})
    assert keys.managed(ws.store.load())[0] == "file"
    assert ws.ring.sets == []
    secret = keys.passphrase_file(ws.user_home / ".ssh" / "shipshape_fleet_ed25519")
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600


def test_a_new_fleet_never_overwrites_keys_already_there(ws):
    admin = ws.user_home / ".ssh" / "shipshape_fleet_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(admin)], check=True)
    before = admin.read_bytes()
    ws.post("/setup/fleet/new", {"your_key": "~/.ssh/id_ed25519.pub"})
    assert not ws.store.exists() and admin.read_bytes() == before
    assert "Connect to an existing fleet or keys" in page(ws)  # and the flash says so


# --- bringing keys --------------------------------------------------------------------------


def _key(path, passphrase=""):
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(path)],
                   check=True)  # (a test fixture; this passphrase is not a secret)


def test_bringing_keys_keeps_their_passphrases_by_default(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    for name in ("admin", "host_ca", "user_ca"):
        _key(other / name, "their own passphrase")
    before = {n: (other / n).read_bytes() for n in ("admin", "host_ca", "user_ca")}
    ws.post("/setup/fleet/import", {"admin_key": str(other / "admin"),
                                    "host_ca_key": str(other / "host_ca"),
                                    "user_ca_key": str(other / "user_ca"),
                                    "your_key": "~/.ssh/id_ed25519.pub", "protect": "no"})
    inv = ws.store.load()
    assert inv.admin_key == other / "admin" and keys.managed(inv) == (None, [])
    assert {n: (other / n).read_bytes() for n in before} == before  # untouched
    text = page(ws)
    assert "You look after these keys' passphrases" in text  # said, and offered
    assert "Let FleetLedger look after them</button>" in text


def test_bringing_keys_can_hand_them_over(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    for name in ("admin", "host_ca", "user_ca"):
        _key(other / name, f"pass {name}")
    publics = {n: keys.public_line(other / n) for n in ("admin", "host_ca", "user_ca")}
    ws.post("/setup/fleet/import", {
        "admin_key": str(other / "admin"), "host_ca_key": str(other / "host_ca"),
        "user_ca_key": str(other / "user_ca"), "your_key": "~/.ssh/id_ed25519.pub",
        "protect": "yes", "admin_passphrase": "pass admin", "host_passphrase": "pass host_ca",
        "user_passphrase": "pass user_ca"})
    inv = ws.store.load()
    assert sorted(p.name for p in keys.managed(inv)[1]) == ["admin", "host_ca", "user_ca"]
    for name, public in publics.items():
        new = ws.ring.items[("fleetledger", keys.item(other / name))]
        assert keys.public_of(other / name, new) == public  # the same keys
    assert not any("pass admin" in p for p in ws.pages)


def test_a_wrong_passphrase_when_handing_over_changes_nothing(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _key(other / "admin", "right one")
    before = (other / "admin").read_bytes()
    ws.post("/setup/fleet/import", {"admin_key": str(other / "admin"),
                                    "your_key": "~/.ssh/id_ed25519.pub", "protect": "yes",
                                    "admin_passphrase": "wrong one"})
    assert not ws.store.exists() and (other / "admin").read_bytes() == before
    assert ws.ring.sets == []
    text = page(ws)
    assert "does not open admin" in text


def test_a_private_key_is_needed_not_a_public_one(ws, tmp_path):
    _key(tmp_path / "admin")
    ws.post("/setup/fleet/import", {"admin_key": str(tmp_path / "admin.pub"),
                                    "your_key": "~/.ssh/id_ed25519.pub"})
    assert not ws.store.exists() and "not a private key file" in page(ws)


# --- a computer part-way through (the text wizard, or an older setup) ---------------------------


@pytest.fixture
def partway(ws, tmp_path):
    """Keys with a passphrase the person chose, both CAs, and "manual" though they
    meant Proxmox."""
    ssh = ws.user_home / ".ssh"
    for name in ("shipshape_fleet_ed25519", "shipshape_host_ca_ed25519",
                 "shipshape_user_ca_ed25519"):
        _key(ssh / name, "chosen by them")
    from fleetledger.commands import make_fleet
    from fleetledger.trustcmd import make_ca

    make_fleet(ws.app, ws.store, "~/.ssh/shipshape_fleet_ed25519", ["~/.ssh/id_ed25519.pub"])
    make_ca(ws.app, ws.store, "~/.ssh/shipshape_host_ca_ed25519",
            "~/.ssh/shipshape_user_ca_ed25519")
    return ws


def test_finished_steps_show_as_done_and_manual_can_become_proxmox(partway):
    text = page(partway)
    assert re.search(r"Your fleet</a>\s*<span class=\"pill good\">done", text)
    assert re.search(r"Where Ships run</a>\s*<span class=\"pill good\">done", text)
    assert "Switch to Proxmox" in text
    assert "keeps a\n    passphrase you chose" in text or "ssh-add" in text  # loading is on them
    partway.post("/setup/proxmox/start")
    assert "Proxmox address" in page(partway)


def test_handing_over_existing_keys_is_opt_in_and_needs_the_box(partway):
    admin = partway.user_home / ".ssh" / "shipshape_fleet_ed25519"
    before = admin.read_bytes()
    partway.post("/setup/fleet/adopt", {"take_0": "yes", "passphrase_0": "chosen by them"})
    assert admin.read_bytes() == before  # no box ticked: nothing changed
    partway.post("/setup/fleet/adopt", {"take_0": "yes", "passphrase_0": "chosen by them",
                                        "confirm": "yes"})
    inv = partway.store.load()
    assert admin in keys.managed(inv)[1] and admin.read_bytes() != before
    assert len(keys.managed(inv)[1]) == 1  # only the one ticked
    new = partway.ring.items[("fleetledger", keys.item(admin))]
    assert keys.public_of(admin, new) == keys.public_line(admin)


# --- Proxmox ------------------------------------------------------------------------------


@pytest.fixture
def chosen(ws):
    """A new fleet, at the hypervisor step."""
    ws.post("/setup/fleet/new", {"your_key": "~/.ssh/id_ed25519.pub"})
    return ws


def token_stored(env) -> bool:
    return any(item.startswith("proxmox-token:") for _, item in env.ring.sets)


def to_choices(env, **extra):
    env.post("/setup/proxmox/address", {"address": "pve.example.com"})
    return env.post("/setup/proxmox/token", {"matches": "yes", "token_id": TOKEN_ID,
                                             "secret": TOKEN, **extra})


def test_proxmox_from_the_address_and_a_token(chosen):
    chosen.post("/setup/proxmox/address", {"address": "pve.example.com"})
    text = page(chosen)
    assert FPR in text and "openssl x509 -in /etc/pve/local/pve-ssl.pem" in text
    assert chosen.pve.paths == []  # nothing sent before the pin is confirmed
    chosen.post("/setup/proxmox/token", {"matches": "yes", "token_id": TOKEN_ID,
                                         "secret": TOKEN})
    assert chosen.ring.items[("fleetledger", "proxmox-token:" + TOKEN_ID)] == TOKEN
    text = page(chosen)
    assert "Test the connection and save" in text
    assert '<option selected>shipshape-seeds</option>' in text  # pre-filled
    chosen.post("/setup/proxmox/save", {})
    config = chosen.store.load().driver_config("proxmox")
    assert config["tls_fingerprint"] == FPR and config["token_store"] == "keyring"
    assert config["template_vmid"] == 9000 and config["bridge"] == "vmbr0"
    assert chosen.store.load().defaults["driver"] == "proxmox"
    assert TOKEN not in "".join(chosen.pages) + chosen.store.path.read_text()


def test_an_unconfirmed_fingerprint_sends_nothing(chosen):
    chosen.post("/setup/proxmox/address", {"address": "pve.example.com"})
    chosen.post("/setup/proxmox/token", {"token_id": TOKEN_ID, "secret": TOKEN})
    assert chosen.pve.paths == [] and not token_stored(chosen)
    assert "Not pinned" in page(chosen)


def test_a_changed_certificate_is_flagged_and_needs_its_own_box(chosen):
    to_choices(chosen)
    chosen.post("/setup/proxmox/save", {})
    chosen.app.cert_fingerprint = lambda host, port: ":".join(["66"] * 32)
    chosen.post("/setup/proxmox/start")
    chosen.post("/setup/proxmox/address", {"address": "pve.example.com"})
    text = page(chosen)
    assert "DIFFERENT from the one you confirmed before" in text and FPR in text
    chosen.pve.paths.clear()
    chosen.post("/setup/proxmox/token", {"matches": "yes", "token_id": TOKEN_ID})
    assert chosen.pve.paths == []  # the renewal box was not ticked
    chosen.post("/setup/proxmox/token", {"matches": "yes", "changed_ok": "yes",
                                         "token_id": TOKEN_ID})  # the stored secret
    assert chosen.pve.paths and set(chosen.pve.secrets_seen) == {TOKEN}


def test_a_refused_token_saves_nothing(chosen):
    chosen.pve.refuse = True
    to_choices(chosen)
    assert not token_stored(chosen)
    assert not (chosen.home / "config" / "proxmox-token").exists()
    assert "refused the token" in page(chosen)
    assert chosen.store.load().driver_config("proxmox") is None


def test_missing_pieces_show_the_root_commands_and_nothing_is_saved(chosen):
    chosen.pve.templates = []
    to_choices(chosen)
    text = page(chosen)
    assert "Still missing on the Proxmox host" in text and "qm template 9000" in text
    assert "Check again" in text and "Test the connection and save" not in text
    chosen.post("/setup/proxmox/save", {})
    assert chosen.store.load().driver_config("proxmox") is None
    chosen.pve.templates = FakePVE().templates  # they ran the commands
    chosen.post("/setup/proxmox/recheck", {"node": "pve"})
    assert "Test the connection and save" in page(chosen)


def test_only_offered_choices_are_saved(chosen):
    to_choices(chosen)
    chosen.post("/setup/proxmox/save", {"seed_storage": "local"})  # mixed content: not offered
    assert chosen.store.load().driver_config("proxmox") is None
    assert "not one of the choices" in page(chosen)


def test_manual_is_one_click(chosen):
    chosen.post("/setup/hypervisor/manual")
    assert chosen.store.load().defaults["driver"] == "manual"


# --- the overlay ------------------------------------------------------------------------------


def test_the_overlay_is_checked_step_by_step_before_saving(chosen):
    chosen.post("/setup/hypervisor/manual")
    text = page(chosen)
    assert "login.tailscale.com/admin/acls/file" in text and "Copy the rules" in text
    chosen.post("/setup/overlay", {"client_id": "kExampleClient1", "secret": OAUTH_SECRET})
    overlay = chosen.store.load().overlay
    assert overlay["api"]["secret_store"] == "keyring"
    assert chosen.tailnet.tried == [["tag:ship"]]
    text = page(chosen)
    assert "can make tag:ship keys" in text
    assert OAUTH_SECRET not in "".join(chosen.pages) + chosen.store.path.read_text()


def test_an_overlay_whose_rules_are_not_in_place_saves_nothing(chosen):
    chosen.tailnet.tag_ok = False
    chosen.post("/setup/overlay", {"client_id": "kExampleClient1", "secret": OAUTH_SECRET})
    assert chosen.store.load().overlay is None
    assert not any(i.startswith("tailscale-oauth:") for _, i in chosen.ring.sets)
    assert "tagOwners" in page(chosen)


def test_an_optional_step_does_not_hold_the_next_one_back(chosen):
    chosen.post("/setup/hypervisor/manual")
    text = page(chosen)
    assert re.search(r"Your first Ship</a>\s*<span class=\"pill warn\">next", text)


def test_the_overlay_can_be_skipped(chosen):
    chosen.post("/setup/overlay/skip")
    assert re.search(r"\(optional\)</a>\s*<span class=\"pill muted\">skipped", page(chosen))


# --- the first Ship ---------------------------------------------------------------------------


def test_the_first_ship_shows_its_progress(chosen):
    import time

    chosen.post("/setup/hypervisor/manual")
    chosen.post("/setup/ship", {"name": "-bad"})
    assert "uses a-z" in page(chosen) and not chosen.store.load().ships()
    chosen.post("/setup/ship", {"name": "ship-01", "tag": "work"})
    assert "Making <strong>ship-01</strong>" in page(chosen) or "ship-01" in page(chosen)
    for _ in range(100):
        response = chosen.get("/setup/ship/progress")
        if "hx-get" not in response.text:
            break
        time.sleep(0.05)
    assert "ship-01" in response.text and "Create the VM" in response.text
    assert chosen.store.load().ship("ship-01").tags == ("work",)


# --- the commands -----------------------------------------------------------------------------


def test_ship_fleet_setup_opens_the_web_setup(monkeypatch, store):
    from fleetledger.web import server

    called = {}
    monkeypatch.setattr(server, "serve", lambda app, **kw: called.update(kw) or 0)
    app = cli.App(out=io.StringIO(), err=io.StringIO(), store_factory=lambda: store)
    assert cli.main(["setup", "--no-browser", "--port", "0"], app) == 0
    assert called == {"port": 0, "open_browser": False, "start_page": "/setup"}
    err = io.StringIO()
    app = cli.App(out=io.StringIO(), err=err, store_factory=lambda: store)
    assert cli.main(["setup", "--redo", "hypervisor"], app) != 0
    assert "--text" in err.getvalue()


# --- review fixes -----------------------------------------------------------------------------


def test_a_bad_import_hands_nothing_over(ws, tmp_path):
    """Everything is checked before any key is re-encrypted: here, your key."""
    other = tmp_path / "other"
    other.mkdir()
    _key(other / "admin", "their own")
    (tmp_path / "garbage.pub").write_text("not a key\n")
    before = (other / "admin").read_bytes()
    ws.post("/setup/fleet/import", {"admin_key": str(other / "admin"),
                                    "your_key_other": str(tmp_path / "garbage.pub"),
                                    "protect": "yes", "admin_passphrase": "their own"})
    assert not ws.store.exists() and (other / "admin").read_bytes() == before
    assert ws.ring.sets == []


def test_a_wrong_ca_passphrase_changes_no_key_at_all(ws, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    for name in ("admin", "host_ca", "user_ca"):
        _key(other / name, f"pass {name}")
    before = {n: (other / n).read_bytes() for n in ("admin", "host_ca", "user_ca")}
    ws.post("/setup/fleet/import", {
        "admin_key": str(other / "admin"), "host_ca_key": str(other / "host_ca"),
        "user_ca_key": str(other / "user_ca"), "your_key": "~/.ssh/id_ed25519.pub",
        "protect": "yes", "admin_passphrase": "pass admin", "host_passphrase": "wrong",
        "user_passphrase": "pass user_ca"})
    assert {n: (other / n).read_bytes() for n in before} == before
    assert not ws.store.exists() and "does not open host_ca" in page(ws)


def test_one_ca_key_without_the_other_is_refused(ws, tmp_path):
    _key(tmp_path / "admin")
    _key(tmp_path / "host_ca")
    ws.post("/setup/fleet/import", {"admin_key": str(tmp_path / "admin"),
                                    "host_ca_key": str(tmp_path / "host_ca"),
                                    "your_key": "~/.ssh/id_ed25519.pub"})
    assert not ws.store.exists() and "both CA keys, or neither" in page(ws)


def test_a_passphrase_is_used_exactly_as_typed(partway):
    admin = partway.user_home / ".ssh" / "shipshape_fleet_ed25519"
    admin.unlink()
    admin.with_name(admin.name + ".pub").unlink()
    _key(admin, " spaced ")
    partway.post("/setup/fleet/adopt", {"take_0": "yes", "passphrase_0": " spaced ",
                                        "confirm": "yes"})
    assert admin in keys.managed(partway.store.load())[1]


def test_a_key_without_its_pub_is_not_offered(partway):
    pub = partway.user_home / ".ssh" / "shipshape_fleet_ed25519.pub"
    pub.unlink()
    text = page(partway)
    form = text.split('action="/setup/fleet/adopt"')[1].split("</form>")[0]
    assert "shipshape_fleet_ed25519</code>" not in form  # not offered: no .pub beside it
    assert "shipshape_host_ca_ed25519</code>" in form


def test_the_ca_button_uses_the_fleets_own_store(ws, monkeypatch):
    ws.post("/setup/fleet/new", {"your_key": "~/.ssh/id_ed25519.pub", "store": "file"})
    with ws.store.edit() as ed:  # as if the CA had not been made
        del ed.doc["ca"]
    for name in ("shipshape_host_ca_ed25519", "shipshape_user_ca_ed25519"):
        for path in (ws.user_home / ".ssh" / name, ws.user_home / ".ssh" / (name + ".pub")):
            path.unlink()
    ws.post("/setup/fleet/ca")
    inv = ws.store.load()
    assert inv.doc["ca"] and len(keys.managed(inv)[1]) == 3
    assert keys.managed(inv)[0] == "file" and ws.ring.sets == []


def test_handing_over_uses_the_fleets_own_store(partway):
    from fleetledger.store import TomlFileStore as _Store

    store = partway.store
    with store.edit() as ed:
        keys.record(ed, "file", [])  # this fleet keeps managed passphrases in files
    assert isinstance(store, _Store)
    partway.post("/setup/fleet/adopt", {"take_0": "yes", "passphrase_0": "chosen by them",
                                        "confirm": "yes", "store": "keyring"})
    assert keys.managed(store.load())[0] == "file" and partway.ring.sets == []


def test_an_implausible_token_secret_is_refused_before_anything_is_sent(chosen):
    chosen.post("/setup/proxmox/address", {"address": "pve.example.com"})
    chosen.post("/setup/proxmox/token", {"matches": "yes", "token_id": TOKEN_ID,
                                         "secret": "has a space"})
    assert chosen.pve.paths == [] and not token_stored(chosen)


def test_ship_fleet_ui_on_a_computer_not_set_up_lands_on_setup(home, fake_repo):
    store = TomlFileStore(home / "config" / "ships.toml")
    app = cli.App(out=io.StringIO(), err=io.StringIO(), store_factory=lambda: store,
                  repo_factory=lambda: fake_repo, ssh_factory=lambda inv: None)
    gate = Gate(PORT)
    asgi = webapp.create_app(app, gate, fleet=Fleet(app, interval=30),
                             concierge_=Concierge(app))  # no start page: ship-fleet ui
    client = TestClient(asgi, base_url=BASE, follow_redirects=False)
    client.get(f"/login?token={gate.new_token()}")
    for _ in range(2):  # every time, not just the first
        response = client.get("/")
        assert response.status_code == 303 and response.headers["location"] == "/setup"


def test_ship_fleet_ui_on_a_set_up_computer_shows_the_fleet(store, fake_repo):
    app = cli.App(out=io.StringIO(), err=io.StringIO(), store_factory=lambda: store,
                  repo_factory=lambda: fake_repo, ssh_factory=lambda inv: None)
    gate = Gate(PORT)
    asgi = webapp.create_app(app, gate, fleet=Fleet(app, interval=30),
                             concierge_=Concierge(app))
    client = TestClient(asgi, base_url=BASE, follow_redirects=False)
    client.get(f"/login?token={gate.new_token()}")
    assert client.get("/").status_code == 200


def test_a_manual_ship_says_to_make_its_vm_and_the_dashboard_knows_it(chosen):
    import time

    ui = chosen.client.app.app.state.ui
    polled = []
    ui.fleet.poll = lambda names=None: polled.append(names)
    chosen.post("/setup/hypervisor/manual")
    chosen.post("/setup/ship", {"name": "ship-01"})
    for _ in range(100):
        response = chosen.get("/setup/ship/progress")
        if "hx-get" not in response.text:
            break
        time.sleep(0.05)
    assert "seed ISO is written" in response.text and "make its VM" in response.text
    assert polled == [["ship-01"]]  # the dashboard polled it when the job finished


# --- what is already on this computer is offered, never asked again --------------------------


def test_a_token_secret_already_on_this_computer_is_offered_up_front(chosen):
    """Made before ships.toml existed, so no token id is recorded anywhere."""
    secret_file = chosen.home / "config" / "proxmox-token"
    secret_file.write_text(TOKEN + "\n")
    secret_file.chmod(0o600)
    chosen.post("/setup/proxmox/address", {"address": "pve.example.com"})
    text = page(chosen)
    assert "A token secret is already on this computer" in text
    assert "type its token id and leave the secret empty" in text
    assert 'name="token_id" value=""' in text  # nothing that only looks filled in
    assert 'placeholder="e.g. shipshape@pve!build"' in text
    chosen.post("/setup/proxmox/token", {"matches": "yes", "token_id": TOKEN_ID})
    assert set(chosen.pve.secrets_seen) == {TOKEN}
    assert "Test the connection and save" in page(chosen)
    assert TOKEN not in "".join(chosen.pages)


def test_nothing_stored_says_nothing_is_stored(chosen):
    chosen.post("/setup/proxmox/address", {"address": "pve.example.com"})
    text = page(chosen)
    assert "already on this computer" not in text and "already in your keyring" not in text
    assert "If its secret is already stored" in text


def test_a_token_secret_in_the_keyring_fills_in_its_token_id(chosen):
    chosen.ring.items[("fleetledger", "proxmox-token:" + TOKEN_ID)] = TOKEN
    chosen.post("/setup/proxmox/address", {"address": "pve.example.com"})
    text = page(chosen)
    assert "This token's secret is already in your keyring" in text
    assert f'name="token_id" value="{TOKEN_ID}"' in text
    chosen.post("/setup/proxmox/token", {"matches": "yes", "token_id": TOKEN_ID})
    assert set(chosen.pve.secrets_seen) == {TOKEN}
    assert "Test the connection and save" in page(chosen)
    assert TOKEN not in "".join(chosen.pages)


def test_the_saved_address_is_filled_in(chosen):
    to_choices(chosen)
    chosen.post("/setup/proxmox/save", {})
    chosen.post("/setup/proxmox/start")
    assert 'name="address" value="pve.example.com"' in page(chosen)


def test_an_oauth_secret_already_in_the_keyring_is_offered(chosen):
    chosen.ring.items[("fleetledger", "tailscale-oauth:kExampleClient1")] = OAUTH_SECRET
    chosen.post("/setup/hypervisor/manual")
    text = page(chosen)
    assert "Your keyring already holds the secret of OAuth client" in text
    assert 'name="client_id" value="kExampleClient1"' in text
    sets = list(chosen.ring.sets)
    chosen.post("/setup/overlay", {"client_id": "kExampleClient1"})
    assert chosen.store.load().overlay["api"]["secret_store"] == "keyring"
    assert chosen.ring.sets == sets  # used as it was, not stored again
    assert OAUTH_SECRET not in "".join(chosen.pages)


def test_an_oauth_secret_file_already_here_is_offered_for_its_own_client(chosen):
    secret_file = chosen.home / "config" / "tailscale-oauth"
    secret_file.write_text(OAUTH_SECRET + "\n")
    secret_file.chmod(0o600)
    chosen.post("/setup/hypervisor/manual")
    text = page(chosen)
    assert "An OAuth client secret is already on this computer" in text
    assert 'name="client_id" value="kExample9CNTRL"' in text  # named by the secret
    chosen.post("/setup/overlay", {"client_id": "kSomeOtherClient"})
    assert chosen.store.load().overlay is None  # not another client's secret
    chosen.post("/setup/overlay", {"client_id": "kExample9CNTRL"})
    api = chosen.store.load().overlay["api"]
    assert api["secret_store"] == "file" and secret_file.read_text().strip() == OAUTH_SECRET
    assert "as it was" in page(chosen)
    assert OAUTH_SECRET not in "".join(chosen.pages)


# --- who looks after the passphrases of keys you bring ----------------------------------------


def test_bringing_keys_asks_plainly_who_looks_after_their_passphrases(ws, tmp_path):
    text = page(ws)
    assert "Who looks after these keys' passphrases?" in text
    before = text.split("<summary>Use my keys</summary>")[1].split("Who looks after")[0]
    assert "<details" not in before  # in the open, not in a collapsed section
    assert "recommended: no prompts" in text and "you are asked for its passphrase" in text
    other = tmp_path / "other"
    other.mkdir()
    _key(other / "admin", "theirs")
    ws.post("/setup/fleet/import", {"admin_key": str(other / "admin"),
                                    "your_key": "~/.ssh/id_ed25519.pub"})
    assert not ws.store.exists() and "Choose who looks after" in page(ws)


def _agent_at(sock):
    out = subprocess.run(["ssh-agent", "-a", str(sock)], capture_output=True, text=True,
                         check=True, timeout=10).stdout
    return int(out.split("SSH_AGENT_PID=")[1].split(";")[0])


@pytest.mark.parametrize("gnome", [True, False])
def test_gnomes_agent_listing_your_key_is_not_taken_as_loaded(partway, tmp_path, monkeypatch,
                                                               gnome):
    """GNOME's agent lists every ~/.ssh key with a .pub whether it holds it or not, and
    asks for the passphrase in a pop-up when one is used: the page cannot tell, so it
    says so. (A real agent here, holding the key, stands in for that list.)"""
    import os
    import signal

    folder = tmp_path / ("keyring" if gnome else "agent")
    folder.mkdir(mode=0o700)
    sock = folder / ("ssh" if gnome else "agent.sock")
    pid = _agent_at(sock)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
    try:
        keys.add_to_agent(partway.user_home / ".ssh" / "shipshape_fleet_ed25519",
                          "chosen by them")
        text = page(partway)
    finally:
        os.kill(pid, signal.SIGTERM)
    assert ("Making a Ship needs your fleet admin key" in text) is gnome
    assert ("pop-up" in text) is gnome


# --- the first Ship's wait -------------------------------------------------------------------


def test_a_first_boot_that_outlasts_the_wait_is_still_starting_not_an_error(chosen,
                                                                            monkeypatch):
    import time

    from fleetledger.enrol import StillStarting

    from fleetledger import commands

    def slow(args, app):
        app.note("ship-01: first boot: cloud-init is installing (the slow part)")
        raise StillStarting("ship-01 did not finish provisioning within 30 minutes (last "
                            "state: provisioning). It may still be starting.")

    monkeypatch.setattr(commands, "cmd_new", slow)
    chosen.post("/setup/hypervisor/manual")
    chosen.post("/setup/ship", {"name": "ship-01"})
    for _ in range(100):
        response = chosen.get("/setup/ship/progress")
        if "hx-get" not in response.text:
            break
        time.sleep(0.05)
    assert "still starting up" in response.text
    assert "ship-fleet status ship-01 --wait" in response.text
    assert "log bad" not in response.text and "did not finish;" not in response.text


# --- VM ids -----------------------------------------------------------------------------------


def test_ships_get_vm_ids_away_from_your_own_by_default(chosen):
    to_choices(chosen)
    text = page(chosen)
    assert 'name="vmid_range" value="9100-9199"' in text
    chosen.post("/setup/proxmox/save", {})
    assert chosen.store.load().driver_config("proxmox")["vmid_range"] == [9100, 9199]


def test_any_free_vm_id_is_still_a_choice(chosen):
    to_choices(chosen)
    chosen.post("/setup/proxmox/save", {"vmid_range": "any"})
    assert "vmid_range" not in chosen.store.load().driver_config("proxmox")
    chosen.post("/setup/proxmox/start")
    to_choices(chosen)
    chosen.post("/setup/proxmox/save", {"vmid_range": "50-60"})
    assert "both 100 or more" in page(chosen)


def test_the_same_certificate_again_is_known_not_flagged(chosen):
    to_choices(chosen)
    chosen.post("/setup/proxmox/save", {})
    chosen.post("/setup/proxmox/start")
    chosen.post("/setup/proxmox/address", {"address": "pve.example.com"})
    text = page(chosen)
    assert "DIFFERENT" not in text and "This is the certificate you confirmed before" in text
    assert 'name="matches" value="yes" checked' in text
    assert 'name="changed_ok"' not in text
    to_choices(chosen)  # no renewal box needed
    assert "Test the connection and save" in page(chosen)


# --- the first Ship: an agent, and the overlay -------------------------------------------------


def _made_with(env, monkeypatch, data):
    import time

    from fleetledger import commands

    seen = []
    monkeypatch.setattr(commands, "cmd_new", lambda args, app: seen.append(args) or 0)
    env.post("/setup/ship", data)
    for _ in range(100):
        if seen and "hx-get" not in env.get("/setup/ship/progress").text:
            break
        time.sleep(0.05)
    return seen[0]


def test_the_first_ship_starts_an_agent_with_remote_control_by_default(chosen, monkeypatch):
    chosen.post("/setup/hypervisor/manual")
    text = page(chosen)
    assert 'name="agent_on" value="yes" checked' in text and 'name="agent" value="main"' in text
    assert "shows in the Claude app" in text
    args = _made_with(chosen, monkeypatch, {"name": "ship-01", "agent_on": "yes",
                                            "agent": "main", "cwd": "~/work"})
    assert args.autostart == ["main:~/work"]
    assert args.no_overlay  # no overlay set up: nothing to join


def test_the_first_ship_can_go_without_an_agent_and_joins_the_overlay(chosen, monkeypatch):
    chosen.post("/setup/hypervisor/manual")
    chosen.post("/setup/overlay", {"client_id": "kExampleClient1", "secret": OAUTH_SECRET})
    assert 'name="overlay" value="yes" checked' in page(chosen)
    args = _made_with(chosen, monkeypatch, {"name": "ship-01", "overlay": "yes"})
    assert args.autostart == [] and not args.no_overlay


def test_a_bad_agent_name_makes_nothing(chosen, monkeypatch):
    from fleetledger import commands

    monkeypatch.setattr(commands, "cmd_new", lambda args, app: pytest.fail("made"))
    chosen.post("/setup/hypervisor/manual")
    chosen.post("/setup/ship", {"name": "ship-01", "agent_on": "yes", "agent": "Main",
                                "cwd": "~/work"})
    assert "a name of a-z" in page(chosen)


def test_a_garbage_token_file_is_not_offered(chosen):
    secret_file = chosen.home / "config" / "proxmox-token"
    secret_file.write_text("not a token at all\n")
    secret_file.chmod(0o600)
    chosen.post("/setup/proxmox/address", {"address": "pve.example.com"})
    assert "already on this computer" not in page(chosen)


def test_a_reversed_vm_id_range_is_refused(chosen):
    to_choices(chosen)
    chosen.post("/setup/proxmox/save", {"vmid_range": "9199-9100"})
    assert "both 100 or more" in page(chosen)
    assert chosen.store.load().driver_config("proxmox") is None


def test_the_first_ship_lets_this_computer_attach(chosen, monkeypatch):
    import time

    from fleetledger.inventory import Ship
    from fleetledger.store import ship_entry

    from fleetledger import commands, trustcmd

    def made(args, app):
        ship = Ship(name="ship-01", id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
                    driver="manual", names=("ship-01.local",), host_key="ssh-ed25519 AAAA",
                    enrolled="2026-01-01T00:00:00Z")
        with app.store_factory().edit() as ed:
            ed.add_ship("ship-01", ship_entry(ship))
        return 0

    trusted = []
    monkeypatch.setattr(commands, "cmd_new", made)
    monkeypatch.setattr(trustcmd, "trust_this_computer",
                        lambda app, store, say, create=True: trusted.append(create) or True)
    chosen.post("/setup/hypervisor/manual")
    chosen.post("/setup/ship", {"name": "ship-01"})
    for _ in range(100):
        response = chosen.get("/setup/ship/progress")
        if "hx-get" not in response.text:
            break
        time.sleep(0.05)
    assert trusted == [True]
    assert "<code>herdr --remote ship-01</code>" in response.text


def test_the_first_ship_can_be_sized(chosen, monkeypatch):
    chosen.post("/setup/hypervisor/manual")
    text = page(chosen)
    # The defaults, within the size contract's limits (ships.toml.schema.json).
    assert 'name="cores" type="number" min="1" max="64" value="4"' in text
    assert 'name="memory_gb" type="number" min="4" max="256" value="8"' in text
    assert 'name="disk_gb" type="number" min="20" max="4096" value="40"' in text
    args = _made_with(chosen, monkeypatch, {"name": "ship-01", "cores": "8", "memory_gb": "16",
                                            "disk_gb": "80"})
    assert (args.cores, args.memory_mb, args.disk_gb) == (8, 16384, 80)


def test_a_silly_size_makes_nothing(chosen, monkeypatch):
    from fleetledger import commands

    monkeypatch.setattr(commands, "cmd_new", lambda args, app: pytest.fail("made"))
    chosen.post("/setup/hypervisor/manual")
    chosen.post("/setup/ship", {"name": "ship-01", "cores": "0"})
    assert "cores: 1 to 64" in page(chosen)


def test_the_first_ship_can_use_fewer_sign_ins(chosen, monkeypatch):
    chosen.post("/setup/hypervisor/manual")
    assert 'name="signin" value="gcloud" checked' in page(chosen)
    args = _made_with(chosen, monkeypatch, {"name": "ship-01", "signins_shown": "yes",
                                            "signin": ["claude", "github"]})
    assert args.signins == "claude,github"
    args = _made_with(chosen, monkeypatch, {"name": "ship-02", "signins_shown": "yes",
                                            "signin": ["claude", "gcloud", "github"]})
    assert args.signins is None  # all of them: nothing recorded


def test_the_first_agent_can_work_in_a_github_repo(chosen, monkeypatch):
    chosen.post("/setup/hypervisor/manual")
    assert 'name="repo" placeholder="e.g. someone/agent-setup (optional)"' in page(chosen)
    args = _made_with(chosen, monkeypatch, {"name": "ship-01", "agent_on": "yes",
                                            "agent": "main", "cwd": "~/work",
                                            "repo": "someone/agent-setup"})
    assert args.repo == "someone/agent-setup"


def test_your_login_name_on_ships_from_the_setup_page(chosen):
    assert 'name="owner" value="human"' in page(chosen)
    chosen.post("/setup/owner", {"owner": "sam"})
    assert chosen.store.load().owner == "sam" and "New Ships get the login sam" in page(chosen)
    chosen.post("/setup/owner", {"owner": "agent"})
    assert chosen.store.load().owner == "sam" and "cannot be your login name" in page(chosen)


# --- what each trusted machine may log in as ------------------------------------------


def test_each_trusted_machine_logs_in_as_what_you_choose(chosen, tmp_path):
    from fleetledger import ca

    _key(tmp_path / "shared")
    with chosen.store.edit() as ed:
        ed.set_machine("shared", {"public_key": (tmp_path / "shared.pub").read_text().strip(),
                                  "added": "2026-01-01T00:00:00Z", "scope": ["all"],
                                  "logins": ["agent", "owner"]})
    text = page(chosen)
    assert "Trusted machines" in text and '<option value="both" selected>' in text
    assert 'data-confirm-title="Change what shared may log in as?"' in text
    chosen.post("/setup/machine", {"machine": "shared", "logins": "agent"})
    assert ca.machine(chosen.store.load(), "shared").logins == ("agent",)
    assert "may now log in as the agent" in page(chosen)
    chosen.post("/setup/machine", {"machine": "shared", "logins": "root"})
    assert ca.machine(chosen.store.load(), "shared").logins == ("agent",)
    chosen.post("/setup/machine", {"machine": "nobody-here", "logins": "both"})
    assert "No trusted machine called nobody-here" in page(chosen)
