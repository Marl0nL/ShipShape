"""FleetLedger's vault (vault.py, vaultcmd.py, web/vaultweb.py): one encrypted file
for every secret, unlocked once per boot.

The scrypt cost is lowered for speed (the file says what it was made with), and the
check that the runtime folder is a RAM file system answers yes for the temporary
folders here (it is tested on its own below). No real keyring is touched: the OS
keyring is the fake from test_secret_store.
"""

from __future__ import annotations

import io
import json
import re
import stat
import subprocess
import time

import pytest
from fleetledger.errors import ConfigError, FleetError
from fleetledger.overlay import secret_store
from test_secret_store import fake_backend
from test_web import ORIGIN, web  # noqa: F401 - the web UI fixture

from fleetledger import cli, keys, vault, vaultcmd

PASS = "correct horse battery staple"
TOKEN = "11111111-2222-3333-4444-555555555555"
OAUTH = "tskey-client-k123EXAMPLE-SECRET"


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(vault, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(vault, "in_ram", lambda path: True)


@pytest.fixture
def ring(monkeypatch):
    backend = fake_backend()
    monkeypatch.setattr(secret_store, "get_keyring", lambda: backend)
    return backend


def asking(*answers):
    """An app hook that answers secret prompts in turn (never a terminal)."""
    queue = list(answers)
    return lambda prompt: queue.pop(0)


def app_for(store, *secrets_, confirm=None):
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store, stdin_isatty=lambda: False)
    app.ask_secret = asking(*secrets_)
    if confirm is not None:
        app.confirm = confirm
    return app, out


# --- the file and its cryptography ---------------------------------------------------------


def test_a_new_vault_is_unlocked_and_holds_secrets_only_encrypted(home):
    code = vault.create(PASS)
    assert vault.state() == "unlocked"
    vault.backend().set_password("fleetledger", "proxmox-token:t", TOKEN)
    raw = vault.vault_path().read_bytes()
    assert TOKEN.encode() not in raw and PASS.encode() not in raw
    assert code.replace("-", "").encode() not in raw
    assert stat.S_IMODE(vault.vault_path().stat().st_mode) == 0o600
    key = vault.key_path()
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(key.parent.stat().st_mode) == 0o700
    doc = json.loads(raw)
    assert [s["kind"] for s in doc["slots"]] == ["passphrase", "recovery"]
    assert re.fullmatch(r"([A-Z2-7]{4}-){7}[A-Z2-7]{4}", code)


def test_locked_nothing_reads_and_the_passphrase_opens_it_again(home):
    vault.create(PASS)
    vault.backend().set_password("fleetledger", "item", "value")
    assert vault.lock() is True
    assert vault.state() == "locked"
    with pytest.raises(vault.Locked):
        vault.backend().get_password("fleetledger", "item")
    with pytest.raises(vault.WrongSecret):
        vault.unlock("not the passphrase at all")
    assert vault.state() == "locked"
    assert vault.unlock(PASS) == "unlocked"
    assert vault.backend().get_password("fleetledger", "item") == "value"
    assert vault.unlock(PASS) == "already"


def test_the_recovery_code_opens_it_and_resets_the_passphrase(home):
    code = vault.create(PASS)
    vault.lock()
    assert vault.unlock(code.lower().replace("-", " "), recovery=True) == "unlocked"
    vault.change_passphrase(code, "a whole new passphrase", recovery=True)
    vault.lock()
    with pytest.raises(vault.WrongSecret):
        vault.unlock(PASS)
    vault.unlock("a whole new passphrase")
    vault.lock()
    vault.unlock(code, recovery=True)  # the code is as it was


def test_a_backup_from_before_a_passphrase_change_opens_with_the_old_one(home):
    vault.create(PASS)
    vault.backend().set_password("fleetledger", "item", "value")
    backup = vault.vault_path().read_bytes()
    vault.change_passphrase(PASS, "the next passphrase")
    vault.lock()
    with pytest.raises(vault.WrongSecret):
        vault.unlock(PASS)
    vault.vault_path().write_bytes(backup)
    vault.unlock(PASS)
    assert vault.backend().get_password("fleetledger", "item") == "value"


def test_a_new_recovery_code_retires_the_old(home):
    old = vault.create(PASS)
    new = vault.new_recovery(PASS)
    assert new != old
    vault.lock()
    with pytest.raises(vault.WrongSecret):
        vault.unlock(old, recovery=True)
    vault.unlock(new, recovery=True)


def test_wrong_tries_slow_down(home):
    vault.create(PASS)
    vault.lock()
    clock = [1000.0]
    for _ in range(vault.FREE_TRIES):
        with pytest.raises(vault.WrongSecret):
            vault.unlock("wrong passphrase!", wall=lambda: clock[0])
    with pytest.raises(FleetError, match="Too many wrong tries: wait 11 seconds"):
        vault.unlock(PASS, wall=lambda: clock[0])  # not even the right one, yet
    clock[0] += 11
    with pytest.raises(vault.WrongSecret):
        vault.unlock("wrong again!!!!", wall=lambda: clock[0])
    clock[0] += 11
    with pytest.raises(FleetError, match="wait 10 seconds"):  # 20 after the fourth
        vault.unlock(PASS, wall=lambda: clock[0])
    clock[0] += 10
    assert vault.unlock(PASS, wall=lambda: clock[0]) == "unlocked"
    assert vault.tries().count == 0
    assert vault.Tries(50, 0.0).wait(0.0) == vault.MAX_WAIT


