"""The fleet dashboard: the history store, alerts, the summary and the SVG charts."""

import copy
import json
import re
import sqlite3
import stat
from datetime import UTC, datetime, timedelta

import pytest
from fleetledger.history import History, Sample, sample_of
from fleetledger.web import charts, dashboard
from fleetledger.web.fleet import Item, attention_items
from test_cli import EXAMPLES, RUNNING
from test_web import web  # noqa: F401 - the shared web UI fixture

from fleetledger import paths, status

NOW = 1_800_000_000.0


class Wall:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def view(store, name="ship-01", doc=None, problem=None):
    inv = store.load()
    v = status.ShipView(inv.ship(name), status=None if problem else copy.deepcopy(doc or RUNNING))
    if problem:
        v.problem, v.message = problem, f"Cannot reach {name}"
    elif v.status is not None:
        v.status["ship"]["name"] = name
        v.attention = list(v.status["attention"])
    return v


# --- the history store ---------------------------------------------------------------


def test_history_records_at_most_one_sample_per_gap_and_reads_back(store, home):
    wall = Wall()
    h = History(wall=wall)
    v = view(store)
    assert h.record([v], NOW) == 1
    assert h.record([v], NOW + 60) == 0  # too soon: one sample per 5 minutes
    assert h.record([v], NOW + 300) == 1
    rows = h.since(NOW - 1)["ship-01"]
    assert [r.at for r in rows] == [NOW, NOW + 300]
    host = RUNNING["host"]
    assert rows[0].up and rows[0].mem == host["mem_used_pct"]
    assert rows[0].disk == host["disk_used_pct"]
    assert rows[0].cpu == pytest.approx(min(100, 100 * host["load1"] / host["cpus"]))
    agents = RUNNING["herdr"]["agents"]
    assert rows[0].agents == len(agents)
    assert rows[0].waiting == sum(a["state"] == "blocked" for a in agents)
    # Private: the file is 0600 in the data folder.
    path = paths.data_dir() / "history.sqlite3"
    assert h.path == path and stat.S_IMODE(path.stat().st_mode) == 0o600


def test_history_samples_an_unreachable_ship_as_down(store, home):
    sample = sample_of(view(store, problem="unreachable"), NOW)
    assert sample == Sample("ship-01", NOW, up=False)


def test_history_prunes_old_samples(store, home):
    wall = Wall()
    h = History(wall=wall)
    h.add([Sample("ship-01", NOW - 8 * 86400, True, cpu=1.0),
           Sample("ship-02", NOW - 3600, True, cpu=2.0)])
    wall.t = NOW + 3601  # pruning runs at most hourly
    h.add([Sample("ship-01", NOW, True, cpu=3.0)])
    found = h.since(0)
    assert [s.cpu for s in found["ship-01"]] == [3.0]  # the 8-day-old one is gone


def test_history_starts_again_when_the_file_is_deleted(store, home):
    """docs/web-ui.md: delete the file to start again, while FleetLedger runs."""
    h = History()
    h.record([view(store)], NOW)
    h.path.unlink()
    assert h.since(0) == {}
    assert h.record([view(store)], NOW + 600) == 1
    assert [s.at for s in h.since(0)["ship-01"]] == [NOW + 600]
    assert stat.S_IMODE(h.path.stat().st_mode) == 0o600


def test_cpu_is_capped_at_100(store):
    doc = copy.deepcopy(RUNNING)
    doc["host"].update(load1=9.0, cpus=4)
    assert sample_of(view(store, doc=doc), NOW).cpu == 100.0


def test_history_holds_no_text_but_ship_names(store, home):
    h = History()
    h.record([view(store)], NOW)
    with sqlite3.connect(h.path) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sample)")]
        texts = conn.execute("SELECT DISTINCT ship FROM sample").fetchall()
    assert cols == ["ship", "at", "up", "cpu", "mem", "disk", "agents", "working", "waiting"]
    assert texts == [("ship-01",)]


def test_a_broken_history_file_never_stops_a_poll(web):  # noqa: F811
    class Broken:
        def record(self, views, at):
            raise sqlite3.DatabaseError("file is not a database")

        def since(self, start, ships=None):
            raise sqlite3.DatabaseError("file is not a database")

    web.fleet.history = Broken()
    web.fleet.poll()
    assert web.fleet.snapshot.polled_at is not None
    web.sign_in()
    page = web.client.get("/")
    assert page.status_code == 200 and "history file could not be read" in page.text
    assert web.client.get("/ships/ship-01").status_code == 200


