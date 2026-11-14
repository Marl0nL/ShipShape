"""Classic herdr Ships (ship.toml runtime = "herdr"): what only herdr's server does.

Every other Ship test runs on herdr-core, the harness's default. The cases here
prove behaviour that exists only on classic herdr (panes, workspaces, names herdr
gives back, the move from pre-record tooling, herdr's own resume setting), so the
`ship` fixture below makes each sandbox Ship a herdr Ship (`ship.use_herdr()`)
and the fake herdr in fakes/fake.py runs its agents. The runtime switch, which
needs both runtimes, is test_ship_runtime_switch.py.

`ship.reboot()` here is a herdr reboot: a new boot id, and herdr back with every
pane restored as a shell under its old id, with no agent in it (herdr's own resume
is off).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from ship_harness import LEDGER, RC, RUNTIME_LINE, SHIP, assert_valid, provision
from test_ship_converge import pack

sys.path.insert(0, str(SHIP / "lib"))
from shipctl import status  # noqa: E402

# Classic herdr only: CI runs these, a local scripts/check leaves them out unless
# SHIPSHAPE_TEST_CLASSIC_HERDR=1 (pytest.ini). They are deleted with classic herdr.
pytestmark = pytest.mark.classic_herdr


@pytest.fixture
def ship(ship):
    """The harness's sandbox Ship, made a classic herdr Ship."""
    ship.use_herdr()
    return ship


def started_args(ship) -> dict[str, list[str]]:
    return {a["name"]: a["args"] for a in ship.scenario()["herdr"]["agents"]}


# --- autostart (test_ship_autostart.py) ------------------------------------------------
def test_a_recorded_agent_that_lost_its_name_gets_it_back(ship):
    """claude started again by hand in its pane (`claude --continue`): the same
    session, but herdr has no name for it. The record knows it by its session."""
    ship.ctl("autostart", "--json")
    agents = ship.scenario()["herdr"]["agents"]
    for a in agents:
        a.pop("name")
    ship.set(herdr={"server": True, "agents": agents, "next_ws": 3})
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert doc["data"]["started"] == []
    renames = [c for c in ship.calls("herdr") if c[1:3] == ["agent", "rename"]]
    assert sorted(c[4] for c in renames) == ["grinder", "main"]


def test_non_claude_agent_in_recorded_pane_is_not_the_autostart_agent(ship):
    """herdr reused the recorded pane for another kind of agent: start the agent again."""
    ship.ctl("autostart", "--json")
    agents = ship.scenario()["herdr"]["agents"]
    main = next(a for a in agents if a["name"] == "main")
    main.update(agent="pi", name=None, agent_session=None)
    ship.set(herdr={"server": True, "agents": agents, "next_ws": 3})
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0
    assert doc["data"]["started"] == ["main"]


