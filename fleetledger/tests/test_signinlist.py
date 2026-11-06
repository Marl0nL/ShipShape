"""Every sign-in on every Ship, most urgent first (signinlist.py, ship-fleet signins)."""

from __future__ import annotations

import copy
import json
import time
from types import SimpleNamespace

from fleetledger.inventory import Ship
from test_cli import RUNNING, env  # noqa: F401 - env is a fixture

from fleetledger import signinlist, status

T0 = "2026-11-01T10:00:00Z"


def ship(name, used=None, tags=("work",)):
    return Ship(name=name, id=f"id-{name}", driver="manual", names=(f"{name}.local",),
                host_key="ssh-ed25519 AAAA", tags=tuple(tags),
                signins=None if used is None else tuple(used))


def view(name, auth=None, attention=(), used=None, problem=None, tools=None, since=None):
    """A Ship view: every sign-in ok unless auth says otherwise ({service: (state,
    account)} or {service: {...entry}})."""
    doc = copy.deepcopy(RUNNING)
    doc["auth"]["gcloud_adc"].update(state="ok", account="work@example.com")
    for service, value in (auth or {}).items():
        entry = value if isinstance(value, dict) else {"state": value[0], "account": value[1]}
        doc["auth"][service].update(entry)
    if tools is not None:
        doc["tools"] = tools
    v = status.ShipView(ship(name, used), status=None if problem else doc)
    v.problem = problem
    v.message = problem and f"Cannot reach {name}."
    v.attention = list(attention)
    v.auth_since = dict(since or {})
    return v


def flow(ship_name, provider, state="code", outcome=None, started=1000.0, deadline=None,
         detail=""):
    return SimpleNamespace(id=f"1-{ship_name[-2:]}{provider[:2]}0000"[:10], batch="ab" * 6,
                           ship=ship_name, provider=provider, label=provider.title(),
                           state=state, done=state == "done", outcome=outcome,
                           detail=detail, started_at=started, deadline=deadline)


def keys(rows):
    return [(r.ship, r.service, r.state) for r in rows]


def test_every_sign_in_of_every_ship_is_a_row():
    rows = signinlist.rows([view("ship-01"), view("ship-02")], None)
    assert len(rows) == 8
    assert {r.group for r in rows} == {"healthy"}
    # healthy rows keep the Ships' and services' own order
    assert keys(rows)[:4] == [("ship-01", s, "ok") for s in signinlist.SERVICES]


def test_needs_you_first_then_degraded_then_healthy_then_off():
    views = [
        view("ship-01", auth={"gcloud": {"state": "expiring", "account": "work@example.com",
                                         "expires_at": "2026-11-09T00:00:00Z"}}),
        view("ship-02", auth={"claude": ("expired", "work@example.com")}),
        view("ship-03", problem="unreachable"),
        view("ship-04", used=["claude", "github"]),
        view("ship-05", auth={"github": ("missing", None)},
             attention=[{"code": "tool-waiting", "subject": "gh-axi", "message": "m"}],
             tools={"gh-axi": {"state": "waiting", "version": None,
                               "waiting_for": ["github"]}}),
    ]
    rows = signinlist.rows(views, None)
    groups = [r.group for r in rows]
    assert groups == sorted(groups, key=signinlist.GROUPS.index)
    assert keys(rows)[:2] == [("ship-02", "claude", "expired"), ("ship-05", "github", "missing")]
    assert rows[0].level == "critical" and rows[1].level == "warning"
    assert "gh-axi waits for it" in rows[1].message
    degraded = [r for r in rows if r.group == "degraded"]
    assert degraded[0].state == "expiring" and "9 Nov 00:00 UTC" in degraded[0].message
    assert {r.ship for r in degraded[1:]} == {"ship-03"}  # not answering: unknown
    assert all(r.state == "unknown" and not r.answers for r in degraded[1:])
    assert [r.service for r in rows if r.group == "off"] == ["gcloud", "gcloud_adc"]
    assert signinlist.counts(rows) == {"action": 2, "underway": 0, "degraded": 5,
                                       "healthy": 11, "off": 2}


def test_missing_needs_you_only_when_something_needs_it():
    idle = view("ship-01", auth={"gcloud_adc": ("missing", None)})
    claude = view("ship-02", auth={"claude": ("missing", None)},
                  attention=[{"code": "signin-missing", "subject": "claude", "message": "m"}])
    repo = view("ship-03", auth={"github": ("missing", None)},
                attention=[{"code": "agent-repo-waiting", "subject": "main", "message": "m"}])
    rows = {(r.ship, r.service): r for r in signinlist.rows([idle, claude, repo], None)}
    assert rows[("ship-01", "gcloud_adc")].group == "degraded"
    assert rows[("ship-02", "claude")].group == "action"
    assert "its agents need it" in rows[("ship-02", "claude")].message
    assert rows[("ship-03", "github")].group == "action"