def test_the_poller_records_history(web, home):  # noqa: F811
    web.fleet.history = History()
    web.fleet.poll(["ship-02"])  # one Ship re-checked (Check now) is sampled too
    assert set(web.fleet.history.since(0)) == {"ship-02"}
    web.fleet.poll()
    assert set(web.fleet.history.since(0)) == {"ship-01", "ship-02"}


# --- alerts and the summary --------------------------------------------------------------


def test_alerts_are_ranked_with_a_fix_each(store):
    full = copy.deepcopy(RUNNING)
    full["host"]["disk_used_pct"] = 93.0
    full["attention"] = [
        {"code": "reboot-required", "message": "Reboot."},
        {"code": "host-cert-expiring", "subject": "ssh", "message": "Soon."},
        {"code": "signin-expired", "subject": "claude", "message": "Expired."},
        {"code": "behind-tooling", "subject": "tooling", "message": "Old."},
        {"code": "agent-waiting", "subject": "main", "message": "Waiting."},
    ]
    views = [view(store, "ship-01", full), view(store, "ship-02", problem="unreachable")]
    items = attention_items(views)
    found = dashboard.alerts(views, items, [], store.load())
    levels = [a.level for a in found]
    assert levels == sorted(levels, key=dashboard.LEVELS.index)  # critical first
    by_code = {a.code: a for a in found}
    assert by_code["unreachable"].level == "critical"
    assert by_code["disk-nearly-full"].level == "critical"  # >= 90%
    assert by_code["signin-expired"].action.kind == "signin"
    assert by_code["signin-expired"].action.value == "claude"
    reboot = by_code["reboot-required"].action
    assert (reboot.kind, reboot.value, reboot.title) == ("post", "/operations",
                                                         "ship-fleet reboot ship-01")
    assert dict(reboot.fields) == {"operation": "reboot", "ship": "ship-01"}
    assert reboot.confirm.danger and reboot.confirm.title == "Reboot ship-01?"
    renew = by_code["host-cert-expiring"].action
    assert (renew.kind, renew.value, renew.title) == ("post", "/ships/ship-01/certs/renew",
                                                      "ship-fleet ca sign ship-01")
    assert by_code["behind-tooling"].level == "notice"
    assert dict(by_code["behind-tooling"].action.fields)["operation"] == "update"
    assert by_code["agent-waiting"].waiting
    assert by_code["agent-waiting"].action.value == "ship-fleet ssh ship-01"  # not trusted here
    assert by_code["disk-nearly-full"].action.kind == "command"


def test_a_waiting_agent_gets_herdr_remote_only_when_it_works_from_here(store, monkeypatch):
    from fleetledger import trustcmd

    views = [view(store)]
    monkeypatch.setattr(trustcmd, "attaches_here", lambda inv, ship: ship.name == "ship-01")
    found = dashboard.alerts(views, attention_items(views), [], store.load())
    waiting = next(a for a in found if a.code == "agent-waiting")
    assert waiting.action.value == "herdr --remote ship-01"


@pytest.mark.parametrize("disk, level", [
    (79.9, None), (80.0, "medium"), (85.0, "medium"), (89.9, "medium"), (90.0, "high"),
])
def test_disk_thresholds(store, disk, level):
    doc = copy.deepcopy(RUNNING)
    doc["host"]["disk_used_pct"] = disk
    found = dashboard.derived([view(store, doc=doc)], None, [])
    assert [(i.code, i.level) for i in found] == ([("disk-nearly-full", level)] if level else [])


def test_off_the_overlay_only_when_one_is_configured(store):
    inv = store.load()
    v = view(store)
    assert dashboard.derived([v], inv, []) == []  # no [overlay] in the inventory
    inv.doc["overlay"] = {"provider": "tailscale"}
    found = dashboard.derived([v], inv, [])
    assert [i.code for i in found] == ["not-on-overlay"]
    join = dashboard.action_for("not-on-overlay", "ship-01", v, None)
    assert (join.kind, dict(join.fields)["operation"]) == ("post", "join")
    assert join.title == "ship-fleet overlay join ship-01"
    # The Ship already says its overlay is down: one alert, not two.
    down = [Item("ship-01", "overlay-down", "Down.", "medium")]
    assert dashboard.derived([v], inv, down) == []


