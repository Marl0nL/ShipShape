"""Where each secret ships.toml names lives: every writer of the store fields agrees."""

from pathlib import Path

import pytest
import tomlkit
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
