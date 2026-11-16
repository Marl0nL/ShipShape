"""ship-fleet doctor --next: the checklist after setup (nextsteps.py), in text and JSON.

Every state of every step is read from files under the test's home: the inventory,
the status cache, this computer's trust record, the remote UI setting, the service
unit and the vault. The keyring is the fake from test_secret_store; doctor's local
commands (ssh-keygen, ssh-add) are faked by test_cli.doctor_tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fl_harness import keygen
from jsonschema import Draft202012Validator
from test_ca import _certified_for, _with_ca, cakeys  # noqa: F401 - the fixture
from test_cli import doctor_tools, env, secure_keyring  # noqa: F401 - the fixture

from fleetledger import nextsteps, paths, service, status, trustcmd, vault

SCHEMA = Path(__file__).resolve().parents[2] / "contracts" / "next-steps.schema.json"
KEYS = [s for s in json.loads(SCHEMA.read_text())["$defs"]["step"]["properties"]["key"]["enum"]]


@pytest.fixture
def nx(env, home, tmp_path, monkeypatch):  # noqa: F811
    """A set-up fleet (two enrolled Ships, manual driver), the admin key in ssh-agent."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(vault, "SCRYPT_N", 2 ** 10)
    monkeypatch.setattr(vault, "in_ram", lambda path: True)
    doctor_tools(env, monkeypatch)
    env.ring = secure_keyring(monkeypatch)
    return env


def run_json(e) -> dict:
    code, out, err = e.run("doctor", "--next", "--json")
    assert code == 0, err
    doc = json.loads(out)
    validator = Draft202012Validator(json.loads(SCHEMA.read_text()))
    errors = [f"{list(x.path)}: {x.message}" for x in validator.iter_errors(doc)]
    assert not errors, errors
    return doc


def states(e) -> dict[str, tuple[str, str]]:
    doc = run_json(e)
    assert [s["key"] for s in doc["steps"]] == KEYS  # every step, always in this order
    return {s["key"]: (s["state"], s["command"]) for s in doc["steps"]}


def with_ca(e, cakeys, *machines):  # noqa: F811
    _with_ca(e.store, cakeys, machines)


def machine(tmp_path, name):
    keygen(tmp_path / name, name)
    return name, {"public_key": (tmp_path / f"{name}.pub").read_text().strip(),
                  "added": "2026-01-01T00:00:00Z", "scope": ["all"], "logins": ["agent"]}


def signed_in(name, *services, missing=()):
    auth = {k: {"state": "ok", "account": "work@example.com"} for k in services}
    auth.update({k: {"state": "missing", "account": None} for k in missing})
    status.write_cache(name, {"auth": auth})


# --- the states -------------------------------------------------------------------------


def test_a_fresh_computer_has_one_step_to_take_and_the_rest_wait(nx, tmp_path):
    from fleetledger.store import TomlFileStore

    nx.app.store_factory = lambda: TomlFileStore(tmp_path / "none" / "ships.toml")
    found = states(nx)
    assert found["setup"] == ("todo", "ship-fleet setup")
    for key in ("first-ship", "signins", "this-computer", "laptop"):
        assert found[key][0] == "blocked", key


def test_right_after_setup_each_step_says_what_does_it(nx):
    found = states(nx)
    assert found["setup"][0] == "attention"  # no CA yet in the test inventory
    assert found["setup"][1] == "ship-fleet setup   (or: ship-fleet ca init)"
    # The rest is still listed: the Ships are there, only trusting machines needs the CA.
    assert found["first-ship"] == ("done", "")
    assert found["signins"] == ("todo", "ship-fleet login ship-01 all")
    assert found["this-computer"] == ("blocked", "") and found["laptop"] == ("blocked", "")


