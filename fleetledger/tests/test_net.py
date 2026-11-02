"""Network visibility in FleetLedger: fetching, first-seen ledger, views, search, CLI."""

from __future__ import annotations

import copy
import json
import stat
from datetime import UTC, datetime

import pytest
from test_cli import EXAMPLES, RUNNING, result  # noqa: F401
from test_cli import env as env  # noqa: F401  (fixture)

from fleetledger import net, paths

SUMMARY = json.loads((EXAMPLES / "net-summary" / "running.json").read_text())
NOW = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)
CONTRACTS = {"ship-status": 1, "ship-toml": 1, "ship-ctl-result": 1, "bundle": 1, "seed": 1,
             "runtime": 1, "net-summary": 1}


@pytest.fixture
def netenv(env):  # noqa: F811
    env.summaries = {"ship-01": copy.deepcopy(SUMMARY), "ship-02": copy.deepcopy(SUMMARY)}
    env.contracts = dict(CONTRACTS)
    original = env.ship.__call__

    class Side:
        calls = env.ship.calls
        status_override = env.ship.status_override

        def __call__(self, ship, command):
            if command[:4] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "net"]:
                return 0, json.dumps(result("net", data=env.summaries[ship.name])), ""
            if command[:4] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "version"]:
                return 0, json.dumps(result("version", data={
                    "tooling": "0.1.0", "commit": "0123456789ab", "contracts": env.contracts})), ""
            return original(ship, command)

    from test_cli import ShipSSH

    env.app.ssh_factory = lambda inv: ShipSSH(inv, Side())
    return env


def test_refresh_caches_privately_and_builds_the_ledger(netenv):
    inv = netenv.store.load()
    netenv.summaries["ship-02"]["destinations"][1]["first_seen"] = "2026-01-08T08:00:00Z"
    ssh = netenv.app.ssh_factory(inv)
    got = net.refresh(ssh, inv, inv.ships())
    assert got["ship-01"].problem is None
    assert "new-service.example.net" in got["ship-01"].new_to_fleet
    cached = paths.data_dir() / "net" / "ship-01.json"
    assert stat.S_IMODE(cached.stat().st_mode) == 0o600
    ledger = net.seen_ledger()
    # ship-02 saw it earlier, so the fleet's first sighting is ship-02's.
    assert ledger["fleet"]["new-service.example.net"] == {
        "first_seen": "2026-01-08T08:00:00Z", "first_ship": "ship-02"}


def test_ship_view_flags_first_seen(netenv):
    inv = netenv.store.load()
    net.refresh(netenv.app.ssh_factory(inv), inv, [inv.ship("ship-01")])
    view = net.ship_view(inv, inv.ship("ship-01"), net.cached_summary("ship-01"), now=NOW)
    assert view["available"] and view["tags"] == ["work"]
    assert [r["name"] for r in view["new"]] == ["new-service.example.net"]
    flags = {r["name"]: (r["new"], r["new_to_fleet"]) for r in view["top_7d"]}
    assert flags["new-service.example.net"] == (True, True)
    assert flags["api.example.com"] == (False, False)
    assert view["top_24h"][0]["name"] == "api.example.com"
    assert all(r["connections_24h"] for r in view["top_24h"])
    assert net.ship_view(inv, inv.ship("ship-02"), None)["available"] is False


def test_new_destinations_across_ships_by_tag(netenv):
    inv = netenv.store.load()
    netenv.summaries["ship-01"]["destinations"][1]["first_seen"] = "2026-01-08T10:00:00Z"
    net.refresh(netenv.app.ssh_factory(inv), inv, inv.ships())
    rows = net.new_destinations(inv, now=NOW)
    assert [(r["ship"], r["name"]) for r in rows] == [
        ("ship-01", "new-service.example.net"), ("ship-02", "new-service.example.net")]
    assert [r["new_to_fleet"] for r in rows] == [False, True]  # ship-02 (09:14) was first
    assert [r["ship"] for r in net.new_destinations(inv, "personal", now=NOW)] == ["ship-02"]


def test_who_talked_matches_subdomains_only(netenv):
    inv = netenv.store.load()
    net.refresh(netenv.app.ssh_factory(inv), inv, inv.ships())
    hits = net.who_talked(inv, "example.com")
    assert {(h["ship"], h["name"]) for h in hits} == {("ship-01", "api.example.com"),
                                                      ("ship-02", "api.example.com")}
    assert net.who_talked(inv, "ample.com") == []
    assert net.who_talked(inv, "fd00:db8::55", tag="work")[0]["programs"] == ["ssh"]
    with pytest.raises(net.FleetError):
        net.who_talked(inv, "<script>")


def test_a_summary_that_breaks_the_contract_is_refused(netenv):
    inv = netenv.store.load()
    net.refresh(netenv.app.ssh_factory(inv), inv, [inv.ship("ship-01")])
    netenv.summaries["ship-01"]["destinations"][0]["name"] = "<img src=x>.example.com"
    got = net.refresh(netenv.app.ssh_factory(inv), inv, [inv.ship("ship-01")])
    assert "breaks the contract" in got["ship-01"].problem
    assert got["ship-01"].summary["destinations"][0]["name"] == "api.example.com"  # last good


def test_old_ship_without_net_summary(netenv):
    netenv.contracts.pop("net-summary")
    inv = netenv.store.load()
    got = net.refresh(netenv.app.ssh_factory(inv), inv, [inv.ship("ship-01")])
    assert "predates network visibility" in got["ship-01"].problem


def test_cli(netenv):
    code, out, err = netenv.run("net")
    assert code == 0, err
    assert "ship-01" in out and "running" in out
    code, out, _ = netenv.run("net", "ship-01")
    assert code == 0 and "api.example.com" in out and "Traffic:" in out
    code, out, _ = netenv.run("net", "ship-01", "--json")
    assert json.loads(out)["ship"] == "ship-01"
    code, out, _ = netenv.run("net", "--who", "example.com", "--offline", "--json")
    assert {r["ship"] for r in json.loads(out)} == {"ship-01", "ship-02"}
    code, out, _ = netenv.run("net", "--new", "--offline")
    assert code == 0
    assert netenv.run("net", "ship-01", "--new")[0] == 2
