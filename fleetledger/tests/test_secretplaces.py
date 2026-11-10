"""Where each secret ships.toml names lives: every writer of the store fields agrees."""

from pathlib import Path

import pytest
import tomlkit
from fl_harness import fake_tskey
from fleetledger.overlay import secret_store

from fleetledger import migrate, paths, vaultcmd

FIELDS = {"proxmox": ("token_store", "keyring_item", "token_file"),
          "oauth": ("secret_store", "keyring_item", "oauth_secret_file")}


def ships_toml(store: str) -> str:
    if store == "keyring":
        proxmox = ('token_store = "keyring"\n'
                   'keyring_item = "proxmox-token:shipshape@pve!build"\n')
        api = 'secret_store = "keyring"\nkeyring_item = "tailscale-oauth:kExampleClient1"\n'
    else:
        proxmox = 'token_file = "~/elsewhere/proxmox-token"\n'
        api = 'secret_store = "file"\noauth_secret_file = "~/elsewhere/oauth-secret"\n'
    return ('schema = 1\n\n[fleet]\nadmin_key = "~/.ssh/shipshape_fleet_ed25519"\n'
            'agent_keys = ["~/.ssh/id_ed25519.pub"]\n\n'
            '[drivers.proxmox]\napi_url = "https://pve.example.com:8006/api2/json"\n'
            f'token_id = "shipshape@pve!build"\n{proxmox}node = "pve"\npool = "shipshape"\n'
            'template_vmid = 9000\nseed_storage = "shipshape-seeds"\n'
            'disk_storage = "local-lvm"\nbridge = "vmbr0"\n'
            f'tls_fingerprint = "{":".join(["AB"] * 32)}"\n\n'
            '[overlay]\nprovider = "tailscale"\nkey_source = "tailscale-api"\n\n'
            f'[overlay.api]\noauth_client_id = "kExampleClient1"\n{api}key_expiry_s = 600\n')


def store_fields(doc) -> dict[str, list[tuple[str, str]]]:
    """The store fields of each secret, in their order in the table."""
    tables = {"proxmox": doc["drivers"]["proxmox"], "oauth": doc["overlay"]["api"]}
    return {kind: [(k, str(v)) for k, v in tables[kind].items() if k in FIELDS[kind]]
            for kind in tables}


class Editor:
    def __init__(self, text: str):
        self.doc = tomlkit.parse(text)


def by_point_at(text: str, store_kind: str) -> dict:
    ed = Editor(text)
    moved = [(vaultcmd.Placed(vaultcmd.Secret("the Proxmox token", "proxmox",
                                              secret_store.proxmox_item("shipshape@pve!build")),
                              "keyring", None), "token"),
             (vaultcmd.Placed(vaultcmd.Secret("the overlay's OAuth secret", "oauth",
                                              secret_store.oauth_item("kExampleClient1")),
                              "keyring", None), "secret")]
    vaultcmd.point_at(ed, moved, store_kind)
    for kind, table in (("proxmox", ed.doc["drivers"]["proxmox"]),
                        ("oauth", ed.doc["overlay"]["api"])):
        kept = [k for k in table if k in FIELDS[kind]]
        assert list(table)[-len(kept):] == kept  # re-added at the table's end, as ever
        assert kept == [k for k in FIELDS[kind] if k in kept]  # store, item, file
    return store_fields(ed.doc)


def by_rewrite(text: str, store_kind: str) -> dict:
    out, _ = migrate.rewrite(text, {"keys": [{"role": "admin"},
                                     {"role": "agent", "member": "keys/agent-0"}]},
                             {"keys/admin": Path("/keys/admin"),
                              "keys/agent-0": Path("/keys/agent.pub")}, store_kind,
                             {"proxmox_token": "token", "oauth_secret": "secret"})
    return store_fields(tomlkit.parse(out))


@pytest.mark.parametrize("before", ["keyring", "file"])
@pytest.mark.parametrize("store_kind", ["keyring", "file"])
def test_point_at_and_migrate_write_the_same_store_fields(home, before, store_kind):
    text = ships_toml(before)
    fields = by_point_at(text, store_kind)
    assert {k: dict(v) for k, v in fields.items()} == \
        {k: dict(v) for k, v in by_rewrite(text, store_kind).items()}
    if store_kind == "keyring":
        assert dict(fields["proxmox"]) == {"token_store": "keyring",
                                           "keyring_item": "proxmox-token:shipshape@pve!build"}
    else:
        assert dict(fields["proxmox"]) == {
            "token_file": paths.tilde(paths.config_dir() / "proxmox-token")}
        assert dict(fields["oauth"])["secret_store"] == "file"


