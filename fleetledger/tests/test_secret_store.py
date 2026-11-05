"""Where the OAuth secret is kept: the OS keyring (secure backends only) or a file.

Every test uses a fake backend; conftest makes sure no test reaches a real keyring.
"""

from __future__ import annotations

import pytest
from fleetledger.errors import ConfigError
from fleetledger.overlay import secret_store

SECRET = "tskey-client-kExample5CNTRL-KEYRINGSECRET0123"


class Locked(Exception):
    """Stands in for keyring.errors.KeyringLocked."""


def fake_backend(module="keyring.backends.SecretService", name="Keyring", locked=False):
    class Backend:
        def __init__(self):
            self.items = {}
            self.sets = []  # every set_password call

        def get_password(self, service, item):
            if locked:
                raise Locked("Failed to unlock the collection!")
            return self.items.get((service, item))

        def set_password(self, service, item, value):
            self.sets.append((service, item))
            if locked:
                raise Locked("Failed to unlock the collection!")
            self.items[(service, item)] = value

        def delete_password(self, service, item):
            if locked:
                raise Locked("Failed to unlock the collection!")
            del self.items[(service, item)]

        # What keyring's SecretService backend offers for listing (stored_names).
        schemes = {"default": {"username": "username", "service": "service"}}
        scheme = "default"

        def _query(self, service, username=None, **base):
            return {"service": service, **({"username": username} if username else {}),
                    **base}

        def get_preferred_collection(self):
            if locked:
                raise Locked("Failed to unlock the collection!")
            return Collection(self)

    class Item:
        def __init__(self, backend, service, username):
            self.backend, self.attributes = backend, {"service": service,
                                                      "username": username}

        def get_attributes(self):
            return dict(self.attributes)

        def get_secret(self):
            self.backend.secrets_read += 1
            return self.backend.items[(self.attributes["service"],
                                       self.attributes["username"])].encode()

    class Collection:
        def __init__(self, backend):
            self.backend = backend

        def search_items(self, query):
            return [Item(self.backend, service, username)
                    for (service, username) in self.backend.items
                    if all({"service": service, "username": username}.get(k) == v
                           for k, v in query.items())]

    Backend.__module__, Backend.__name__ = module, name
    backend = Backend()
    backend.secrets_read = 0
    return backend


@pytest.fixture
def use(monkeypatch):
    def install(backend):
        monkeypatch.setattr(secret_store, "get_keyring", lambda: backend)
        return backend
    return install


KEYRING_API = {"oauth_client_id": "kExampleClient1", "secret_store": "keyring",
               "keyring_item": "tailscale-oauth:kExampleClient1"}


def test_a_secure_backend_stores_and_reads(use):
    backend = use(fake_backend())
    assert secret_store.keyring_usable()[0]
    secret_store.store_in_keyring("tailscale-oauth:kExampleClient1", SECRET)
    assert backend.items == {("fleetledger", "tailscale-oauth:kExampleClient1"): SECRET}
    assert secret_store.load(KEYRING_API) == SECRET


@pytest.mark.parametrize("module, name", [
    ("keyring.backends.fail", "Keyring"),
    ("keyring.backends.null", "Keyring"),
    ("keyrings.alt.file", "PlaintextKeyring"),
    ("keyrings.alt.file", "EncryptedKeyring"),
])
def test_insecure_backends_are_refused(use, module, name):
    use(fake_backend(module, name))
    usable, why = secret_store.keyring_usable()
    assert not usable and "no secure keyring backend" in why
    with pytest.raises(ConfigError, match="Cannot use the keyring"):
        secret_store.store_in_keyring("x", SECRET)
    with pytest.raises(ConfigError, match="no secure keyring backend"):
        secret_store.load(KEYRING_API)


def test_a_chained_backend_uses_its_secure_member(use):
    secure = fake_backend()

    class Chainer:
        backends = [fake_backend("keyrings.alt.file", "PlaintextKeyring"), secure]

    use(Chainer())
    secret_store.store_in_keyring("item", SECRET)
    assert secure.items == {("fleetledger", "item"): SECRET}


def test_a_locked_keyring_says_how_to_unlock_or_switch(use, tmp_path):
    use(fake_backend(locked=True))
    usable, why = secret_store.keyring_usable()
    assert not usable and "not usable" in why
    with pytest.raises(ConfigError) as exc:
        secret_store.load(KEYRING_API)
    assert "locked" in exc.value.message and "Unlock it" in exc.value.hint
    assert "file instead" in exc.value.hint
    assert SECRET not in str(exc.value) + (exc.value.hint or "")


def test_a_keyring_secret_never_falls_back_to_a_file(use, tmp_path):
    """Even with a readable secret file next to it, a locked keyring is an error."""
    use(fake_backend(locked=True))
    stray = tmp_path / "tailscale-oauth"
    stray.write_text("tskey-client-OTHERFILESECRET\n")
    stray.chmod(0o600)
    api = {**KEYRING_API, "oauth_secret_file": str(stray)}
    with pytest.raises(ConfigError):
        secret_store.load(api)


