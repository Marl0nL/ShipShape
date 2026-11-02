"""End-to-end, phase 2 day-2 operations on one real Ship (no sign-ins needed).

    SHIPSHAPE_E2E=~/.config/fleetledger/e2e.toml python -m pytest tests/e2e/test_e2e_ops.py -v -s

1. `ship-fleet update --force --reboot never`: the bundle from this checkout is
   copied over and converged; the Ship reports its commit; agents keep running.
2. herdr replaced under the running server (a copy, so a new inode): status says
   `herdr-restart-pending`; `ship-fleet update` (reboot auto) reboots exactly
   because of it, the Ship comes back, and the flag is gone.
3. LAN guard: with it on, the Ship (as agent and as fleet) is refused a private
   LAN address at once, still reaches the internet, keeps the guard across a
   `ship-fleet reboot`, lets an allowed address through, and drops it all when
   switched off.
4. `[updates]`: the unattended-upgrades file follows `ship-fleet set`.
5. `ship-fleet logs` and `attention` answer.

The private LAN address probed is the Proxmox API host from the config (the
Ship can reach it on the LAN); the guard checks are skipped if it is not private.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import socket
from urllib.parse import urlparse

import pytest
from e2e_harness import Fleet, e2e_config, log
from fleetledger.seed.shiptoml import local_timezone

CONFIG, WHY_NOT = e2e_config()
pytestmark = pytest.mark.skipif(CONFIG is None, reason=WHY_NOT)
PREFIX = os.environ.get("SHIPSHAPE_E2E_PREFIX", "e2e")


@pytest.fixture
def fleet(tmp_path):
    fleet = Fleet(tmp_path, CONFIG)
    vmids_before, seeds_before = fleet.pool_vmids(), fleet.seed_volumes()
    try:
        yield fleet
    finally:
        try:
            fleet.clean_up(vmids_before, seeds_before)
        finally:
            fleet.close()
    first, last = fleet.proxmox.get("vmid_range", [0, 1 << 30])
    left = {v for v in fleet.pool_vmids() - vmids_before if first <= v <= last}
    assert not left, f"VMs left in the pool: {sorted(left)}"


def lan_target() -> tuple[str, int] | None:
    """A private address on the Ship's LAN that answers TCP: the Proxmox API."""
    url = urlparse(CONFIG["drivers"]["proxmox"]["api_url"])
    try:
        address = socket.gethostbyname(url.hostname)
    except OSError:
        return None
    if not ipaddress.ip_address(address).is_private:
        return None
    return address, url.port or 8006


def connect_code(fleet: Fleet, name: str, address: str, port: int, as_agent: bool) -> int:
    """curl's exit code for a TCP+TLS connection from the Ship: 7 means refused."""
    probe = (f"curl -sk -o /dev/null --connect-timeout 5 --max-time 10 "
             f"https://{address}:{port}/; echo $?")
    command = ["sudo", "-n", "-u", "agent", "bash", "-c", probe] if as_agent else [
        "bash", "-c", probe]
    result = fleet.ssh(name, *command, timeout=60)
    return int(result.text.strip().splitlines()[-1])


def ship_sh(fleet: Fleet, name: str, command: str) -> str:
    result = fleet.ssh(name, "bash", "-c", command, timeout=120)
    assert result.returncode == 0, (command, result.stderr)
    return result.text


def attention_codes(fleet: Fleet, name: str) -> set[str]:
    return {a["code"] for a in fleet.status(name)["attention"]}


def outcome(proc) -> dict:
    [result] = json.loads(proc.stdout)
    assert result["ok"], result
    return result