def test_each_place_reads_where_its_table_says():
    from fleetledger.secretplaces import OAUTH, PROXMOX

    doc = tomlkit.parse(ships_toml("keyring"))
    assert PROXMOX.location(PROXMOX.table(doc)) == ("keyring",
                                                    "proxmox-token:shipshape@pve!build")
    assert OAUTH.location(OAUTH.table(doc)) == ("keyring", "tailscale-oauth:kExampleClient1")
    doc = tomlkit.parse(ships_toml("file"))
    assert PROXMOX.location(PROXMOX.table(doc)) == ("file", "~/elsewhere/proxmox-token")
    assert OAUTH.location(OAUTH.table(doc)) == ("file", "~/elsewhere/oauth-secret")
    assert PROXMOX.item(PROXMOX.table(doc)) == "proxmox-token:shipshape@pve!build"
    assert PROXMOX.location({"token_store": "keyring"}) is None  # no token id: no secret
    assert OAUTH.location({"oauth_client_id": "c"}) is None  # the file store, but no file
    assert PROXMOX.table({"drivers": {}}) is None


def test_set_keeps_a_field_where_it_is_and_drops_the_other_store(home):
    from fleetledger.secretplaces import OAUTH, PROXMOX

    config = {"token_id": "t", "token_store": "keyring", "keyring_item": "i", "node": "pve"}
    PROXMOX.set(config, "file", file=paths.config_dir() / "proxmox-token")
    assert list(config) == ["token_id", "node", "token_file"]  # no token_store: file
    PROXMOX.set(config, "keyring", item="j")
    assert config == {"token_id": "t", "node": "pve", "token_store": "keyring",
                      "keyring_item": "j"}
    api = {"oauth_client_id": "c"}
    OAUTH.set(api, "file", file=Path("/x/oauth"))
    assert api == {"oauth_client_id": "c", "secret_store": "file", "oauth_secret_file": "/x/oauth"}


def test_vault_names_every_secret_the_table_has(store, home):
    from fleetledger.secretplaces import PLACES

    store.path.write_text(ships_toml("file"))
    inv = store.load()
    named = {p.secret.kind: p for p in vaultcmd.named(inv)}
    assert {p.kind for p in PLACES} <= set(named)
    assert named["proxmox"].store == "file"
    assert named["proxmox"].file == paths.expand("~/elsewhere/proxmox-token")
    assert named["oauth"].secret.item == "tailscale-oauth:kExampleClient1"


def test_vault_names_keyring_secrets_by_the_item_ships_toml_gives(store, home):
    # Items may be stored by hand under other names: the recorded item wins over the id's.
    text = (ships_toml("keyring")
            .replace('keyring_item = "proxmox-token:shipshape@pve!build"',
                     'keyring_item = "proxmox-token:by-hand"')
            .replace('keyring_item = "tailscale-oauth:kExampleClient1"',
                     'keyring_item = "tailscale-oauth:by-hand"'))
    store.path.write_text(text)
    named = {p.secret.kind: p for p in vaultcmd.named(store.load())}
    for kind, item, label in (("proxmox", "proxmox-token:by-hand", "the Proxmox token"),
                              ("oauth", "tailscale-oauth:by-hand",
                               "the overlay's OAuth secret")):
        assert (named[kind].secret.label, named[kind].secret.item) == (label, item)
        assert (named[kind].store, named[kind].file) == ("keyring", None)


def test_the_proxmox_setup_reads_the_old_copy_with_home_expanded(home):
    from fleetledger.proxmox_setup import _secret_location

    assert _secret_location({"token_id": "t", "token_file": "~/x/proxmox-token"}) == (
        "file", str(Path.home() / "x" / "proxmox-token"))
    assert _secret_location({"token_id": "t", "token_store": "keyring",
                             "keyring_item": "i"}) == ("keyring", "i")
    assert _secret_location({"token_id": "t", "token_store": "keyring"}) == ("keyring", "")
    assert _secret_location({"token_file": "~/x/proxmox-token"}) is None  # no token id
    assert _secret_location({"token_id": "t"}) is None  # the file store, but no file