# --- agent restart (test_ship_agents.py) -----------------------------------------------
@pytest.fixture
def agents_running(ship):
    """main (auto, no mode of its own) and grinder (bypass) running, as after autostart,
    with the ledger the hook would have written."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.ctl("apply", "--json")
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0, doc
    agents_write_ledger(ship, {"sess-main": "auto", "sess-grinder": "bypassPermissions"})
    s = ship.scenario()
    s["herdr"]["prompts"] = []
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    return ship


def agents_write_ledger(ship, modes: dict[str, str]) -> None:
    sessions = {sid: {"mode": m, "updated_at": "2026-01-01T00:00:00Z"} for sid, m in modes.items()}
    ship.p(LEDGER).write_text(json.dumps({"schema": "shipshape.modes/1", "sessions": sessions}))


def agents_ledger(ship) -> dict[str, str]:
    return {sid: e["mode"] for sid, e in ship.json(LEDGER)["sessions"].items()}


def agents_agent(ship, name: str) -> dict:
    return next(a for a in ship.scenario()["herdr"]["agents"] if a["name"] == name)


def agents_status_modes(ship) -> dict[str, str]:
    rc, doc, _ = ship.ctl("status", "--json")
    return {a["name"]: a["mode"] for a in doc["herdr"]["agents"]}


def test_all_restarts_each_named_agent_in_the_mode_it_would_start_in(agents_running):
    ship = agents_running
    s = ship.scenario()
    s["herdr"]["agents"].append({"name": None, "agent": "claude", "agent_status": "idle",
                                 "pane_id": "w9:p1"})
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    rc, doc, _ = ship.ctl("set", "claude.default_mode=acceptEdits", "--json")
    rc, doc, _ = ship.ctl("agent", "restart", "--all", "--json")
    assert rc == 0, doc
    assert_valid("ship-ctl-result", doc)
    # grinder has a mode of its own in [[autostart]]; main follows the default.
    assert {r["name"]: r["mode"] for r in doc["data"]["restarted"]} == {
        "main": "acceptEdits", "grinder": "bypassPermissions"}
    assert doc["data"]["skipped"] == [{"name": "w9:p1", "reason": "it has no herdr name"}]


def test_ctrl_c_twice_is_the_fallback_when_exit_is_ignored(agents_running):
    ship = agents_running
    s = ship.scenario()
    s["herdr"]["exit_on_ctrl_c"] = ["main"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    pane = agents_agent(ship, "main")["pane_id"]
    assert [pane, "ctrl+c", "ctrl+c"] in ship.scenario()["herdr"]["keys"]
    # Ctrl+C before /exit goes to the agent; the fallback goes to its pane, which
    # takes keys even when herdr no longer sees an agent there.
    calls = ship.calls("herdr")
    assert ["herdr", "agent", "send-keys", pane, "ctrl+c"] in calls
    assert ["herdr", "pane", "send-keys", pane, "ctrl+c", "ctrl+c"] in calls


def agents_forget_herdr_session(ship, name: str) -> None:
    s = ship.scenario()
    for a in s["herdr"]["agents"]:
        if a["name"] == name:
            a.pop("agent_session")
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))


def test_the_session_comes_from_the_record_when_herdr_does_not_report_it(agents_running):
    ship = agents_running
    agents_forget_herdr_session(ship, "main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    assert agents_agent(ship, "main")["args"] == ["--permission-mode", "auto",
                                                  "--resume", "sess-main", RC]


def test_without_a_known_session_only_a_fresh_start_works(agents_running):
    ship = agents_running
    agents_forget_herdr_session(ship, "main")
    doc = ship.record()
    doc["agents"]["main"]["session"] = None
    ship.p("/var/lib/shipshape/agents.json").write_text(json.dumps(doc))
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 1 and doc["error"]["code"] == "agent-failed"
    assert "restart it fresh" in doc["data"]["skipped"][0]["reason"]
    assert not ship.scenario()["herdr"].get("keys")  # never stopped
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--fresh", "--json")
    assert rc == 0, doc


# --- notify-signin (test_ship_notice.py) ----------------------------------------------
def notice_start_core_agents(ship, extra=()):
    """Autostart main and grinder, then add other live agents beside them."""
    ship.ctl("autostart", "--json")
    s = ship.scenario()
    for name in extra:
        s["herdr"]["agents"].append({"name": name, "agent": "claude", "agent_status": "idle",
                                     "pane_id": f"x-{name}:p1", "workspace_id": f"x-{name}"})
    s["herdr"]["prompts"] = []  # forget autostart's first prompts
    ship.set(herdr=s["herdr"])


def test_a_core_agent_that_lost_its_name_is_still_found(ship):
    """An agent without its herdr name (started again by hand in its pane) is matched
    by the session the agent record keeps for it."""
    notice_start_core_agents(ship, extra=("worker-1",))
    s = ship.scenario()
    for a in s["herdr"]["agents"]:
        if a["name"] in ("main", "grinder"):
            a.pop("name")
    ship.set(herdr=s["herdr"])
    rc, doc, _ = ship.ctl("notify-signin", "github", "--json")
    assert doc["data"]["sent"] == ["main", "grinder"]
    targets = [p[0] for p in ship.prompts()]
    assert len(targets) == 2 and "worker-1" not in targets


# --- the agent record (test_ship_record.py) ---------------------------------------
RECORD = "/var/lib/shipshape/agents.json"


def herdr_args(ship) -> dict[str, list[str]]:
    return {a["name"]: a["args"] for a in ship.scenario()["herdr"]["agents"]}


def herdr_starts(ship) -> list[list[str]]:
    return [c for c in ship.calls("herdr") if c[1:3] == ["agent", "start"]]


def herdr_add_live(ship, name, session, cwd, pane, mode=None, kind="claude", watched=True):
    """An agent started some other way than ship-ctl (by hand, or by a firstmate).
    watched: it started under this tooling's hooks (else before a Ship's update)."""
    s = ship.scenario()
    s["herdr"]["agents"].append({
        "name": name, "agent": kind, "agent_status": "idle", "pane_id": pane,
        "workspace_id": pane.split(":")[0], "cwd": cwd,
        **({"agent_session": {"source": "herdr:claude", "agent": "claude", "kind": "id",
                              "value": session}} if session else {})})
    s["herdr"].setdefault("panes", []).append(
        {"pane_id": pane, "workspace_id": pane.split(":")[0], "cwd": cwd})
    ship.set(herdr=s["herdr"])
    if mode:
        path = ship.p(LEDGER)
        doc = json.loads(path.read_text()) if path.exists() else {
            "schema": "shipshape.modes/1", "sessions": {}}
        doc["sessions"][session] = {"mode": mode, "updated_at": "2026-01-01T00:00:00Z",
                                    **({"started_at": "2026-01-01T00:00:00Z"} if watched
                                       else {})}
        path.write_text(json.dumps(doc))


@pytest.fixture
def herdr_started(ship):
    """main and grinder started, as at a Ship's first boot: by the status refresh
    that sees Claude signed in (so later refreshes see no new sign-in)."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.ctl("apply", "--json")
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert set(herdr_args(ship)) == {"main", "grinder"}
    return ship


def test_a_restored_pane_that_is_gone_gets_a_new_workspace(herdr_started):
    ship = herdr_started
    ship.reboot()
    s = ship.scenario()
    s["herdr"]["panes"] = [p for p in s["herdr"]["panes"] if p["pane_id"] != "w1:p1"]
    ship.set(herdr=s["herdr"])  # herdr lost main's pane (its layout was not saved)
    ship.ctl("refresh")
    main = ship.agent("main")
    assert main["args"] == ["--permission-mode", "auto", "--resume", "sess-main", RC]
    assert main["pane_id"] == "w3:p1" and main["cwd"] == "/home/agent/work/main"
    assert ship.agent("grinder")["pane_id"] == "w2:p1"


def test_another_agent_in_its_old_pane_never_becomes_it(herdr_started):
    """herdr's pane list cannot be read, and another, named agent sits in main's old
    pane: main must not take that agent's session (or its mode)."""
    ship = herdr_started
    ship.reboot()
    s = ship.scenario()
    s["herdr"]["panes"] = [p for p in s["herdr"]["panes"] if p["pane_id"] != "w1:p1"]
    ship.set(herdr=s["herdr"])
    herdr_add_live(ship, "other", "sess-other", "/home/agent/work/main", "w1:p1", mode="plan")
    ship.set(herdr={**ship.scenario()["herdr"], "pane_list_fails": True})
    ship.ctl("refresh")
    record = ship.record()["agents"]
    assert record["main"]["session"] == "sess-main" and record["main"]["mode"] == "auto"
    assert record["other"]["session"] == "sess-other"
    main = ship.agent("main")
    assert main["args"] == ["--permission-mode", "auto", "--resume", "sess-main", RC]
    assert main["pane_id"] == "w3:p1"  # a new workspace, not the busy pane
    assert not [c for c in herdr_starts(ship) if "sess-other" in c]
    ship.ctl("refresh")
    assert ship.record()["agents"]["main"]["session"] == "sess-main"