@pytest.mark.parametrize("change", ["bit", "cost", "schema", "slot", "symlink", "salt"])
def test_a_vault_changed_by_hand_is_refused_never_guessed_at(home, change):
    vault.create(PASS)
    vault.backend().set_password("fleetledger", "item", "value")
    vault.lock()
    path = vault.vault_path()
    doc = json.loads(path.read_text())
    if change == "bit":
        data = doc["data"]
        doc["data"] = data[:40] + ("A" if data[40] != "A" else "B") + data[41:]
    elif change == "cost":
        doc["slots"][0]["n"] = 2 ** 30  # a huge cost is refused, not run
    elif change == "schema":
        doc["schema"] = "fleetledger.vault/2"
    elif change == "slot":
        doc["slots"] = [s for s in doc["slots"] if s["kind"] != "passphrase"]
    elif change == "salt":
        doc["slots"][0]["salt"] = "not base64!"
    if change == "symlink":
        real = path.with_name("elsewhere.json")
        path.rename(real)
        path.symlink_to(real)
    else:
        path.write_text(json.dumps(doc))
    with pytest.raises(ConfigError):
        vault.unlock(PASS)
    assert vault.state() == "locked"


def test_it_never_unlocks_into_a_folder_on_disk(home, monkeypatch):
    monkeypatch.setattr(vault, "in_ram", lambda path: False)
    with pytest.raises(ConfigError, match="not in RAM"):
        vault.create(PASS)
    assert not vault.is_on()


def test_ram_is_told_from_the_mount_table():
    table = "\n".join([
        "22 1 259:5 / / rw,relatime shared:1 - ext4 /dev/sda1 rw",
        "25 22 0:23 / /run rw,nosuid shared:5 - tmpfs tmpfs rw,mode=755",
        "60 25 0:50 / /run/user/1000 rw,nosuid,nodev shared:300 - tmpfs tmpfs rw,mode=700",
        "61 60 0:51 / /run/user/1000/doc rw shared:301 - fuse.portal portal rw",
        "70 22 0:60 / /home/example/with\\040space rw - ext4 /dev/sdb1 rw",
    ])
    from pathlib import Path

    assert vault.mount_of(Path("/run/user/1000/fleetledger/vault"), table)[1] == "tmpfs"
    assert vault.mount_of(Path("/run/user/1000/doc/x"), table)[1] == "fuse.portal"
    assert vault.mount_of(Path("/home/example/.cache/fleetledger"), table)[1] == "ext4"
    assert vault.mount_of(Path("/home/example/with space/f"), table)[0] == \
        "/home/example/with space"
    assert vault.mount_of(Path("/run2/x"), table)[1] == "ext4"  # not under /run
    assert vault.swap_on("Filename Type Size Used Priority\n") is False
    assert vault.swap_on("Filename Type Size Used Priority\n/swapfile file 1 0 -2\n")


def test_a_short_passphrase_is_refused(home):
    with pytest.raises(FleetError, match="at least 12"):
        vault.create("short")
    assert not vault.is_on()


# --- the vault as this computer's keyring ------------------------------------------------------


def test_on_it_is_the_keyring_and_never_falls_back(home, ring):
    ring.set_password("fleetledger", "proxmox-token:t", "from the OS keyring")
    vault.create(PASS)
    backend, name = secret_store.secure_backend()
    assert name == vault.BACKEND_NAME
    assert secret_store.existing_in_keyring("proxmox-token:t") is None  # not the OS one
    secret_store.store_in_keyring("proxmox-token:t", TOKEN)
    assert secret_store.read_keyring("proxmox-token:t", "Proxmox token", "") == TOKEN
    assert ring.items[("fleetledger", "proxmox-token:t")] == "from the OS keyring"
    assert secret_store.stored_names("proxmox-token:") == ["t"]
    vault.lock()
    assert secret_store.keyring_usable() == (False, "FleetLedger's vault is locked "
                                                    "(ship-fleet unlock)")
    with pytest.raises(vault.Locked, match="so the Proxmox token cannot be read"):
        secret_store.read_keyring("proxmox-token:t", "Proxmox token", "")
    with pytest.raises(vault.Locked):
        secret_store.store_in_keyring("proxmox-token:t", TOKEN)


def test_on_no_secret_goes_to_a_file(home, tmp_path):
    vault.create(PASS)
    assert keys.default_store() == "keyring"
    vault.lock()
    assert keys.default_store() == "keyring"  # locked: still never a file
    with pytest.raises(ConfigError, match="never in a file"):
        keys.store_passphrase(tmp_path / "k", "x" * 20, "file")
    from fleetledger import migrate, proxmox_setup

    with pytest.raises(ConfigError, match="never in a file"):
        proxmox_setup.keep_secret({"token_id": "t@pve!x"}, TOKEN, "file")
    with pytest.raises(ConfigError, match="never in a file"):
        migrate.choose_store("file")


# --- ship-fleet vault init / unlock / lock / off ------------------------------------------------