def test_set_records_a_file_under_home_with_a_tilde(home, monkeypatch, tmp_path):
    from fleetledger.secretplaces import OAUTH, PROXMOX

    monkeypatch.setenv("HOME", str(tmp_path))
    config = {"token_id": "t"}
    PROXMOX.set(config, "file", file=tmp_path / "config" / "proxmox-token")
    assert config == {"token_id": "t", "token_file": "~/config/proxmox-token"}
    api = {"oauth_client_id": "c"}
    OAUTH.set(api, "file", file=tmp_path / "config" / "oauth-secret")
    assert api["oauth_secret_file"] == "~/config/oauth-secret"


@pytest.mark.parametrize("store, given", [("keyring", {}), ("file", {}), ("vault", {"item": "i"}),
                                          ("keyring", {"file": Path("/x")}),
                                          ("file", {"item": "i"})])
def test_set_changes_nothing_without_a_place_for_its_store(store, given):
    from fleetledger.secretplaces import OAUTH

    api = {"oauth_client_id": "c", "secret_store": "keyring", "keyring_item": "i"}
    with pytest.raises(ValueError, match="the overlay's OAuth secret"):
        OAUTH.set(api, store, **given)
    assert api == {"oauth_client_id": "c", "secret_store": "keyring", "keyring_item": "i"}


def test_point_at_says_which_table_is_missing():
    from fleetledger.errors import ConfigError

    ed = Editor('schema = 1\n\n[overlay]\nprovider = "tailscale"\n')
    moved = [(vaultcmd.Placed(vaultcmd.Secret("the overlay's OAuth secret", "oauth",
                                              secret_store.oauth_item("kExampleClient1")),
                              "keyring", None), "secret")]
    with pytest.raises(ConfigError, match=r"no \[overlay.api\] table"):
        vaultcmd.point_at(ed, moved, "file")


@pytest.mark.parametrize("store_kind", ["keyring", "file"])
def test_proxmox_setup_records_the_token_as_ever(home, store_kind):
    # Reused, so nothing is stored: only what ships.toml records, with its key order.
    from fleetledger import proxmox_setup

    config = {"api_url": "https://pve.example.com:8006/api2/json",
              "token_id": "shipshape@pve!build", "token_store": "file",
              "keyring_item": "old", "token_file": "~/old", "node": "pve"}
    path = paths.config_dir() / "proxmox-token"
    proxmox_setup.keep_secret(config, "unused", store_kind, reused=True, secret_path=path)
    head = ["api_url", "token_id"]
    if store_kind == "keyring":
        assert list(config.items()) == [
            *((k, config[k]) for k in head), ("token_store", "keyring"),
            ("keyring_item", "proxmox-token:shipshape@pve!build"), ("node", "pve")]
    else:
        assert list(config.items()) == [*((k, config[k]) for k in head),
                                        ("token_file", paths.tilde(path)), ("node", "pve")]


@pytest.mark.parametrize("store_kind", ["keyring", "file"])
def test_overlay_setup_records_the_secret_as_ever(store, home, store_kind):
    from fleetledger import cli, overlay_setup

    class Tailnet:
        granted = frozenset({"auth_keys"})

        def __init__(self, api, secret=None):
            pass

        def token(self):
            return "t"

    app = cli.App(store_factory=lambda: store)
    app.overlay_api = Tailnet
    overlay_setup.connect_oauth(app, store, client_id="kExampleClient1",
                                secret=fake_tskey("client", "kExampleClient1-SECRET"), reused=True,
                                store_kind=store_kind, level="keys")
    api = store.load().overlay["api"]
    file = paths.tilde(paths.config_dir() / overlay_setup.SECRET_FILE)
    where = ([("secret_store", "keyring"), ("keyring_item", "tailscale-oauth:kExampleClient1")]
             if store_kind == "keyring" else
             [("secret_store", "file"), ("oauth_secret_file", file)])
    assert list(api.items()) == [("tailnet", "-"), ("oauth_client_id", "kExampleClient1"),
                                 *where, ("key_expiry_s", 600), ("remove_devices", False),
                                 ("manage_policy", False)]