def test_wrong_account_needs_you_and_can_be_signed_out():
    v = view("ship-01", attention=[{"code": "signin-wrong-account", "subject": "github",
                                    "message": "github is signed in as someone-else."}])
    row = next(r for r in signinlist.rows([v], None) if r.group == "action")
    assert (row.service, row.state, row.level) == ("github", "wrong account", "critical")
    assert row.signed_in and signinlist.fix_command(row) == "ship-fleet logout ship-01 github"


def test_within_a_group_the_oldest_first():
    views = [view("ship-01", auth={"claude": ("expired", "a@example.com")},
                  since={"claude": "2026-11-05T00:00:00Z"}),
             view("ship-02", auth={"github": ("expired", "b")},
                  since={"github": "2026-11-02T00:00:00Z"}),
             view("ship-03", auth={"claude": {"state": "expired", "account": "c@example.com",
                                              "expires_at": "2026-10-30T00:00:00Z"}})]
    rows = [r for r in signinlist.rows(views, None) if r.group == "action"]
    assert [r.ship for r in rows] == ["ship-03", "ship-02", "ship-01"]
    assert rows[0].since == "2026-10-30T00:00:00Z"  # an expired one: since it expired


def test_a_waiting_sign_in_comes_first_and_replaces_its_row():
    views = [view("ship-01", auth={"claude": ("expired", "a@example.com")}), view("ship-02")]
    flows = [flow("ship-02", "gcloud-adc", state="link", deadline=time.time() + 300),
             flow("ship-02", "github", state="starting")]
    rows = signinlist.rows(views, None, flows)
    first = rows[0]
    assert (first.ship, first.service, first.state, first.group) == (
        "ship-02", "gcloud_adc", "waiting", "action")
    assert first.flow == flows[0].id and first.deadline == flows[0].deadline
    assert rows[1].state == "expired"
    underway = [r for r in rows if r.group == "underway"]
    assert keys(underway) == [("ship-02", "github", "starting")]
    assert len(rows) == 8  # replaced, not added
    assert signinlist.needing_action(views, None, flows) == 2


def test_a_failed_sign_in_needs_you_until_it_is_done():
    missing = view("ship-01", auth={"github": ("missing", None)})
    failed = flow("ship-01", "github", state="done", outcome="failed",
                  detail="The tool exited with 1.")
    row = next(r for r in signinlist.rows([missing], None, [failed]) if r.service == "github")
    assert (row.group, row.state) == ("action", "failed") and "exited with 1" in row.message
    # signed in since: the failure no longer matters
    row = next(r for r in signinlist.rows([view("ship-01")], None, [failed])
               if r.service == "github")
    assert row.group == "healthy"
    # cancelled: you meant it; nothing to do
    cancelled = flow("ship-01", "github", state="done", outcome="cancelled")
    row = next(r for r in signinlist.rows([missing], None, [cancelled])
               if r.service == "github")
    assert row.state == "missing"


def test_a_sign_in_turned_off_ignores_its_flows():
    v = view("ship-01", used=["claude"])
    rows = signinlist.rows([v], None, [flow("ship-01", "github", state="code")])
    assert next(r for r in rows if r.service == "github").group == "off"


def test_narrow():
    views = [view("ship-01"), view("ship-02", auth={"claude": ("expired", "x@example.com")})]
    rows = signinlist.rows(views, None)
    assert keys(signinlist.narrow(rows, ship="ship-02", service="claude")) == [
        ("ship-02", "claude", "expired")]
    assert len(signinlist.narrow(rows, group="healthy")) == 7
    assert len(signinlist.narrow(rows, tag="work")) == 8
    assert signinlist.narrow(rows, tag="personal") == []


def test_since_is_kept_while_a_sign_in_stays_as_it_is(home):
    doc = copy.deepcopy(RUNNING)
    doc["auth"]["claude"]["checked_at"] = T0
    assert status.track_since("ship-01", doc)["claude"] == T0
    later = copy.deepcopy(doc)
    later["auth"]["claude"]["checked_at"] = "2026-11-01T11:00:00Z"
    assert status.track_since("ship-01", later)["claude"] == T0  # the same: kept
    later["auth"]["claude"].update(state="expired")
    assert status.track_since("ship-01", later)["claude"] == "2026-11-01T11:00:00Z"
    assert status.read_since("ship-01")["claude"] == "2026-11-01T11:00:00Z"
    status.since_path("ship-01").write_text("[not what we wrote")
    assert status.read_since("ship-01") == {}  # garbage: as if nothing was recorded


def test_ago():
    now = 1_800_000_000.0
    stamp = lambda s: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - s))  # noqa: E731
    assert signinlist.ago(stamp(120), now) == "2 min ago"
    assert signinlist.ago(stamp(5 * 3600), now) == "5 h ago"
    assert signinlist.ago(stamp(3 * 86400), now) == "3 d ago"
    assert signinlist.ago(None) == "-" and signinlist.ago("soon") == "soon"


# --- ship-fleet signins -------------------------------------------------------------------


