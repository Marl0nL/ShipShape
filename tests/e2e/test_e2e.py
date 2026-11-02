"""End-to-end (W11), unattended part: seed, boot, needs sign-in, reboot, destroy.

Runs only when SHIPSHAPE_E2E names a ships.toml with a [drivers.proxmox]
section (scoped token, pool, storages, vmid_range); CI never runs it.

    SHIPSHAPE_E2E=~/.config/fleetledger/e2e.toml python -m pytest tests/e2e -v -s

The parts that need a person (sign-ins, the Claude app) are in acceptance.py.
"""

from __future__ import annotations

import json
import secrets
import tomllib

import pytest
from e2e_harness import ROOT, Fleet, e2e_config, log
from fleetledger.catalogue import Catalogue
from fleetledger.repo import Repo
from jsonschema import Draft202012Validator

CONFIG = e2e_config()
pytestmark = pytest.mark.skipif(CONFIG is None, reason="set SHIPSHAPE_E2E to run")
VERSIONS = tomllib.loads((ROOT / "versions.toml").read_text())
DEFAULT_TOOLS = Catalogue.from_repo(Repo(ROOT)).default_tools()
STATUS_SCHEMA = Draft202012Validator(
    json.loads((ROOT / "contracts" / "ship-status.schema.json").read_text())
)


@pytest.fixture
def fleet(tmp_path):
    fleet = Fleet(tmp_path, CONFIG)
    yield fleet
    for ship in fleet.store.load().ships():
        fleet.destroy(ship.name)
    fleet.close()


def check_needs_sign_in(view: dict) -> dict:
    """The status document of a provisioned, signed-out Ship."""
    doc = view["status"]
    assert doc is not None, view
    assert not list(STATUS_SCHEMA.iter_errors(doc))
    assert view["state"] == "needs-sign-in", view["state"]
    assert doc["provision"]["result"] == "ok"
    # "disabled" once the seed is ejected: no datasource is left, which is right.
    assert doc["provision"]["cloud_init"] in ("done", "disabled"), doc["provision"]
    assert doc["herdr"]["server"] == "running"
    assert doc["herdr"]["version"] and VERSIONS["herdr"]["version"] in doc["herdr"]["version"]
    assert doc["claude"]["version"]
    assert doc["claude"] == {**doc["claude"], "remote_control": "on", "default_mode": "auto",
                             "allow_bypass": True, "channel": "latest"}
    assert doc["ship"]["tooling"] == VERSIONS["shipshape"]["tooling"]
    assert doc["ship"]["tags"] == ["e2e"]
    assert doc["auth"]["claude"]["state"] == "missing"
    codes = {a["code"] for a in view["attention"]}
    assert not codes & {"unit-failed", "provision-failed", "tool-failed"}, view["attention"]
    # The default catalogue tools: installed, and waiting only on a sign-in.
    tools = doc["tools"]
    for tool in DEFAULT_TOOLS:
        assert tools[tool]["state"] in ("ok", "waiting"), (tool, tools.get(tool))
        if tools[tool]["state"] == "waiting":
            assert tools[tool]["waiting_for"], tool
    if "gh-axi" in DEFAULT_TOOLS:
        assert tools["gh-axi"] == {**tools["gh-axi"], "state": "waiting",
                                   "waiting_for": ["github"]}
    return doc


def check_runtime_contract(fleet: Fleet, name: str) -> None:
    """contracts/ship-runtime.md, as seen from FleetLedger."""
    def sh(command: str) -> str:
        result = fleet.ssh(name, "bash", "-c", command)
        assert result.returncode == 0, (command, result.stderr)
        return result.text

    assert sh("ls /etc/ssh/ | grep '^ssh_host_.*_key.pub$'").split() == [
        "ssh_host_ed25519_key.pub"]
    sshd = sh("sudo -n sshd -T").lower()
    for line in ("passwordauthentication no", "permitrootlogin no",
                 "allowagentforwarding no", "kbdinteractiveauthentication no"):
        assert line in sshd, line
    assert "allowusers fleet" in sshd and "allowusers agent" in sshd
    assert sh("loginctl show-user agent -p Linger").strip() == "Linger=yes"
    assert "not allowed" in sh("sudo -n -l -U agent || true")
    assert sh("stat -c '%a %U' /etc/shipshape/ship.toml").strip() == "644 root"
    assert sh("readlink -f /usr/local/bin/ship-ctl").startswith("/opt/shipshape/releases/")
    managed = json.loads(sh("cat /etc/claude-code/managed-settings.json"))
    assert managed["remoteControlAtStartup"] is True
    assert managed["permissions"]["defaultMode"] == "auto"
    assert "disableBypassPermissionsMode" not in managed["permissions"]
    assert managed["env"]["CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX"] == name
    user = json.loads(sh("sudo -n cat /home/agent/.claude/settings.json"))
    assert user["skipDangerousModePermissionPrompt"] is True
    if "ship-axi" in DEFAULT_TOOLS:
        assert name in sh("sudo -n -iu agent -- ship-axi")  # agents can see their Ship


def test_seed_boot_reboot_destroy(fleet):
    name = f"e2e-{secrets.token_hex(2)}"
    proc = fleet.cli(
        "new", name, "--tag", "e2e", "--driver", "proxmox", "--mode", "auto", "--allow-bypass",
        "--autostart", "main:~/work/main:auto",
        "--autostart", "grinder:~/work/grinder:bypassPermissions",
        timeout=3600,
    )
    assert f"{name} is enrolled." in proc.stdout
    ship = fleet.ship(name)
    vmid = ship.driver_ref["vmid"]
    assert 9100 <= vmid and ship.enrolled and "seed" not in ship.driver_ref

    log("the seed is gone: ejected, deleted from storage, and no local copy")
    config = fleet.driver.api.get(f"/nodes/{fleet.proxmox['node']}/qemu/{vmid}/config")
    assert config.get("ide2", "none").startswith("none")
    assert not any(v.startswith(f"{fleet.proxmox['seed_storage']}:iso/{name}-")
                   for v in fleet.seed_volumes())
    assert not list((fleet.workdir / "data" / "seeds").glob("*.iso"))

    log("status: needs sign-in, valid against the contract")
    check_needs_sign_in(fleet.status(name))
    log("runtime contract")
    check_runtime_contract(fleet, name)

    fleet.reboot(name)
    log("after the reboot: herdr back, still needs sign-in")
    view = fleet.wait_for(
        "status after reboot",
        lambda: (v := fleet.status(name))["state"] == "needs-sign-in"
        and v["status"]["herdr"]["last_resume"] is not None and v,
        timeout=600,
    )
    check_needs_sign_in(view)

    fleet.destroy(name)
    assert vmid not in fleet.pool_vmids()
