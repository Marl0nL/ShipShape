"""LAN guard, OS update policy and herdr-restart-pending (phase 2).

Most tests run ship-ctl in the sandbox Ship with fake nft, ip and systemctl.
One test loads the real rules with the real nft inside a throwaway user and
network namespace and checks what they block; it is skipped where unprivileged
namespaces or nft are not available.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from ship_harness import BASIC_TOML, SHIP, assert_valid

sys.path.insert(0, str(SHIP / "lib"))
from shipctl import languard, status  # noqa: E402

GUARD_TOML = BASIC_TOML + """
[network]
lan_guard = true
lan_guard_allow = ["192.0.2.10", "198.51.100.0/24"]
"""
RULES = "/etc/shipshape/lan-guard.nft"
UPGRADES = "/etc/apt/apt.conf.d/52shipshape-unattended-upgrades"


def resolv(ship, *servers):
    path = ship.p(languard.RESOLV_CONF)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"nameserver {s}\n" for s in servers) + "search example.com\n")


# --- LAN guard ------------------------------------------------------------------


def test_guard_on_writes_loads_and_enables(ship):
    resolv(ship, "192.0.2.53", "fe80::53%ens18")
    ship.write_toml(GUARD_TOML)
    rc, doc, out = ship.ctl("apply", "--json")
    assert rc == 0, out
    rules = ship.p(RULES).read_text()
    assert "elements = { 192.0.2.1, 192.0.2.53, 192.0.2.10, 198.51.100.0/24 }" in rules
    assert "elements = { fe80::53 }" in rules  # zone id stripped
    assert RULES in doc["changed"]
    assert "ship-lan-guard.service (enabled)" in doc["changed"]
    assert "ship-lan-guard-early.service (enabled)" in doc["changed"]
    nft = ship.calls("nft")
    check = nft.index(["nft", "-c", "-f", str(ship.p(RULES))])
    load = nft.index(["nft", "-f", str(ship.p(RULES))])
    assert check < load  # checked before it is loaded
    s = ship.scenario()
    assert s["nft"]["tables"] == ["inet shipshape_lan_guard"]
    assert s["enabled_units"] == ["ship-lan-guard-early.service", "ship-lan-guard.service"]


def test_guard_apply_is_idempotent(ship):
    ship.write_toml(GUARD_TOML)
    ship.ctl("apply", "--json")
    loads = ship.scenario()["nft"]["loads"]
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0 and doc["changed"] == []
    assert ship.scenario()["nft"]["loads"] == loads


def test_guard_reloads_if_the_table_went_missing(ship):
    ship.write_toml(GUARD_TOML)
    ship.ctl("apply", "--json")
    s = ship.scenario()
    s["nft"]["tables"] = []
    ship.set(nft=s["nft"])
    rc, doc, _ = ship.ctl("apply", "--json")
    assert "nftables table inet shipshape_lan_guard (loaded)" in doc["changed"]
    assert ship.scenario()["nft"]["tables"] == ["inet shipshape_lan_guard"]


def test_guard_off_removes_table_file_and_unit(ship):
    ship.write_toml(GUARD_TOML)
    ship.ctl("apply", "--json")
    ship.write_toml(BASIC_TOML)
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    assert not ship.p(RULES).exists()
    assert ship.scenario()["nft"]["tables"] == []
    assert ship.scenario()["enabled_units"] == []
    assert {"nftables table inet shipshape_lan_guard (removed)",
            "ship-lan-guard-early.service (disabled)",
            "ship-lan-guard.service (disabled)", RULES} <= set(doc["changed"])


def test_guard_enables_a_missing_early_unit(ship):
    """A Ship whose guard was turned on before the early unit existed gets it on the next apply."""
    ship.write_toml(GUARD_TOML)
    ship.ctl("apply", "--json")
    ship.set(enabled_units=["ship-lan-guard.service"])
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0 and doc["changed"] == ["ship-lan-guard-early.service (enabled)"]


def test_guard_never_off_by_default_touches_nothing(ship):
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    assert not any(c[:2] == ["nft", "-f"] or c[:2] == ["nft", "delete"]
                   for c in ship.calls("nft"))
    assert not any("enable" in c for c in ship.calls("systemctl"))


def test_guard_dry_run_writes_and_loads_nothing(ship):
    ship.write_toml(GUARD_TOML)
    rc, doc, _ = ship.ctl("apply", "--dry-run", "--json")
    assert rc == 0
    assert RULES in doc["changed"]
    assert not ship.p(RULES).exists()
    assert not any(c[1:2] == ["-f"] for c in ship.calls("nft"))


def test_guard_load_failure_fails_apply(ship):
    ship.write_toml(GUARD_TOML)
    ship.set(nft={"tables": [], "fail": True})
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 1
    assert doc["error"]["code"] == "internal"
    assert "lan guard" in doc["error"]["message"]


def test_boot_unit_command_reloads_even_without_changes(ship):
    ship.write_toml(GUARD_TOML)
    ship.ctl("apply", "--json")
    loads = ship.scenario()["nft"]["loads"]
    rc, doc, _ = ship.ctl("lan-guard", "--json")
    assert rc == 0
    assert_valid("ship-ctl-result", doc)
    assert doc["command"] == "apply" and doc["messages"][0] == "LAN guard on."
    assert ship.scenario()["nft"]["loads"] == loads + 1


def test_boot_unit_command_picks_up_a_new_gateway(ship):
    ship.write_toml(GUARD_TOML)
    ship.ctl("apply", "--json")
    ship.set(routes={"-4": [{"gateway": "192.0.2.254"}], "-6": [{"gateway": "fe80::1"}]})
    rc, doc, _ = ship.ctl("lan-guard", "--json")
    assert rc == 0
    rules = ship.p(RULES).read_text()
    assert "192.0.2.254" in rules and "192.0.2.1," not in rules
    assert "fe80::1" in rules


def test_boot_unit_command_with_the_guard_off(ship):
    rc, doc, _ = ship.ctl("lan-guard", "--json")
    assert rc == 0 and doc["messages"] == ["LAN guard off."]


def test_unparsable_entries_never_reach_nft(ship):
    """The schema pattern is loose; only clean, re-printed addresses go in the file."""
    ship.write_toml(BASIC_TOML + """
