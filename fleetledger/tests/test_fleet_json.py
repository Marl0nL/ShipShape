"""The ship-fleet --json output another program reads (contracts/ship-fleet.md): real
command output, from the sandbox fleets the other tests use, checked against its schema.

ls and status (fleetledger-ship), attention (fleetledger-attention), signins
(fleetledger-signin), ca show (fleetledger-ca), doctor (fleetledger-doctor) and
snapshot ls (fleetledger-snapshot), online and --offline, with every fleet-level
attention item at once: a machine's certificate lapsed and another in its last day,
the Proxmox token about to expire, and the vault locked.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fl_harness import keygen
from test_ca import _certified_for, caenv, cakeys  # noqa: F401 - the fixtures
from test_cli import (  # noqa: F401 - env is the fixture
    config_snapshot,
    doctor_rows,
    doctor_tools,
    env,
    secure_keyring,
)
from test_ops import locked_vault  # noqa: F401 - the fixture

from fleetledger import (
    appliance,
    commands,
    firstrun,
    lifecycle,
    nextsteps,
    schemas,
    setupcmd,
    sshkeys,
    trustcmd,
)


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
                        ("fleetledger-doctor", setupcmd.SCHEMA),
                        ("next-steps", nextsteps.SCHEMA),
                        ("fleetledger-snapshot", lifecycle.SNAPSHOT_SCHEMA)):
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


def _doctor(e, *argv) -> tuple[int, dict, str]:
    code, out, err = e.run("doctor", *argv)
    doc = json.loads(out)  # one JSON document, nothing else on stdout
    check("fleetledger-doctor", doc)
    assert doc["schema"] == setupcmd.SCHEMA
    return code, doc, err


def test_doctor_json_is_the_text_forms_checks(env, home, monkeypatch):  # noqa: F811
    doctor_tools(env, monkeypatch, loaded=False)
    secure_keyring(monkeypatch)
    before = config_snapshot(home)
    code, doc, err = _doctor(env, "--offline", "--json")
    text_code, out, _ = env.run("doctor", "--offline")
    assert config_snapshot(home) == before  # read-only, as the text form is
    assert code == text_code == 1  # ssh-agent and the CA are missing
    rows = doctor_rows(out)
    assert [i["title"] for i in doc["items"]] == list(rows)  # the same checks, in order
    assert {i["title"]: i["state"] for i in doc["items"]} == {
        t: {"WARN": "warn", "MISSING": "missing", "FAIL": "fail", "-": "skip"}.get(m, m)
        for t, m in rows.items()}
    by_key = {i["key"]: i for i in doc["items"]}
    assert by_key["fleet.ssh-agent"]["state"] == "missing"
    assert by_key["fleet.ssh-agent"]["fix"].startswith("ssh-add ")
    assert "ship-fleet ca init" in by_key["fleet.certificate-authority"]["fix"]
    assert by_key["overlay.overlay"]["state"] == "skip"
    assert all(i["subject"] is None for i in doc["items"])


def test_doctor_json_on_its_own_vm_locked_with_a_lapsed_machine(
        caenv, tmp_path, monkeypatch, locked_vault):  # noqa: F811
    """The appliance wrapper's item, a locked vault, a machine whose certificate lapsed
    and a Ship not enrolled yet: each its own key, the last two with their subject."""
    from test_appliance import _self

    doctor_tools(caenv, monkeypatch, loaded=True)
    secure_keyring(monkeypatch)
    locked_vault.unlock("correct horse battery staple")
    keygen(tmp_path / "laptop", "laptop")
    caenv.run("trust", "laptop", "--key", str(tmp_path / "laptop.pub"), "--scope", "all",
              "--out", str(tmp_path / "b-laptop"))
    _certified_for(caenv.store, "laptop", -1)
    with caenv.store.edit() as ed:
        ed.set_field("ship-02", "enrolled", None)
    _self(caenv.store)
    monkeypatch.setattr("socket.gethostname", lambda: "fleetledger")
    monkeypatch.setattr(appliance, "WRAPPER_PATH", str(tmp_path / "no" / "ship-fleet"))
    locked_vault.lock()
    code, doc, err = _doctor(caenv, "--offline", "--json")
    assert code == caenv.run("doctor", "--offline")[0] == 1  # the wrapper is missing
    items = {(i["key"], i["subject"]): i for i in doc["items"]}
    wrapper = items[("computer.ship-fleet-wrapper", None)]
    assert wrapper["state"] == "missing" and wrapper["fix"] == appliance.WRAPPER_FIX
    assert wrapper["title"] == "ship-fleet on ssh's PATH" and wrapper["area"] == "computer"
    assert items[("computer.vault", None)]["state"] == "warn"
    assert items[("computer.vault", None)]["fix"] == "ship-fleet unlock"
    lapsed = items[("fleet.machine-certificate", "laptop")]
    assert lapsed["state"] == "warn" and lapsed["fix"].startswith("ship-fleet trust laptop")
    assert items[("ships.not-enrolled", "ship-02")]["fix"] == "ship-fleet status ship-02 --wait"
    assert len(items) == len(doc["items"])  # key and subject name one item


def test_doctor_json_without_offline_asks_what_the_text_form_asks(env, monkeypatch):  # noqa: F811
    doctor_tools(env, monkeypatch, loaded=True)
    code, doc, err = _doctor(env, "--json")
    assert code == env.run("doctor")[0]
    assert doc["items"] and "hypervisor.driver" in {i["key"] for i in doc["items"]}


def test_every_doctor_check_has_a_key_in_the_contract():
    """Each Item doctor can make (firstrun.py) has a key the schema lists, and a check
    whose name is not fixed text names its key itself."""
    import ast

    tree = ast.parse(Path(firstrun.__file__).read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Item"):
            continue
        given = {k.arg: k.value for k in node.keywords}
        if "key" in given:
            value = given["key"]
            if isinstance(value, ast.Subscript):  # MACHINE_KEYS[code]: every one it names
                found.update(getattr(firstrun, value.value.id).values())
            else:
                found.add(value.value if isinstance(value, ast.Constant)
                          else getattr(firstrun, value.id))
            continue
        area, name = node.args[:2]
        assert isinstance(name, ast.Constant), ast.unparse(node)  # else: give it key=
        found.add(firstrun.key_of(firstrun.Item(area.value, name.value, "ok")))
    schema = json.loads((Path(__file__).resolve().parents[2] / "contracts"
                         / "fleetledger-doctor.schema.json").read_text())
    assert found == set(schema["$defs"]["item"]["properties"]["key"]["enum"])
    # Every machine item attention can give has its doctor key.
    attention = json.loads((Path(__file__).resolve().parents[2] / "contracts"
                            / "fleetledger-attention.schema.json").read_text())
    codes = {c for c in json.dumps(attention).split('"') if c.startswith("machine-")}
    assert set(firstrun.MACHINE_KEYS) == codes


def test_ca_show_json_has_each_machines_logins_and_finds_one_by_its_key(
        caenv, tmp_path):  # noqa: F811
    for name, logins in (("laptop", "agent"), ("desk", "both")):
        keygen(tmp_path / name, name)
        code, _, err = caenv.run("trust", name, "--key", str(tmp_path / f"{name}.pub"),
                                 "--logins", logins, "--out", str(tmp_path / f"b-{name}"))
        assert code == 0, err
    text = caenv.run("ca", "show")[1]
    doc = documents(caenv, "ca", "show", "--json")
    check("fleetledger-ca", doc)
    assert {m["machine"]: m["logins"] for m in doc["machines"]} == {
        "laptop": ["agent"], "desk": ["agent", "owner"]}
    assert "logins" not in text and "agent" not in text  # the text form is as it was
    # A machine finds its own live entry, and its trusted name, by its key's fingerprint,
    # even beside a revoked entry with the same key (trust --this-computer after an
    # untrust adds one under a new name).
    key = (tmp_path / "laptop.pub").read_text()
    with caenv.store.edit() as ed:
        ed.set_machine("old-laptop", {"public_key": key.strip(), "added": "2026-01-01T00:00:00Z",
                                      "scope": ["all"], "logins": ["agent"],
                                      "revoked": "2026-01-02T00:00:00Z"})
    doc = documents(caenv, "ca", "show", "--json")
    check("fleetledger-ca", doc)
    mine = [m for m in doc["machines"] if m["fingerprint"] == sshkeys.fingerprint(key)]
    assert sorted(m["machine"] for m in mine) == ["laptop", "old-laptop"]
    assert [m["machine"] for m in mine if m["revoked"] is None] == ["laptop"]
    live = [m["fingerprint"] for m in doc["machines"] if m["revoked"] is None]
    assert len(set(live)) == len(live)


def test_doctor_json_names_each_managed_key_that_fails(tmp_path, monkeypatch):
    """One fail item per managed key gone, each with the key file's path as its subject, so
    key and subject still name one item."""
    from fleetledger import keys

    # Two keys of one name in two folders, one with a space: each its own item.
    gone = [tmp_path / "admin" / "id_ed25519", tmp_path / "ca key" / "id_ed25519"]
    monkeypatch.setattr(keys, "managed", lambda inv: (None, gone))
    doc = setupcmd.as_json(firstrun._managed_items(None))
    check("fleetledger-doctor", doc)
    assert [(i["key"], i["subject"], i["state"]) for i in doc["items"]] == [
        ("fleet.managed-keys", str(gone[0]), "fail"),
        ("fleet.managed-keys", str(gone[1]), "fail")]
    monkeypatch.setattr(keys, "managed", lambda inv: (None, []))
    monkeypatch.setattr(keys, "describe", lambda store: "the keyring")
    [ok] = setupcmd.as_json(firstrun._managed_items(None))["items"]
    assert ok["subject"] is None and ok["state"] == "ok"
    bad = {"schema": setupcmd.SCHEMA, "items": [dict(ok, subject="x")]}
    assert schemas.errors("fleetledger-doctor", bad) != []  # ok: no subject


def test_snapshot_ls_json_lists_what_the_text_shows(env, monkeypatch):  # noqa: F811
    from fleetledger.drivers import Snapshot

    env.run("new", "ship-04", "--on", "proxmox")
    taken = [Snapshot("before", "2026-11-01T00:00:00Z", "a note"),
             Snapshot("snap-20261102-071500", None)]
    monkeypatch.setattr(env.driver, "snapshots", lambda ship: taken)
    snaps = check_each("fleetledger-snapshot",
                       documents(env, "snapshot", "ls", "ship-04", "--json"))
    assert snaps == [
        {"schema": lifecycle.SNAPSHOT_SCHEMA, "ship": "ship-04", "name": "before",
         "taken": "2026-11-01T00:00:00Z", "note": "a note"},
        {"schema": lifecycle.SNAPSHOT_SCHEMA, "ship": "ship-04",
         "name": "snap-20261102-071500", "taken": None, "note": ""}]
    code, out, err = env.run("snapshot", "ls", "ship-04")  # the text is as it was
    assert code == 0 and out.splitlines() == [
        "SNAPSHOT              TAKEN                 NOTE",
        "before                2026-11-01T00:00:00Z  a note",
        "snap-20261102-071500  -"]
    monkeypatch.setattr(env.driver, "snapshots", lambda ship: [])
    assert documents(env, "snapshot", "ls", "ship-04", "--json") == []
    assert env.run("snapshot", "ls", "ship-04")[1].strip() == "ship-04 has no snapshots."


def test_snapshot_ls_json_errors_go_to_stderr(env):  # noqa: F811
    env.run("new", "ship-04", "--on", "proxmox")
    for argv, exits, said in ((("ship-01",), 1, "cannot list snapshots"),  # manual
                              (("ship-09",), 2, "No Ship named 'ship-09'"),
                              (("ship-04", "before"), 1, "takes only the Ship's name")):
        code, out, err = env.run("snapshot", "ls", *argv, "--json")
        assert (code, out) == (exits, "") and said in err
    for action in ("take", "rollback", "rm"):
        code, out, err = env.run("snapshot", action, "ship-04", "before", "--yes", "--json")
        assert (code, out) == (1, "") and "only snapshot ls" in err
    assert env.driver.snaps == []  # refused before the driver was asked anything