def test_an_admin_key_out_of_ssh_agent_hides_no_ship(nx, cakeys, monkeypatch):  # noqa: F811
    with_ca(nx, cakeys)
    doctor_tools(nx, monkeypatch, loaded=False)  # after a reboot, say
    found = states(nx)
    assert found["setup"] == ("attention", "ssh-add " + str(nx.store.load().admin_key))
    assert found["first-ship"] == ("done", "")
    assert found["signins"][0] == "todo" and found["this-computer"][0] == "todo"


def test_a_set_up_fleet_lists_what_is_left(nx, cakeys):  # noqa: F811
    with_ca(nx, cakeys)
    found = states(nx)
    assert found["setup"] == ("done", "")
    assert found["first-ship"] == ("done", "")
    assert found["signins"] == ("todo", "ship-fleet login ship-01 all")  # never checked
    assert found["this-computer"] == ("todo", "ship-fleet trust --this-computer")
    assert found["other-devices"][0] == "todo"
    assert found["other-devices"][1].startswith("ship-fleet ui remote ")
    assert found["laptop"] == ("todo", nextsteps.LAPTOP_COMMAND)
    assert found["always-on"] == ("todo", "ship-fleet service install")
    assert found["vault"] == ("todo", "ship-fleet vault init")
    assert found["backups"] == ("blocked", "")


def test_no_ship_yet(nx, cakeys):  # noqa: F811
    with_ca(nx, cakeys)
    with nx.store.edit() as ed:
        ed.remove_ship("ship-01")
        ed.remove_ship("ship-02")
    found = states(nx)
    assert found["first-ship"] == ("todo", "ship-fleet new ship-01")
    assert found["signins"] == ("blocked", "")


def test_a_ship_not_enrolled_yet_is_waited_for(nx, cakeys):  # noqa: F811
    with_ca(nx, cakeys)
    with nx.store.edit() as ed:
        ed.remove_ship("ship-02")
        ed.set_field("ship-01", "enrolled", None)
    found = states(nx)
    assert found["first-ship"] == ("todo", "ship-fleet status ship-01 --wait")


def test_sign_ins_are_read_from_the_last_saved_status(nx, cakeys):  # noqa: F811
    with_ca(nx, cakeys)
    every = status.AUTH_SERVICES
    signed_in("ship-01", *every[1:], missing=every[:1])
    doc = run_json(nx)
    step = next(s for s in doc["steps"] if s["key"] == "signins")
    assert step["state"] == "todo" and every[0] in step["detail"]
    signed_in("ship-02", *every)
    assert states(nx)["signins"] == ("done", "")


def test_trusted_machines_and_their_certificates(nx, cakeys, tmp_path):  # noqa: F811
    with_ca(nx, cakeys, machine(tmp_path, "desk"), machine(tmp_path, "laptop"))
    paths.write_private(paths.config_dir() / trustcmd.THIS_COMPUTER,
                        json.dumps({"machine": "desk"}).encode())
    _certified_for(nx.store, "desk", 48)
    _certified_for(nx.store, "laptop", 48)
    found = states(nx)
    assert found["this-computer"] == ("done", "")
    assert found["laptop"] == ("done", "")
    _certified_for(nx.store, "laptop", 2)  # its last day: renewed by hand
    state, command = states(nx)["laptop"]
    assert state == "attention" and command.startswith("ship-fleet trust laptop")


def test_the_remote_ui_and_the_service(nx, cakeys):  # noqa: F811
    from fleetledger.web import remote

    with_ca(nx, cakeys)
    remote.save(remote.Remote(origin="https://fleetledger.example-tailnet.ts.net", port=8765,
                              logins=("you@example.com",)))
    service.unit_path().parent.mkdir(parents=True)
    service.unit_path().write_text("[Unit]\n")
    found = states(nx)
    assert found["other-devices"] == ("done", "")
    assert found["always-on"] == ("done", "")
    (paths.config_dir() / remote.REMOTE_FILE).write_text("{}")  # not one it wrote
    state, command = states(nx)["other-devices"]
    assert state == "attention" and "ship-fleet ui remote" in command