@pytest.mark.parametrize("code, kind", [
    ("reboot-required", "post"), ("herdr-restart-pending", "post"), ("behind-tooling", "post"),
    ("not-on-overlay", "post"), ("unreachable", "post"),
    ("host-cert-expiring", "post"), ("ssh-trust-behind", "post"),
    ("overlay-down", "link"),  # to Settings ▸ Access ▸ Overlay, where Check is
    # These need a shell: copy the command.
    ("agent-waiting", "command"), ("disk-nearly-full", "command"),
])
def test_fixes_the_ui_can_do_are_buttons(code, kind):
    action = dashboard.action_for(code, "ship-01", None, None)
    assert action.kind == kind
    if kind == "post":
        assert action.title.startswith("ship-fleet ")  # the CLI equivalent, as a tooltip
        # Anything that changes the Ship goes through the dialog; checking again and
        # renewing a certificate (no access changes) do not.
        assert (action.confirm is None) == (code in ("unreachable", "host-cert-expiring",
                                                     "ssh-trust-behind"))


def test_unknown_codes_link_to_the_ship(store):
    action = dashboard.action_for("brand-new-code", "ship-01", None, None)
    assert (action.kind, action.value) == ("link", "/ships/ship-01")
    assert dashboard.action_for("provision-failed", "ship-01", None, None).value == \
        "/ships/ship-01/logs?source=provision"


def test_summary_counts(store):
    doc = copy.deepcopy(RUNNING)
    doc["host"]["reboot_required"] = True
    doc["host"]["updates_pending"] = 4
    doc["herdr"]["agents"].append({"name": "extra", "kind": "claude", "state": "working",
                                   "mode": "auto"})
    views = [view(store, "ship-01", doc), view(store, "ship-02", problem="unreachable")]
    alerts = dashboard.alerts(views, attention_items(views), [], store.load())
    s = dashboard.summary(views, alerts, store.load())
    agents = doc["herdr"]["agents"]
    assert (s["ships"], s["up"], s["down"]) == (2, 1, 1)
    assert s["agents"] == len(agents)
    assert s["waiting"] == sum(a["state"] == "blocked" for a in agents)
    assert s["working"] == sum(a["state"] == "working" for a in agents)
    assert s["working"] != s["waiting"]  # the fixture can tell them apart
    assert s["remote_control"] == (len(agents) if RUNNING["claude"]["remote_control"] == "on"
                                   else 0)
    assert s["reboots"] == 1 and (s["os_updates"], s["os_update_ships"]) == (4, 1)
    assert s["levels"]["critical"] >= 1
    assert s["overlay"] is None


# --- charts ---------------------------------------------------------------------------------


def test_line_chart_breaks_at_gaps_and_marks_lone_points():
    starts = [NOW + i * 60 for i in range(5)]
    c = charts.line_chart(starts, 60, [charts.Series("s1", "CPU", [10, 20, None, 40, None])])
    d = c["lines"][0]["d"]
    assert d.count("M") == 2 and d.count("L") == 1  # two runs: [10, 20] and [40]
    assert len(c["lines"][0]["dots"]) == 1  # the lone 40
    assert c["lines"][0]["end"]["value"] == "40%"
    assert c["columns"][2]["title"].endswith("no data")
    assert [r["cells"] for r in c["table"]["rows"]] == [["40%"], ["20%"], ["10%"]]


def test_empty_chart_says_so():
    c = charts.line_chart([NOW], 60, [charts.Series("s1", "CPU", [None])])
    assert c["empty"] and c["lines"][0]["end"] is None


def test_axis_tops_are_round():
    assert [charts.nice_max(v) for v in (0, 0.7, 3, 13, 180, 2400)] == [1, 1, 5, 20, 200, 2500]
    c = charts.stacked_columns([NOW, NOW + 60], 60, [charts.Series("s1", "Working", [1, 3])])
    assert [t["label"] for t in c["yticks"]] == ["0", "3", "6"]  # counts: whole numbers


def test_buckets_end_at_now():
    starts = charts.buckets(3600, 900, now=NOW + 10)
    assert len(starts) == 4 and starts[-1] <= NOW + 10 < starts[-1] + 900


def test_fleet_charts_from_history(store, home):
    h = History()
    v1, v2 = view(store, "ship-01"), view(store, "ship-02")
    h.add([Sample("ship-01", NOW - 600, True, cpu=10, mem=40, disk=50, agents=2, working=1,
                  waiting=1),
           Sample("ship-02", NOW - 600, True, cpu=30, mem=60, disk=95, agents=1, working=1,
                  waiting=0),
           Sample("ship-02", NOW - 7200, False)])
    c = dashboard.fleet_charts(h, [v1, v2], "6h", NOW)
    assert c["has_history"]
    avg, top = (line["end"]["value"] for line in c["cpu"]["lines"])
    assert (avg, top) == ("20%", "30%")
    latest = [col for col in c["agents"]["columns"] if col["segments"]][-1]
    assert latest["title"].endswith("Working 2; Waiting for you 1; Idle 0")
    up = c["up"]["lines"][0]
    assert up["end"]["value"] == "2"
    assert "Ships answering 0" in c["up"]["columns"][-25]["title"]  # ship-02 down, 2 h ago
    # Disk now comes from the live status, fullest first.
    assert [r["label"] for r in c["disk_now"]["rows"]][0] in ("ship-01", "ship-02")
    assert dashboard.fleet_charts(None, [v1], "24h", NOW)["has_history"] is False