def test_update_reboot_lan_guard(fleet):
    name = f"{PREFIX}-{secrets.token_hex(2)}"
    fleet.cli("new", name, "--tag", "e2e", "--driver", "proxmox", timeout=3600)
    commit = fleet.status(name)["status"]["ship"]["bundle_commit"]
    log(f"provisioned with bundle {commit}")

    log("1. update: converge this checkout again, no reboot")
    result = outcome(fleet.cli("update", name, "--force", "--reboot", "never", "--json",
                               timeout=3600))
    assert result["data"]["rebooted"] is False
    assert result["data"]["converge"]["from"].endswith(commit[:12])
    release = result["data"]["converge"]["to"]
    doc = fleet.status(name)["status"]
    assert release.endswith(doc["ship"]["bundle_commit"][:12])  # the Ship reports it
    assert doc["provision"]["result"] == "ok"
    assert "herdr-restart-pending" not in {a["code"] for a in doc["attention"]}

    log("2. herdr replaced under the running server -> herdr-restart-pending")
    ship_sh(fleet, name, "sudo -n cp -p /usr/local/bin/herdr /usr/local/bin/.herdr.e2e && "
                         "sudo -n mv /usr/local/bin/.herdr.e2e /usr/local/bin/herdr")
    assert "herdr-restart-pending" in attention_codes(fleet, name)
    result = outcome(fleet.cli("update", name, "--json", timeout=3600))
    assert result["data"]["rebooted"] is True, result
    assert "herdr-restart-pending" not in attention_codes(fleet, name)

    target = lan_target()
    if target is None:
        log("3. skipped: the Proxmox API host is not a private address")
    else:
        address, port = target
        log("3. LAN guard")
        assert connect_code(fleet, name, address, port, as_agent=True) != 7
        fleet.cli("set", name, "lan_guard=true")
        assert connect_code(fleet, name, address, port, as_agent=True) == 7
        assert connect_code(fleet, name, address, port, as_agent=False) == 7
        internet = ship_sh(fleet, name, "curl -s -o /dev/null -w '%{http_code}' "
                                        "--max-time 20 https://example.com/ || true")
        assert internet.strip() in ("200", "301", "302"), internet
        log("   survives a reboot")
        outcome(fleet.cli("reboot", name, "--json", timeout=1200))
        assert "shipshape_lan_guard" in ship_sh(
            fleet, name, "sudo -n nft list table inet shipshape_lan_guard")
        # The stored rules were in place before the network came up and before
        # the agent's session (herdr) started.
        times = ship_sh(fleet, name, "systemctl show -P ActiveEnterTimestampMonotonic "
                                     "ship-lan-guard-early.service systemd-networkd.service "
                                     "\"user@$(id -u agent).service\"").split()
        early, networkd, user = (int(t) for t in times)
        assert 0 < early < networkd and early < user, times
        assert connect_code(fleet, name, address, port, as_agent=True) == 7
        log("   an allowed address passes")
        fleet.cli("set", name, f"lan_guard_allow={address}")
        assert connect_code(fleet, name, address, port, as_agent=True) != 7
        log("   off removes it")
        fleet.cli("set", name, "lan_guard=false", "lan_guard_allow=")
        assert ship_sh(fleet, name, "sudo -n nft list tables").count("shipshape") == 0
        for unit in ("ship-lan-guard-early.service", "ship-lan-guard.service"):
            assert ship_sh(fleet, name, f"systemctl is-enabled {unit} || true"
                           ).strip() == "disabled"

    log("4. the time zone and the OS update reboot policy")
    zone = ship_sh(fleet, name, "timedatectl show --property=Timezone --value").strip()
    assert zone == local_timezone() or {zone, local_timezone()} <= {"UTC", "Etc/UTC"}
    fleet.cli("set", name, "timezone=America/New_York")
    assert ship_sh(fleet, name, "timedatectl show --property=Timezone --value"
                   ).strip() == "America/New_York"
    path = "/etc/apt/apt.conf.d/52shipshape-unattended-upgrades"
    assert 'Automatic-Reboot "false"' in ship_sh(fleet, name, f"cat {path}")
    fleet.cli("set", name, "auto_reboot=true", "reboot_time=03:30")
    text = ship_sh(fleet, name, f"cat {path}")
    assert 'Automatic-Reboot "true"' in text and '"03:30"' in text
    # apt itself reads it back the same way.
    config = ship_sh(fleet, name, "apt-config dump | grep -i automatic-reboot")
    assert '"true"' in config and "03:30" in config

    log("5. logs and attention")
    assert fleet.cli("logs", name, "provision", "--lines", "5").stdout.strip()
    json.loads(fleet.cli("attention", "--json").stdout)

    fleet.destroy(name)