def secrets_fleet(store, home, ring, *, kind: str):
    """The store fixture's fleet, with managed keys (passphrases in kind), a Proxmox token
    and an OAuth secret kept the same way."""
    made = []
    for name in ("admin", "host_ca"):
        path = home / "ssh" / f"fleet_{name}"
        keys.make(path, name, kind)
        made.append(str(path))
    with store.edit() as ed:
        keys.record(ed, kind, made)
        ed.doc["fleet"]["admin_key"] = made[0]
        if kind == "keyring":
            ring.set_password("fleetledger", secret_store.proxmox_item("fleet@pve!x"), TOKEN)
            ring.set_password("fleetledger", secret_store.oauth_item("k123EXAMPLE"), OAUTH)
            token = {"token_store": "keyring",
                     "keyring_item": secret_store.proxmox_item("fleet@pve!x")}
            api = {"secret_store": "keyring",
                   "keyring_item": secret_store.oauth_item("k123EXAMPLE")}
        else:
            (home / "config" / "proxmox-token").write_text(TOKEN + "\n")
            (home / "config" / "tailscale-oauth").write_text(OAUTH + "\n")
            for f in ("proxmox-token", "tailscale-oauth"):
                (home / "config" / f).chmod(0o600)
            token = {"token_file": str(home / "config" / "proxmox-token")}
            api = {"secret_store": "file",
                   "oauth_secret_file": str(home / "config" / "tailscale-oauth")}
        ed.set_driver("proxmox", {
            "api_url": "https://pve.example.com:8006/api2/json", "node": "pve",
            "pool": "shipshape", "template_vmid": 9000, "seed_storage": "local",
            "disk_storage": "local-lvm", "bridge": "vmbr0", "token_id": "fleet@pve!x",
            "tls_fingerprint": ":".join(["AB"] * 32), **token})
        ed.set_overlay({"provider": "tailscale", "tags": ["tag:ship"],
                        "key_source": "tailscale-api",
                        "api": {"tailnet": "-", "oauth_client_id": "k123EXAMPLE", **api}})
    return [home / "ssh" / "fleet_admin", home / "ssh" / "fleet_host_ca"]


def every_byte(home) -> bytes:
    """Everything FleetLedger keeps on disk (what a backup of the computer would hold):
    its folders and the keys; never the runtime folder, which is RAM."""
    out = b""
    for base in (home / "config", home / "data", home / "ssh", home / "keys"):
        for f in sorted(base.rglob("*")):
            if f.is_file():
                out += f.read_bytes()
    return out


@pytest.mark.parametrize("kind", ["file", "keyring"])
def test_init_moves_every_secret_in_and_a_backup_then_holds_none(store, home, ring, kind):
    managed = secrets_fleet(store, home, ring, kind=kind)
    passphrases = [keys.read_passphrase(k, kind) for k in managed]
    code_seen = []

    def confirm(prompt):
        return code_seen[0] if code_seen else ""

    app, out = app_for(store, PASS, PASS, confirm=confirm)
    app.stdin_isatty = lambda: True
    real = vaultcmd._show_recovery

    def show(app_, code):
        code_seen.append(code.rsplit("-", 1)[-1])
        real(app_, code)

    vaultcmd._show_recovery, saved = show, vaultcmd._show_recovery
    try:
        assert cli.main(["vault", "init"], app) == 0, out.getvalue()
    finally:
        vaultcmd._show_recovery = saved
    said = out.getvalue()
    assert "Moved into it:" in said and "the Proxmox token" in said
    assert "Recovery code (shown once" in said and "Not confirmed" not in said
    doc = store.load().doc
    assert doc["fleet"]["managed_keys"]["store"] == "keyring"
    assert doc["drivers"]["proxmox"]["token_store"] == "keyring"
    assert "token_file" not in doc["drivers"]["proxmox"]
    assert doc["overlay"]["api"]["secret_store"] == "keyring"
    # Every secret reads through FleetLedger's own readers, from the vault.
    from fleetledger.drivers import proxmox_api

    assert [keys.read_passphrase(k, "keyring") for k in managed] == passphrases
    assert proxmox_api.token_secret(doc["drivers"]["proxmox"]) == TOKEN
    assert secret_store.load(doc["overlay"]["api"]) == OAUTH
    # The old copies are gone: no file, nothing left in the OS keyring.
    assert not (home / "config" / "proxmox-token").exists()
    assert not (home / "config" / "key-passphrases").exists() or \
        not any((home / "config" / "key-passphrases").iterdir())
    assert not ring.items
    # A copy of the disk holds no secret in the clear.
    disk = every_byte(home)
    for secret in (TOKEN, OAUTH, PASS, *passphrases):
        assert secret.encode() not in disk
    # ... and nothing opens without the passphrase: locked, every key's passphrase is out
    # of reach, and the keys themselves are encrypted.
    vault.lock()
    with pytest.raises(vault.Locked):
        keys.read_passphrase(managed[0], "keyring")
    assert keys.needs_passphrase(managed[0])


def test_init_changes_nothing_when_a_secret_cannot_be_read(store, home, ring):
    secrets_fleet(store, home, ring, kind="file")
    (home / "config" / "proxmox-token").unlink()
    before = store.path.read_text()
    app, out = app_for(store, PASS, PASS)
    assert cli.main(["vault", "init"], app) == 1
    assert "Cannot read every secret" in out.getvalue() and "the Proxmox token" in out.getvalue()
    assert not vault.is_on() and store.path.read_text() == before


def test_init_needs_a_terminal(store, home):
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store, stdin_isatty=lambda: False)
    assert cli.main(["vault", "init"], app) == 1
    assert "run it in a terminal" in out.getvalue() and not vault.is_on()


def test_unlock_and_lock_on_the_command_line(store, home, monkeypatch):
    vault.create(PASS)
    vault.lock()
    app, out = app_for(store, "wrong passphrase!")
    assert cli.main(["unlock"], app) == 1
    assert "does not open the vault" in out.getvalue()
    app, out = app_for(store, PASS)
    assert cli.main(["unlock"], app) == 0 and "Unlocked" in out.getvalue()
    monkeypatch.setattr("sys.stdin", io.StringIO(PASS + "\n"))
    assert cli.main(["vault", "lock"], app_for(store)[0]) == 0
    assert cli.main(["unlock", "--stdin"], app_for(store)[0]) == 0
    assert vault.state() == "unlocked"
    from fleetledger.concierge import audit

    outcomes = [(e["action"], e["outcome"]) for e in audit.read()]
    assert outcomes == [("vault-unlock", "wrong"), ("vault-unlock", "ok"),
                        ("vault-lock", "ok"), ("vault-unlock", "ok")]
    assert PASS not in (home / "data" / "audit.log").read_text()