def test_an_unnamed_agent_in_its_pane_but_another_folder_never_becomes_it(herdr_started):
    """main crashed; someone uses its pane, `cd`s elsewhere and runs claude there by
    hand (no herdr name). That is another agent: main keeps its own session, mode
    and folder, and the other is recorded under its own name."""
    ship = herdr_started
    ship.crash("main")
    herdr_add_live(ship, None, "sess-x", "/home/agent/work/elsewhere", "w1:p1", mode="plan")
    ship.ctl("refresh")
    record = ship.record()["agents"]
    assert (record["main"]["session"], record["main"]["mode"], record["main"]["cwd"]) == (
        "sess-main", "auto", "/home/agent/work/main")
    assert record["elsewhere"]["session"] == "sess-x"  # adopted, named after its folder
    renames = [c[3:] for c in ship.calls("herdr") if c[1:3] == ["agent", "rename"]]
    assert ["w1:p1", "main"] not in renames and ["w1:p1", "elsewhere"] in renames
    assert record["main"]["want"] == "stopped"  # its pane holds another agent now
    # An [[autostart]] agent, so the next boot starts it afresh, and the other resumes.
    ship.p("/home/agent/work/elsewhere").mkdir(parents=True)
    ship.reboot()
    ship.ctl("refresh")
    # Adopted: no Remote Control until it is turned on.
    assert herdr_args(ship)["elsewhere"] == ["--permission-mode", "plan", "--resume", "sess-x"]
    assert herdr_args(ship)["main"] == ["--permission-mode", "auto", "--name", "ship-01-main", RC]
    assert not [c for c in herdr_starts(ship) if c[3] == "main" and "sess-x" in c]


def test_claude_started_again_by_hand_in_its_pane_and_folder_is_still_it(herdr_started):
    """The same, in its own folder (claude started again there by hand, in a new
    session): that is the recorded agent, its name given back, its session followed."""
    ship = herdr_started
    ship.crash("main")
    herdr_add_live(ship, None, "sess-main-2", "/home/agent/work/main", "w1:p1", mode="auto")
    ship.ctl("refresh")
    assert set(ship.record()["agents"]) == {"main", "grinder"}
    assert ship.record()["agents"]["main"]["session"] == "sess-main-2"
    assert ["w1:p1", "main"] in [c[3:] for c in ship.calls("herdr") if c[1:3] == ["agent",
                                                                                   "rename"]]
    assert len([c for c in herdr_starts(ship) if c[3] == "main"]) == 1  # not started again


def test_a_pane_in_another_folder_is_not_used(herdr_started):
    ship = herdr_started
    ship.reboot()
    s = ship.scenario()
    for p in s["herdr"]["panes"]:
        if p["pane_id"] == "w1:p1":
            p["cwd"] = "/home/agent/elsewhere"
    ship.set(herdr=s["herdr"])
    ship.ctl("refresh")
    assert ship.agent("main")["pane_id"] == "w3:p1"  # claude --resume needs its folder


def test_without_herdrs_pane_list_the_agents_own_panes_are_tried_first(herdr_started):
    ship = herdr_started
    ship.reboot()
    ship.set(herdr={**ship.scenario()["herdr"], "pane_list_fails": True})
    ship.ctl("refresh")
    assert {a["name"]: a["pane_id"] for a in ship.scenario()["herdr"]["agents"]} == {
        "main": "w1:p1", "grinder": "w2:p1"}
    assert len(ship.scenario()["herdr"]["workspaces"]) == 2


def test_an_unnamed_agent_is_named_after_its_folder(herdr_started):
    ship = herdr_started
    herdr_add_live(ship, None, "sess-x", "/home/agent/work/My Project", "w8:p1")
    herdr_add_live(ship, None, "sess-y", "/home/agent/work/main", "w9:p1")  # main is taken
    ship.ctl("refresh")
    renames = sorted(c[3:] for c in ship.calls("herdr") if c[1:3] == ["agent", "rename"])
    assert renames == [["w8:p1", "my-project"], ["w9:p1", "main-2"]]
    assert {"my-project", "main-2"} <= set(ship.record()["agents"])


# --- moving a Ship onto the record (migration) -------------------------------------------------
def old_ship(ship):
    """A Ship on tooling from before the agent record, as an update finds it: herdr
    resumed its agents (resume_agents_on_restore = true) and autostart kept its own
    record. Running: main (auto) and grinder (bypassPermissions) from autostart, a
    hand-started helper in bypassPermissions, and one in auto whose name herdr lost
    after an earlier resume (autostart's record still knew its pane)."""
    ship.runtime = None  # tooling from then wrote no runtime to ship.toml
    ship.write_toml(RUNTIME_LINE.sub("", ship.p("/etc/shipshape/ship.toml").read_text()))
    cfg = ship.p("/home/agent/.config/herdr/config.toml")
    cfg.parent.mkdir(parents=True)
    cfg.write_text("onboarding = false\n\n[session]\nresume_agents_on_restore = true\n")
    for i, (name, mode, cwd) in enumerate([
        ("main", "auto", "/home/agent/work/main"),
        ("grinder", "bypassPermissions", "/home/agent/work/grinder"),
        ("helper", "bypassPermissions", "/home/agent/work/side"),
    ], 1):
        ship.p(cwd).mkdir(parents=True, exist_ok=True)
        herdr_add_live(ship, None if name == "main" else name, f"sess-{name}", cwd,
                       f"w{i}:p1", mode,
                       watched=False)  # started under the old tooling's hooks: no SessionEnd
    ship.p("/var/lib/shipshape/autostart.json").write_text(json.dumps({
        "main": {"pane": "w1:p1", "mode": "auto", "session_name": "ship-01-main",
                 "started_at": "2026-01-01T00:00:00Z"},
        "grinder": {"pane": "w2:p1", "mode": "bypassPermissions", "session": "sess-grinder",
                    "session_name": "ship-01-grinder", "started_at": "2026-01-01T00:00:00Z"},
    }))
    s = ship.scenario()
    s["herdr"]["next_ws"] = 4
    ship.set(herdr=s["herdr"])


