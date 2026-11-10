"""Managed keys (keys.py): real ssh-keygen, ssh-add and a private ssh-agent, a fake
keyring. Nothing here touches your own agent, keys or keyring."""

from __future__ import annotations

import os
import stat
import subprocess

import pytest
from fleetledger.errors import ConfigError, FleetError
from fleetledger.overlay import secret_store
from fleetledger.store import TomlFileStore
from test_secret_store import fake_backend

from fleetledger import keys

DEFAULT_KDF_ROUNDS = keys.KDF_ROUNDS  # read before any fixture lowers it


@pytest.fixture
def agent(ssh_agent):
    """A private ssh-agent for this test only."""
    return ssh_agent()


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
    passphrase = ring.items[("fleetledger", keys.item(key))]
    assert len(passphrase) >= 40
    assert keys.public_of(key, passphrase) == keys.public_line(key)
    assert all(passphrase not in " ".join(argv) for argv in spawned)  # never on a command line
    assert not list(key.parent.glob(".*"))  # no temporary copies left


def test_the_file_store_keeps_the_passphrase_in_a_private_file(home, ring, tmp_path):
    key = tmp_path / "ssh" / "fleet_admin"
    keys.make(key, "fleetledger-admin", "file")
    secret = keys.passphrase_file(key)
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


def test_a_key_the_agent_only_lists_is_still_loaded(home, ring, agent, monkeypatch, tmp_path):
    """GNOME's agent lists every ~/.ssh key that has a .pub, held or not, and using one
    it does not hold pops up a passphrase prompt nobody can answer for a managed key.
    So the list alone never stops FleetLedger adding its keys."""
    admin = tmp_path / "ssh" / "admin"
    keys.make(admin, "admin", "keyring")
    inv = inventory_with(home, tmp_path, "keyring", admin)
    monkeypatch.setattr(keys, "loaded_fingerprints", lambda: {keys.fingerprint(admin)})
    assert keys.fingerprint(admin) not in held()
    assert keys.ensure_loaded(inv) == ["admin"]  # listed, but not trusted
    assert keys.fingerprint(admin) in held()
    assert keys.ensure_loaded(inv) == []  # added just now and still listed
    monkeypatch.setattr(keys, "ADDED_FRESH", 0)
    assert keys.ensure_loaded(inv) == ["admin"]  # added a while ago: again


def test_gnomes_agent_is_told_from_its_socket(tmp_path, monkeypatch):
    for parent in ("keyring", "gcr"):
        (tmp_path / parent).mkdir()
        (tmp_path / parent / "ssh").touch()
        link = tmp_path / f"link-{parent}"  # as a terminal multiplexer might point at it
        link.symlink_to(tmp_path / parent / "ssh")
        monkeypatch.setenv("SSH_AUTH_SOCK", str(link))
        assert keys.desktop_agent_preloads()
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "agent.sock"))
    assert not keys.desktop_agent_preloads()
    monkeypatch.delenv("SSH_AUTH_SOCK")
    assert not keys.desktop_agent_preloads()


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


def test_fleetledger_starts_its_own_agent_when_none_answers(home, ring, monkeypatch, tmp_path,
                                                           short_dir):
    # A runtime folder short enough for the agent's socket: under a long TMPDIR, home's
    # would not be, and FleetLedger would put its agent in the shared /tmp folder.
    runtime = short_dir / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("SSH_AUTH_SOCK", str(tmp_path / "nothing-here.sock"))
    admin = tmp_path / "ssh" / "admin"
    keys.make(admin, "admin", "keyring")
    inv = inventory_with(home, tmp_path, "keyring", admin)
    sock = runtime / "agent.sock"
    try:
        assert keys.ensure_loaded(inv) == ["admin"]
        assert os.environ["SSH_AUTH_SOCK"] == str(sock)
        assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
        assert keys.fingerprint(admin) in held()
    finally:
        subprocess.run(["pkill", "-f", f"ssh-agent -a {sock}"])


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
    new = ring.items[("fleetledger", keys.item(key))]
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
    with pytest.raises(FleetError, match="does not open fleet_admin"):
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
    passphrase = ring.items[("fleetledger", keys.item(admin))]
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


@pytest.fixture
def short_folder():
    """The short /tmp folder these tests may make: removed again if they made it."""
    folder = keys.Path("/tmp") / f"fleetledger-{os.getuid()}"
    existed = folder.exists()
    yield folder
    if not existed:
        os.chmod(folder, 0o700)
        folder.rmdir()


def test_a_runtime_folder_too_long_for_a_socket_uses_a_short_private_one(monkeypatch, tmp_path,
                                                                         short_folder):
    deep = tmp_path / ("x" * 60) / ("y" * 60)
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(deep))
    sock = keys._agent_socket()
    assert len(str(sock)) <= keys.SOCKET_PATH_MAX
    assert sock.parent.stat().st_uid == os.getuid()
    assert stat.S_IMODE(sock.parent.stat().st_mode) == 0o700


def test_a_short_folder_someone_else_can_open_is_refused(monkeypatch, tmp_path, short_folder):
    deep = tmp_path / ("x" * 60) / ("y" * 60)
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(deep))
    short = short_folder
    short.mkdir(mode=0o700, exist_ok=True)
    os.chmod(short, 0o755)
    try:
        with pytest.raises(FleetError, match="not a private folder"):
            keys._agent_socket()
    finally:
        os.chmod(short, 0o700)