[network]
lan_guard = true
lan_guard_allow = ["192.0.2.10/999", "::::", "198.51.100.7"]
""")
    resolv(ship, "not-an-address;", "192.0.2.53")
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    rules = ship.p(RULES).read_text()
    assert "999" not in rules and "::::" not in rules and "not-an-address" not in rules
    assert "198.51.100.7" in rules and "192.0.2.53" in rules
    assert sum("ignoring" in m for m in doc["messages"]) == 3


def test_hostile_route_output_is_ignored(ship):
    ship.write_toml(GUARD_TOML)
    ship.set(routes={"-4": [{"gateway": "1.2.3.4 } ; flush ruleset ; {"}], "-6": "junk"})
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    rules = ship.p(RULES).read_text()
    assert "flush" not in rules


def test_render_puts_every_accept_before_the_rejects():
    text = languard.render([ipaddress.ip_network("192.0.2.1")])
    chain = text[text.index("chain output"):]
    last_accept = max(chain.index(r) for r in (
        'oifname "lo" accept', "ct state established,related accept",
        "udp dport { 67, 547 } accept", "ip daddr @allow4 accept"))
    assert last_accept < chain.index("ip daddr @private4 counter reject")
    assert text.splitlines()[2:4] == ["table inet shipshape_lan_guard",
                                      "delete table inet shipshape_lan_guard"]
    for private in languard.PRIVATE_V4 + languard.PRIVATE_V6:
        assert private in text
    # Only private ranges are refused: everything else leaves as before. (The
    # namespace test cannot tell a dropped packet from no answer.)
    assert "type filter hook output priority filter; policy accept;" in chain
    assert "policy drop" not in text


def _netns_ok() -> bool:
    if not (shutil.which("unshare") and shutil.which("nft") or os.path.exists("/usr/sbin/nft")):
        return False
    try:
        r = subprocess.run(["unshare", "-rn", "sh", "-c", "PATH=$PATH:/usr/sbin; nft list ruleset"],
                           capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


PROBE = """
import socket, sys
for host in sys.argv[1:]:
    s = socket.socket(); s.settimeout(0.5)
    try:
        s.connect((host, 80)); r = "connected"
    except ConnectionRefusedError:
        r = "refused"
    except OSError:
        r = "no-answer"
    print(host, r)
