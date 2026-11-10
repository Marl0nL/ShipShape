"""`ship-ctl runtime switch core|herdr` (switch.py): moving a live Ship's agents between
herdr and herdr-core, with a rollback on any failure.

The invariants every test checks (`intact`): each agent runs exactly once, on one
runtime, continuing its own session, in its own mode; nothing ended an agent the
hard way (no `hc kill`, no `hc server stop`, no herdr workspace closed with an agent
in it); and the record, ship.toml and the boot units agree on the runtime.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from ship_harness import LEDGER, assert_valid

LAUNCHER = "/opt/shipshape/bin/agent-launch"


# --- helpers ------------------------------------------------------------------------------
def switch(ship, target: str, *extra: str):
    rc, doc, out = ship.ctl("runtime", "switch", target, *extra, "--json")
    assert_valid("ship-ctl-result", doc)
    return rc, doc, out


def status(ship) -> dict:
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert_valid("ship-status", doc)
    return doc


def ledger(ship) -> dict:
    return json.loads(ship.p(LEDGER).read_text())["sessions"]


def herdr_agents(ship) -> dict[str, dict]:
    return {a["name"]: a for a in ship.scenario()["herdr"]["agents"]}


def core_agents(ship) -> dict[str, list[dict]]:
    """Every live core terminal running claude, by name."""
    out: dict[str, list[dict]] = {}
    for t in ship.scenario().get("hc", {}).get("terminals", []):
        if t["state"] == "live" and (t.get("foreground") or {}).get("name") == "claude":
            out.setdefault(t["name"], []).append(t)
    return out


def herdr_starts(ship) -> list[list[str]]:
    return [c for c in ship.calls("herdr") if c[1:3] == ["agent", "start"]]


def hc_runs(ship) -> list[list[str]]:
    return [c for c in ship.calls("hc") if c[1:2] == ["run"]]


def runtime_in_toml(ship) -> str:
    text = ship.p("/etc/shipshape/ship.toml").read_text()
    return "core" if 'runtime = "core"' in text else "herdr"


def intact(ship, runtime: str, expect: dict[str, tuple[str, str]],
           never_stopped: tuple[str, ...] = ()) -> None:
    """Each agent (name -> (session, mode)) runs exactly once, on `runtime` only, in its
    own session and mode; nothing was ended the hard way; everything says `runtime`.
    never_stopped: agents still running as they were first started."""
    h, c = herdr_agents(ship), core_agents(ship)
    for name, (session, mode) in expect.items():
        if runtime == "herdr":
            assert name in h and name not in c, (name, h.keys(), c.keys())
            assert h[name]["agent_session"]["value"] == session
            args = h[name]["args"]
        else:
            assert len(c.get(name, [])) == 1 and name not in h, (name, h.keys(), c.keys())
            assert c[name][0]["session"] == session
            args = c[name][0]["claude_args"]
        if name not in never_stopped:
            assert args[-2:] == ["--resume", session], args
        flag = (["--dangerously-skip-permissions"] if mode == "bypassPermissions"
                else ["--permission-mode", mode])
        assert args[:len(flag)] == flag, args
        assert ledger(ship)[session]["mode"] == mode
    hc = ship.scenario().get("hc", {})
    assert not hc.get("forbidden"), hc.get("forbidden")
    assert not ship.scenario()["herdr"].get("killed")
    assert runtime_in_toml(ship) == runtime
    record = ship.record()
    assert record["runtime"] == runtime
    assert "switch" not in record
    for name in expect:
        assert record["agents"][name]["place"]["runtime"] == runtime
    units = ship.scenario().get("global_units", ["herdr.service"])
    assert units == ["herdr.service" if runtime == "herdr" else "ship-core.service"]
    modes = {a["name"]: a["mode"] for a in status(ship)["herdr"]["agents"] if a["name"]}
    assert modes == {n: m for n, (_, m) in expect.items()}
    assert_valid("agent-record", record)


def as_running(ship) -> dict[str, tuple[str, str]]:
    """name -> (session, mode) of every recorded agent, from the record and ledger."""
    led = ledger(ship)
    return {n: (e["session"], led[e["session"]]["mode"])
            for n, e in ship.record()["agents"].items() if e["want"] == "running"}


@pytest.fixture
def herdr_ship(ship):
    """main (auto) and grinder (bypassPermissions) running under herdr."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.ctl("apply", "--json")
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert set(herdr_agents(ship)) == {"main", "grinder"}
    return ship