def test_an_update_records_what_runs_and_a_reboot_brings_it_back_in_its_modes(ship, tmp_path):
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    old_ship(ship)
    before = {a["pane"]: a["mode"] for a in ship.status()["herdr"]["agents"]}
    assert before == {"w1:p1": "auto", "w2:p1": "bypassPermissions",
                      "w3:p1": "bypassPermissions"}

    # ship-fleet update: converge to this tooling (its ship-ctl apply records them).
    rc, doc, out = ship.ctl("converge", "--bundle", str(pack(tmp_path)), "--json")
    assert rc == 0, out
    record = ship.record()
    assert_valid("agent-record", record)
    assert {n: (e["mode"], e["session"], e["origin"]) for n, e in record["agents"].items()} == {
        "main": ("auto", "sess-main", "autostart"),
        "grinder": ("bypassPermissions", "sess-grinder", "autostart"),
        "helper": ("bypassPermissions", "sess-helper", "adopted"),
    }
    assert record["agents"]["main"]["session_name"] == "ship-01-main"
    assert ["w1:p1", "main"] in [c[3:] for c in ship.calls("herdr") if c[1:3] == ["agent",
                                                                                   "rename"]]
    assert not ship.p("/var/lib/shipshape/autostart.json").exists()
    cfg = ship.p("/home/agent/.config/herdr/config.toml").read_text()
    assert "resume_agents_on_restore = false" in cfg  # from the next boot, ship-ctl resumes
    assert ship.status()["herdr"]["last_resume"] is None

    # The reboot that follows: herdr brings its panes back empty; the refresh resumes.
    ship.reboot()
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert herdr_args(ship) == {
        "main": ["--permission-mode", "auto", "--resume", "sess-main", RC],
        "grinder": ["--dangerously-skip-permissions", "--resume", "sess-grinder", RC],
        "helper": ["--dangerously-skip-permissions", "--resume", "sess-helper"],  # adopted
    }
    assert not [c for c in ship.calls("herdr") if c[1:3] == ["workspace", "create"]]
    doc = ship.status()
    after = {a["pane"]: a["mode"] for a in doc["herdr"]["agents"]}
    assert after == before
    assert {a["name"]: a["mode"] for a in doc["herdr"]["agents"]} == {
        "main": "auto", "grinder": "bypassPermissions", "helper": "bypassPermissions"}
    assert doc["herdr"]["last_resume"]["resumed"] == 3
    assert ship.scenario()["herdr"].get("prompts") is None  # resumed, never prompted afresh