"""


# Addresses inside the throwaway namespace only (a made-up LAN on a dummy link).
NS_LAN = "192.168.50."  # hygiene: allow
NS_OTHER_PRIVATE = ["10.1.1.1", "172.16.0.5", "100.64.0.1"]  # hygiene: allow
NS_INTERNET = "203.0.113.5"


@pytest.mark.skipif(not _netns_ok(), reason="no unprivileged network namespaces or nft")
def test_real_rules_block_private_addresses_only(tmp_path):
    """Load the rendered rules into a throwaway namespace with a fake LAN on a dummy
    interface: private addresses are refused at once; the gateway, an allowed
    address and the internet get through (no answer comes back, but nothing is
    refused)."""
    gateway, allowed_host, other, me = (NS_LAN + n for n in ("1", "7", "9", "2"))
    rules = tmp_path / "guard.nft"
    rules.write_text(languard.render([ipaddress.ip_network(gateway),
                                      ipaddress.ip_network(allowed_host)]))
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    hosts = [other, allowed_host, gateway, *NS_OTHER_PRIVATE, NS_INTERNET]
    script = (
        "set -e; PATH=$PATH:/usr/sbin; ip link set lo up; ip link add d0 type dummy; "
        f"ip link set d0 up; ip addr add {me}/24 dev d0; "
        f"ip route add default via {gateway}; "
        f"nft -f {rules}; nft -f {rules}; "  # loading twice replaces the table
        f"{sys.executable} -I {probe} {' '.join(hosts)}"
    )
    r = subprocess.run(["unshare", "-rn", "sh", "-c", script], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    got = dict(line.split() for line in r.stdout.splitlines())
    assert got == {
        other: "refused",
        allowed_host: "no-answer",
        gateway: "no-answer",
        **{host: "refused" for host in NS_OTHER_PRIVATE},
        NS_INTERNET: "no-answer",
    }


# --- OS update policy -------------------------------------------------------------


def test_unattended_upgrades_policy_default_and_set(ship):
    rc, _, _ = ship.ctl("apply", "--json")
    assert rc == 0
    text = ship.p(UPGRADES).read_text()
    assert 'Unattended-Upgrade::Automatic-Reboot "false";' in text
    assert 'Unattended-Upgrade::Automatic-Reboot-Time "04:00";' in text
    rc, doc, out = ship.ctl("set", "updates.auto_reboot=true", "updates.reboot_time=03:30",
                            "--json")
    assert rc == 0, out
    text = ship.p(UPGRADES).read_text()
    assert 'Unattended-Upgrade::Automatic-Reboot "true";' in text
    assert 'Unattended-Upgrade::Automatic-Reboot-Time "03:30";' in text
    assert UPGRADES in doc["changed"]


def test_set_rejects_a_bad_reboot_time(ship):
    rc, doc, _ = ship.ctl("set", "updates.reboot_time=25:00", "--json")
    assert rc == 4 and doc["error"]["code"] == "invalid-ship-toml"
    assert not ship.p(UPGRADES).exists()


# --- time zone ---------------------------------------------------------------------


def tz_calls(ship):
    return [c for c in ship.calls("timedatectl") if c[1:2] == ["set-timezone"]]


def test_default_timezone_is_utc_and_leaves_the_cloud_image_alone(ship):
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    assert tz_calls(ship) == []  # Etc/UTC already counts as UTC
    assert not any("time zone" in c for c in doc["changed"])


def test_timezone_from_ship_toml_is_set_once(ship):
    ship.write_toml(BASIC_TOML.replace('name = "ship-01"',
                                       'name = "ship-01"\ntimezone = "Europe/Berlin"'))
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    assert "time zone Europe/Berlin (was Etc/UTC)" in doc["changed"]
    assert tz_calls(ship) == [["timedatectl", "set-timezone", "Europe/Berlin"]]
    assert ship.scenario()["timezone"] == "Europe/Berlin"
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0 and doc["changed"] == []
    assert len(tz_calls(ship)) == 1


def test_set_timezone_and_reboot_time_together(ship):
    rc, doc, out = ship.ctl("set", "ship.timezone=America/New_York",
                            "updates.auto_reboot=true", "updates.reboot_time=03:30", "--json")
    assert rc == 0, out
    assert ship.scenario()["timezone"] == "America/New_York"
    assert 'Automatic-Reboot-Time "03:30";' in ship.p(UPGRADES).read_text()
    assert 'timezone = "America/New_York"' in ship.p("/etc/shipshape/ship.toml").read_text()


@pytest.mark.parametrize("zone", ["Mars/Olympus_Mons", "../../etc/shadow", "Europe/../../x",
                                  "/etc/localtime", "europe/berlin"])
def test_set_refuses_a_zone_that_is_not_installed_or_not_a_name(ship, zone):
    before = ship.p("/etc/shipshape/ship.toml").read_text()
    rc, doc, _ = ship.ctl("set", f"ship.timezone={zone}", "--json")
    assert rc == 4 and doc["error"]["code"] == "invalid-ship-toml"
    assert ship.p("/etc/shipshape/ship.toml").read_text() == before
    assert tz_calls(ship) == []


def test_an_uninstalled_zone_in_ship_toml_does_not_stop_apply(ship):
    ship.write_toml(BASIC_TOML.replace('name = "ship-01"',
                                       'name = "ship-01"\ntimezone = "Mars/Olympus_Mons"'))
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    assert "time zone Mars/Olympus_Mons is not installed on this Ship; left at Etc/UTC" \
        in doc["messages"]
    assert tz_calls(ship) == []
    assert ship.p(UPGRADES).exists()  # the rest of apply still ran


def test_timezone_unreadable_is_reported_not_fatal(ship):
    ship.write_toml(BASIC_TOML.replace('name = "ship-01"',
                                       'name = "ship-01"\ntimezone = "Europe/Berlin"'))
    ship.set(timedatectl_fail=True)
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    assert any(m.startswith("time zone: cannot read it") for m in doc["messages"])


def test_timezone_dry_run_changes_nothing(ship):
    ship.write_toml(BASIC_TOML.replace('name = "ship-01"',
                                       'name = "ship-01"\ntimezone = "Europe/Berlin"'))
    rc, doc, _ = ship.ctl("apply", "--dry-run", "--json")
    assert rc == 0
    assert "time zone Europe/Berlin (was Etc/UTC)" in doc["changed"]
    assert tz_calls(ship) == []


def test_set_lan_guard_allow_list_and_clear(ship):
    rc, _, out = ship.ctl("set", "network.lan_guard=true",
                          "network.lan_guard_allow=192.0.2.10,198.51.100.0/24", "--json")
    assert rc == 0, out
    assert "198.51.100.0/24" in ship.p(RULES).read_text()
    rc, _, _ = ship.ctl("set", "network.lan_guard_allow=", "--json")
    assert rc == 0
    assert "198.51.100.0/24" not in ship.p(RULES).read_text()
    rc, doc, _ = ship.ctl("set", "network.lan_guard_allow=router.example.com", "--json")
    assert rc == 4


# --- herdr-restart-pending ----------------------------------------------------------


def fake_process(ship, pid: int, argv: list[str], exe: Path) -> None:
    proc = ship.p(f"/proc/{pid}")
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (proc / "exe").symlink_to(exe)


def install_herdr(ship) -> Path:
    path = ship.p("/usr/local/bin/herdr")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("new herdr")
    return path


def codes(doc):
    return [a["code"] for a in doc["attention"]]


def test_herdr_restart_pending_when_the_server_runs_an_old_binary(ship, tmp_path):
    install_herdr(ship)
    old = tmp_path / "old-herdr"
    old.write_text("old herdr")
    fake_process(ship, 4242, ["/usr/local/bin/herdr", "server"], old)
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert_valid("ship-status", doc)
    assert "herdr-restart-pending" in codes(doc)


def test_no_herdr_restart_pending_when_the_server_is_current(ship):
    installed = install_herdr(ship)
    fake_process(ship, 4242, ["/usr/local/bin/herdr", "server"], installed)
    rc, doc, _ = ship.ctl("status", "--json")
    assert "herdr-restart-pending" not in codes(doc)


def test_only_the_herdr_server_counts(ship, tmp_path):
    install_herdr(ship)
    other = tmp_path / "other"
    other.write_text("x")
    fake_process(ship, 100, ["/usr/local/bin/herdr", "agent", "list"], other)
    fake_process(ship, 101, ["/usr/bin/bash", "server"], other)
    fake_process(ship, 102, ["herdr-server-lookalike", "server"], other)
    rc, doc, _ = ship.ctl("status", "--json")
    assert "herdr-restart-pending" not in codes(doc)


def _probe_ctx(ship, agent_uid):
    """A context for calling herdr_restart_pending in-process with an agent uid:
    the sandbox ship-ctl has no agent user, so the owner filter only runs here."""
    from types import SimpleNamespace

    paths = SimpleNamespace(p=ship.p, proc=ship.p("/proc"))
    return SimpleNamespace(paths=paths, agent_owner=(agent_uid, agent_uid))


def test_herdr_pending_looks_only_at_the_agents_processes(ship, tmp_path):
    """A look-alike `herdr server` owned by someone else (root, say) never counts."""
    install_herdr(ship)
    old = tmp_path / "old-herdr"
    old.write_text("old herdr")
    fake_process(ship, 4242, ["/usr/local/bin/herdr", "server"], old)
    me = os.getuid()
    assert status.herdr_restart_pending(_probe_ctx(ship, me)) is True  # the agent's own
    assert status.herdr_restart_pending(_probe_ctx(ship, me + 1)) is False  # someone else's


def test_herdr_pending_compares_device_and_inode(ship, tmp_path, monkeypatch):
    """Same inode number on another filesystem is a different file."""
    installed = install_herdr(ship)
    fake_process(ship, 4242, ["/usr/local/bin/herdr", "server"], installed)
    exe = ship.p("/proc/4242/exe")
    real_stat = os.stat

    def other_device(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if Path(path) == exe:
            fields = list(st[:10])
            fields[2] = st.st_dev + 1  # st_dev
            return os.stat_result(fields)
        return st

    ctx = _probe_ctx(ship, os.getuid())
    assert status.herdr_restart_pending(ctx) is False
    monkeypatch.setattr(status.os, "stat", other_device)
    assert status.herdr_restart_pending(ctx) is True


def test_no_herdr_installed_means_nothing_pending(ship, tmp_path):
    old = tmp_path / "old-herdr"
    old.write_text("old")
    fake_process(ship, 4242, ["herdr", "server"], old)
    rc, doc, _ = ship.ctl("status", "--json")
    assert "herdr-restart-pending" not in codes(doc)


def test_status_document_stays_valid_with_the_guard_on(ship):
    ship.write_toml(GUARD_TOML)
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert_valid("ship-status", doc)
    json.dumps(doc)