def test_a_missing_keyring_item(use):
    use(fake_backend())
    with pytest.raises(ConfigError, match="not in the keyring"):
        secret_store.load(KEYRING_API)


def test_no_keyring_library_or_bus(monkeypatch):
    def broken():
        raise ImportError("no module named keyring")

    monkeypatch.setattr(secret_store, "get_keyring", broken)
    usable, why = secret_store.keyring_usable()
    assert not usable and "no keyring is available" in why


def test_the_file_store_is_unchanged(tmp_path):
    secret = tmp_path / "s"
    secret.write_text(SECRET + "\n")
    secret.chmod(0o600)
    assert secret_store.load({"oauth_secret_file": str(secret)}) == SECRET
    secret.chmod(0o640)
    with pytest.raises(ConfigError, match="chmod 600"):
        secret_store.load({"oauth_secret_file": str(secret)})


def test_keyring_errors_never_relay_the_backends_text(use):
    """A backend error could echo the value it was handed; only its kind is shown."""

    class Leaky(Exception):
        pass

    backend = fake_backend()

    def set_password(service, item, value):
        raise Leaky(f"refused value {value}")

    def get_password(service, item):
        raise Leaky(f"locked; last value {SECRET}")

    backend.set_password, backend.get_password = set_password, get_password
    use(backend)
    with pytest.raises(ConfigError) as exc:
        secret_store.store_in_keyring("item", SECRET)
    assert SECRET not in exc.value.message and "Leaky" in exc.value.message
    with pytest.raises(ConfigError) as exc:
        secret_store.load(KEYRING_API)
    assert SECRET not in exc.value.message + (exc.value.hint or "")
    usable, why = secret_store.keyring_usable()
    assert not usable and SECRET not in why


@pytest.mark.parametrize("module, name", [
    ("keyring.backends.fail", "Keyring"),
    ("keyrings.alt.file", "PlaintextKeyring"),
])
def test_an_existing_item_is_only_read_from_a_secure_keyring(use, module, name):
    """Setup offers to reuse an item only from a secure backend: a plaintext keyring
    holding the item is ignored (load() would refuse it later anyway)."""
    backend = use(fake_backend(module, name))
    backend.items[("fleetledger", "tailscale-oauth:kExampleClient1")] = SECRET
    assert secret_store.existing_in_keyring("tailscale-oauth:kExampleClient1") is None


def test_an_existing_item_is_read_from_a_secure_keyring(use):
    backend = use(fake_backend())
    backend.items[("fleetledger", "tailscale-oauth:kExampleClient1")] = SECRET + "\n"
    assert secret_store.existing_in_keyring("tailscale-oauth:kExampleClient1") == SECRET
    assert secret_store.existing_in_keyring("tailscale-oauth:other") is None
    assert backend.sets == []  # looking never writes


def test_a_secret_file_that_cannot_be_read_is_a_plain_error(tmp_path):
    folder = tmp_path / "tailscale-oauth"
    folder.mkdir(mode=0o700)
    with pytest.raises(ConfigError, match="Cannot read"):
        secret_store.read_file(str(folder))


@pytest.mark.parametrize("stored", ["", "   \n"])
def test_an_empty_keyring_item_is_not_a_secret(use, stored):
    backend = use(fake_backend())
    backend.items[("fleetledger", "proxmox-token:x@pve!y")] = stored
    with pytest.raises(ConfigError, match="not in the keyring"):
        secret_store.read_keyring("proxmox-token:x@pve!y", "Proxmox token", "hint")


# --- what is already stored (setup offers it instead of asking again) --------------------------


def test_stored_names_lists_item_names_without_reading_a_secret(use):
    backend = use(fake_backend())
    backend.items = {("fleetledger", "tailscale-oauth:kOne"): "tskey-client-kOne-s1",
                     ("fleetledger", "proxmox-token:shipshape@pve!build"): "a-secret",
                     ("fleetledger", "tailscale-oauth:kTwo"): "tskey-client-kTwo-s2",
                     ("another-app", "tailscale-oauth:kThree"): "not ours"}
    assert secret_store.stored_names("tailscale-oauth:") == ["kOne", "kTwo"]
    assert secret_store.stored_names("proxmox-token:") == ["shipshape@pve!build"]
    assert backend.secrets_read == 0  # names only


def test_stored_names_is_unknown_where_the_keyring_cannot_list(use):
    use(fake_backend("keyring.backends.macOS", "Keyring"))  # secure, but no listing
    assert secret_store.stored_names("tailscale-oauth:") is None
    use(fake_backend(locked=True))
    assert secret_store.stored_names("tailscale-oauth:") is None
    use(fake_backend("keyrings.alt.file", "PlaintextKeyring"))  # refused outright
    assert secret_store.stored_names("tailscale-oauth:") is None


def test_stored_names_is_empty_when_nothing_is_stored(use):
    use(fake_backend())
    assert secret_store.stored_names("tailscale-oauth:") == []