def test_before_the_first_reboot_an_old_agent_that_stops_is_not_started_again(ship, tmp_path):
    """Sessions started before the update run without the SessionEnd hook, so an exit
    of one looks like a crash: such an agent is left stopped, as before the update,
    until the reboot resumes everything under the new hooks."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    old_ship(ship)
    ship.ctl("converge", "--bundle", str(pack(tmp_path)), "--json")
    ship.ctl("refresh")  # the Ship's refreshes go on: Claude was signed in all along
    assert not herdr_starts(ship)
    ship.crash("helper")
    ship.crash("grinder")
    rc, _, out = ship.ctl("refresh")
    assert not herdr_starts(ship)
    assert "helper" not in ship.record()["agents"]  # let go, as herdr would have
    assert ship.record()["agents"]["grinder"]["want"] == "stopped"  # autostart: next boot
    assert "it started before the Ship could tell an exit from a crash" in out
    ship.reboot()
    ship.ctl("refresh")
    assert herdr_args(ship)["main"] == ["--permission-mode", "auto", "--resume", "sess-main", RC]
    assert herdr_args(ship)["grinder"] == ["--dangerously-skip-permissions", "--name",
                                     "ship-01-grinder", RC]  # afresh, as autostart did
    ship.crash("main")  # resumed under the new hooks: now a crash is one
    ship.ctl("refresh")
    assert herdr_args(ship)["main"] == ["--permission-mode", "auto", "--resume", "sess-main", RC]
    assert len([c for c in herdr_starts(ship) if c[3] == "main"]) == 2


def test_an_update_while_herdr_does_not_answer_records_them_at_the_next_refresh(ship):
    old_ship(ship)
    ship.set(herdr={**ship.scenario()["herdr"], "server": False})
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    assert not ship.p(RECORD).exists()
    assert ship.p("/var/lib/shipshape/autostart.json").exists()  # kept for the next try
    ship.set(herdr={**ship.scenario()["herdr"], "server": True})
    ship.ctl("refresh")
    assert set(ship.record()["agents"]) == {"main", "grinder", "helper"}


# --- status (test_ship_ctl.py) -------------------------------------------------------
def test_status_running_ship_validates(ship):
    ship.set(
        herdr={
            "server": True,
            "agents": [
                {
                    "name": "main",
                    "agent": "claude",
                    "agent_status": "blocked",
                    "pane_id": "w1:p1",
                    "workspace_id": "w1",
                    "tab_id": "w1:t1",
                    "terminal_id": "t1",
                    "focused": False,
                    "revision": 3,
                    "cwd": "/home/agent/work/main",
                    "agent_session": {
                        "source": "herdr:claude",
                        "agent": "claude",
                        "kind": "id",
                        "value": "sess-a",
                    },
                },
                {
                    "agent": "claude",
                    "agent_status": "working",
                    "pane_id": "w2:p1",
                    "workspace_id": "w2",
                    "tab_id": "w2:t1",
                    "terminal_id": "t2",
                    "focused": False,
                    "revision": 1,
                    "agent_session": {
                        "source": "herdr:claude",
                        "agent": "claude",
                        "kind": "id",
                        "value": "sess-b",
                    },
                },
            ],
        }
    )
    ledger = ship.p("/home/agent/.local/state/shipshape/modes.json")
    ledger.write_text(
        json.dumps(
            {
                "schema": "shipshape.modes/1",
                "sessions": {
                    "sess-a": {"mode": "auto", "updated_at": "2026-01-01T00:00:00Z"},
                    "sess-b": {"mode": "bypassPermissions", "updated_at": "2026-01-01T00:00:00Z"},
                },
            }
        )
    )
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert_valid("ship-status", doc)
    assert doc["state"] == "running"
    assert [a["mode"] for a in doc["herdr"]["agents"]] == ["auto", "bypassPermissions"]
    assert doc["herdr"]["agents"][0]["name"] == "main"
    assert doc["auth"]["claude"] == {
        "state": "ok",
        "account": "work@example.com",
        "detail": "max",
        "checked_at": doc["generated_at"],
    }
    assert doc["auth"]["github"]["account"] == "example-user"
    assert doc["auth"]["gcloud_adc"]["state"] == "ok"
    assert doc["herdr"]["last_resume"] is None  # nothing resumed yet: no boot since
    assert doc["agent_record"] == {"runtime": "herdr", "agents": [], "resume": None,
                                   "primary": None}
    # Classic herdr: told it is going, and how to move.
    assert {a["code"] for a in doc["attention"]} == {"agent-waiting", "runtime-deprecated"}
    assert doc["host"]["mem_used_pct"] == 50.0 and doc["host"]["uptime_s"] == 4000


@pytest.mark.parametrize(
    "change,state",
    [
        ({"herdr": {"server": False}}, "unknown"),
    ],
)
def test_status_states(ship, change, state):
    ship.set(**change)
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert_valid("ship-status", doc)
    assert doc["state"] == state


def test_status_tells_a_classic_herdr_ship_it_is_deprecated(ship):
    """runtime-deprecated: classic herdr goes in the next release; the item names the
    exact command that moves this Ship to herdr-core. ship-ctl status prints it too."""
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0 and doc["herdr"]["runtime"] == "herdr"
    assert_valid("ship-status", doc)
    (item,) = [a for a in doc["attention"] if a["code"] == "runtime-deprecated"]
    name = doc["ship"]["name"]
    assert item["subject"] == "runtime"
    assert "removed in the next ShipShape release" in item["message"]
    assert f"ship-fleet runtime {name} core" in item["message"]
    assert "ship-ctl runtime switch core" in item["message"]
    rc, _, out = ship.ctl("status")
    assert rc == 0 and f"! {item['message']}" in out


# --- provisioning (test_ship_provision.py) ---------------------------------------------
def test_units_are_enabled(tmp_path):
    if shutil.which("systemctl") is None:
        pytest.skip("needs systemctl")
    provision("--first-boot", "--root", str(tmp_path))
    assert (tmp_path / "etc/systemd/system/timers.target.wants/ship-status.timer").is_symlink()
    wants = tmp_path / "etc/systemd/user/default.target.wants"
    assert sorted(p.name for p in wants.iterdir()) == ["herdr.service"]


# --- herdr-restart-pending (test_ship_languard.py) ------------------------------------


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


# A herdr server another tool runs beside ShipShape: a named session (a supervising
# agent's crew), or a default one a laptop's saved herdr entry started over ssh.
OTHER_HERDR_SERVERS = {
    "named-session": ["/usr/local/bin/herdr", "server", "--session", "crew"],
    "default": ["herdr", "server"],
}


def _old_herdr_server(ship, tmp_path, argv) -> None:
    """herdr was replaced (a converge with a new pin) under a running `herdr server`."""
    herdr = ship.p("/usr/local/bin/herdr")
    herdr.parent.mkdir(parents=True, exist_ok=True)
    herdr.write_text("new herdr")
    old = tmp_path / "old-herdr"
    old.write_text("old herdr")
    fake_process(ship, 4500, argv, old)


@pytest.mark.parametrize("server", OTHER_HERDR_SERVERS)
def test_a_herdr_ship_still_reports_any_old_herdr_server(ship, tmp_path, server):
    """A classic herdr Ship keeps today's rule: any agent-user herdr server on an old
    binary means a reboot is needed for the new herdr."""
    _old_herdr_server(ship, tmp_path, OTHER_HERDR_SERVERS[server])
    doc = ship.status()
    assert doc["herdr"]["runtime"] == "herdr"
    assert "herdr-restart-pending" in codes(doc)


# --- choosing the runtime, and the status document (test_ship_core.py) -----------------
def _install_core(ship) -> Path:
    path = ship.p("/usr/local/bin/herdr-core")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("new herdr-core")
    return path


def test_herdr_stays_the_default_and_never_meets_hc(ship):
    """A ship.toml that names no runtime is a herdr Ship."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.runtime = None
    ship.write_toml(RUNTIME_LINE.sub("", ship.p("/etc/shipshape/ship.toml").read_text()))
    assert "runtime" not in ship.p("/etc/shipshape/ship.toml").read_text()
    ship.ctl("apply", "--json")
    ship.ctl("refresh")
    assert {a["name"] for a in ship.scenario()["herdr"]["agents"]} == {"main", "grinder"}
    assert ship.calls("hc") == []
    doc = ship.status()
    assert doc["herdr"]["runtime"] == "herdr" and doc["agent_record"]["runtime"] == "herdr"
    assert ship.record()["runtime"] == "herdr"


