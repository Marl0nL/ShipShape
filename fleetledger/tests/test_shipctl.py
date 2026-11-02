import json
from pathlib import Path

import pytest
from fleetledger.shipctl import ShipCtl, ShipCtlError

EXAMPLES = Path(__file__).resolve().parents[2] / "contracts" / "examples" / "ship-ctl-result"
VERSION = {
    "schema": "shipshape.ship-ctl-result/1",
    "command": "version",
    "ok": True,
    "changed": [],
    "messages": [],
    "data": {
        "tooling": "0.1.0",
        "commit": "0123456789ab",
        "contracts": {
            "ship-status": 1,
            "ship-toml": 1,
            "ship-ctl-result": 1,
            "bundle": 1,
            "seed": 1,
            "runtime": 1,
        },
    },
    "error": None,
}


def handler_for(results, version=VERSION):
    def handler(ship, cmd, inp):
        command = cmd[3]
        if command == "version":
            return 0, json.dumps(version), ""
        code, doc = results[command]
        return code, json.dumps(doc), ""

    return handler


def test_set_checks_version_then_calls(store, fake_ssh):
    ok = json.loads((EXAMPLES / "ok.json").read_text())
    ssh = fake_ssh(handler_for({"set": (0, ok)}))
    ctl = ShipCtl(ssh, store.load().ship("ship-01"))
    result = ctl.set(["claude.remote_control=off"])
    assert result.ok
    assert [c[1] for c in ssh.calls] == [
        ["sudo", "-n", "/usr/local/bin/ship-ctl", "version", "--json"],
        ["sudo", "-n", "/usr/local/bin/ship-ctl", "set", "claude.remote_control=off", "--json"],
    ]
    ctl.set(["agent.sudo=true"])
    assert len(ssh.calls) == 3  # version checked once per ShipCtl


def test_set_file_sends_stdin(store, fake_ssh):
    ok = json.loads((EXAMPLES / "ok.json").read_text())
    ssh = fake_ssh(handler_for({"set": (0, ok)}))
    ShipCtl(ssh, store.load().ship("ship-01")).set_file("schema = 1\n")
    assert ssh.calls[-1][1][-3:] == ["--file", "-", "--json"]
    assert ssh.calls[-1][2] == b"schema = 1\n"


def test_failure_carries_code(store, fake_ssh):
    error = json.loads((EXAMPLES / "error.json").read_text())
    ssh = fake_ssh(handler_for({"set": (4, error)}))
    with pytest.raises(ShipCtlError) as exc:
        ShipCtl(ssh, store.load().ship("ship-01")).set(["claude.default_mode=bypassPermissions"])
    assert exc.value.code == "invalid-ship-toml"
    assert "allow_bypass" in exc.value.message


def test_unknown_contract_major_is_refused(store, fake_ssh):
    newer = json.loads(json.dumps(VERSION))
    newer["data"]["contracts"]["ship-toml"] = 2
    ssh = fake_ssh(handler_for({}, version=newer))
    with pytest.raises(ShipCtlError, match="ship-toml=2") as exc:
        ShipCtl(ssh, store.load().ship("ship-01")).logs("herdr", 10)
    assert exc.value.code == "incompatible"
    assert len(ssh.calls) == 1


def test_not_installed_and_garbage(store, fake_ssh):
    ship = store.load().ship("ship-01")
    missing = fake_ssh(lambda s, c, i: (1, b"", "sudo: /usr/local/bin/ship-ctl: command not found"))
    with pytest.raises(ShipCtlError) as exc:
        ShipCtl(missing, ship).call("version", check_version=False)
    assert exc.value.code == "not-installed"
    garbage = fake_ssh(lambda s, c, i: (0, b"hello", ""))
    with pytest.raises(ShipCtlError, match="did not print JSON"):
        ShipCtl(garbage, ship).call("version", check_version=False)
    wrong = fake_ssh(lambda s, c, i: (0, json.dumps({"schema": "x"}), ""))
    with pytest.raises(ShipCtlError, match="unknown document"):
        ShipCtl(wrong, ship).call("version", check_version=False)


def test_logs_returns_lines(store, fake_ssh):
    logs = dict(VERSION, command="logs", data={"lines": ["one", "two"]})
    ssh = fake_ssh(handler_for({"logs": (0, logs)}))
    assert ShipCtl(ssh, store.load().ship("ship-01")).logs("herdr", 2) == ["one", "two"]
    assert ssh.calls[-1][1][3:] == ["logs", "herdr", "--lines", "2", "--json"]
