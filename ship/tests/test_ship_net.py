"""Network visibility: the ship-netwatch collector and `ship-ctl net` (contracts/ship-ctl.md).

The collector's parsers and store are tested directly with recorded-style tool
output; `ship-ctl net` and the status `net` section run in the sandbox Ship with
a store file and fake systemctl and vnstat.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ship_harness import assert_valid, ship  # noqa: F401  (fixture)

LIBEXEC = Path(__file__).resolve().parents[1] / "files" / "libexec" / "ship_netwatch.py"
_spec = importlib.util.spec_from_file_location("ship_netwatch", LIBEXEC)
nw = importlib.util.module_from_spec(_spec)
sys.modules["ship_netwatch"] = nw
_spec.loader.exec_module(nw)

NOW = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, when=NOW):
        self.when = when

    def __call__(self):
        return self.when


def collector(store=None):
    logged = []
    c = nw.Collector(store or nw.Store(now=NOW), nw.Names(), clock=Clock(), log=logged.append)
    return c, logged


def settle(c):
    """Let connections that waited for a DNS name be recorded as addresses."""
    c.clock.when += nw.Collector.NAME_WAIT
    c.flush()


def dns_line(name, *answers, cname=None):
    rrs = []
    if cname:
        rrs.append({"rr": {"key": {"class": 1, "type": 5, "name": name}, "name": cname}})
    for a in answers:
        raw = list(bytes(map(int, a.split(".")))) if "." in a else a
        rrs.append(
            {
                "rr": {"key": {"class": 1, "type": 1, "name": cname or name}, "address": raw},
                "raw": "AAAA",
            }
        )
    return json.dumps(
        {
            "state": "success",
            "result": "success",
            "rcode": 0,
            "question": [{"class": 1, "type": 1, "name": name}],
            "answer": rrs,
        }
    )


# --- parsers -----------------------------------------------------------------------
def test_parse_tcpconnect():
    # As tcpconnect-libbpf -t prints it on Ubuntu 26.04 (trailing spaces included).
    real = "5.955    8824   HTTP Client      6  2001:db8::5 2001:db8::10    443  "
    assert nw.parse_tcpconnect(real) == (8824, "HTTP_Client", "2001:db8::10", 443)
    assert nw.parse_tcpconnect("TIME(s)  PID     COMM         IP SADDR    DADDR   DPORT") is None
    assert nw.parse_tcpconnect("0.000    4021    curl         4  192.0.2.5  198.51.100.7  443") == (
        4021,
        "curl",
        "198.51.100.7",
        443,
    )
    assert nw.parse_tcpconnect("1.5 77 Web Content 6 2001:db8::5 2001:db8::10 443") == (
        77,
        "Web_Content",
        "2001:db8::10",
        443,
    )
    assert nw.parse_tcpconnect("1.5 77 x 4 192.0.2.5 not-an-ip 443") is None


def test_parse_tcplife():
    line = "4021  curl       192.0.2.5  51122 198.51.100.7  443   12   340  85.21"
    assert nw.parse_tcplife(line) == (
        4021,
        "curl",
        "192.0.2.5",
        51122,
        "198.51.100.7",
        443,
        12 * 1024,
        340 * 1024,
    )
    assert nw.parse_tcplife("PID   COMM  LADDR LPORT RADDR RPORT TX_KB RX_KB MS") is None


def test_parse_dns_keeps_the_question_name_through_cnames():
    pairs = nw.parse_dns_event(dns_line("www.example.com", "198.51.100.7", cname="cdn.example.net"))
    assert pairs == [("198.51.100.7", "www.example.com")]
    v6 = json.dumps(
        {
            "question": [{"name": "example.org"}],
            "answer": [{"rr": {"key": {"name": "example.org"}, "address": list(bytes(16))}}],
        }
    )
    assert nw.parse_dns_event(v6) == [("::", "example.org")]
    assert nw.parse_dns_event("not json") == []
    assert nw.parse_dns_event(json.dumps({"question": "x", "answer": 5})) == []


def test_untrusted_names_and_programs_are_cleaned():
    bad = dns_line("<img src=x>.example.com", "198.51.100.9")
    assert nw.parse_dns_event(bad) == []
    assert nw.clean_name("Example.COM.") == "example.com"
    assert nw.clean_name("a" * 64 + ".example.com") is None
    assert nw.clean_program("cl\x1b[2Jaude") == "cl__2Jaude"
    assert nw.clean_program("") == "?"
    assert len(nw.clean_program("x" * 99)) == 32


def test_parse_ss_reads_both_ss_layouts():
    """`ss -tnpH` starts each line with the state; with a state filter ss drops it."""
    unfiltered = (
        "ESTAB      0      0        192.0.2.5:56742        198.51.100.7:443"
        '  users:(("chrome",pid=4082,fd=50))            \n'
        "TIME-WAIT  0      0        192.0.2.5:56744        198.51.100.7:443\n"
    )
    filtered = (
        "0      0                                  192.0.2.5:56742"
        '                198.51.100.7:443  users:(("chrome",pid=4082,fd=50)) \n'
    )
    want = [("192.0.2.5", 56742, "198.51.100.7", 443, "chrome")]
    assert nw.parse_ss(unfiltered) == want
    assert nw.parse_ss(filtered) == want


def test_parse_ss_and_listening_ports():
    est = (
        'ESTAB 0 0 192.0.2.5:51122 198.51.100.7:443 users:(("node",pid=88,fd=21))\n'
        'ESTAB 0 0 192.0.2.5:22 192.0.2.99:60001 users:(("sshd",pid=9,fd=4))\n'
        "ESTAB 0 0 [2001:db8::5]:40000 [2001:db8::10]:443\n"
    )
    assert nw.parse_ss(est) == [
        ("192.0.2.5", 51122, "198.51.100.7", 443, "node"),
        ("192.0.2.5", 22, "192.0.2.99", 60001, "sshd"),
        ("2001:db8::5", 40000, "2001:db8::10", 443, "?"),
    ]
    assert nw.listening_ports("LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 5 [::]:22 [::]:*") == {
        22
    }


def test_addresses_are_written_canonically():
    """Contract: no zone index, IPv4-mapped IPv6 as plain IPv4."""
    line = "1.5 77 curl 6 fe80::5%eth0 fe80::1%eth0 443"
    assert nw.parse_tcpconnect(line) == (77, "curl", "fe80::1", 443)
    line = "1.5 77 curl 6 ::ffff:192.0.2.5 ::ffff:198.51.100.7 443"
    assert nw.parse_tcpconnect(line) == (77, "curl", "198.51.100.7", 443)
    est = "ESTAB 0 0 [fe80::5%eth0]:40000 [::ffff:198.51.100.7]:443\n"
    assert nw.parse_ss(est) == [("fe80::5", 40000, "198.51.100.7", 443, "?")]
    for raw in ("fe80::1%eth0", "[::ffff:192.0.2.1]", "::FFFF:192.0.2.1"):
        assert "%" not in nw.clean_address(raw) and "ffff" not in nw.clean_address(raw)


def test_ship_patterns_match_the_contract(monkeypatch):
    ship_dir = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(ship_dir / "lib"))
    from shipctl import net

    schema = json.loads((ship_dir / "schemas/net-summary.schema.json").read_text())
    dest = schema["properties"]["destinations"]["items"]["properties"]
    assert net.NAME.pattern == dest["name"]["pattern"]
    assert net.ADDRESS.pattern == dest["addresses"]["items"]["pattern"]


def test_scope():
    assert nw.scope_of("127.0.0.1") == "local"
    assert nw.scope_of("10.1.2.3") == "lan" and nw.scope_of("100.64.0.1") == "lan"  # hygiene: allow
    assert nw.scope_of("fd00::1") == "lan" and nw.scope_of("fe80::1") == "lan"
    assert nw.scope_of("8.8.8.8") == "internet"


# --- the collector -------------------------------------------------------------------
def test_connections_are_named_by_dns_and_new_ones_logged_once():
    c, logged = collector()
    c.on_dns(dns_line("api.example.com", "198.51.100.7"))
    c.on_tcpconnect("0.0 4021 curl 4 192.0.2.5 198.51.100.7 443")
    c.on_tcpconnect("0.1 4022 curl 4 192.0.2.5 198.51.100.7 443")
    c.on_tcpconnect("0.2 4023 node 4 192.0.2.5 203.0.113.9 8443")
    c.on_tcpconnect("0.3 4024 node 4 127.0.0.1 127.0.0.1 8080")  # loopback: ignored
    settle(c)
    d = c.store.destinations
    assert set(d) == {"api.example.com", "203.0.113.9"}
    api = d["api.example.com"]
    assert api["kind"] == "domain" and api["ports"] == [443]
    assert api["hours"]["2026-01-08T12"]["c"] == 2
    assert api["hours"]["2026-01-08T12"]["p"] == {"curl": 2}
    assert d["203.0.113.9"]["kind"] == "address"
    assert d["203.0.113.9"]["hours"]["2026-01-08T12"]["c"] == 1
    assert logged == [
        "new destination: api.example.com (198.51.100.7 port 443, curl)",
        "new destination: 203.0.113.9 (203.0.113.9 port 8443, node)",
    ]


def test_a_dns_answer_arriving_after_the_connection_still_names_it():
    """The two tools race: curl's connect can be printed before resolvectl's answer."""
    c, _ = collector()
    c.on_tcpconnect("0.0 8794 curl 6 2001:db8::5 2001:db8::10 443")
    c.on_tcplife("8794 curl 2001:db8::5 47988 2001:db8::10 443 1.88 6.31 59.51")
    assert c.store.destinations == {}  # waiting for a name
    c.on_dns(json.dumps({"question": [{"class": 1, "type": 28, "name": "example.com"}],
                         "answer": [{"rr": {"key": {"class": 1, "type": 28, "name": "example.com"},
                                            "address": list(bytes.fromhex(
                                                "20010db8000000000000000000000010"))},
                                     "raw": "AAAA", "ifindex": 2}]}))
    dest = c.store.destinations["example.com"]
    bucket = dest["hours"]["2026-01-08T12"]
    assert (bucket["c"], bucket["tx"], bucket["rx"]) == (1, 1925, 6461)
    settle(c)
    assert set(c.store.destinations) == {"example.com"}


def test_bytes_go_only_to_connections_the_ship_opened():
    c, _ = collector()
    c.on_tcpconnect("0.0 4021 curl 4 192.0.2.5 198.51.100.7 443")
    c.on_tcplife("4021 curl 192.0.2.5 51122 198.51.100.7 443 2.00 30.00 85.2")
    # An SSH session into the Ship: its remote port is the client's.
    c.on_tcplife("900 sshd 192.0.2.5 22 192.0.2.99 60001 500.00 9000.00 120000.0")
    settle(c)
    bucket = c.store.destinations["198.51.100.7"]["hours"]["2026-01-08T12"]
    assert (bucket["tx"], bucket["rx"]) == (2048, 30 * 1024)
    assert "192.0.2.99" not in c.store.destinations


def test_store_prunes_caps_and_remembers_first_seen(monkeypatch):
    store = nw.Store(now=NOW)
    old = NOW - timedelta(days=8)
    store.record_connect("old.example.com", "198.51.100.1", 443, "curl", old)
    store.record_connect("new.example.com", "198.51.100.2", 443, "curl", NOW)
    store.prune(NOW)
    assert set(store.destinations) == {"new.example.com"}
    assert store.first_seen["old.example.com"] == nw.utc(old)  # still known: not "new" again
    store.record_connect("old.example.com", "198.51.100.1", 443, "curl", NOW)
    assert store.destinations["old.example.com"]["first_seen"] == nw.utc(old)
    monkeypatch.setattr(nw, "MAX_DESTINATIONS", 2)
    store.record_connect("third.example.com", "198.51.100.3", 443, "curl", NOW + timedelta(1))
    assert len(store.destinations) == 2 and store.dropped == 1


def test_store_saves_privately_and_reloads(tmp_path):
    store = nw.Store(now=NOW)
    store.record_connect("api.example.com", "198.51.100.7", 443, "curl", NOW)
    store.save(tmp_path / "net")
    path = tmp_path / "net" / "state.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = nw.load_store(tmp_path / "net")
    assert again.destinations == store.destinations
    assert nw.load_store(tmp_path / "missing").destinations == {}


def test_ss_poller_counts_each_new_outbound_connection_once():
    c, _ = collector()
    # As ss prints them (iproute2 6.x): column padding and trailing spaces included.
    out = {
        "ss -tlnH": (
            "LISTEN 0      4096         0.0.0.0:22         0.0.0.0:*\n"
            "LISTEN 0      4096            [::]:22            [::]:*\n"
        ),
        "ss -tnpH": (
            "ESTAB      0      0                 192.0.2.5:51122       198.51.100.7:443"
            '   users:(("node",pid=88,fd=21))             \n'
            "ESTAB      0      0                 192.0.2.5:22           192.0.2.99:60001"
            '  users:(("sshd-session",pid=9,fd=4))      \n'
            "SYN-SENT   0      1                 192.0.2.5:51130       198.51.100.8:443"
            '   users:(("curl",pid=91,fd=5))             \n'
            "CLOSE-WAIT 1      0                 192.0.2.5:51124       198.51.100.9:443"
            '   users:(("node",pid=88,fd=22))             \n'
        ),
    }
    poller = nw.SSPoller(c, lambda argv: out[" ".join(argv)])
    poller.poll()
    poller.poll()
    settle(c)
    assert set(c.store.destinations) == {"198.51.100.7"}
    assert c.store.destinations["198.51.100.7"]["hours"]["2026-01-08T12"]["c"] == 1


# --- ship-ctl net and status ---------------------------------------------------------------
def write_store(ship, doc):  # noqa: F811
    folder = ship.p("/var/lib/shipshape/net")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(json.dumps(doc))


def install_unit(ship, active=True):  # noqa: F811
    unit = ship.p("/etc/systemd/system/ship-netwatch.service")
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text("[Service]\n")
    ship.set(active={"ship-netwatch.service": "active" if active else "failed"})


def recorded_store():
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    s = nw.Store(now=now - timedelta(days=3))
    s.source = "ebpf"
    s.record_connect("api.example.com", "198.51.100.7", 443, "claude", now - timedelta(days=3))
    s.record_connect("api.example.com", "198.51.100.7", 443, "claude", now)
    s.record_bytes("api.example.com", 1000, 50000, now)
    s.record_connect(None, "198.51.100.9", 8443, "node", now - timedelta(days=2))
    s.record_connect("new.example.net", "203.0.113.4", 443, "curl", now)
    return s.to_doc()


def vnstat_doc():
    now = int(datetime.now(UTC).timestamp()) // 3600 * 3600
    hours = [
        {"id": i, "timestamp": now - 3600 * i, "rx": 2_000_000, "tx": 500_000} for i in range(200)
    ]
    return {
        "vnstatversion": "2.12",
        "jsonversion": "2",
        "interfaces": [
            {"name": "ens18", "traffic": {"hour": hours}},
            {"name": "lo", "traffic": {"hour": hours}},
        ],
    }


def test_net_summary(ship):  # noqa: F811
    install_unit(ship)
    write_store(ship, recorded_store())
    ship.set(vnstat=json.dumps(vnstat_doc()))
    rc, doc, out = ship.ctl("net", "--json")
    assert rc == 0, out
    assert_valid("ship-ctl-result", doc)
    data = doc["data"]
    assert_valid("net-summary", data)
    assert data["collector"]["state"] == "running" and data["collector"]["source"] == "ebpf"
    by_name = {d["name"]: d for d in data["destinations"]}
    api = by_name["api.example.com"]
    assert api["connections_7d"] == 2 and api["connections_24h"] == 1
    assert (api["tx_bytes_24h"], api["rx_bytes_24h"]) == (1000, 50000)
    assert by_name["198.51.100.9"]["kind"] == "address"
    assert by_name["198.51.100.9"]["tx_bytes_7d"] is None  # never measured
    assert data["destinations"][0]["name"] == "api.example.com"  # busiest first
    progs = {p["name"]: p for p in data["programs"]}
    assert progs["claude"]["connections_7d"] == 2 and progs["claude"]["connections_24h"] == 1
    t = data["totals"]
    assert t["source"] == "vnstat" and [i["name"] for i in t["interfaces"]] == ["ens18"]
    assert t["rx_mb_24h"] == pytest.approx(25 * 2.0, abs=2.1)  # 24 or 25 hourly slots
    assert t["rx_mb_7d"] == pytest.approx(169 * 2.0, abs=2.1)


def test_net_summary_keeps_the_500_busiest_and_counts_the_rest(ship):  # noqa: F811
    install_unit(ship)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    s = nw.Store(now=now)
    s.source = "ebpf"
    for i in range(600):
        for _ in range(2 if i < 10 else 1):  # ten busier ones
            s.record_connect(f"d{i}.example.com", "198.51.100.7", 443, "curl", now)
    write_store(ship, s.to_doc())
    rc, doc, out = ship.ctl("net", "--json")
    assert rc == 0, out
    data = doc["data"]
    assert_valid("net-summary", data)
    assert len(data["destinations"]) == 500 and data["truncated"] == 100
    names = {d["name"] for d in data["destinations"]}
    assert {f"d{i}.example.com" for i in range(10)} <= names


def test_status_net_section_and_new_destinations(ship):  # noqa: F811
    install_unit(ship)
    write_store(ship, recorded_store())
    rc, status, out = ship.ctl("status", "--json")
    assert rc == 0, out
    assert_valid("ship-status", status)
    net = status["net"]
    assert net["collector"] == "running" and net["window_h"] == 24
    assert net["new_destinations"] == ["new.example.net"]  # api.example.com is 3 days old
    assert net["destinations"] == 2 and net["connections"] == 2
    assert net["rx_mb"] == 0.1  # no vnstat: the collector's own byte counts
    assert "net-collector-down" not in [a["code"] for a in status["attention"]]


def test_stopped_collector_is_flagged(ship):  # noqa: F811
    install_unit(ship, active=False)
    write_store(ship, recorded_store())
    rc, status, _ = ship.ctl("status", "--json")
    assert status["net"]["collector"] == "stopped"
    assert "net-collector-down" in [a["code"] for a in status["attention"]]
    rc, doc, _ = ship.ctl("net", "--json")
    assert doc["data"]["collector"]["state"] == "stopped"
    assert doc["data"]["destinations"]  # the last saved summary is still reported


def test_without_collector(ship):  # noqa: F811
    rc, status, _ = ship.ctl("status", "--json")
    assert status["net"] is None
    rc, doc, _ = ship.ctl("net", "--json")
    assert rc == 0
    assert_valid("net-summary", doc["data"])
    assert doc["data"]["collector"]["state"] == "unavailable"


def test_a_tampered_store_cannot_inject_names(ship):  # noqa: F811
    install_unit(ship)
    doc = recorded_store()
    hour = next(iter(doc["destinations"]["api.example.com"]["hours"]))
    doc["destinations"]["<script>alert(1)</script>"] = {
        "kind": "domain",
        "scope": "internet",
        "first_seen": "2026-01-01T00:00:00Z",
        "last_seen": "2026-01-01T00:00:00Z",
        "ports": [443],
        "addresses": ["x y"],
        "hours": {hour: {"c": 5, "tx": None, "rx": None, "p": {}}},
    }
    doc["destinations"]["api.example.com"]["hours"][hour]["p"]["bad\nname"] = 3
    write_store(ship, doc)
    rc, out, _ = ship.ctl("net", "--json")
    data = out["data"]
    assert_valid("net-summary", data)
    names = [d["name"] for d in data["destinations"]]
    assert "<script>alert(1)</script>" not in names
    assert all(p["name"] != "bad\nname" for p in data["programs"])


def test_collector_unit_is_hardened():
    unit = Path(__file__).resolve().parents[1] / "files/systemd/system/ship-netwatch.service"
    text = unit.read_text()
    for line in ("ProtectHome=yes", "NoNewPrivileges=yes", "ReadWritePaths=/var/lib/shipshape/net"):
        assert line in text.splitlines()
    assert "ship_netwatch.py" in text and os.path.basename(LIBEXEC) in text