def test_the_claude_floor_is_only_for_core(ship):
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.set(claude={"logged_in": True, "email": "work@example.com", "version": "2.1.290"})
    ship.ctl("apply", "--json")
    ship.ctl("refresh")
    assert {a["name"] for a in ship.scenario()["herdr"]["agents"]} == {"main", "grinder"}


def test_an_old_herdr_server_is_still_herdr_restart_pending_not_core(ship, tmp_path):
    herdr = ship.p("/usr/local/bin/herdr")
    herdr.parent.mkdir(parents=True, exist_ok=True)
    herdr.write_text("new herdr")
    _install_core(ship)
    old = tmp_path / "old"
    old.write_text("old")
    fake_process(ship, 4400, ["/usr/local/bin/herdr", "server"], old)
    found = codes(ship.status())
    assert "herdr-restart-pending" in found and "core-restart-pending" not in found


# --- agent restart on herdr (test_ship_agents.py has the core versions) ----------
def test_restart_continues_the_conversation_in_the_new_default_mode(agents_running):
    ship = agents_running
    rc, doc, _ = ship.ctl("set", "claude.default_mode=bypassPermissions", "--json")
    assert rc == 0, doc
    pane = agents_agent(ship, "main")["pane_id"]
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    assert_valid("ship-ctl-result", doc)
    assert doc["changed"] == ["main"]
    assert doc["data"]["restarted"] == [{
        "name": "main", "mode": "bypassPermissions", "previous_mode": "auto",
        "conversation": "continued", "was": "idle"}]
    assert "main restarted in bypassPermissions, continuing its conversation." in doc["messages"]
    # The same pane and name, the same session, with the mode flag in front.
    main = agents_agent(ship, "main")
    assert main["pane_id"] == pane
    assert main["args"] == ["--dangerously-skip-permissions", "--resume", "sess-main", RC]
    # It was asked to exit, after a Ctrl+C that clears a half-typed line.
    h = ship.scenario()["herdr"]
    assert [pane, "ctrl+c"] in h["keys"] and [pane, "/exit"] in h["prompts"]
    assert not any(k[1:] == ["esc"] for k in h["keys"])  # idle: nothing interrupted
    # The ledger, the status document and the agent record all say the new mode...
    assert agents_ledger(ship)["sess-main"] == "bypassPermissions"
    assert agents_status_modes(ship) == {"main": "bypassPermissions",
                                         "grinder": "bypassPermissions"}
    assert ship.record()["agents"]["main"]["mode"] == "bypassPermissions"
    # No new workspace, and grinder was not touched.
    assert len(h["workspaces"]) == 2
    assert agents_agent(ship, "grinder")["args"][0] == "--dangerously-skip-permissions"
    # ...so the next boot resumes it in that mode.
    ship.reboot()
    ship.ctl("refresh")
    assert agents_agent(ship, "main")["args"] == ["--dangerously-skip-permissions",
                                           "--resume", "sess-main", RC]
    assert agents_agent(ship, "main")["pane_id"] == pane