def test_a_key_comment_that_says_bad_does_not_read_as_a_failure(home, ring, tmp_path):
    """ssh-keygen -p echoes the key's comment; "me@badger" is not an error."""
    key = tmp_path / "ssh" / "fleet_admin"
    key.parent.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-C", "me@badger-failed-incorrect",
                    "-N", "their own passphrase", "-f", str(key)], check=True)
    public = keys.public_line(key)
    keys.adopt(key, "their own passphrase", "keyring")
    assert keys.public_of(key, ring.items[("fleetledger", keys.item(key))]) == public


def test_a_key_without_its_public_half_is_a_plain_error(home, ring, tmp_path):
    key = tmp_path / "ssh" / "fleet_admin"
    _key_with(key, "")
    key.with_name("fleet_admin.pub").unlink()
    before = key.read_bytes()
    with pytest.raises(FleetError, match="public half"):
        keys.adopt(key, "", "keyring")
    assert key.read_bytes() == before and ring.sets == []


def test_keys_with_the_same_file_name_keep_their_own_passphrases(home, ring, tmp_path):
    one, two = tmp_path / "a" / "id", tmp_path / "b" / "id"
    keys.make(one, "one", "keyring")
    keys.make(two, "two", "keyring")
    assert keys.item(one) != keys.item(two)
    assert keys.public_of(one, keys.read_passphrase(one, "keyring")) == keys.public_line(one)
    assert keys.public_of(two, keys.read_passphrase(two, "keyring")) == keys.public_line(two)


def test_a_short_folder_owned_by_someone_else_is_refused(monkeypatch, tmp_path):
    deep = tmp_path / ("x" * 60) / ("y" * 60)
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(deep))
    other_uid = 4242424
    monkeypatch.setattr(keys.os, "getuid", lambda: other_uid)
    folder = keys.Path("/tmp") / f"fleetledger-{other_uid}"
    try:
        with pytest.raises(FleetError, match="not a private folder"):
            keys._agent_socket()  # it exists (made just now) but belongs to the real uid
    finally:
        folder.rmdir()


def test_handing_over_several_keys_checks_every_passphrase_first(home, ring, tmp_path):
    one, two = tmp_path / "ssh" / "one", tmp_path / "ssh" / "two"
    _key_with(one, "first")
    _key_with(two, "second")
    before = (one.read_bytes(), two.read_bytes())
    path = home / "config" / "ships.toml"
    path.write_text(f'schema = 1\n[fleet]\nadmin_key = "{one}"\nagent_keys = ["{one}.pub"]\n')
    store = TomlFileStore(path)
    with pytest.raises(FleetError, match="Nothing was changed"):
        keys.adopt_all(store, [(one, "first", str(one)), (two, "wrong", str(two))], "keyring")
    assert (one.read_bytes(), two.read_bytes()) == before and ring.sets == []


def test_a_failure_part_way_names_the_keys_already_handed_over(home, ring, monkeypatch,
                                                               tmp_path):
    one, two = tmp_path / "ssh" / "one", tmp_path / "ssh" / "two"
    _key_with(one, "first")
    _key_with(two, "second")
    path = home / "config" / "ships.toml"
    path.write_text(f'schema = 1\n[fleet]\nadmin_key = "{one}"\nagent_keys = ["{one}.pub"]\n')
    store = TomlFileStore(path)
    real = keys.adopt

    def adopt(key, current, managed):
        if key == two:
            raise FleetError("The disk is full.")
        real(key, current, managed)

    monkeypatch.setattr(keys, "adopt", adopt)
    with pytest.raises(FleetError) as exc:
        keys.adopt_all(store, [(one, "first", str(one)), (two, "second", str(two))], "keyring")
    assert "Already handed over: one" in exc.value.message and "keyring" in exc.value.message
    assert keys.managed(store.load())[1] == [one]  # recorded the moment it was handed over


def kdf_rounds(path) -> int:
    """The bcrypt rounds recorded in an OpenSSH private key file."""
    import base64
    import struct

    body = "".join(line for line in path.read_text().splitlines() if "-----" not in line)
    blob = base64.b64decode(body)
    magic = b"openssh-key-v1\0"
    assert blob.startswith(magic)
    pos = len(magic)

    def string():
        nonlocal pos
        (n,) = struct.unpack(">I", blob[pos:pos + 4])
        pos += 4 + n
        return blob[pos - n:pos]

    string()  # cipher
    assert string() == b"bcrypt"
    options = string()
    (salt_len,) = struct.unpack(">I", options[:4])
    (rounds,) = struct.unpack(">I", options[4 + salt_len:8 + salt_len])
    return rounds


def test_managed_keys_use_ssh_keygens_default_rounds_and_tests_lower_them(
        home, ring, tmp_path, monkeypatch):
    assert DEFAULT_KDF_ROUNDS == 16  # what a real FleetLedger uses
    monkeypatch.setattr(keys, "KDF_ROUNDS", 3)
    key = tmp_path / "ssh" / "admin"
    keys.make(key, "admin", "keyring")
    assert kdf_rounds(key) == 3
    other = tmp_path / "ssh" / "theirs"
    _key_with(other, "theirs-passphrase")
    monkeypatch.setattr(keys, "KDF_ROUNDS", 2)
    keys.adopt(other, "theirs-passphrase", "keyring")
    assert kdf_rounds(other) == 2
