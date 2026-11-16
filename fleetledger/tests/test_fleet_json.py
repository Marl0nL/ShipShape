"""The ship-fleet --json output another program reads (contracts/ship-fleet.md): real
command output, from the sandbox fleets the other tests use, checked against its schema.

ls and status (fleetledger-ship), attention (fleetledger-attention), signins
(fleetledger-signin) and ca show (fleetledger-ca), online and --offline, with every
fleet-level attention item at once: a machine's certificate lapsed and another in its
last day, the Proxmox token about to expire, and the vault locked.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fl_harness import keygen
from test_ca import _certified_for, caenv, cakeys  # noqa: F401 - the fixtures
from test_cli import env  # noqa: F401 - the fixture
from test_ops import locked_vault  # noqa: F401 - the fixture

from fleetledger import commands, nextsteps, schemas, trustcmd


def documents(e, *argv) -> object:
    code, out, err = e.run(*argv)
    assert code == 0, err
    return json.loads(out)  # one JSON document, nothing else on stdout


def check(name: str, doc: object) -> None:
    assert schemas.errors(name, doc) == []


def check_each(name: str, docs: object) -> list[dict]:
    assert isinstance(docs, list)
    for doc in docs:
        check(name, doc)
    return docs


def test_ls_and_status_name_their_shape(env):  # noqa: F811
    env.ship.down.add("ship-02")
    ships = check_each("fleetledger-ship", documents(env, "ls", "--json"))
    assert [(s["name"], s["state"], s["problem"]) for s in ships] == [
        ("ship-01", "running", None), ("ship-02", "unreachable", "unreachable")]
    assert {s["schema"] for s in ships} == {commands.SHIP_SCHEMA}
    check("ship-status", ships[0]["status"])  # the Ship's own document, as it is
    offline = check_each("fleetledger-ship", documents(env, "ls", "--json", "--offline"))
    assert [(s["state"], s["problem"]) for s in offline] == [
        ("running", None), ("unknown", "unknown")]  # ship-02 was never fetched
    check("fleetledger-ship", documents(env, "status", "ship-01", "--json"))


def test_attention_and_signins_name_their_shape(env):  # noqa: F811
    env.ship.down.add("ship-02")
    for argv in (("attention", "--json"), ("attention", "--offline", "--json")):
        items = check_each("fleetledger-attention", documents(env, *argv))
        assert items and {i["schema"] for i in items} == {commands.ATTENTION_SCHEMA}
    for argv in (("signins", "--json"), ("signins", "--offline", "--json")):
        rows = check_each("fleetledger-signin", documents(env, *argv))
        assert rows and {r["schema"] for r in rows} == {commands.SIGNIN_SCHEMA}
        assert {r["group"] for r in rows} >= {"healthy", "degraded"}


def test_every_fleet_item_at_once_and_while_locked(caenv, tmp_path, locked_vault):  # noqa: F811
    locked_vault.unlock("correct horse battery staple")
    for name in ("laptop", "desk"):
        keygen(tmp_path / name, name)
        caenv.run("trust", name, "--key", str(tmp_path / f"{name}.pub"), "--scope", "tag:work",
                  "--out", str(tmp_path / f"b-{name}"))
    _certified_for(caenv.store, "laptop", 5)
    _certified_for(caenv.store, "desk", -1)
    soon = (datetime.now(UTC).date() + timedelta(days=3)).isoformat()
    with caenv.store.edit() as ed:
        ed.set_driver("proxmox", {
            "api_url": "https://pve.example.com:8006/api2/json", "node": "pve",
            "pool": "shipshape", "template_vmid": 9000, "seed_storage": "s",
            "disk_storage": "d", "bridge": "vmbr0", "token_id": "shipshape@pve!fleet",
            "token_file": "~/t", "tls_fingerprint": ":".join(["AB"] * 32),
            "token_expires": soon})
    ca = documents(caenv, "ca", "show", "--json")
    check("fleetledger-ca", ca)
    assert ca["schema"] == trustcmd.CA_SCHEMA
    assert {m["machine"]: m["state"] for m in ca["machines"]} == {
        "laptop": "expiring", "desk": "lapsed"}
    assert documents(caenv, "attention", "--json")  # fills each Ship's cache
    locked_vault.lock()
    items = check_each("fleetledger-attention",
                       documents(caenv, "attention", "--offline", "--json"))
    fleet = {(i["code"], i["subject"]) for i in items if i["ship"] is None}
    assert fleet == {("vault-locked", "vault"), ("machine-cert-lapsed", "desk"),
                     ("machine-cert-expiring", "laptop"),
                     ("proxmox-token-expiring", "shipshape@pve!fleet")}
    check_each("fleetledger-ship", documents(caenv, "ls", "--offline", "--json"))
    check_each("fleetledger-signin", documents(caenv, "signins", "--offline", "--json"))


@pytest.mark.parametrize("argv", [("ls", "--json"), ("attention", "--json"),
                                  ("signins", "--json"), ("attention", "--offline", "--json"),
                                  ("signins", "--offline", "--json")])
def test_no_ships_is_an_empty_list_not_words(env, argv):  # noqa: F811
    with env.store.edit() as ed:
        for name in ("ship-01", "ship-02"):
            ed.remove_ship(name)
    assert documents(env, *argv) == []
    words = env.run(*[a for a in argv if a != "--json"])[1]
    assert words.strip() == "No Ships yet."  # the text is as it was


def test_one_version_convention():
    """Every document names itself shipshape.<its contract>/<version>, which is the
    const in that contract's schema."""
    for name, value in (("fleetledger-ship", commands.SHIP_SCHEMA),
                        ("fleetledger-attention", commands.ATTENTION_SCHEMA),
                        ("fleetledger-signin", commands.SIGNIN_SCHEMA),
                        ("fleetledger-ca", trustcmd.CA_SCHEMA),
                        ("next-steps", nextsteps.SCHEMA)):
        assert value == f"shipshape.{name}/1"
        assert schemas.errors(name, {"schema": value}) != []  # the rest is checked too
        assert any("schema" in e for e in schemas.errors(name, {"schema": "x/1"}))


def test_a_ships_status_in_the_examples_is_a_status_document():
    folder = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "fleetledger-ship"
    shown = [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]
    assert any(doc["status"] for doc in shown)
    for doc in shown:
        if doc["status"] is not None:
            check("ship-status", doc["status"])
