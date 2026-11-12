"""ship-ctl agent restart: swap a running agent for one in a new mode (contracts/ship-ctl.md).

The sandbox Ship is a herdr-core Ship: agents are terminals of the fake `hc`. A
restart types Ctrl+C and `/exit` into the agent's terminal (`hc keys`, `hc send`);
/exit finishes that terminal, so the agent comes back in a new terminal by the
same name. Sessions are the uuids ShipShape chose (the agent record has them).
"""

from __future__ import annotations

import json

import pytest
from ship_harness import LEDGER, assert_valid


@pytest.fixture
def running(ship):
    """main (auto, no mode of its own) and grinder (bypass) running, as after autostart,
    with the ledger the hook wrote as they started."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.ctl("apply", "--json")
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0, doc
    assert ledger(ship) == {sid(ship, "main"): "auto", sid(ship, "grinder"): "bypassPermissions"}
    s = ship.scenario()
    s["hc"]["sent"] = []
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    return ship


def sid(ship, name: str) -> str:
    """The session ShipShape chose for the agent (the record has it)."""
    return ship.record()["agents"][name]["session"]


def hc(ship) -> dict:
    return ship.scenario()["hc"]


def runs(ship) -> list[list[str]]:
    """Each `hc run` (a new terminal)."""
    return [c for c in ship.calls("hc") if c[1:2] == ["run"]]


def ledger(ship) -> dict[str, str]:
    return {sid: e["mode"] for sid, e in ship.json(LEDGER)["sessions"].items()}


def args(ship, name: str) -> list[str]:
    """claude's arguments in the agent's live terminal."""
    return ship.args()[name]


def status_modes(ship) -> dict[str, str]:
    rc, doc, _ = ship.ctl("status", "--json")
    return {a["name"]: a["mode"] for a in doc["herdr"]["agents"]}


def test_restart_continues_the_conversation_in_the_new_default_mode(running):
    ship = running
    session = sid(ship, "main")
    rc, doc, _ = ship.ctl("set", "claude.default_mode=bypassPermissions", "--json")
    assert rc == 0, doc
    old = ship.place("main")
    grinder = ship.place("grinder")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    assert_valid("ship-ctl-result", doc)
    assert doc["changed"] == ["main"]
    assert doc["data"]["restarted"] == [{
        "name": "main", "mode": "bypassPermissions", "previous_mode": "auto",
        "conversation": "continued", "was": "idle"}]
    assert "main restarted in bypassPermissions, continuing its conversation." in doc["messages"]
    # The same name and folder, the same session, with the mode flag in front: /exit
    # finished its old terminal, so it runs in a new one by that name.
    main = ship.agent("main")
    new = main["address"]
    assert new != old and main["cwd"] == "/home/agent/work/main"
    assert main["claude_args"] == ["--dangerously-skip-permissions", "--resume", session]
    # It was asked to exit, after a Ctrl+C that clears a half-typed line.
    h = hc(ship)
    assert [old, "ctrl+c"] in h["keys"] and [old, "/exit"] in h["sent"]
    assert not any(k[1:] == ["esc"] for k in h["keys"])  # idle: nothing interrupted
    # The ledger, the status document and the agent record all say the new mode...
    assert ledger(ship)[session] == "bypassPermissions"
    assert status_modes(ship) == {"main": "bypassPermissions", "grinder": "bypassPermissions"}
    assert ship.record()["agents"]["main"]["mode"] == "bypassPermissions"
    # One new terminal, for main only, and grinder was not touched.
    assert len(runs(ship)) == 3
    assert ship.place("grinder") == grinder
    assert args(ship, "grinder")[0] == "--dangerously-skip-permissions"
    # ...so the next boot resumes it in that mode, in its new kept terminal.
    ship.reboot()
    ship.ctl("refresh")
    assert args(ship, "main") == ["--dangerously-skip-permissions", "--resume", session]
    assert ship.place("main") == new


def test_restart_in_a_chosen_mode_comes_back_in_it_after_a_reboot(running):
    ship = running
    session = sid(ship, "grinder")
    rc, doc, _ = ship.ctl("agent", "restart", "grinder", "--mode", "plan", "--json")
    assert rc == 0, doc
    assert args(ship, "grinder") == ["--permission-mode", "plan", "--resume", session]
    assert status_modes(ship)["grinder"] == "plan"
    ship.reboot()
    ship.ctl("refresh")
    assert args(ship, "grinder") == ["--permission-mode", "plan", "--resume", session]
    assert status_modes(ship)["grinder"] == "plan"


def test_bypass_is_refused_where_it_is_not_allowed(running):
    ship = running
    text = ship.p("/etc/shipshape/ship.toml").read_text()
    text = text.replace("allow_bypass = true", "allow_bypass = false")
    text = text.replace('mode = "bypassPermissions"\n', "")
    ship.write_toml(text)
    before = ship.terminals()
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "bypassPermissions", "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "invalid-input"
    assert "bypassPermissions is not allowed on this Ship" in doc["error"]["message"]
    assert ship.terminals() == before and not hc(ship).get("keys") and not ship.prompts()


def test_a_working_agent_is_left_alone_unless_forced(running):
    ship = running
    session = sid(ship, "main")
    ship.set_state("main", "working")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "acceptEdits", "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "agent-busy"
    assert doc["data"]["skipped"] == [{"name": "main", "reason": "it is working; not interrupted"}]
    assert any("force it" in m for m in doc["messages"])
    assert args(ship, "main")[:2] == ["--permission-mode", "auto"]
    assert not hc(ship).get("keys")
    assert ledger(ship)[session] == "auto"

    old = ship.place("main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "acceptEdits", "--force",
                          "--json")
    assert rc == 0, doc
    assert hc(ship)["keys"][0] == [old, "esc"]
    assert any("its turn was interrupted" in m for m in doc["messages"])
    assert doc["data"]["restarted"][0]["was"] == "working"
    assert ledger(ship)[session] == "acceptEdits"


def test_a_waiting_agent_has_its_question_dismissed_and_says_so(running):
    ship = running
    ship.set_state("main", "blocked")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    assert any("its open question was dismissed" in m for m in doc["messages"])
    assert args(ship, "main") == ["--permission-mode", "auto", "--resume", sid(ship, "main")]


def test_fresh_starts_a_new_conversation_with_the_same_name(running):
    ship = running
    old = sid(ship, "main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--fresh", "--mode", "plan", "--json")
    assert rc == 0, doc
    new = ship.session("main")
    assert args(ship, "main") == ["--permission-mode", "plan", "--name", "ship-01-main",
                                  "--session-id", new]
    assert new != old
    assert ledger(ship)[new] == "plan"
    assert ship.record()["agents"]["main"]["session"] == new
    assert doc["data"]["restarted"][0]["conversation"] == "fresh"
    assert ["main", "Get oriented."] in ship.prompts()  # its first prompt


def test_all_restarts_each_named_agent_in_the_mode_it_would_start_in(running):
    ship = running
    s = ship.scenario()
    # A Claude someone started by hand in a terminal of its own, with no name.
    s["hc"]["terminals"].append({
        "address": "t9", "name": None, "argv": ["claude"], "labels": {}, "cwd": "/home/agent",
        "state": "live", "keep": False, "created_ms": 1, "status":
        {"app": "claude-code", "state": "idle"}, "turn": 0, "exit_code": None,
        "reason": None, "foreground": {"pid": 4545, "name": "claude", "argv": ["claude"],
                                       "cwd": "/home/agent"}})
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    rc, doc, _ = ship.ctl("set", "claude.default_mode=acceptEdits", "--json")
    rc, doc, _ = ship.ctl("agent", "restart", "--all", "--json")
    assert rc == 0, doc
    assert_valid("ship-ctl-result", doc)
    # grinder has a mode of its own in [[autostart]]; main follows the default.
    assert {r["name"]: r["mode"] for r in doc["data"]["restarted"]} == {
        "main": "acceptEdits", "grinder": "bypassPermissions"}
    assert doc["data"]["skipped"] == [{"name": "t9", "reason": "it has no herdr name"}]


def test_an_agent_that_will_not_exit_is_left_running(running):
    ship = running
    s = ship.scenario()
    s["hc"]["stubborn"] = ["main"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "plan", "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "agent-failed"
    assert any("still running as before" in m for m in doc["messages"])
    assert len(runs(ship)) == 2  # the two autostart launches only
    assert ledger(ship)[sid(ship, "main")] == "auto"  # still running in auto, and said so
    assert status_modes(ship)["main"] == "auto"


def test_ctrl_c_twice_is_the_fallback_when_exit_is_ignored(running):
    ship = running
    s = ship.scenario()
    s["hc"]["exit_on_ctrl_c"] = ["main"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    old = ship.place("main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    keys = hc(ship)["keys"]
    assert [old, "/exit"] in hc(ship)["sent"]
    assert keys.index([old, "ctrl+c"]) < keys.index([old, "ctrl+c", "ctrl+c"])
    # Both reach the terminal (hc keys), whatever runs in it.
    calls = ship.calls("hc")
    assert ["hc", "keys", "--json", old, "ctrl+c"] in calls
    assert ["hc", "keys", "--json", old, "ctrl+c", "ctrl+c"] in calls
    assert args(ship, "main") == ["--permission-mode", "auto", "--resume", sid(ship, "main")]


def test_unknown_names_and_bad_arguments(running):
    ship = running
    rc, doc, _ = ship.ctl("agent", "restart", "nobody", "--json")
    assert rc == 1 and doc["error"]["code"] == "invalid-input"
    assert "running: grinder, main" in doc["error"]["message"]
    rc, doc, _ = ship.ctl("agent", "restart", "--json")
    assert rc == 2 and doc["error"]["code"] == "usage"
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--all", "--json")
    assert rc == 2 and doc["error"]["code"] == "usage"
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "yolo", "--json")
    assert rc == 2 and doc["error"]["code"] == "usage"


def forget_core_session(ship, name: str) -> None:
    """The core no longer tells main's session: its terminal lost the session label
    ShipShape gave it, and the mode-ledger hook did not see it start there."""
    s = ship.scenario()
    place = None
    for t in s["hc"]["terminals"]:
        if t["name"] == name and t["state"] == "live":
            t["labels"].pop("shipshape.session", None)
            place = t["address"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    doc = ship.json(LEDGER)
    for e in doc["sessions"].values():
        if e.get("terminal") == place:
            del e["terminal"]
    ship.p(LEDGER).write_text(json.dumps(doc))


def test_the_session_comes_from_the_record_when_the_core_does_not_report_it(running):
    ship = running
    session = sid(ship, "main")
    forget_core_session(ship, "main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    assert args(ship, "main") == ["--permission-mode", "auto", "--resume", session]


def test_without_a_known_session_only_a_fresh_start_works(running):
    ship = running
    forget_core_session(ship, "main")
    doc = ship.record()
    doc["agents"]["main"]["session"] = None
    ship.p("/var/lib/shipshape/agents.json").write_text(json.dumps(doc))
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 1 and doc["error"]["code"] == "agent-failed"
    assert "restart it fresh" in doc["data"]["skipped"][0]["reason"]
    assert not hc(ship).get("keys")  # never stopped
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--fresh", "--json")
    assert rc == 0, doc


def test_without_herdr(running):
    ship = running
    ship.set(hc={"server": False})
    rc, doc, _ = ship.ctl("agent", "restart", "--all", "--json")
    assert rc == 1 and doc["error"]["code"] == "herdr-unavailable"


def test_exit_waits_until_escape_has_dismissed_the_question(running):
    # Claude reports the question gone a poll or two after Escape, and hc refuses to
    # type into an agent that is still blocked.
    ship = running
    ship.env["SHIPCTL_TEST_WAIT_S"] = "5"
    s = ship.scenario()
    s["hc"]["slow_esc"] = ["main"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    ship.set_state("main", "blocked")
    old = ship.place("main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    h = hc(ship)
    assert [old, "/exit"] in h["sent"]  # the graceful way, not the fallback
    assert [old, "ctrl+c", "ctrl+c"] not in h["keys"]
    assert any("its open question was dismissed" in m for m in doc["messages"])


def test_an_agent_in_an_unknown_state_counts_as_busy(running):
    ship = running
    ship.set_state("main", "unknown")  # Claude has not reported its state
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 1 and doc["error"]["code"] == "agent-busy"
    assert doc["data"]["skipped"][0]["reason"] == (
        "it is in a state herdr cannot tell; not interrupted")
    assert not hc(ship).get("keys")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--force", "--json")
    assert rc == 0, doc
    assert any("Claude had not reported its state" in m for m in doc["messages"])


def test_an_agent_that_stops_but_does_not_start_again_says_how_to_start_it(running):
    ship = running
    session = sid(ship, "main")
    s = ship.scenario()
    s["hc"]["fail_run"] = ["main"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "plan", "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "agent-failed"
    assert doc["data"]["skipped"] == [{"name": "main", "reason": (
        "stopped, but did not start again: spawn_failed")}]
    assert any(m.endswith("Start it in its pane with: claude --permission-mode plan "
                          f"--resume {session}") for m in doc["messages"])
    assert len(runs(ship)) == 3  # two autostart launches, then one try (no retry on core)


def test_set_notes_name_the_restart_command(running):
    ship = running
    rc, doc, _ = ship.ctl("set", "claude.remote_control=off", "claude.session_prefix=lab",
                          "--json")
    assert rc == 0, doc
    assert any("ship-fleet agent restart ship-01 --all" in m for m in doc["messages"])
    assert any("ship-fleet agent restart ship-01 --all --fresh" in m for m in doc["messages"])