def test_locked_every_command_that_acts_refuses_before_it_starts(store, home):
    vault.create(PASS)
    vault.lock()
    for argv in (["ls"], ["update", "ship-01"], ["ca", "sign"], ["migrate", "check"],
                 ["trust", "laptop", "--key", "x.pub"]):
        app, out = app_for(store)
        assert cli.main(argv, app) == 1, argv
        assert "vault is locked, so FleetLedger is read-only" in out.getvalue()
        assert "ship-fleet unlock" in out.getvalue()
    app, out = app_for(store)
    assert cli.main(["ls", "--offline"], app) == 0  # the caches, read-only
    # A copy that came back after a restore elsewhere steps aside without an unlock.
    app, out = app_for(store)
    assert cli.main(["migrate", "retire", "--to", "elsewhere", "--yes"], app) == 0, \
        out.getvalue()
    from fleetledger import handover

    assert handover.marker(store).role == "retired"
    handover.clear(store, "retired")
    for argv in (["vault", "status"], ["doctor", "--offline"]):
        app, out = app_for(store)
        cli.main(argv, app)
        assert "locked" in out.getvalue()
    allowed = {c for c in cli.COMMANDS if c in cli.LOCKED_ALLOWED}
    assert allowed == {"unlock", "vault", "doctor", "ui"} | (
        {"service", "self-update", "backup"} & set(cli.COMMANDS))


def test_nothing_changes_for_an_owner_who_never_turns_it_on(store, home, ring):
    assert vault.state() == "off"
    assert secret_store.secure_backend()[1] == "keyring.backends.SecretService.Keyring"
    assert keys.describe("keyring") == "your OS keyring"
    app, out = app_for(store)
    assert cli.main(["ls", "--offline"], app) == 0
    assert vault.Backend is not type(secret_store.secure_backend()[0])
    app, out = app_for(store)
    assert cli.main(["vault", "status"], app) == 0 and "Vault: off" in out.getvalue()
    app, out = app_for(store)
    assert cli.main(["unlock"], app) == 1 and "nothing to unlock" in out.getvalue()


@pytest.mark.parametrize("to", ["file", "keyring"])
def test_off_moves_every_secret_back_out(store, home, ring, to):
    managed = secrets_fleet(store, home, ring, kind="file")
    passphrases = [keys.read_passphrase(k, "file") for k in managed]
    vault.create(PASS)
    vaultcmd.move_in(cli.App(), store)
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", to, "--yes"], app) == 0, out.getvalue()
    assert not vault.is_on() and vault.state() == "off"
    assert not vault.key_path().exists()
    doc = store.load().doc
    assert doc["fleet"]["managed_keys"]["store"] == to
    assert [keys.read_passphrase(k, to) for k in managed] == passphrases
    from fleetledger.drivers import proxmox_api

    assert proxmox_api.token_secret(doc["drivers"]["proxmox"]) == TOKEN
    assert secret_store.load(doc["overlay"]["api"]) == OAUTH


def test_off_needs_it_unlocked(store, home):
    vault.create(PASS)
    vault.lock()
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--yes"], app) == 2
    assert vault.is_on()


def test_passphrase_and_recovery_on_the_command_line(store, home):
    vault.create(PASS)
    app, out = app_for(store, PASS, "another passphrase", "another passphrase")
    assert cli.main(["vault", "passphrase"], app) == 0, out.getvalue()
    vault.lock()
    vault.unlock("another passphrase")
    app, out = app_for(store, "another passphrase")
    assert cli.main(["vault", "recovery"], app) == 0
    code = re.search(r"\n  ([A-Z2-7-]{39})\n", out.getvalue()).group(1)
    vault.lock()
    vault.unlock(code, recovery=True)


def test_locking_takes_the_managed_keys_out_of_ssh_agent(store, home, monkeypatch, ssh_agent):
    ssh_agent()
    monkeypatch.setattr(keys, "_ADDED", {})
    vault.create(PASS)
    path = home / "ssh" / "fleet_admin"
    keys.make(path, "admin", "keyring")
    with store.edit() as ed:
        keys.record(ed, "keyring", [str(path)])
    keys.ensure_loaded(store.load())
    held = lambda: subprocess.run(["ssh-add", "-l"], capture_output=True,  # noqa: E731
                                  text=True).stdout
    assert keys.fingerprint(path) in held()
    assert cli.main(["vault", "lock"], app_for(store)[0]) == 0
    assert keys.fingerprint(path) not in held()
    with pytest.raises(vault.Locked):
        keys.ensure_loaded(store.load())


# --- the web UI ----------------------------------------------------------------------------


def test_locked_every_page_says_so_and_changes_are_refused(web):  # noqa: F811
    vault.create(PASS)
    vault.lock()
    web.sign_in()
    page = web.client.get("/").text
    assert "FleetLedger is locked." in page and 'href="/unlock"' in page
    response = web.post("/ships/ship-01/refresh")
    assert response.status_code == 303 and response.headers["location"] == "/unlock"
    assert "changes nothing until you unlock it" in web.client.get("/unlock").text
    response = web.post("/ships/ship-01/power", {"action": "reboot", "confirm": "yes"},
                        headers={"HX-Request": "true"})
    assert response.status_code == 423
    assert web.ship.calls == [] or all("reboot" not in str(c) for c in web.ship.calls)