def test_cli_with_no_ship_lists_every_sign_in_most_urgent_first(env):  # noqa: F811
    expired = copy.deepcopy(RUNNING)
    expired["auth"]["claude"].update(state="expired")
    env.ship.status_override["ship-02"] = expired
    code, out, err = env.run("signins")
    assert code == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("Needs you: ")
    first = next(i for i, line in enumerate(lines) if line.startswith("Needs you ("))
    assert "ship-02  claude" in lines[first + 1] and "critical" in lines[first + 1]
    assert "ship-fleet login ship-02 claude" in lines[first + 1]
    order = [line.split(" (")[0] for line in lines if line and not line.startswith("  ")][1:]
    assert order == sorted(order, key=list(signinlist.GROUP_LABELS.values()).index)


def test_cli_list_filters_and_json(env):  # noqa: F811
    code, out, _ = env.run("signins", "--json", "--service", "gcloud-adc")
    assert code == 0
    rows = json.loads(out)
    assert {r["service"] for r in rows} == {"gcloud_adc"} and len(rows) == 2
    assert {"ship", "group", "state", "level", "since", "fix", "provider"} <= set(rows[0])
    _, out, _ = env.run("signins", "--tag", "personal", "--json")
    assert {r["ship"] for r in json.loads(out)} == {"ship-02"}
    _, out, _ = env.run("signins", "--group", "healthy", "--json")
    assert {r["group"] for r in json.loads(out)} == {"healthy"}
    _, out, _ = env.run("signins", "ship-01", "--json")  # a Ship and a list flag: its rows
    assert {r["ship"] for r in json.loads(out)} == {"ship-01"}
    calls = len(env.ship.calls)
    _, out, _ = env.run("signins", "--offline")  # the cached status: no SSH
    assert len(env.ship.calls) == calls and "ship-01" in out
    assert env.run("signins", "--service", "agy")[0] == 1


def test_cli_named_ship_still_shows_and_changes_what_it_uses(env):  # noqa: F811
    assert env.run("signins", "ship-01")[1].split() == ["claude", "on", "gcloud", "on",
                                                        "github", "on"]
    code, _, err = env.run("signins", "--off", "gcloud")
    assert code == 1 and "Name the Ship" in err
    code, _, err = env.run("signins", "ship-01", "--off", "gcloud", "--json")
    assert code == 1 and "without --on" in err


def test_a_timed_out_sign_in_keeps_its_rows_own_state_with_a_note():
    timed_out = flow("ship-01", "github", state="done", outcome="timeout", detail="Timed out.")
    # unused and missing: still degraded, not pinned as critical
    missing = view("ship-01", auth={"github": ("missing", None)})
    row = next(r for r in signinlist.rows([missing], None, [timed_out])
               if r.service == "github")
    assert (row.group, row.state, row.level) == ("degraded", "missing", "notice")
    assert row.note == signinlist.TIMED_OUT and row.flow == timed_out.id
    # expired: it needs you because it expired, and says the last try timed out
    expired = view("ship-01", auth={"github": ("expired", "x")})
    row = next(r for r in signinlist.rows([expired], None, [timed_out])
               if r.service == "github")
    assert (row.group, row.state, row.note) == ("action", "expired", signinlist.TIMED_OUT)
    # signed in since: no note
    row = next(r for r in signinlist.rows([view("ship-01")], None, [timed_out])
               if r.service == "github")
    assert row.note is None and row.group == "healthy"


def test_two_waiting_sign_ins_the_link_that_runs_out_first_comes_first():
    now = time.time()
    flows = [flow("ship-01", "claude", state="code", started=now - 10, deadline=now + 500),
             flow("ship-02", "github", state="link", started=now, deadline=now + 60)]
    rows = signinlist.rows([view("ship-01"), view("ship-02")], None, flows)
    assert keys(rows[:2]) == [("ship-02", "github", "waiting"), ("ship-01", "claude", "waiting")]


def test_a_sign_in_offering_the_live_terminal_needs_you():
    for state in ("live-offered", "live"):
        row = signinlist.rows([view("ship-01")], None, [flow("ship-01", "claude", state=state)])[0]
        assert (row.ship, row.service, row.group, row.state) == (
            "ship-01", "claude", "action", "waiting")


def test_expiring_sign_ins_the_one_that_goes_first_comes_first():
    def expiring(name, when, seen):
        return view(name, auth={"gcloud": {"state": "expiring", "account": "w@example.com",
                                           "expires_at": when}}, since={"gcloud": seen})
    rows = signinlist.rows([expiring("ship-01", "2026-11-20T00:00:00Z", "2026-11-01T00:00:00Z"),
                            expiring("ship-02", "2026-11-08T00:00:00Z", "2026-11-05T00:00:00Z")],
                           None)
    assert [r.ship for r in rows if r.state == "expiring"] == ["ship-02", "ship-01"]


def test_since_starts_again_when_the_account_changes(home):
    doc = copy.deepcopy(RUNNING)
    doc["auth"]["github"]["checked_at"] = T0
    status.track_since("ship-01", doc)
    later = copy.deepcopy(doc)
    later["auth"]["github"].update(account="someone-else", checked_at="2026-11-02T00:00:00Z")
    assert status.track_since("ship-01", later)["github"] == "2026-11-02T00:00:00Z"