@pytest.fixture
def core_ship(ship):
    """main and grinder running under herdr-core; herdr's server is not running."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.use_core()
    ship.set(global_units=["ship-core.service"])
    s = ship.scenario()
    s["herdr"]["server"] = False
    ship.set(herdr=s["herdr"])
    ship.ctl("apply", "--json")
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert set(core_agents(ship)) == {"main", "grinder"}
    return ship


# --- refusals: nothing is touched -----------------------------------------------------------
def untouched(ship, before: dict) -> None:
    assert {"main", "grinder"} <= set(herdr_agents(ship))
    assert not [c for c in ship.calls("herdr") if c[1:2] == ["agent"] and "/exit" in c]
    assert hc_runs(ship) == [] and runtime_in_toml(ship) == "herdr"
    assert "switch" not in ship.record() and "global_units" not in ship.scenario()
    assert {n: e["session"] for n, e in ship.record()["agents"].items()} == before


@pytest.mark.parametrize("setup,code,why", [
    (lambda s: s.set(hc={"installed": False}), "runtime-missing",
     "core is not installed on this Ship (/usr/local/bin/hc does not answer)"),
    (lambda s: s.set(claude={"logged_in": True, "version": "2.1.290"}), "runtime-missing",
     "older than 2.1.295"),
    (lambda s: s.set(claude={"logged_in": True, "version": "unknown"}), "runtime-missing",
     "version could not be read"),
])
def test_refuses_when_core_cannot_run_the_agents(herdr_ship, setup, code, why):
    ship = herdr_ship
    before = {n: e["session"] for n, e in ship.record()["agents"].items()}
    setup(ship)
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == code and why in doc["error"]["message"]
    untouched(ship, before)


def test_refuses_when_herdr_is_not_installed(core_ship):
    ship = core_ship
    s = ship.scenario()
    s["herdr"]["installed"] = False
    ship.set(herdr=s["herdr"])
    rc, doc, _ = switch(ship, "herdr")
    assert rc != 0 and doc["error"]["code"] == "runtime-missing"
    assert ("herdr is not installed on this Ship (/usr/local/bin/herdr does not answer)"
            in doc["error"]["message"])
    assert set(core_agents(ship)) == {"main", "grinder"} and runtime_in_toml(ship) == "core"


@pytest.mark.parametrize("state", ["working", "blocked", "unknown"])
def test_refuses_while_an_agent_is_not_idle(herdr_ship, state):
    ship = herdr_ship
    before = {n: e["session"] for n, e in ship.record()["agents"].items()}
    s = ship.scenario()
    for a in s["herdr"]["agents"]:
        if a["name"] == "main":
            a["agent_status"] = state
    ship.set(herdr=s["herdr"])
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "agent-busy"
    assert f"main ({state})" in doc["error"]["message"]
    assert not [c for c in ship.calls("herdr") if "send-keys" in c]  # not even Escape
    untouched(ship, before)


def test_refuses_an_agent_whose_session_is_not_known(herdr_ship):
    ship = herdr_ship
    before = {n: e["session"] for n, e in ship.record()["agents"].items()}
    s = ship.scenario()
    s["herdr"]["agents"].append({"name": None, "agent": "claude", "agent_status": "idle",
                                 "pane_id": "w9:p1", "workspace_id": "w9",
                                 "cwd": "/home/agent/work/main"})
    ship.set(herdr=s["herdr"])
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "agent-failed"
    assert "not in the agent record" in doc["error"]["message"]
    untouched(ship, before)


def test_refuses_an_agent_whose_mode_is_not_known(herdr_ship):
    """A Claude started by hand whose mode the ledger never saw: it could come back in
    another mode, so the switch does not start."""
    ship = herdr_ship
    s = ship.scenario()
    s["herdr"]["agents"].append({"name": "hand", "agent": "claude", "agent_status": "idle",
                                 "pane_id": "w9:p1", "workspace_id": "w9",
                                 "cwd": "/home/agent/work/main",
                                 "agent_session": {"value": "sess-hand"}})
    ship.set(herdr=s["herdr"])
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "agent-failed"
    assert "hand: its permission mode is not known" in doc["error"]["message"]
    assert hc_runs(ship) == [] and len(herdr_agents(ship)) == 3


def test_refuses_bypass_the_ship_no_longer_allows(herdr_ship):
    ship = herdr_ship
    text = ship.p("/etc/shipshape/ship.toml").read_text()
    ship.write_toml(text.replace("allow_bypass = true", "allow_bypass = false")
                    .replace('mode = "bypassPermissions"\n', ""))
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "agent-failed"
    assert "grinder runs in bypassPermissions" in doc["error"]["message"]
    assert hc_runs(ship) == [] and set(herdr_agents(ship)) == {"main", "grinder"}


def test_refuses_when_stopping_herdr_would_end_another_agent(herdr_ship):
    ship = herdr_ship
    s = ship.scenario()
    s["herdr"]["agents"].append({"name": "cx", "agent": "codex", "agent_status": "idle",
                                 "pane_id": "w9:p1", "workspace_id": "w9"})
    ship.set(herdr=s["herdr"])
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "invalid-input"
    assert ("herdr also runs cx (codex); stopping herdr would end it, so exit it first"
            in doc["error"]["message"])
    assert hc_runs(ship) == []


def test_refuses_when_the_runtime_does_not_answer(herdr_ship):
    ship = herdr_ship
    s = ship.scenario()
    s["herdr"]["server"] = False
    ship.set(herdr=s["herdr"])
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "herdr-unavailable"
    assert hc_runs(ship) == [] and runtime_in_toml(ship) == "herdr"


def test_already_on_the_target_changes_nothing(herdr_ship):
    ship = herdr_ship
    rc, doc, _ = switch(ship, "herdr")
    assert rc == 0 and doc["data"]["outcome"] == "already" and doc["changed"] == []
    assert not [c for c in ship.calls("herdr") if "/exit" in c]


def test_refuses_after_an_unplanned_ship_toml_change(herdr_ship):
    """ship.toml says core while the agents still run under herdr: the switch says
    how to get out of it rather than guessing."""
    ship = herdr_ship
    ship.use_core()
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "invalid-input"
    assert "ran under herdr this boot" in doc["error"]["message"]
    assert hc_runs(ship) == [] and set(herdr_agents(ship)) == {"main", "grinder"}


# --- clean switches -------------------------------------------------------------------------
def test_herdr_to_core(herdr_ship):
    ship = herdr_ship
    expect = as_running(ship)
    workspaces = {a["workspace_id"] for a in herdr_agents(ship).values()}
    panes = [a["pane_id"] for a in herdr_agents(ship).values()]
    starts = len(herdr_starts(ship))
    rc, doc, out = switch(ship, "core")
    assert rc == 0, out
    assert doc["data"]["outcome"] == "switched" and doc["data"]["runtime"] == "core"
    assert {a["name"]: a["mode_now"] for a in doc["data"]["agents"]} == {
        "main": "auto", "grinder": "bypassPermissions"}
    assert any("trust bundle again" in m for m in doc["messages"])
    intact(ship, "core", expect)
    assert herdr_agents(ship) == {} and len(herdr_starts(ship)) == starts
    # Graceful only: Ctrl+C and /exit typed through herdr, its workspaces closed with no
    # agent in them, herdr stopped, then the core started; its own resume stays off.
    prompts = ship.scenario()["herdr"]["prompts"]
    assert all([pane, "/exit"] in prompts for pane in panes)
    assert set(ship.scenario()["herdr"]["closed"]) == workspaces
    assert ship.scenario()["user_unit_calls"] == [["stop", "herdr.service"],
                                                  ["start", "ship-core.service"]]
    herdr_conf = ship.p("/home/agent/.config/herdr/config.toml").read_text()
    assert "resume_agents_on_restore = false" in herdr_conf
    # Each agent's terminal is labelled with its own session, in the app's space.
    for name, (session, _) in expect.items():
        labels = core_agents(ship)[name][0]["labels"]
        assert labels["shipshape.session"] == session and labels["herdr.space"] == "shipshape"
    last = status(ship)["agent_record"]["last_switch"]
    assert last["outcome"] == "switched" and (last["from"], last["to"]) == ("herdr", "core")
    assert ship.record()["switches"][-1]["agents"] == 2
    # Afterwards the refresh starts nothing more, and a reboot resumes them on core.
    runs = len(hc_runs(ship))
    ship.ctl("refresh")
    assert len(hc_runs(ship)) == runs
    ship.core_reboot()
    ship.ctl("refresh")
    intact(ship, "core", expect)


def test_core_to_herdr(core_ship):
    ship = core_ship
    expect = as_running(ship)
    old = {n: t[0]["address"] for n, t in core_agents(ship).items()}
    rc, doc, out = switch(ship, "herdr")
    assert rc == 0, out
    assert doc["data"]["outcome"] == "switched"
    intact(ship, "herdr", expect)
    assert core_agents(ship) == {}
    hc = ship.scenario()["hc"]
    assert sorted(hc["removed"]) == sorted(old.values())  # finished terminals only
    for address in old.values():
        assert [address, "/exit"] in hc["sent"]
    # The core keeps running (stopping it would end every terminal); herdr starts.
    assert ship.scenario()["user_unit_calls"] == [["start", "herdr.service"]]
    runs = len(herdr_starts(ship))
    ship.ctl("refresh")
    assert len(herdr_starts(ship)) == runs


def test_modes_survive_a_round_trip(herdr_ship):
    """acceptEdits and plan (and bypass, and auto) come back as they were, both ways."""
    ship = herdr_ship
    for name, mode in (("main", "acceptEdits"), ("grinder", "plan")):
        rc, _, out = ship.ctl("agent", "restart", name, "--mode", mode, "--json")
        assert rc == 0, out
    expect = as_running(ship)
    assert {n: m for n, (_, m) in expect.items()} == {"main": "acceptEdits", "grinder": "plan"}
    rc, _, out = switch(ship, "core")
    assert rc == 0, out
    intact(ship, "core", expect)
    rc, _, out = switch(ship, "herdr")
    assert rc == 0, out
    intact(ship, "herdr", expect)
    assert [s["outcome"] for s in ship.record()["switches"]] == ["switched", "switched"]


def test_a_forced_switch_interrupts_a_busy_agent_first(herdr_ship):
    ship = herdr_ship
    expect = as_running(ship)
    pane = herdr_agents(ship)["main"]["pane_id"]
    s = ship.scenario()
    for a in s["herdr"]["agents"]:
        if a["name"] == "main":
            a["agent_status"] = "working"
    ship.set(herdr=s["herdr"])
    rc, doc, out = switch(ship, "core", "--force")
    assert rc == 0, out
    keys = ship.scenario()["herdr"]["keys"]
    assert keys.index([pane, "esc"]) < keys.index([pane, "ctrl+c"])
    assert "main: its turn was interrupted" in doc["messages"]
    assert doc["data"]["forced"] and {a["name"]: a["was"] for a in doc["data"]["agents"]} == {
        "main": "working", "grinder": "idle"}
    intact(ship, "core", expect)


def test_an_agent_not_running_is_carried_over(herdr_ship):
    """A recorded agent that crashed (and is waiting for the next refresh) is not
    moved by the switch, but nothing holds it back on the new runtime either."""
    ship = herdr_ship
    session = ship.record()["agents"]["grinder"]["session"]
    ship.crash("grinder")
    rc, doc, out = switch(ship, "core")
    assert rc == 0, out
    assert [a["name"] for a in doc["data"]["agents"]] == ["main"]
    assert ship.record()["agents"]["grinder"]["place"] is None
    ship.ctl("refresh")
    assert core_agents(ship)["grinder"][0]["claude_args"][-2:] == ["--resume", session]
    assert len(core_agents(ship)["grinder"]) == 1 and "grinder" not in herdr_agents(ship)


# --- failures roll back -------------------------------------------------------------------
def test_a_failed_launch_rolls_back_to_herdr(herdr_ship):
    """main starts on core, grinder does not: main is stopped there (gracefully), and
    both are resumed on herdr, each once, in its mode."""
    ship = herdr_ship
    expect = as_running(ship)
    s = ship.scenario()
    s.setdefault("hc", {})["fail_run"] = ["main"]
    ship.set(hc=s["hc"])
    rc, doc, out = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "switch-failed", out
    assert doc["data"]["outcome"] == "rolled-back" and doc["data"]["runtime"] == "herdr"
    assert "main did not start on core" in doc["error"]["message"]
    intact(ship, "herdr", expect)
    # grinder had started on core first: it was stopped there with /exit, never killed.
    hc = ship.scenario()["hc"]
    assert any(text == "/exit" for _, text in hc["sent"])
    assert ship.scenario()["user_unit_calls"] == [
        ["stop", "herdr.service"], ["start", "ship-core.service"], ["start", "herdr.service"]]
    last = status(ship)["agent_record"]["last_switch"]
    assert last["outcome"] == "rolled-back"
    assert "runtime-switch-failed" not in {a["code"] for a in status(ship)["attention"]}


def test_a_lost_session_on_the_target_rolls_back(core_ship):
    """herdr cannot start grinder: everything goes back on core."""
    ship = core_ship
    expect = as_running(ship)
    s = ship.scenario()
    s["herdr"]["fail_start"] = ["grinder"]
    ship.set(herdr=s["herdr"])
    rc, doc, out = switch(ship, "herdr")
    assert rc != 0 and doc["data"]["outcome"] == "rolled-back", out
    intact(ship, "core", expect)
    assert herdr_agents(ship) == {}
    assert ship.scenario()["herdr"]["server"] is False  # herdr stopped again
    assert ship.scenario()["user_unit_calls"][-2:] == [["stop", "herdr.service"],
                                                       ["start", "ship-core.service"]]


def test_an_agent_back_in_another_mode_rolls_back(herdr_ship):
    """grinder comes up on core in default mode, not bypass: the check sees it, and
    both go back to herdr, each in its own mode."""
    ship = herdr_ship
    expect = as_running(ship)
    s = ship.scenario()
    s.setdefault("hc", {})["mode_drift"] = {"grinder": "default"}
    ship.set(hc=s["hc"])
    rc, doc, out = switch(ship, "core")
    assert rc != 0 and doc["data"]["outcome"] == "rolled-back", out
    assert "grinder came back in default, was bypassPermissions" in doc["error"]["message"]
    intact(ship, "herdr", expect)
    assert core_agents(ship) == {}


def test_an_agent_already_on_the_target_is_never_started_twice(herdr_ship):
    """The core already runs main's session (left from an earlier try): the switch
    does not start it a second time; this start is not one it can vouch for, so it
    rolls back, stopping that one too, and main runs once, on herdr."""
    ship = herdr_ship
    expect = as_running(ship)
    session = expect["main"][0]
    s = ship.scenario()
    s["hc"] = {"server": True, "next": 2, "terminals": [{
        "address": "t1", "name": "main", "argv": [LAUNCHER, "--resume", session],
        "labels": {"shipshape.session": session}, "cwd": "/home/agent/work/main",
        "state": "live", "keep": True, "created_ms": 1, "status": {
            "app": "claude-code", "state": "idle"}, "exit_code": None, "reason": None,
        "foreground": {"pid": 1, "name": "claude", "argv": ["claude", "--resume", session],
                       "cwd": "/home/agent/work/main"},
        "claude_args": ["--resume", session], "session": session}]}
    ship.set(hc=s["hc"])
    rc, doc, out = switch(ship, "core")
    assert rc != 0 and doc["data"]["outcome"] == "rolled-back", out
    assert "main was already running on core; not started again" in doc["messages"]
    assert [c for c in hc_runs(ship) if "main" in c] == []
    intact(ship, "herdr", expect)
    assert core_agents(ship) == {}


def test_an_agent_that_will_not_stop_rolls_back_before_anything_changes(herdr_ship):
    """grinder stops first, then main will not: grinder goes back into its own pane,
    main is never touched again, and nothing else changed."""
    ship = herdr_ship
    expect = as_running(ship)
    s = ship.scenario()
    s["herdr"]["stubborn"] = ["main"]
    ship.set(herdr=s["herdr"])
    panes = {n: a["pane_id"] for n, a in herdr_agents(ship).items()}
    starts = len(herdr_starts(ship))
    rc, doc, out = switch(ship, "core")
    assert rc != 0 and doc["data"]["outcome"] == "rolled-back", out
    assert "main did not exit" in doc["error"]["message"]
    intact(ship, "herdr", expect, never_stopped=("main",))
    assert hc_runs(ship) == [] and "user_unit_calls" not in ship.scenario()
    assert {n: a["pane_id"] for n, a in herdr_agents(ship).items()} == panes
    assert [c[3] for c in herdr_starts(ship)[starts:]] == ["grinder"]


def test_a_unit_that_fails_to_start_rolls_back(herdr_ship):
    ship = herdr_ship
    expect = as_running(ship)
    ship.set(unit_fails=["start ship-core.service"])
    rc, doc, out = switch(ship, "core")
    assert rc != 0 and doc["data"]["outcome"] == "rolled-back", out
    assert "ship-core.service failed" in doc["error"]["message"]
    intact(ship, "herdr", expect)
    assert hc_runs(ship) == []


def test_an_agent_stuck_on_the_target_is_never_started_twice(herdr_ship):
    """The rollback cannot stop grinder on core: it is left running there and not
    started on herdr; the switch says so, and status asks a person to look."""
    ship = herdr_ship
    expect = as_running(ship)
    s = ship.scenario()
    s.setdefault("hc", {}).update(fail_run=["main"], stubborn=["grinder"])
    ship.set(hc=s["hc"])
    rc, doc, out = switch(ship, "core")
    assert rc != 0 and doc["data"]["outcome"] == "rollback-failed", out
    assert "grinder is still on core" in doc["data"]["problems"]
    assert "grinder" not in herdr_agents(ship) and len(core_agents(ship)["grinder"]) == 1
    assert herdr_agents(ship)["main"]["args"][-2:] == ["--resume", expect["main"][0]]
    assert not ship.scenario()["hc"].get("forbidden")
    doc = status(ship)
    assert doc["agent_record"]["last_switch"]["outcome"] == "rollback-failed"
    assert "runtime-switch-failed" in {a["code"] for a in doc["attention"]}
    # The record keeps grinder on core this boot, so herdr never starts it twice.
    ship.ctl("refresh")
    assert "grinder" not in herdr_agents(ship)


def test_an_interrupted_switch_is_recorded_and_shown(herdr_ship):
    ship = herdr_ship
    record = ship.record()
    long_ago = (datetime.now(UTC) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    record["switch"] = {"from": "herdr", "to": "core", "started_at": long_ago,
                        "forced": False, "step": "starting",
                        "agents": {"main": {"cwd": "/home/agent/work/main",
                                            "session": "sess-main", "mode": "auto"}}}
    ship.p("/var/lib/shipshape/agents.json").write_text(json.dumps(record))
    doc = status(ship)
    assert doc["agent_record"]["last_switch"]["outcome"] == "in-progress"
    assert "runtime-switch-unfinished" in {a["code"] for a in doc["attention"]}
    rc, doc, out = switch(ship, "core")
    assert rc == 0, out
    assert [s["outcome"] for s in ship.record()["switches"]] == ["interrupted", "switched"]
    assert "interrupted while starting" in ship.record()["switches"][0]["message"]


# --- found in review ------------------------------------------------------------------------
def test_an_agent_exited_on_purpose_is_not_brought_back(herdr_ship):
    """grinder was /exited and no refresh ran before the switch: the switch lets it go,
    as the refresh would, rather than resuming it on the new runtime."""
    ship = herdr_ship
    expect = {"main": as_running(ship)["main"]}
    ship.exit("grinder")
    rc, doc, out = switch(ship, "core")
    assert rc == 0, out
    assert any(m.startswith("grinder stopped (it was exited") for m in doc["messages"])
    intact(ship, "core", expect)
    ship.ctl("refresh")
    assert "grinder" not in core_agents(ship)
    assert ship.record()["agents"]["grinder"]["want"] == "stopped"  # an [[autostart]] one
    assert [c for c in hc_runs(ship) if "grinder" in c] == []


def test_a_dropped_ssh_session_still_rolls_back(herdr_ship):
    """The SSH session that ran the switch drops (no tty, so no SIGHUP: stderr becomes a
    closed pipe): the failure's log line must not stop the rollback."""
    import subprocess
    import sys

    ship = herdr_ship
    expect = as_running(ship)
    s = ship.scenario()
    s.setdefault("hc", {})["fail_run"] = ["main"]
    ship.set(hc=s["hc"])
    proc = subprocess.Popen(
        [sys.executable, "-I", str(ship.bundle / "bin/ship-ctl"), "runtime", "switch", "core",
         "--json"], env=ship.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    proc.stderr.close()  # nobody reads it any more
    out, _ = proc.communicate(timeout=60)
    doc = json.loads(out)
    assert doc["data"]["outcome"] == "rolled-back", out
    intact(ship, "herdr", expect)
    assert ship.record()["switches"][-1]["outcome"] == "rolled-back"


def test_switching_again_after_a_failed_rollback_puts_it_right(herdr_ship):
    """Neither unit would start: the switch failed and so did its rollback, leaving no
    runtime running. Switching to the runtime ship.toml names starts it again and
    resumes every agent there, each in its mode, once."""
    ship = herdr_ship
    expect = as_running(ship)
    ship.set(unit_fails=["start ship-core.service", "start herdr.service"])
    rc, doc, out = switch(ship, "core")
    assert rc != 0 and doc["data"]["outcome"] == "rollback-failed", out
    assert runtime_in_toml(ship) == "herdr" and herdr_agents(ship) == {}
    assert ship.scenario()["herdr"]["server"] is False
    assert not any(m.endswith("were not started on herdr") for m in doc["messages"])
    assert "runtime-switch-failed" in {a["code"] for a in status(ship)["attention"]}
    ship.set(unit_fails=[])
    # Switching to core refuses (herdr, which runs the agents' record, is down) and
    # says how to put it right.
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and "ship-ctl runtime switch herdr starts it again" in doc["error"]["message"]
    rc, doc, out = switch(ship, "herdr")
    assert rc == 0 and doc["data"]["outcome"] == "repaired", out
    intact(ship, "herdr", expect)
    assert hc_runs(ship) == []
    last = status(ship)["agent_record"]["last_switch"]
    assert (last["outcome"], last["from"], last["to"]) == ("repaired", "herdr", "herdr")
    assert "runtime-switch-failed" not in {a["code"] for a in status(ship)["attention"]}
    rc, doc, _ = switch(ship, "herdr")  # nothing left to put right
    assert rc == 0 and doc["data"]["outcome"] == "already"


def test_a_repair_that_fails_says_so(herdr_ship):
    ship = herdr_ship
    ship.set(unit_fails=["start ship-core.service", "start herdr.service"])
    switch(ship, "core")
    rc, doc, _ = switch(ship, "herdr")
    assert rc != 0 and doc["error"]["code"] == "switch-failed"
    assert doc["data"]["outcome"] == "rollback-failed"
    attention = {a["code"]: a["message"] for a in status(ship)["attention"]}
    assert attention["runtime-switch-failed"].startswith("herdr could not be put right")


def test_a_repair_never_stops_herdr_under_an_agent(core_ship):
    """ship.toml says core after an interrupted switch, but herdr runs something:
    the repair refuses rather than stop herdr under it."""
    ship = core_ship
    record = ship.record()
    record["switches"] = [{"at": "2026-01-01T00:00:00Z", "from": "herdr", "to": "core",
                           "outcome": "interrupted", "forced": False, "agents": 2,
                           "message": "interrupted while starting"}]
    ship.p("/var/lib/shipshape/agents.json").write_text(json.dumps(record))
    s = ship.scenario()
    s["herdr"]["server"] = True
    s["herdr"]["agents"] = [{"name": "cx", "agent": "codex", "agent_status": "idle",
                             "pane_id": "w9:p1", "workspace_id": "w9"}]
    ship.set(herdr=s["herdr"])
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "invalid-input"
    assert "herdr still runs cx (codex)" in doc["error"]["message"]
    assert "user_unit_calls" not in ship.scenario()


def test_an_agent_that_exits_late_is_resumed_in_the_rollback(herdr_ship):
    """main does not exit within the stop's wait, so the switch rolls back; it exits
    a moment later. That exit was the switch's: main is resumed on herdr in its mode,
    not let go by the next refresh as one exited on purpose."""
    ship = herdr_ship
    expect = as_running(ship)
    s = ship.scenario()
    s["herdr"]["late_exit"] = {"main": 4}
    ship.set(herdr=s["herdr"])
    rc, doc, out = switch(ship, "core")
    assert rc != 0 and doc["data"]["outcome"] == "rolled-back", out
    assert "main exited after all; it is resumed on herdr" in doc["messages"]
    intact(ship, "herdr", expect)
    ship.ctl("refresh")
    intact(ship, "herdr", expect)
    assert hc_runs(ship) == []


def test_a_start_still_on_its_way_on_the_core_is_not_doubled(herdr_ship):
    """grinder's start was typed into its kept core terminal and has not come up yet
    when main's start fails: grinder may still come up there, so the rollback does
    not start it on herdr too; it says so, and main goes back to herdr."""
    ship = herdr_ship
    expect = as_running(ship)
    s = ship.scenario()
    s["hc"] = {"server": True, "next": 2, "fail_run": ["main"], "slow_shell": ["grinder"],
               "terminals": [{
                   "address": "t1", "name": "grinder", "argv": ["bash"], "labels": {},
                   "cwd": "/home/agent/work/grinder", "state": "live", "keep": True,
                   "created_ms": 1, "status": None, "exit_code": None, "reason": None,
                   "foreground": {"pid": 1, "name": "bash", "argv": ["bash"],
                                  "cwd": "/home/agent/work/grinder"}}]}
    ship.set(hc=s["hc"])
    rc, doc, out = switch(ship, "core")
    assert rc != 0 and doc["data"]["outcome"] == "rollback-failed", out
    assert "grinder is still on core" in doc["data"]["problems"]
    assert any("grinder's start on core had been typed" in m for m in doc["messages"])
    assert "grinder" not in herdr_agents(ship)
    assert herdr_agents(ship)["main"]["args"][-2:] == ["--resume", expect["main"][0]]
    assert not ship.scenario()["hc"].get("forbidden")


def test_refuses_while_another_ship_ctl_holds_the_record(herdr_ship):
    import fcntl

    ship = herdr_ship
    before = {n: e["session"] for n, e in ship.record()["agents"].items()}
    lock = next(ship.p("/var/lib/shipshape").glob("agents*.lock"))
    with lock.open("a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "agent-busy"
    untouched(ship, before)


def test_refuses_an_agent_whose_folder_is_gone(herdr_ship):
    ship = herdr_ship
    before = {n: e["session"] for n, e in ship.record()["agents"].items()}
    cwd = ship.record()["agents"]["grinder"]["cwd"]
    ship.p(cwd).rename(ship.p(cwd + "-moved"))
    rc, doc, _ = switch(ship, "core")
    assert rc != 0 and doc["error"]["code"] == "agent-failed"
    assert f"grinder: its folder {cwd} is not there" in doc["error"]["message"]
    untouched(ship, before)
