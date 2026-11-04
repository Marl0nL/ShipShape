"""Managed keys (keys.py): real ssh-keygen, ssh-add and a private ssh-agent, a fake
keyring. Nothing here touches your own agent, keys or keyring."""

from __future__ import annotations

import os
import signal
import stat
import subprocess

import pytest
from fleetledger.errors import ConfigError, FleetError
from fleetledger.overlay import secret_store
from fleetledger.store import TomlFileStore
from test_secret_store import fake_backend

from fleetledger import keys


def _agent(sock):
    out = subprocess.run(["ssh-agent", "-a", str(sock)], capture_output=True, text=True,
                         check=True, timeout=10).stdout
    return int(out.split("SSH_AGENT_PID=")[1].split(";")[0])


@pytest.fixture
def agent(tmp_path, monkeypatch):
    """A private ssh-agent for this test only."""
    sock = tmp_path / "agent.sock"
    pid = _agent(sock)
    monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
    yield sock
    os.kill(pid, signal.SIGTERM)


@pytest.fixture
def ring(monkeypatch):
    backend = fake_backend()
    monkeypatch.setattr(secret_store, "get_keyring", lambda: backend)
    return backend


@pytest.fixture
def spawned(monkeypatch):
    """Every command keys.py drives through a terminal (argv only)."""
    seen = []
    real = keys._spawn

    def spy(argv, env=None):
        seen.append(list(argv))
        return real(argv, env)

    monkeypatch.setattr(keys, "_spawn", spy)
    return seen


def held():
    return subprocess.run(["ssh-add", "-l"], capture_output=True, text=True).stdout


def test_a_managed_key_is_encrypted_and_its_passphrase_kept_in_the_keyring(
        home, ring, spawned, tmp_path):
    key = tmp_path / "ssh" / "fleet_admin"
    keys.make(key, "fleetledger-admin", "keyring")
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert keys.needs_passphrase(key)
    passphrase = ring.items[("fleetledger", "key-passphrase:fleet_admin")]
    assert len(passphrase) >= 40
    assert keys.public_of(key, passphrase) == keys.public_line(key)
    assert all(passphrase not in " ".join(argv) for argv in spawned)  # never on a command line
    assert not list(key.parent.glob(".*"))  # no temporary copies left


def test_the_file_store_keeps_the_passphrase_in_a_private_file(home, ring, tmp_path):
    key = tmp_path / "ssh" / "fleet_admin"
    keys.make(key, "fleetledger-admin", "file")
    secret = home / "config" / "key-passphrases" / "fleet_admin"
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret.parent.stat().st_mode) == 0o700
    assert keys.public_of(key, secret.read_text().strip()) == keys.public_line(key)
    assert ring.sets == []


def test_a_key_is_never_overwritten(home, ring, tmp_path):
    key = tmp_path / "ssh" / "fleet_admin"
    keys.make(key, "x", "keyring")
    before = key.read_bytes()
    with pytest.raises(FleetError, match="never overwrites"):
        keys.make(key, "x", "keyring")
    assert key.read_bytes() == before


def test_a_failed_store_leaves_no_key_behind(home, monkeypatch, tmp_path):
    monkeypatch.setattr(secret_store, "get_keyring", lambda: fake_backend(locked=True))
    key = tmp_path / "ssh" / "fleet_admin"
    with pytest.raises(ConfigError):
        keys.make(key, "x", "keyring")
    assert not key.exists() and not list(key.parent.iterdir())


def inventory_with(home, tmp_path, store, *managed):
    path = home / "config" / "ships.toml"
    you = tmp_path / "you.pub"
    if not you.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                        str(tmp_path / "you")], check=True)
    keys_list = ", ".join(f'"{k}"' for k in managed)
    path.write_text(f'schema = 1\n[fleet]\nadmin_key = "{managed[0]}"\n'
                    f'agent_keys = ["{you}"]\n'
                    f'[fleet.managed_keys]\nstore = "{store}"\nkeys = [{keys_list}]\n')
    return TomlFileStore(path).load()


def test_managed_keys_are_loaded_into_the_agent_once(home, ring, agent, spawned, tmp_path):
    admin, user_ca = tmp_path / "ssh" / "admin", tmp_path / "ssh" / "user_ca"
    keys.make(admin, "admin", "keyring")
    keys.make(user_ca, "user-ca", "keyring")
    inv = inventory_with(home, tmp_path, "keyring", admin, user_ca)
    assert keys.ensure_loaded(inv) == ["admin", "user_ca"]
    listed = held()
    assert keys.fingerprint(admin) in listed and keys.fingerprint(user_ca) in listed
    spawned.clear()
    assert keys.ensure_loaded(inv) == []  # already there: nothing asked
    assert spawned == []


