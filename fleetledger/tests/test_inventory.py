import os
import shutil
import tomllib
from pathlib import Path

import pytest
import tomlkit
from fleetledger.errors import ConfigError, FleetError, NotFound
from fleetledger.inventory import ExpectedAccount, Ship
from fleetledger.store import TomlFileStore, new_inventory, ship_entry, validate

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"


def test_contract_example_loads(tmp_path):
    path = tmp_path / "ships.toml"
    shutil.copy(CONTRACTS / "examples" / "ships.toml" / "basic.toml", path)
    inv = TomlFileStore(path).load()
    assert [s.name for s in inv.ships()] == ["ship-01", "ship-02"]
    ship = inv.ship("ship-01")
    assert ship.driver_ref == {"vmid": 9101}
    assert ship.enrolled == "2026-01-01T00:05:00Z"
    assert inv.driver_config("proxmox")["pool"] == "shipshape"


def test_tags_and_selection(store):
    inv = store.load()
    assert [s.name for s in inv.ships("work")] == ["ship-01"]
    assert [s.name for s in inv.select(tag="personal")] == ["ship-02"]
    assert [s.name for s in inv.select(["ship-02", "ship-01"])] == ["ship-02", "ship-01"]
    assert inv.all_tags() == ["personal", "work"]
    with pytest.raises(NotFound, match="ship-09"):
        inv.ship("ship-09")
    with pytest.raises(NotFound, match="tag 'gpu'"):
        inv.select(tag="gpu")


def test_expected_accounts(store):
    inv = store.load()
    work = inv.ship("ship-01")
    personal = inv.ship("ship-02")
    assert inv.expected_account(work, "claude").accounts == {"work@example.com"}
    # gcloud ADC follows the gcloud account.
    assert inv.expected_account(work, "gcloud_adc").accounts == {"work@example.com"}
    # personal names no gcloud account: nothing to compare.
    assert inv.expected_account(personal, "gcloud").matches("x@example.com") is None

    exp = inv.expected_account(work, "claude")
    assert exp.matches("Work@Example.com ") is True
    assert exp.matches("you@example.com") is False
    assert exp.matches(None) is None


def test_conflicting_tags_never_match():
    exp = ExpectedAccount("claude", frozenset({"a@example.com", "b@example.com"}))
    assert exp.conflicting
    assert exp.matches("a@example.com") is False


def test_effective_settings(store):
    inv = store.load()
    assert inv.effective_claude(inv.ship("ship-01")) == {
        "remote_control": "on",
        "default_mode": "auto",
    }
    assert inv.effective_claude(inv.ship("ship-02"))["default_mode"] == "bypassPermissions"
    assert inv.effective_size() == {"cores": 4, "memory_mb": 8192, "disk_gb": 40}
    assert inv.admin_public_key_path.name == "fleet_admin.pub"
    assert inv.known_hosts.name == "known_hosts"


def test_edit_keeps_comments_and_mode(store):
    with store.edit() as ed:
        ed.set_tags("ship-01", ["work", "gpu", "work"])
        ed.mark_enrolled("ship-01", "2026-02-01T00:00:00Z")
        ed.set_nested("ship-01", "driver_ref", "vmid", 9101)
    text = store.path.read_text()
    assert text.startswith("# My fleet (a comment that must survive edits)")
    ship = store.load().ship("ship-01")
    assert ship.tags == ("gpu", "work")
    assert ship.enrolled == "2026-02-01T00:00:00Z"
    assert ship.driver_ref == {"vmid": 9101}
    assert os.stat(store.path).st_mode & 0o777 == 0o600

    with store.edit() as ed:
        ed.set_nested("ship-01", "driver_ref", "vmid", None)
    assert "driver_ref" not in tomllib.loads(store.path.read_text())["ship"]["ship-01"]


def test_add_and_remove_ship(store):
    ship = Ship(
        name="ship-03",
        id="0b9f6c1e-7d7a-4d4c-8f39-2f1d7e1f0c11",
        driver="manual",
        names=("ship-03.local",),
        host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHNoaXAwM2V4YW1wbGVob3N0a2V5c2hpcDAzZXhh",
        tags=("work",),
        created="2026-02-01T00:00:00Z",
        claude={"default_mode": "auto"},
    )
    with store.edit() as ed:
        ed.add_ship("ship-03", ship_entry(ship))
    assert store.load().ship("ship-03") == ship
    with pytest.raises(FleetError, match="already"):
        with store.edit() as ed:
            ed.add_ship("ship-03", ship_entry(ship))
    with store.edit() as ed:
        ed.remove_ship("ship-03")
    assert not store.load().has_ship("ship-03")


def test_invalid_edit_is_not_written(store):
    before = store.path.read_text()
    with pytest.raises(ConfigError, match="ships.toml"):
        with store.edit() as ed:
            ed.set_tags("ship-01", ["Not A Tag"])
    assert store.path.read_text() == before


def test_duplicate_names_and_keys_rejected(store):
    with pytest.raises(ConfigError, match="also used by"):
        with store.edit() as ed:
            ed.set_field("ship-02", "names", ["ship-01.local"])
    key = store.load().ship("ship-01").host_key
    with pytest.raises(ConfigError, match="same host key"):
        with store.edit() as ed:
            ed.set_field("ship-02", "host_key", key)


def test_new_inventory_is_valid(home):
    doc = new_inventory("~/.ssh/fleet_admin", ["~/.ssh/you.pub"])
    validate(tomllib.loads(tomlkit.dumps(doc)))
    store = TomlFileStore(home / "config" / "ships.toml")
    store.create(doc)
    assert store.load().ships() == []
    with pytest.raises(FleetError, match="already exists"):
        store.create(doc)


def test_missing_inventory_hint(home):
    with pytest.raises(ConfigError) as exc:
        TomlFileStore(home / "config" / "nope.toml").load()
    assert "ship-fleet init" in exc.value.hint