def test_exit_waits_until_escape_has_dismissed_the_question(agents_running):
    # herdr reads the screen, so it sees the question gone a poll or two after Escape,
    # and refuses a prompt to an agent it still sees waiting.
    ship = agents_running
    ship.env["SHIPCTL_TEST_WAIT_S"] = "5"
    s = ship.scenario()
    s["herdr"]["slow_esc"] = ["main"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    ship.set_state("main", "blocked")
    pane = agents_agent(ship, "main")["pane_id"]
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    h = ship.scenario()["herdr"]
    assert [pane, "/exit"] in h["prompts"]  # the graceful way, not the fallback
    assert [pane, "ctrl+c", "ctrl+c"] not in h["keys"]
    assert any("its open question was dismissed" in m for m in doc["messages"])


def test_an_agent_in_an_unknown_state_counts_as_busy(agents_running):
    ship = agents_running
    ship.set_state("main", "unknown")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 1 and doc["error"]["code"] == "agent-busy"
    assert doc["data"]["skipped"][0]["reason"] == (
        "it is in a state herdr cannot tell; not interrupted")
    assert not ship.scenario()["herdr"].get("keys")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--force", "--json")
    assert rc == 0, doc
    assert any("herdr could not tell its state" in m for m in doc["messages"])


def test_an_agent_that_stops_but_does_not_start_again_says_how_to_start_it(agents_running):
    ship = agents_running
    s = ship.scenario()
    s["herdr"]["fail_start"] = ["main"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "plan", "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "agent-failed"
    assert doc["data"]["skipped"] == [{"name": "main", "reason": (
        "stopped, but did not start again: agent_not_ready")}]
    assert any(m.endswith("Start it in its pane with: claude --permission-mode plan "
                          "--resume sess-main --remote-control") for m in doc["messages"])
    starts = [c for c in ship.calls("herdr") if c[1:3] == ["agent", "start"]]
    assert len(starts) == 4  # two autostart launches, then a try and one retry



# --- notify-signin on herdr (test_ship_notice.py has the core versions) ---------
def test_an_agent_at_a_permission_prompt_is_not_typed_into(ship):
    """Text and Enter could answer the permission dialog."""
    notice_start_core_agents(ship)
    s = ship.scenario()
    for a in s["herdr"]["agents"]:
        if a["name"] == "main":
            a["agent_status"] = "blocked"
    ship.set(herdr=s["herdr"])
    rc, doc, _ = ship.ctl("notify-signin", "github", "--json")
    assert doc["data"]["sent"] == ["grinder"]
    assert doc["data"]["skipped"] == [{"name": "main", "reason": "waiting-for-permission"}]
    assert [p[0] for p in ship.prompts()] == ["grinder"]


def test_an_agent_herdr_cannot_classify_is_not_typed_into(ship):
    """`unknown`, as just after an agent was started again."""
    notice_start_core_agents(ship)
    s = ship.scenario()
    for a in s["herdr"]["agents"]:
        if a["name"] == "grinder":
            a["agent_status"] = "unknown"
    ship.set(herdr=s["herdr"])
    rc, doc, _ = ship.ctl("notify-signin", "github", "--json")
    assert doc["data"]["sent"] == ["main"]
    assert doc["data"]["skipped"] == [{"name": "grinder", "reason": "not-ready"}]
    assert [p[0] for p in ship.prompts()] == ["main"]


def test_the_line_is_typed_as_the_agent_user_and_logged_as_root(ship):
    """herdr's socket and the agents belong to agent; the journal record is root's."""
    notice_start_core_agents(ship)
    ship.ctl("notify-signin", "github", "--json")
    agent_home = str(ship.p("/home/agent"))
    typed = [home for argv, home in ship.homes("herdr") if argv[1:3] == ["agent", "prompt"]]
    assert typed and all(home == agent_home for home in typed)
    logged = [home for _, home in ship.homes("logger")]
    assert logged and all(home != agent_home for home in logged)


def test_a_failed_send_is_reported(ship):
    notice_start_core_agents(ship)
    s = ship.scenario()
    s["herdr"]["fail_prompt"] = ["grinder"]
    ship.set(herdr=s["herdr"])
    rc, doc, _ = ship.ctl("notify-signin", "github", "--json")
    assert rc == 0
    assert doc["data"]["sent"] == ["main"]
    assert doc["data"]["skipped"] == [{"name": "grinder", "reason": "send-failed"}]



# --- the wake-up on herdr (test_ship_wake.py has the core versions) -------------
WAKE_STATE = "/var/lib/shipshape/wake.json"
WAKE_LINE = ("Your Ship rebooted 10 minutes ago. Make sure you've resumed any background "
             "processes or work that you should be doing, and review whether subagents or "
             "crews may have been interrupted by the reboot too.")


def wake_turn_on(ship, *pairs: str) -> dict:
    rc, doc, out = ship.ctl("set", "wake.after_boot=true", *pairs, "--json")
    assert rc == 0, out
    return doc


def wake_uptime(ship, seconds: float) -> None:
    ship.p("/proc/uptime").write_text(f"{seconds} 1.0\n")


def wake_run(ship) -> dict:
    rc, doc, out = ship.ctl("wake", "--json")
    assert rc == 0, out
    assert_valid("ship-ctl-result", doc)
    assert doc["command"] == "wake"
    return doc["data"]


def wake_logged(ship) -> list[str]:
    return [e["message"] for e in ship.scenario().get("logged", [])
            if e["message"].startswith("wake: ")]


@pytest.fixture
def wake_booted(ship):
    """main and grinder started by the first refresh of the boot (which records the
    boot's resume), autostart's first prompts forgotten, 10 minutes into the boot."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    s = ship.scenario()
    assert {a["name"] for a in s["herdr"]["agents"]} == {"main", "grinder"}
    s["herdr"]["prompts"] = []
    ship.set(herdr=s["herdr"])
    wake_uptime(ship, 605)
    return ship


def test_on_sends_once_to_the_primary(wake_booted):
    ship = wake_booted
    wake_turn_on(ship)
    data = wake_run(ship)
    assert data == {"outcome": "sent", "agent": "main", "tries": 1, "reason": None}
    assert ship.prompts() == [["main", WAKE_LINE]]
    assert wake_logged(ship) == ["wake: sent to the primary agent main."]
    assert ship.json(WAKE_STATE)["outcome"] == "sent"


def test_a_blocked_primary_is_retried_then_given_up(wake_booted):
    """Text and Enter could answer its permission prompt: never typed into."""
    ship = wake_booted
    wake_turn_on(ship)
    ship.set_state("main", "blocked")
    data = wake_run(ship)
    assert data == {"outcome": "gave-up", "agent": "main", "tries": 6,
                    "reason": "waiting-for-permission"}
    assert ship.prompts() == []
    assert wake_logged(ship) == ["wake: not sent to the primary agent main after 6 tries "
                                "(waiting-for-permission); gave up."]
    ship.set_state("main", "idle")
    assert wake_run(ship)["outcome"] == "already-done"  # given up for this boot
    assert ship.prompts() == []


def test_an_agent_in_an_unknown_state_or_not_running_is_not_typed_into(wake_booted):
    ship = wake_booted
    wake_turn_on(ship)
    ship.set_state("main", "unknown")
    assert wake_run(ship)["reason"] == "not-ready"
    ship.reboot(uptime=40)
    ship.ctl("refresh")
    ship.crash("main")
    s = ship.scenario()
    s["herdr"]["fail_start"] = ["main"]  # it is not back by the time the wake runs
    ship.set(herdr=s["herdr"])
    wake_uptime(ship, 605)
    data = wake_run(ship)
    assert data["outcome"] == "gave-up"
    assert data["reason"] == "not-running"
    assert ship.prompts() == []



# --- the agent record on herdr (test_ship_record.py has the core versions) -------
def herdr_recorded(ship) -> dict[str, dict]:
    return {a["name"]: a for a in ship.status()["agent_record"]["agents"]}


def test_after_a_reboot_the_refresh_resumes_every_agent_in_its_folder_and_mode(herdr_started):
    ship = herdr_started
    panes = {a["name"]: a["pane_id"] for a in ship.scenario()["herdr"]["agents"]}
    ship.reboot()
    assert herdr_recorded(ship)["main"]["state"] == "starting"
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert herdr_args(ship) == {
        "main": ["--permission-mode", "auto", "--resume", "sess-main", RC],
        "grinder": ["--dangerously-skip-permissions", "--resume", "sess-grinder", RC],
    }
    # In the panes herdr restored, under their names: no new workspace, no first prompt.
    assert {a["name"]: a["pane_id"] for a in ship.scenario()["herdr"]["agents"]} == panes
    assert len(ship.scenario()["herdr"]["workspaces"]) == 2
    assert [p for p in ship.scenario()["herdr"]["prompts"]] == [
        ["main", "Get oriented."], ["grinder", "Get oriented and ready to work."]]  # boot 1
    doc = ship.status()
    assert {a["name"]: a["mode"] for a in doc["herdr"]["agents"]} == {
        "main": "auto", "grinder": "bypassPermissions"}
    assert doc["herdr"]["last_resume"]["method"] == "api-fallback"
    assert doc["herdr"]["last_resume"]["resumed"] == 2
    assert doc["agent_record"]["resume"]["method"] == "ship-ctl"
    assert {a["state"] for a in doc["agent_record"]["agents"]} == {"running"}
    assert "agent record: main started again, continuing its session." in out
    assert_valid("agent-record", ship.record())


def test_an_agent_that_crashed_is_started_again_in_its_pane(herdr_started):
    ship = herdr_started
    ship.crash("grinder")
    assert herdr_recorded(ship)["grinder"]["state"] == "starting"
    ship.ctl("refresh")
    grinder = ship.agent("grinder")
    assert grinder["args"] == ["--dangerously-skip-permissions", "--resume", "sess-grinder", RC]
    assert grinder["pane_id"] == "w2:p1"
    assert herdr_recorded(ship)["grinder"]["state"] == "running"


def test_after_three_failed_tries_it_is_left_alone_with_attention(herdr_started):
    ship = herdr_started
    ship.crash("grinder")  # within 10 minutes of its start: the first failed try
    s = ship.scenario()
    s["herdr"]["fail_start"] = ["grinder"]
    ship.set(herdr=s["herdr"])
    for _ in range(4):
        ship.ctl("refresh")
    # Its first start, then two failed starts (each tried twice), then it is let be.
    assert len([c for c in herdr_starts(ship) if c[3] == "grinder"]) == 1 + 2 * 2
    doc = ship.status()
    grinder = next(a for a in doc["agent_record"]["agents"] if a["name"] == "grinder")
    assert grinder["state"] == "failed" and grinder["relaunch_failures"] == 3
    assert grinder["last_error"] == "agent_not_ready"
    item = next(a for a in doc["attention"] if a["code"] == "agent-relaunch-failed")
    assert item["subject"] == "grinder" and "ship-ctl agent forget grinder" in item["message"]
    # Restarting it by hand starts it again, and the attention goes.
    s = ship.scenario()
    s["herdr"]["fail_start"] = []
    ship.set(herdr=s["herdr"])
    rc, doc, _ = ship.ctl("agent", "restart", "grinder", "--mode", "dontAsk", "--json")
    assert rc == 0, doc
    assert doc["data"]["restarted"][0]["was"] == "failed"
    assert doc["data"]["restarted"][0]["previous_mode"] == "bypassPermissions"
    assert ship.agent("grinder")["args"] == ["--permission-mode", "dontAsk",
                                             "--resume", "sess-grinder", RC]
    assert ship.record()["agents"]["grinder"]["mode"] == "dontAsk"
    assert "agent-relaunch-failed" not in {a["code"] for a in ship.status()["attention"]}


def test_a_lost_conversation_counts_as_a_failed_start(herdr_started):
    ship = herdr_started
    s = ship.scenario()
    s["herdr"]["lost_sessions"] = ["sess-main"]  # claude --resume: No conversation found
    ship.set(herdr=s["herdr"])
    ship.reboot()
    ship.ctl("refresh")
    assert "main" not in herdr_args(ship) and "grinder" in herdr_args(ship)
    assert ship.status()["herdr"]["last_resume"] == {
        "at": ship.record()["resume"]["at"], "method": "api-fallback", "resumed": 1,
        "failed": 1}
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--fresh", "--json")
    assert rc == 0, doc
    assert ship.agent("main")["args"] == ["--permission-mode", "auto", "--name", "ship-01-main", RC]
    assert ["main", "Get oriented."] in ship.scenario()["herdr"]["prompts"]


def test_a_closed_pane_is_a_stopped_agent(herdr_started):
    ship = herdr_started
    s = ship.scenario()
    s["herdr"]["agents"] = [a for a in s["herdr"]["agents"] if a["name"] != "main"]
    s["herdr"]["panes"] = [p for p in s["herdr"]["panes"] if p["pane_id"] != "w1:p1"]
    ship.set(herdr=s["herdr"])  # someone closed main's pane: SIGHUP, reason "other"
    ship.ctl("refresh")
    assert "main" not in herdr_args(ship)
    assert ship.record()["agents"]["main"]["want"] == "stopped"


def test_forget_refuses_while_herdr_does_not_answer(herdr_started):
    """Nothing tells whether the agent runs: it stays recorded."""
    ship = herdr_started
    ship.set(herdr={**ship.scenario()["herdr"], "server": False})
    rc, doc, _ = ship.ctl("agent", "forget", "main", "--json")
    assert rc == 1 and doc["error"]["code"] == "herdr-unavailable"
    assert "main" in ship.record()["agents"]