def test_the_poller_does_not_poll_while_locked(web):  # noqa: F811
    vault.create(PASS)
    vault.lock()
    assert web.fleet._may_poll() is False
    vault.unlock(PASS)
    assert web.fleet._may_poll() is True


def test_unlock_in_the_browser(web):  # noqa: F811
    vault.create(PASS)
    vault.lock()
    web.sign_in()
    page = web.client.get("/unlock").text
    assert 'type="password" name="passphrase"' in page
    response = web.post("/unlock", {"with": "passphrase", "passphrase": "wrong passphrase!"})
    assert response.status_code == 303 and response.headers["location"] == "/unlock"
    assert "does not open the vault" in web.client.get("/unlock").text
    assert vault.state() == "locked"
    response = web.post("/unlock", {"with": "passphrase", "passphrase": PASS})
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert vault.state() == "unlocked"
    page = web.client.get("/").text
    assert "Unlocked, until the next boot" in page and "FleetLedger is locked." not in page
    assert PASS not in page


def test_unlock_needs_the_csrf_token_like_every_change(web):  # noqa: F811
    vault.create(PASS)
    vault.lock()
    web.sign_in()
    response = web.client.post("/unlock", data={"with": "passphrase", "passphrase": PASS},
                               headers=ORIGIN)
    assert response.status_code == 403 and vault.state() == "locked"


def test_lock_in_the_browser_and_the_settings_section(web):  # noqa: F811
    vault.create(PASS)
    web.sign_in()
    page = web.client.get("/settings").text
    assert 'id="vault"' in page and "Lock now" in page
    response = web.post("/vault/lock", {})
    assert response.status_code == 303 and vault.state() == "unlocked"  # no tick: nothing
    response = web.post("/vault/lock", {"confirm": "yes"})
    assert response.status_code == 303 and vault.state() == "locked"


def test_with_the_vault_off_the_ui_is_as_ever(web):  # noqa: F811
    web.sign_in()
    page = web.client.get("/").text
    assert "FleetLedger is locked." not in page
    assert 'id="vault"' not in web.client.get("/settings").text
    assert web.fleet._may_poll() is True
    assert "nothing to unlock" in web.client.get("/unlock").text



def test_an_open_page_learns_the_vault_was_locked(web):  # noqa: F811
    vault.create(PASS)
    web.sign_in()
    assert 'hx-get="/partials/banners"' in web.client.get("/").text
    assert "FleetLedger is locked." not in web.client.get("/partials/banners").text
    vault.lock()  # elsewhere: ship-fleet vault lock over ssh, say
    assert "FleetLedger is locked." in web.client.get("/partials/banners").text


# --- the review's fixes -----------------------------------------------------------------------


@pytest.mark.parametrize("role, then", [("standby", ["migrate", "activate", "--yes"]),
                                        ("retired", ["migrate", "unretire", "--yes"])])
def test_a_handed_over_copy_whose_vault_locked_can_unlock_then_move_on(store, home, role,
                                                                       then):
    """After a reboot, a copy on standby (the appliance between receive and activate)
    or retired (the source kept for a rollback) must be able to open its vault."""
    from fleetledger import handover

    vault.create(PASS)
    vault.lock()
    handover.write(store, handover.Marker(role, handover.now(), "elsewhere.example", 1))
    app, out = app_for(store)
    assert cli.main(then, app) == 1 and "vault is locked" in out.getvalue()
    app, out = app_for(store, PASS)
    assert cli.main(["unlock"], app) == 0, out.getvalue()
    app, out = app_for(store)
    assert cli.main(["vault", "status"], app) == 0 and "unlocked" in out.getvalue()
    app, out = app_for(store)
    code = cli.main(then, app)
    said = out.getvalue()
    assert "vault is locked" not in said and "on standby: it has" not in said
    if role == "retired":
        assert code == 0, said
        assert handover.marker(store) is None
    else:  # past both gates; activate then checks every Ship, which are not here
        assert "Not made active: a check failed" in said