def test_the_vault_is_optional_with_a_keyring_and_recommended_without(nx, monkeypatch):
    def vault_step():
        return next(s for s in run_json(nx)["steps"] if s["key"] == "vault")

    assert vault_step()["optional"] is True
    secure_keyring(monkeypatch, locked=True)
    step = vault_step()
    assert step["optional"] is False and step["state"] == "todo"
    assert "no usable OS keyring" in step["detail"]


def test_the_vault_on_then_locked_and_backups(nx, cakeys):  # noqa: F811
    with_ca(nx, cakeys)
    vault.create("correct horse battery staple")
    found = states(nx)
    assert found["vault"] == ("done", "")
    assert found["backups"] == ("todo", nextsteps.BACKUP_COMMAND)
    vault.lock()
    found = states(nx)
    assert found["vault"] == ("attention", "ship-fleet unlock")
    assert found["setup"] == ("blocked", "")  # its secrets cannot be read while locked
    assert found["first-ship"] == ("done", "")  # the rest is still read from files


def test_a_vault_init_cut_short_needs_attention(nx, cakeys, tmp_path):  # noqa: F811
    with_ca(nx, cakeys)
    token = tmp_path / "proxmox-token"
    paths.write_private(token, b"a-token\n")
    with nx.store.edit() as ed:
        ed.set_default("driver", "proxmox")
        ed.set_driver("proxmox", {"api_url": "https://pve.example.com:8006/api2/json",
                                  "node": "pve", "pool": "shipshape", "template_vmid": 9000,
                                  "seed_storage": "s", "disk_storage": "d", "bridge": "vmbr0",
                                  "token_id": "shipshape@pve!build", "token_file": str(token),
                                  "tls_fingerprint": ":".join(["5E"] * 32)})
    vault.create("correct horse battery staple")  # on, nothing moved in yet
    state, command = states(nx)["vault"]
    assert (state, command) == ("attention", "ship-fleet vault init")


def test_a_failed_backup_needs_attention(nx, cakeys):  # noqa: F811
    from fleetledger import backup

    with_ca(nx, cakeys)
    vault.create("correct horse battery staple")
    paths.write_private(paths.data_dir() / backup.LAST, json.dumps(
        {"ok": False, "at": "2026-01-01T03:30:00Z", "to": "/mnt/backups",
         "why": "the folder is gone"}).encode())
    state, command = states(nx)["backups"]
    assert state == "attention" and "/mnt/backups" in command


# --- the output ---------------------------------------------------------------------------


def test_the_text_names_each_step_its_state_and_its_command(nx, cakeys):  # noqa: F811
    with_ca(nx, cakeys)
    code, out, err = nx.run("doctor", "--next")
    assert code == 0, err
    for step in nextsteps.steps(nx.app):
        assert step.title in out
        if step.command:
            assert f"run: {step.command}" in out
    assert "to do  Sign the first Ship in" in out
    assert "after: The vault" in out  # backups wait for it
    assert "left," in out


def test_it_reads_only_and_contacts_nothing(nx, cakeys, home):  # noqa: F811
    from test_cli import config_snapshot

    with_ca(nx, cakeys)

    def no_proxmox(config):
        raise AssertionError("asked Proxmox")

    nx.app.proxmox_api = no_proxmox
    before = config_snapshot(home)
    run_json(nx)
    assert config_snapshot(home) == before
    assert nx.ring.sets == []
    assert {tuple(c) for c in nx.commands} <= {("ssh-keygen", "-lf"), ("ssh-add", "-l")}


def test_json_alone_is_refused(nx):
    code, out, err = nx.run("doctor", "--json")
    assert code != 0 and "--next" in err


def test_plain_doctor_is_unchanged(nx, cakeys):  # noqa: F811
    with_ca(nx, cakeys)
    code, out, err = nx.run("doctor", "--offline")
    assert "Next steps" not in out and "ship-fleet service install" not in out

