import copy
import json
import stat
from pathlib import Path

import pytest
from fleetledger.ssh import HostKeyMismatch, Unreachable
from fleetledger.status import ShipView, fetch, poll, read_cache, version_tuple

from fleetledger import status as status_mod

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples"
RUNNING = json.loads((EXAMPLES / "ship-status" / "running.json").read_text())


def answer(doc, code=0):
    return lambda ship, cmd, inp: (code, json.dumps(doc), "")


def test_running_ship(store, fake_ssh, home):
    inv = store.load()
    view = fetch(fake_ssh(answer(RUNNING)), inv, inv.ship("ship-01"), tooling="0.1.0")
    assert view.problem is None and view.state == "running"
    assert view.provision_result == "ok"
    assert [a["code"] for a in view.attention] == ["agent-waiting"]
    assert view.auth("claude")["account"] == "work@example.com"
    cached = home / "data" / "status" / "ship-01.json"
    assert stat.S_IMODE(cached.stat().st_mode) == 0o600
    assert read_cache("ship-01") == RUNNING


def test_command_is_the_contract_one(store, fake_ssh):
    inv = store.load()
    ssh = fake_ssh(answer(RUNNING))
    fetch(ssh, inv, inv.ship("ship-01"))
    assert ssh.calls[0][1] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "status", "--json"]


def test_wrong_account_and_fleet_attention(store, fake_ssh):
    doc = copy.deepcopy(RUNNING)
    doc["auth"]["claude"]["account"] = "you@example.com"  # the personal account
    doc["auth"]["github"]["account"] = "Example-Work"  # same login, other case: fine
    doc["ship"]["tooling"] = "0.0.9"
    doc["ship"]["tags"] = ["work", "gpu"]
    inv = store.load()
    view = fetch(fake_ssh(answer(doc)), inv, inv.ship("ship-01"), tooling="0.1.0")
    codes = {a["code"]: a for a in view.attention}
    assert codes["signin-wrong-account"]["subject"] == "claude"
    assert "you@example.com" in codes["signin-wrong-account"]["message"]
    assert "work@example.com" in codes["signin-wrong-account"]["message"]
    assert "behind-tooling" in codes and "tags-differ" in codes
    assert sum(a["code"] == "signin-wrong-account" for a in view.attention) == 1


def test_tags_differ_uses_default_tags(store, fake_ssh):
    with store.edit() as ed:
        ed.set_field("ship-01", "tags", None)  # defaults.tags = ["work"], as the Ship says
    inv = store.load()
    view = fetch(fake_ssh(answer(RUNNING)), inv, inv.ship("ship-01"))
    assert all(a["code"] != "tags-differ" for a in view.attention)


def test_signed_out_services_are_not_compared(store, fake_ssh):
    doc = copy.deepcopy(RUNNING)
    doc["auth"]["claude"].update(state="missing", account="you@example.com")
    inv = store.load()
    view = fetch(fake_ssh(answer(doc)), inv, inv.ship("ship-01"))
    assert all(a["code"] != "signin-wrong-account" for a in view.attention)


def test_identity_mismatch_is_not_trusted(store, fake_ssh):
    inv = store.load()
    view = fetch(fake_ssh(answer(RUNNING)), inv, inv.ship("ship-02"))
    assert view.problem == "identity" and view.status is None
    assert "says it is ship-01" in view.message


def test_same_name_other_id_is_not_trusted(store, fake_ssh):
    """A rebuilt or impostor Ship that keeps the name but not the id."""
    doc = copy.deepcopy(RUNNING)
    doc["ship"]["id"] = "00000000-0000-4000-8000-000000000000"
    inv = store.load()
    view = fetch(fake_ssh(answer(doc)), inv, inv.ship("ship-01"))
    assert view.problem == "identity" and view.status is None


def test_invalid_and_failed_documents(store, fake_ssh):
    inv = store.load()
    ship = inv.ship("ship-01")
    bad = copy.deepcopy(RUNNING)
    bad["state"] = "sailing"
    assert fetch(fake_ssh(answer(bad)), inv, ship).problem == "invalid"
    assert fetch(fake_ssh(lambda s, c, i: (0, "not json", "")), inv, ship).problem == "invalid"
    other = dict(RUNNING, schema="shipshape.ship-status/2")
    assert "version" in fetch(fake_ssh(answer(other)), inv, ship).message
    failed = json.loads((EXAMPLES / "ship-ctl-result" / "status-failed.json").read_text())
    view = fetch(fake_ssh(answer(failed, code=1)), inv, ship)
    assert view.problem == "error" and "ship-ctl status failed" in view.message


def test_cloud_init_stands_in_before_ship_ctl(store, fake_ssh):
    def handler(ship, cmd, inp):
        if "ship-ctl" in cmd[2]:
            return 1, b"", "sudo: /usr/local/bin/ship-ctl: command not found"
        assert cmd == ["cloud-init", "status", "--format", "json"]
        return 0, json.dumps({"status": "running"}), ""

    inv = store.load()
    view = fetch(fake_ssh(handler), inv, inv.ship("ship-01"))
    assert view.state == "provisioning" and view.provision_result == "running"
    view.cloud_init = {"status": "done"}
    assert view.provision_result == "ok"
    view.cloud_init = {"status": "error"}
    assert view.provision_result == "failed"


def test_connection_problems(store, fake_ssh):
    inv = store.load()
    ship = inv.ship("ship-01")

    def boom(exc):
        def handler(s, c, i):
            raise exc

        return handler

    assert fetch(fake_ssh(boom(Unreachable("down"))), inv, ship).state == "unreachable"
    view = fetch(fake_ssh(boom(HostKeyMismatch("impostor"))), inv, ship)
    assert view.state == "host-key" and view.message == "impostor"


def test_poll_keeps_order(store):
    inv = store.load()
    ships = inv.ships()

    def fetcher(ssh, inventory, ship, tooling):
        return ShipView(ship, problem="unreachable")

    views = poll(None, inv, list(reversed(ships)), fetcher=fetcher)
    assert [v.ship.name for v in views] == ["ship-02", "ship-01"]
    assert poll(None, inv, []) == []


@pytest.mark.parametrize(
    "a,b", [("0.1.0", "0.2.0"), ("0.9.9", "0.10.0"), ("1.0.0-rc1", "1.0.1")]
)
def test_version_order(a, b):
    assert version_tuple(a) < version_tuple(b)


def test_never_provisioned_counts_as_running():
    view = ShipView(ship=None, status={"provision": {"result": "never"}, "state": "provisioning"})
    assert view.provision_result == "running"
    assert status_mod.ShipView(ship=None).state == "unknown"
