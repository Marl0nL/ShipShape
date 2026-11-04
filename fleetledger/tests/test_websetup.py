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
                  sleep=lambda s: None, ssh_factory=lambda inv: None)
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
        assert ("fleetledger", f"key-passphrase:{name}") in ws.ring.items
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
    secret = ws.home / "config" / "key-passphrases" / "shipshape_fleet_ed25519"
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
    assert "Let FleetLedger look after these keys" in page(ws)  # offered, opt-in


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
        new = ws.ring.items[("fleetledger", f"key-passphrase:{name}")]
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
    assert "not the key&#39;s passphrase" in page(ws)


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
    new = partway.ring.items[("fleetledger", "key-passphrase:shipshape_fleet_ed25519")]
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
