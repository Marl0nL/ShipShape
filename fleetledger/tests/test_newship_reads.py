"""The reads a New Ship form in another program needs (contracts/ship-fleet.md):
`tools ls --json`, `image ls --json` and `defaults --json`. Each matches its schema, says
what `new` itself would use, and agrees with the web form's own choices (web/newship.py)."""

from __future__ import annotations

import json

import pytest
from fleetledger.web import newship
from test_cli import IMG, add_image, env, proxmox_inventory  # noqa: F401  (fixtures)

from fleetledger import images, newdefaults, schemas


def read(env, *argv):  # noqa: F811
    code, out, err = env.run(*argv)
    assert code == 0, err
    return json.loads(out)


def test_tools_ls_json_lists_the_catalogue_as_tools_ls_does(env):  # noqa: F811
    doc = read(env, "tools", "ls", "--json")
    assert schemas.errors("fleetledger-tools", doc) == []
    assert doc["schema"] == "shipshape.fleetledger-tools/1"
    assert [t["id"] for t in doc["tools"]] == ["gh-axi", "ship-axi", "tasks-axi"]
    assert {t["id"]: t["default"] for t in doc["tools"]} == {
        "gh-axi": True, "ship-axi": True, "tasks-axi": False}
    gh = doc["tools"][0]
    assert gh == {"id": "gh-axi", "kind": "npm", "description": "GitHub CLI for agents.",
                  "default": True, "needs": ["github"], "toolkit": None, "only_tags": None}
    assert doc["profiles"] == [{"id": "orchestrator",
                                "description": "An orchestration repo and its tools.",
                                "tools": ["tasks-axi"], "toolkit": None, "only_tags": None}]
    assert doc["broken"] == []
    # The table is as it was.
    code, out, _ = env.run("tools", "ls")
    assert code == 0 and out.splitlines()[0].split() == [
        "TOOL", "KIND", "DEFAULT", "NEEDS", "DESCRIPTION"]
    assert not out.lstrip().startswith("{")


def test_image_ls_json_lists_images_and_each_drivers_choices(env, home):  # noqa: F811
    doc = read(env, "image", "ls", "--json")
    assert schemas.errors("fleetledger-images", doc) == []
    assert doc == {"schema": "shipshape.fleetledger-images/1", "pinned": "stock", "images": [],
                   "drivers": [{"id": "manual", "images": ["stock"]}]}
    add_image(env, published={"proxmox": {"template_vmid": 9181}})
    proxmox_inventory(env, home)
    with env.store.edit() as ed:
        ed.set_default("image", IMG)
    doc = read(env, "image", "ls", "--json")
    assert schemas.errors("fleetledger-images", doc) == []
    assert doc["pinned"] == IMG
    assert doc["drivers"] == [{"id": "manual", "images": ["stock"]},
                              {"id": "proxmox", "images": ["stock", IMG]}]
    (image,) = doc["images"]
    assert image == images.listing(env.store.load())[0]
    assert image["published"] == {"proxmox": {"template_vmid": 9181}} and image["pinned"]
    # The web form offers the same drivers, and the same images on each.
    inv = env.store.load()
    nf = newship.choices(env.app, inv)
    assert nf.drivers == [d["id"] for d in doc["drivers"]]
    for d in doc["drivers"]:
        assert newship.images_for(inv, d["id"]) == d["images"]
    # The table is as it was.
    code, out, _ = env.run("image", "ls")
    assert code == 0 and out.split()[:2] == ["IMAGE", "UBUNTU"]


def test_json_is_only_for_the_listings(env):  # noqa: F811
    code, _, err = env.run("tools", "add", "ship-01", "tasks-axi", "--json")
    assert code == 1 and "--json is for tools ls" in err
    code, _, err = env.run("image", "use", "stock", "--json")
    assert code == 1 and "--json is for image ls" in err