def test_a_locked_keyring_is_a_plain_error_and_loads_nothing(home, ring, agent, monkeypatch,
                                                             tmp_path):
    admin = tmp_path / "ssh" / "admin"
    keys.make(admin, "admin", "keyring")
    inv = inventory_with(home, tmp_path, "keyring", admin)
    monkeypatch.setattr(secret_store, "get_keyring", lambda: fake_backend(locked=True))
    with pytest.raises(ConfigError) as exc:
        keys.ensure_loaded(inv)
    assert "Unlock" in exc.value.hint
    assert keys.fingerprint(admin) not in held()


def test_without_an_inventory_table_nothing_is_managed(home, ring, agent, tmp_path):
    path = home / "config" / "ships.toml"
    path.write_text('schema = 1\n[fleet]\nadmin_key = "~/.ssh/x"\nagent_keys = ["~/.ssh/x.pub"]\n')
    assert keys.ensure_loaded(TomlFileStore(path).load()) == []


def test_fleetledger_starts_its_own_agent_when_none_answers(home, ring, monkeypatch, tmp_path):
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "nothing-here.sock"))
    admin = tmp_path / "ssh" / "admin"
    keys.make(admin, "admin", "keyring")
    inv = inventory_with(home, tmp_path, "keyring", admin)
    try:
        assert keys.ensure_loaded(inv) == ["admin"]
        sock = home / "run" / "agent.sock"
        assert os.environ["SSH_AUTH_SOCK"] == str(sock)
        assert stat.S_IMODE((home / "run").stat().st_mode) == 0o700
        assert keys.fingerprint(admin) in held()
    finally:
        subprocess.run(["pkill", "-f", f"ssh-agent -a {home / 'run' / 'agent.sock'}"])


def _key_with(path, passphrase):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(path)],
                   check=True)  # (a test fixture: this one passphrase is not a secret)


@pytest.mark.parametrize("current", ["their own passphrase", ""])
def test_an_existing_key_can_be_taken_over(home, ring, spawned, tmp_path, current):
    key = tmp_path / "ssh" / "fleet_admin"
    _key_with(key, current)
    public = keys.public_line(key)
    keys.adopt(key, current, "keyring")
    new = ring.items[("fleetledger", "key-passphrase:fleet_admin")]
    assert keys.public_of(key, new) == public  # the same key, re-encrypted
    if current:
        with pytest.raises(FleetError):
            keys.public_of(key, current)
    assert all(new not in " ".join(a) and (not current or current not in " ".join(a))
               for a in spawned)
    assert not list(key.parent.glob(".*"))


def test_a_wrong_current_passphrase_changes_nothing(home, ring, tmp_path):
    key = tmp_path / "ssh" / "fleet_admin"
    _key_with(key, "their own passphrase")
    before = key.read_bytes()
    with pytest.raises(FleetError, match="not the key's passphrase"):
        keys.adopt(key, "a wrong guess", "keyring")
    assert key.read_bytes() == before and ring.sets == []
    assert not list(key.parent.glob(".*"))


def test_record_lists_each_key_once(home, tmp_path):
    from fleetledger.store import TomlFileStore as Store

    path = home / "config" / "ships.toml"
    path.write_text('schema = 1\n[fleet]\nadmin_key = "~/.ssh/a"\nagent_keys = ["~/.ssh/b.pub"]\n')
    store = Store(path)
    for _ in range(2):
        with store.edit() as ed:
            keys.record(ed, "keyring", ["~/.ssh/a"])
    assert store.load().doc["fleet"]["managed_keys"] == {"store": "keyring",
                                                         "keys": ["~/.ssh/a"]}
    with pytest.raises(FleetError, match="same store"), store.edit() as ed:
        keys.record(ed, "file", ["~/.ssh/c"])


def test_doctor_checks_managed_keys_without_loading_them(home, ring, agent, tmp_path,
                                                          fake_repo):
    import io

    from fleetledger import cli

    admin = tmp_path / "ssh" / "admin"
    keys.make(admin, "admin", "keyring")
    inventory_with(home, tmp_path, "keyring", admin)
    store = TomlFileStore(home / "config" / "ships.toml")
    out = io.StringIO()
    app = cli.App(out=out, err=io.StringIO(), store_factory=lambda: store,
                  repo_factory=lambda: fake_repo)
    cli.main(["doctor", "--offline"], app)
    text = out.getvalue()
    assert "managed keys" in text and "loaded into ssh-agent when needed" in text
    assert keys.fingerprint(admin) not in held()  # doctor only looks
    passphrase = ring.items[("fleetledger", "key-passphrase:admin")]
    assert passphrase not in out.getvalue()
    ring.items.clear()
    out.seek(0), out.truncate()
    assert cli.main(["doctor", "--offline"], app) == 1
    assert "not in the keyring" in out.getvalue()


def test_ssh_and_signing_load_managed_keys_first(home, monkeypatch, store):
    from fleetledger import cli, trustcmd

    loaded = []
    monkeypatch.setattr(keys, "ensure_loaded", lambda inv, wanted=None: loaded.append(inv))
    app = cli.App(store_factory=lambda: store)
    inv = store.load()
    app.ssh_factory(inv)
    trustcmd._signer(app, inv)
    assert loaded == [inv, inv]