def test_unlock_tries_are_one_at_a_time(home, monkeypatch):
    """A burst of wrong tries runs scrypt once: the rest see the count and wait (they
    would otherwise each run scrypt, 128 MiB apiece, and each slip past the wait)."""
    import threading

    vault.create(PASS)
    vault.lock()
    monkeypatch.setattr(vault, "FREE_TRIES", 1)
    runs = []
    real = vault.open_slot

    def slow(slot, secret):
        runs.append(1)
        time.sleep(0.2)
        return real(slot, secret)

    monkeypatch.setattr(vault, "open_slot", slow)
    outcomes = []

    def guess():
        try:
            vault.unlock("not the passphrase at all")
        except FleetError as exc:
            outcomes.append(type(exc).__name__)

    threads = [threading.Thread(target=guess) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(runs) == 1 and vault.tries().count == 1
    assert sorted(outcomes) == ["Waiting"] * 5 + ["WrongSecret"]


def test_your_own_terminal_is_not_held_to_the_wait(store, home, monkeypatch):
    vault.create(PASS)
    vault.lock()
    monkeypatch.setattr(vault, "FREE_TRIES", 1)
    with pytest.raises(vault.WrongSecret):
        vaultcmd.unlock("a guess from the web", account="you@example.com")
    with pytest.raises(vault.Waiting) as waited:
        vaultcmd.unlock(PASS, account="you@example.com")  # the web waits
    assert "ship-fleet ui revoke" in waited.value.hint
    app, out = app_for(store, PASS)
    assert cli.main(["unlock"], app) == 0, out.getvalue()  # your ssh does not
    from fleetledger.concierge import audit

    assert [(e["outcome"], e["account"]) for e in audit.read()] == [
        ("wrong", "you@example.com"), ("waiting", "you@example.com"), ("ok", None)]


def test_off_to_files_changes_nothing_when_a_write_fails(store, home, ring, monkeypatch):
    managed = secrets_fleet(store, home, ring, kind="file")
    vault.create(PASS)
    vaultcmd.move_in(cli.App(), store)
    before = store.path.read_text()
    from fleetledger import paths

    real, calls = paths.write_private, []

    def full(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError(28, "No space left on device")
        return real(path, data)

    monkeypatch.setattr(paths, "write_private", full)
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", "file", "--yes"], app) == 2
    assert "the vault is unchanged" in out.getvalue()
    monkeypatch.setattr(paths, "write_private", real)
    assert vault.is_on() and store.path.read_text() == before
    assert not keys.passphrase_file(managed[0]).exists()  # what it wrote is taken back
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", "file", "--yes"], app) == 0, out.getvalue()
    assert keys.read_passphrase(managed[0], "file")


def test_a_failed_init_is_taken_back_and_can_run_again(store, home, ring, monkeypatch):
    managed = secrets_fleet(store, home, ring, kind="file")
    before = store.path.read_text()

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    real_point_at = vaultcmd.point_at
    monkeypatch.setattr(vaultcmd, "point_at", full)
    app, out = app_for(store, PASS, PASS)
    assert cli.main(["vault", "init"], app) == 1
    assert "It was taken back: nothing changed" in out.getvalue()
    assert not vault.is_on() and not vault.key_path().exists()
    assert store.path.read_text() == before and keys.read_passphrase(managed[0], "file")
    monkeypatch.setattr(vaultcmd, "point_at", real_point_at)
    app, out = app_for(store, PASS, PASS)
    assert cli.main(["vault", "init"], app) == 0, out.getvalue()


def test_a_reused_token_file_goes_into_the_vault(home):
    from fleetledger import proxmox_setup

    vault.create(PASS)
    found = home / "config" / "proxmox-token"
    found.write_text(TOKEN + "\n")
    found.chmod(0o600)
    config = {"token_id": "fleet@pve!x"}
    where = proxmox_setup.keep_secret(config, TOKEN, "file", reused=True, secret_path=found)
    assert "FleetLedger's vault" in where
    assert config["token_store"] == "keyring" and "token_file" not in config
    assert secret_store.read_keyring(config["keyring_item"], "Proxmox token", "") == TOKEN
    assert not found.exists()


def test_a_reused_oauth_file_goes_into_the_vault(store, home):
    from fleetledger import overlay_setup

    vault.create(PASS)
    found = home / "config" / overlay_setup.SECRET_FILE
    found.write_text(OAUTH + "\n")
    found.chmod(0o600)

    class Tailnet:
        granted = frozenset({"auth_keys"})

        def __init__(self, api, secret=None):
            pass

        def token(self):
            return "t"

    app = cli.App(store_factory=lambda: store)
    app.overlay_api = Tailnet
    overlay_setup.connect_oauth(app, store, client_id="k123EXAMPLE", secret=OAUTH, reused=True,
                                store_kind="file", level="keys")
    api = store.load().overlay["api"]
    assert api["secret_store"] == "keyring" and "oauth_secret_file" not in api
    assert secret_store.load(api) == OAUTH and not found.exists()


REAL_IN_RAM = vault.in_ram


def test_in_ram_tells_ram_from_disk_by_the_mount_holding_the_path(home, monkeypatch, tmp_path):
    seen = []

    def mounts(path, mountinfo=None):
        seen.append(path)
        return ("/run/user/1000", "tmpfs") if str(path).startswith("/run/") else ("/", "ext4")

    monkeypatch.setattr(vault, "mount_of", mounts)
    assert REAL_IN_RAM(vault.Path("/run/user/1000/fleetledger/vault/not-yet")) is True
    assert REAL_IN_RAM(tmp_path / "missing" / "deeper") is False
    assert seen[-1] == tmp_path.resolve()  # the nearest folder that exists, resolved
    monkeypatch.setattr(vault, "mount_of", lambda path, mountinfo=None: None)
    assert REAL_IN_RAM(tmp_path) is False  # unknown: never taken for RAM


def test_unlock_never_keeps_the_key_on_disk(home, monkeypatch):
    vault.create(PASS)
    vault.lock()
    monkeypatch.setattr(vault, "in_ram", lambda path: False)
    with pytest.raises(ConfigError, match="not in RAM"):
        vault.unlock(PASS)
    assert vault.state() == "locked" and not vault.key_path().exists()


# --- the re-check's fixes: an interrupted init never loses a secret --------------------------


def _readable(store, managed, passphrases):
    """Every secret, read through FleetLedger's own readers as ships.toml now says, still
    has its value."""
    from fleetledger.drivers import proxmox_api

    doc = store.load().doc
    kind = doc["fleet"]["managed_keys"]["store"]
    assert [keys.read_passphrase(k, kind) for k in managed] == passphrases
    assert proxmox_api.token_secret(doc["drivers"]["proxmox"]) == TOKEN
    assert secret_store.load(doc["overlay"]["api"]) == OAUTH


def _old_copies(home, ring, managed):
    """Old copies of the fleet's secrets outside the vault: 0600 files, OS keyring items."""
    files = [p for p in (*(keys.passphrase_file(k) for k in managed),
                         home / "config" / "proxmox-token", home / "config" / "tailscale-oauth")
             if p.exists()]
    return files + sorted(item for _service, item in ring.items)


@pytest.mark.parametrize("origin", ["keyring", "file"])
@pytest.mark.parametrize("step", ["in-create", "create", "store", "point", "write", "check",
                                  "delete"])
def test_an_init_interrupted_at_any_step_loses_no_secret_and_can_be_finished(
        store, home, ring, monkeypatch, origin, step):
    """A Ctrl-C at each step of vault init. Before ships.toml is written, the vault is
    taken back and every secret is where it was; from that write on, the vault stays and
    holds them all. Either way, vault init again ends with every secret in the vault and
    no old copy left behind."""
    managed = secrets_fleet(store, home, ring, kind=origin)
    passphrases = [keys.read_passphrase(k, origin) for k in managed]
    patched = []

    def patch(obj, name, value):
        patched.append((obj, name, getattr(obj, name)))
        monkeypatch.setattr(obj, name, value)

    calls = []

    def second_call_interrupts(real):
        def wrapped(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(*args, **kwargs)
        return wrapped

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    if step == "in-create":  # vault.json has landed; its RAM key not yet kept
        patch(vault, "_keep", interrupt)
    elif step == "create":  # created, the move not begun
        real_create = vault.create

        def created_then_interrupted(*args, **kwargs):
            real_create(*args, **kwargs)
            raise KeyboardInterrupt

        patch(vault, "create", created_then_interrupted)
    elif step == "store":
        patch(vault.Backend, "set_password", second_call_interrupts(vault.Backend.set_password))
    elif step == "point":  # before anything is written
        patch(vaultcmd, "point_at", interrupt)
    elif step == "write":  # in ships.toml's own write: it may or may not have landed
        from fleetledger import paths

        real_write = paths.write_private

        def interrupted(path, data):
            if path.name == "ships.toml":
                raise KeyboardInterrupt
            return real_write(path, data)

        patch(paths, "write_private", interrupted)
    elif step == "check":
        patch(vaultcmd, "_check_back", interrupt)
    elif origin == "keyring":  # a Ctrl-C during the OS keyring's own delete (D-Bus)
        patch(ring, "delete_password", second_call_interrupts(ring.delete_password))
    else:
        patch(vaultcmd, "_delete_old", second_call_interrupts(vaultcmd._delete_old))
    app, out = app_for(store, PASS, PASS)
    assert cli.main(["vault", "init"], app) == 130, out.getvalue()
    if step in ("in-create", "create", "store", "point"):
        assert not vault.is_on() and not vault.key_path().exists()  # taken back
    else:
        assert vault.is_on() and vault.state() == "unlocked"
        names = vault.backend().item_names("fleetledger")
        assert len(names) == len(managed) + 2  # every secret is in it
    _readable(store, managed, passphrases)
    if step == "write" and origin == "file":  # stored, but ships.toml never named it
        app, out = app_for(store)
        cli.main(["vault", "status"], app)
        assert "in the vault, but read from its file still" in out.getvalue()
        assert "an old copy" not in out.getvalue()

    for obj, name, real in reversed(patched):  # the interrupt is over: run it again
        monkeypatch.setattr(obj, name, real)
    app, out = app_for(store, PASS, PASS)
    assert cli.main(["vault", "init"], app) == 0, out.getvalue()
    assert vault.is_on() and _old_copies(home, ring, managed) == []
    assert len(vault.backend().item_names("fleetledger")) == len(managed) + 2
    _readable(store, managed, passphrases)
    app, out = app_for(store)
    cli.main(["vault", "status"], app)
    assert "old copy" not in out.getvalue() and "NOT in the vault" not in out.getvalue()


def test_status_shows_old_copies_and_init_keeps_one_that_differs(store, home, ring):
    managed = secrets_fleet(store, home, ring, kind="file")
    vault.create(PASS)
    vaultcmd.move_in(cli.App(), store)
    stale = home / "config" / "proxmox-token"
    stale.write_text(TOKEN + "\n")  # an old copy, as an interrupt would leave it
    stale.chmod(0o600)
    other = keys.passphrase_file(managed[0])
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("not the same passphrase\n")
    other.chmod(0o600)
    app, out = app_for(store)
    cli.main(["vault", "status"], app)
    said = out.getvalue()
    assert "an old copy is left behind at ~/" in said or "an old copy is left behind at" in said
    assert "(ship-fleet vault init deletes it)" in said and "differs from the vault's" in said
    app, out = app_for(store)
    assert cli.main(["vault", "init"], app) == 0, out.getvalue()
    assert "deleted 1 old copy left behind" in out.getvalue()
    assert not stale.exists() and other.exists()  # the one that differs is only reported


def test_off_to_files_takes_them_back_when_ships_toml_cannot_be_written(store, home, ring,
                                                                       monkeypatch):
    managed = secrets_fleet(store, home, ring, kind="file")
    vault.create(PASS)
    vaultcmd.move_in(cli.App(), store)
    from fleetledger import paths

    real = paths.write_private

    def full(path, data):
        if path.name == "ships.toml":
            raise OSError(28, "No space left on device")
        return real(path, data)

    monkeypatch.setattr(paths, "write_private", full)
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", "file", "--yes"], app) == 2
    monkeypatch.setattr(paths, "write_private", real)
    assert vault.is_on()
    left = [p for p in (keys.passphrase_file(managed[0]), home / "config" / "proxmox-token",
                        home / "config" / "tailscale-oauth") if p.exists()]
    assert left == []  # no secret left in a file while the vault holds them
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", "file", "--yes"], app) == 0, out.getvalue()


def test_the_web_says_the_way_out_of_the_wait(web, monkeypatch):  # noqa: F811
    vault.create(PASS)
    vault.lock()
    monkeypatch.setattr(vault, "FREE_TRIES", 1)
    web.sign_in()
    web.post("/unlock", {"with": "passphrase", "passphrase": "a wrong guess!!"})
    web.client.get("/unlock")  # that flash shown
    web.post("/unlock", {"with": "passphrase", "passphrase": PASS})
    page = web.client.get("/unlock").text
    assert "Too many wrong tries" in page and "ship-fleet ui revoke" in page


def test_off_cut_short_after_ships_toml_names_the_files_keeps_them_and_runs_again(
        store, home, ring, monkeypatch):
    managed = secrets_fleet(store, home, ring, kind="file")
    passphrases = [keys.read_passphrase(k, "file") for k in managed]
    vault.create(PASS)
    vaultcmd.move_in(cli.App(), store)
    from fleetledger import paths

    real = paths.write_private

    def landed_then_interrupted(path, data):
        real(path, data)
        if path.name == "ships.toml":
            raise KeyboardInterrupt

    monkeypatch.setattr(paths, "write_private", landed_then_interrupted)
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", "file", "--yes"], app) == 130
    monkeypatch.setattr(paths, "write_private", real)
    assert store.load().doc["fleet"]["managed_keys"]["store"] == "file"
    _readable(store, managed, passphrases)  # ships.toml names the files, and they are there
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", "file", "--yes"], app) == 0, out.getvalue()
    assert not vault.is_on()
    _readable(store, managed, passphrases)


def test_off_cut_short_before_the_vault_is_deleted_runs_again(store, home, ring, monkeypatch):
    managed = secrets_fleet(store, home, ring, kind="file")
    passphrases = [keys.read_passphrase(k, "file") for k in managed]
    vault.create(PASS)
    vaultcmd.move_in(cli.App(), store)
    real = vaultcmd.move_out

    def moved_then_interrupted(*args, **kwargs):
        real(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(vaultcmd, "move_out", moved_then_interrupted)
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", "file", "--yes"], app) == 130
    monkeypatch.setattr(vaultcmd, "move_out", real)
    assert vault.is_on()
    _readable(store, managed, passphrases)
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", "file", "--yes"], app) == 0, out.getvalue()
    assert not vault.is_on() and not vault.key_path().exists()
    _readable(store, managed, passphrases)


# --- the second re-check's notes -----------------------------------------------------------


def test_the_finisher_leaves_a_line_in_the_audit_log(store, home, ring):
    from fleetledger.concierge import audit

    secrets_fleet(store, home, ring, kind="file")
    vault.create(PASS)
    vaultcmd.move_in(cli.App(), store)
    stale = home / "config" / "proxmox-token"
    stale.write_text(TOKEN + "\n")
    stale.chmod(0o600)
    assert cli.main(["vault", "init"], app_for(store)[0]) == 0
    assert ("vault-init", "finished") in [(e["action"], e["outcome"]) for e in audit.read()]


def test_a_differing_os_keyring_copy_is_reported_and_kept(store, home, ring):
    secrets_fleet(store, home, ring, kind="keyring")
    vault.create(PASS)
    vaultcmd.move_in(cli.App(), store)
    item = secret_store.proxmox_item("fleet@pve!x")
    ring.set_password("fleetledger", item, "an older token, not the vault's")
    app, out = app_for(store)
    cli.main(["vault", "status"], app)
    assert f"your OS keyring ({item}) and differs from the vault's" in out.getvalue()
    assert cli.main(["vault", "init"], app_for(store)[0]) == 0
    assert ring.items[("fleetledger", item)] == "an older token, not the vault's"  # kept


def test_off_cut_short_right_before_the_delete_stays_unlocked_and_runs_again(store, home, ring,
                                                                           monkeypatch):
    from pathlib import Path

    managed = secrets_fleet(store, home, ring, kind="file")
    passphrases = [keys.read_passphrase(k, "file") for k in managed]
    vault.create(PASS)
    vaultcmd.move_in(cli.App(), store)
    real_unlink = Path.unlink

    def interrupted(self, missing_ok=False):
        if self.name == "vault.json":
            raise KeyboardInterrupt
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", interrupted)
    assert cli.main(["vault", "off", "--store", "file", "--yes"], app_for(store)[0]) == 130
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert vault.state() == "unlocked"  # not locked by the take-back: no unlock needed
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", "file", "--yes"], app) == 0, out.getvalue()
    assert vault.state() == "off" and not vault.key_path().exists()
    _readable(store, managed, passphrases)


def test_off_asks_for_off_in_a_terminal(store, home, ring):
    vault.create(PASS)
    app, out = app_for(store)
    assert cli.main(["vault", "off", "--store", "file"], app) == 1
    assert ("Turning the vault off puts every secret back in 0600 files. Run it in a "
            "terminal to confirm, or add --yes.") in out.getvalue()
    asked = []
    app, out = app_for(store, confirm=lambda prompt: asked.append(prompt) or "no")
    app.stdin_isatty = lambda: True
    assert cli.main(["vault", "off", "--store", "keyring"], app) == 1
    assert "Nothing done." in out.getvalue() and vault.is_on()
    assert asked == ["Move every secret out of the vault into your OS keyring and delete the "
                     "vault? Type off to go on: "]
    app, out = app_for(store, confirm=lambda prompt: " OFF ")  # off in any case
    app.stdin_isatty = lambda: True
    assert cli.main(["vault", "off", "--store", "keyring"], app) == 0, out.getvalue()
    assert not vault.is_on()