def test_traffic_chart_sums_cached_hourly_summaries(store, home):
    from fleetledger import net
    summary = json.loads((EXAMPLES / "net-summary" / "running.json").read_text())
    hour = datetime.fromtimestamp(NOW, UTC).replace(minute=0, second=0) - timedelta(hours=1)
    stamp = hour.strftime("%Y-%m-%dT%H:%M:%SZ")
    summary["totals"]["hourly"] = [{"hour": stamp, "rx_mb": 5.0, "tx_mb": 1.0}]
    folder = paths.private_dir(net.net_dir())
    for name in ("ship-01", "ship-02"):
        (folder / f"{name}.json").write_text(json.dumps(summary))
    c = dashboard.traffic_chart([view(store, "ship-01"), view(store, "ship-02")], "6h", NOW)
    assert c["sources"] == 2
    filled = [col for col in c["columns"] if col["segments"]]
    assert len(filled) == 1 and filled[0]["title"].endswith("Received 10 MB; Sent 2.0 MB")


def test_traffic_chart_ignores_bad_hours(store, home):
    from fleetledger import net
    folder = paths.private_dir(net.net_dir())
    (folder / "ship-01.json").write_text("not json")
    assert dashboard.traffic_chart([view(store)], "24h", NOW)["empty"]


def test_sparklines_per_ship(store, home):
    h = History()
    h.add([Sample("ship-01", NOW - 1800, True, cpu=50, mem=20, disk=1)])
    sparks = dashboard.sparklines(h, [view(store, "ship-01"), view(store, "ship-02")], NOW)
    assert not sparks["ship-01"]["cpu"]["empty"] and sparks["ship-02"]["cpu"]["empty"]


def test_range_keys():
    assert dashboard.range_key("7d") == "7d"
    assert dashboard.range_key("forever") == dashboard.range_key(None) == "24h"


# --- the pages ------------------------------------------------------------------------------


def test_dashboard_page(web, home):  # noqa: F811
    web.fleet.history = History()
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/?range=7d").text
    assert "Ships up" in page and "Needs attention" in page and "The last 7 days" in page
    assert 'class="sev-tag lvl-warning"' in page  # the waiting agent: icon and word
    assert 'data-copy="ship-fleet ssh ship-01"' in page  # the fix, copyable
    assert '<svg class="plot"' in page and "spark-plot" in page
    assert not re.search(r"\sstyle=", page)  # the CSP allows no inline styles
    assert "<script>" not in page


def test_tiles_say_last_known_before_the_first_poll(web):  # noqa: F811
    """Until a poll finishes, the cached status is not news: no "all answering"."""
    web.ship.down.add("ship-02")
    web.fleet.load_cached()  # as at start-up: the last good status, not polled yet
    web.sign_in()
    page = web.client.get("/").text
    assert "all answering" not in page and "not checked yet" in page
    assert 'class="tiles stale"' in page
    web.fleet.poll()
    page = web.client.get("/").text
    assert "not checked yet" not in page and "1 not answering" in page


def test_ship_page_alerts_match_the_dashboard(web):  # noqa: F811
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    assert 'class="sev-tag lvl-warning"' in page  # the waiting agent: icon and word
    assert 'data-copy="ship-fleet ssh ship-01"' in page  # and its fix


def test_charts_partial_answers_204_when_nothing_changed(web):  # noqa: F811
    web.sign_in()
    first = web.client.get("/partials/charts?range=6h")
    assert first.status_code == 200 and "The last 6 hours" in first.text
    version = re.search(r"v=([0-9]+)", first.text).group(1)
    assert web.client.get(f"/partials/charts?range=6h&v={version}").status_code == 204
    web.fleet.poll()
    assert web.client.get(f"/partials/charts?range=6h&v={version}").status_code == 200


def test_ship_page_has_its_history(web, home):  # noqa: F811
    web.fleet.history = History()
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/ships/ship-01?range=6h").text
    assert "CPU, memory and disk" in page and "The last 6 hours" in page
    assert not re.search(r"\sstyle=", page)


def test_the_dashboard_shows_no_secrets(web):  # noqa: F811
    doc = copy.deepcopy(RUNNING)
    doc["auth"]["github"]["detail"] = "token ghp_EXAMPLESECRET"
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    assert "ghp_EXAMPLESECRET" not in web.client.get("/").text