def test_defaults_json_is_what_new_uses(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TZ", "America/Argentina/Buenos_Aires")
    doc = read(env, "defaults", "--json")
    assert schemas.errors("fleetledger-defaults", doc) == []
    assert doc == {"schema": newdefaults.SCHEMA, "driver": "manual", "image": "stock",
                   "tags": ["work"], "size": {"cores": 4, "memory_mb": 8192, "disk_gb": 40},
                   "mode": "auto", "allow_bypass": False, "tools": ["gh-axi", "ship-axi"],
                   "tools_from": "catalogue", "profiles": [],
                   "timezone": "America/Argentina/Buenos_Aires"}
    # The web form shows the same defaults.
    nf = newship.choices(env.app, env.store.load())
    assert (nf.driver, nf.image, nf.tags, nf.size, nf.mode, nf.timezone) == (
        doc["driver"], doc["image"], doc["tags"], doc["size"], doc["mode"], doc["timezone"])
    assert [t for t, _, on in nf.tools if on] == doc["tools"]
    # A Ship made with no options gets exactly those.
    code, _, err = env.run("new", "ship-09")
    assert code == 0, err
    inv = env.store.load()
    ship = inv.ship("ship-09")
    from fleetledger.catalogue import Catalogue
    from fleetledger.seed.shiptoml import profiles_for, tools_for

    assert (ship.driver, ship.image, list(inv.effective_tags(ship)), inv.effective_size(ship),
            inv.effective_claude(ship).get("default_mode", "auto")) == (
        doc["driver"], doc["image"], doc["tags"], doc["size"], doc["mode"])
    assert tools_for(inv, ship, Catalogue.from_repo(env.app.repo_factory())) == doc["tools"]
    assert profiles_for(inv, ship) == doc["profiles"]


def test_defaults_from_ships_toml(env, monkeypatch):  # noqa: F811
    monkeypatch.setenv("TZ", "UTC")
    with env.store.edit() as ed:
        ed.doc["defaults"]["tools"] = ["tasks-axi"]
        ed.doc["defaults"]["profiles"] = ["orchestrator"]
        ed.doc["defaults"]["size"] = {"cores": 8, "disk_gb": 80}
        ed.doc["defaults"]["claude"] = {"default_mode": "bypassPermissions",
                                        "allow_bypass": True}
        ed.doc["defaults"]["tags"] = []
    doc = read(env, "defaults", "--json")
    assert schemas.errors("fleetledger-defaults", doc) == []
    assert (doc["tools"], doc["tools_from"], doc["profiles"]) == (
        ["tasks-axi"], "defaults", ["orchestrator"])
    assert doc["size"] == {"cores": 8, "memory_mb": 8192, "disk_gb": 80}
    assert (doc["mode"], doc["allow_bypass"], doc["tags"]) == ("bypassPermissions", True, [])
    code, out, _ = env.run("defaults")
    assert code == 0
    assert "A new Ship gets, unless ship-fleet new says otherwise:" in out
    assert "8 vCPUs, 8192 MB, 80 GB disk" in out and "tasks-axi (ships.toml defaults.tools)" in out
    assert "bypassPermissions (bypassPermissions allowed)" in out and "UTC (this computer's)" in out


def test_a_default_mode_of_bypass_alone_does_not_allow_it(env):  # noqa: F811
    """Only new's own --mode bypassPermissions implies --allow-bypass: defaults.claude with
    that mode and no allow_bypass makes a Ship new refuses, so it is not reported allowed."""
    with env.store.edit() as ed:
        ed.doc["defaults"]["claude"] = {"default_mode": "bypassPermissions"}
    doc = read(env, "defaults", "--json")
    assert (doc["mode"], doc["allow_bypass"]) == ("bypassPermissions", False)
    assert env.run("new", "ship-09")[0] != 0  # as new says: it needs allow_bypass too


@pytest.mark.parametrize("argv", [["tools", "ls", "--json"], ["image", "ls", "--json"],
                                  ["defaults", "--json"]])
def test_the_reads_ask_no_ship(env, argv):  # noqa: F811
    calls = len(env.ship.calls)
    read(env, *argv)
    assert len(env.ship.calls) == calls
