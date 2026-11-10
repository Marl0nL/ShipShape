"""ship-ctl agent restart: swap a running agent for one in a new mode (contracts/ship-ctl.md)."""

from __future__ import annotations

import json

import pytest
from ship_harness import LEDGER, assert_valid


@pytest.fixture
def running(ship):
    """main (auto, no mode of its own) and grinder (bypass) running, as after autostart,
    with the ledger the hook would have written."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.ctl("apply", "--json")
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0, doc
    write_ledger(ship, {"sess-main": "auto", "sess-grinder": "bypassPermissions"})
    s = ship.scenario()
    s["herdr"]["prompts"] = []
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    return ship


def write_ledger(ship, modes: dict[str, str]) -> None:
    sessions = {sid: {"mode": m, "updated_at": "2026-01-01T00:00:00Z"} for sid, m in modes.items()}
    ship.p(LEDGER).write_text(json.dumps({"schema": "shipshape.modes/1", "sessions": sessions}))


def ledger(ship) -> dict[str, str]:
    return {sid: e["mode"] for sid, e in ship.json(LEDGER)["sessions"].items()}


def agent(ship, name: str) -> dict:
    return next(a for a in ship.scenario()["herdr"]["agents"] if a["name"] == name)


def status_modes(ship) -> dict[str, str]:
    rc, doc, _ = ship.ctl("status", "--json")
    return {a["name"]: a["mode"] for a in doc["herdr"]["agents"]}


def test_restart_continues_the_conversation_in_the_new_default_mode(running):
    ship = running
    rc, doc, _ = ship.ctl("set", "claude.default_mode=bypassPermissions", "--json")
    assert rc == 0, doc
    pane = agent(ship, "main")["pane_id"]
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    assert_valid("ship-ctl-result", doc)
    assert doc["changed"] == ["main"]
    assert doc["data"]["restarted"] == [{
        "name": "main", "mode": "bypassPermissions", "previous_mode": "auto",
        "conversation": "continued", "was": "idle"}]
    assert "main restarted in bypassPermissions, continuing its conversation." in doc["messages"]
    # The same pane and name, the same session, with the mode flag in front.
    main = agent(ship, "main")
    assert main["pane_id"] == pane
    assert main["args"] == ["--dangerously-skip-permissions", "--resume", "sess-main"]
    # It was asked to exit, after a Ctrl+C that clears a half-typed line.
    h = ship.scenario()["herdr"]
    assert [pane, "ctrl+c"] in h["keys"] and [pane, "/exit"] in h["prompts"]
    assert not any(k[1:] == ["esc"] for k in h["keys"])  # idle: nothing interrupted
    # The ledger, the status document and the agent record all say the new mode...
    assert ledger(ship)["sess-main"] == "bypassPermissions"
    assert status_modes(ship) == {"main": "bypassPermissions", "grinder": "bypassPermissions"}
    assert ship.record()["agents"]["main"]["mode"] == "bypassPermissions"
    # No new workspace, and grinder was not touched.
    assert len(h["workspaces"]) == 2
    assert agent(ship, "grinder")["args"][0] == "--dangerously-skip-permissions"
    # ...so the next boot resumes it in that mode.
    ship.reboot()
    ship.ctl("refresh")
    assert agent(ship, "main")["args"] == ["--dangerously-skip-permissions",
                                           "--resume", "sess-main"]
    assert agent(ship, "main")["pane_id"] == pane


def test_restart_in_a_chosen_mode_comes_back_in_it_after_a_reboot(running):
    ship = running
    rc, doc, _ = ship.ctl("agent", "restart", "grinder", "--mode", "plan", "--json")
    assert rc == 0, doc
    assert agent(ship, "grinder")["args"] == ["--permission-mode", "plan",
                                              "--resume", "sess-grinder"]
    assert status_modes(ship)["grinder"] == "plan"
    ship.reboot()
    ship.ctl("refresh")
    assert agent(ship, "grinder")["args"] == ["--permission-mode", "plan",
                                              "--resume", "sess-grinder"]
    assert status_modes(ship)["grinder"] == "plan"


def test_bypass_is_refused_where_it_is_not_allowed(running):
    ship = running
    text = ship.p("/etc/shipshape/ship.toml").read_text()
    text = text.replace("allow_bypass = true", "allow_bypass = false")
    text = text.replace('mode = "bypassPermissions"\n', "")
    ship.write_toml(text)
    before = ship.scenario()["herdr"]["agents"]
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "bypassPermissions", "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "invalid-input"
    assert "bypassPermissions is not allowed on this Ship" in doc["error"]["message"]
    h = ship.scenario()["herdr"]
    assert h["agents"] == before and not h.get("keys") and not h["prompts"]


def test_a_working_agent_is_left_alone_unless_forced(running):
    ship = running
    ship.set_state("main", "working")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "acceptEdits", "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "agent-busy"
    assert doc["data"]["skipped"] == [{"name": "main", "reason": "it is working; not interrupted"}]
    assert any("force it" in m for m in doc["messages"])
    assert agent(ship, "main")["args"][:2] == ["--permission-mode", "auto"]
    assert not ship.scenario()["herdr"].get("keys")
    assert ledger(ship)["sess-main"] == "auto"

    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "acceptEdits", "--force",
                          "--json")
    assert rc == 0, doc
    pane = agent(ship, "main")["pane_id"]
    assert ship.scenario()["herdr"]["keys"][0] == [pane, "esc"]
    assert any("its turn was interrupted" in m for m in doc["messages"])
    assert doc["data"]["restarted"][0]["was"] == "working"
    assert ledger(ship)["sess-main"] == "acceptEdits"


def test_a_waiting_agent_has_its_question_dismissed_and_says_so(running):
    ship = running
    ship.set_state("main", "blocked")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    assert any("its open question was dismissed" in m for m in doc["messages"])
    assert agent(ship, "main")["args"] == ["--permission-mode", "auto", "--resume", "sess-main"]


def test_fresh_starts_a_new_conversation_with_the_same_name(running):
    ship = running
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--fresh", "--mode", "plan", "--json")
    assert rc == 0, doc
    main = agent(ship, "main")
    assert main["args"] == ["--permission-mode", "plan", "--name", "ship-01-main"]
    new = main["agent_session"]["value"]
    assert new != "sess-main"
    assert ledger(ship)[new] == "plan"
    assert ship.record()["agents"]["main"]["session"] == new
    assert doc["data"]["restarted"][0]["conversation"] == "fresh"
    assert ["main", "Get oriented."] in ship.scenario()["herdr"]["prompts"]  # its first prompt


def test_all_restarts_each_named_agent_in_the_mode_it_would_start_in(running):
    ship = running
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


def test_an_agent_that_will_not_exit_is_left_running(running):
    ship = running
    s = ship.scenario()
    s["herdr"]["stubborn"] = ["main"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "plan", "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "agent-failed"
    assert any("still running as before" in m for m in doc["messages"])
    starts = [c for c in ship.calls("herdr") if c[1:3] == ["agent", "start"]]
    assert len(starts) == 2  # the two autostart launches only
    assert ledger(ship)["sess-main"] == "auto"  # still running in auto, and said so
    assert status_modes(ship)["main"] == "auto"


def test_ctrl_c_twice_is_the_fallback_when_exit_is_ignored(running):
    ship = running
    s = ship.scenario()
    s["herdr"]["exit_on_ctrl_c"] = ["main"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    pane = agent(ship, "main")["pane_id"]
    assert [pane, "ctrl+c", "ctrl+c"] in ship.scenario()["herdr"]["keys"]
    # Ctrl+C before /exit goes to the agent; the fallback goes to its pane, which
    # takes keys even when herdr no longer sees an agent there.
    calls = ship.calls("herdr")
    assert ["herdr", "agent", "send-keys", pane, "ctrl+c"] in calls
    assert ["herdr", "pane", "send-keys", pane, "ctrl+c", "ctrl+c"] in calls


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


def forget_herdr_session(ship, name: str) -> None:
    s = ship.scenario()
    for a in s["herdr"]["agents"]:
        if a["name"] == name:
            a.pop("agent_session")
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))


def test_the_session_comes_from_the_record_when_herdr_does_not_report_it(running):
    ship = running
    forget_herdr_session(ship, "main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    assert agent(ship, "main")["args"] == ["--permission-mode", "auto", "--resume", "sess-main"]


def test_without_a_known_session_only_a_fresh_start_works(running):
    ship = running
    forget_herdr_session(ship, "main")
    doc = ship.record()
    doc["agents"]["main"]["session"] = None
    ship.p("/var/lib/shipshape/agents.json").write_text(json.dumps(doc))
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 1 and doc["error"]["code"] == "agent-failed"
    assert "restart it fresh" in doc["data"]["skipped"][0]["reason"]
    assert not ship.scenario()["herdr"].get("keys")  # never stopped
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--fresh", "--json")
    assert rc == 0, doc


def test_without_herdr(running):
    ship = running
    ship.set(herdr={"server": False})
    rc, doc, _ = ship.ctl("agent", "restart", "--all", "--json")
    assert rc == 1 and doc["error"]["code"] == "herdr-unavailable"


def test_exit_waits_until_escape_has_dismissed_the_question(running):
    # herdr reads the screen, so it sees the question gone a poll or two after Escape,
    # and refuses a prompt to an agent it still sees waiting.
    ship = running
    ship.env["SHIPCTL_TEST_WAIT_S"] = "5"
    s = ship.scenario()
    s["herdr"]["slow_esc"] = ["main"]
    (ship.fake_dir / "scenario.json").write_text(json.dumps(s))
    ship.set_state("main", "blocked")
    pane = agent(ship, "main")["pane_id"]
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, doc
    h = ship.scenario()["herdr"]
    assert [pane, "/exit"] in h["prompts"]  # the graceful way, not the fallback
    assert [pane, "ctrl+c", "ctrl+c"] not in h["keys"]
    assert any("its open question was dismissed" in m for m in doc["messages"])


def test_an_agent_in_an_unknown_state_counts_as_busy(running):
    ship = running
    ship.set_state("main", "unknown")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 1 and doc["error"]["code"] == "agent-busy"
    assert doc["data"]["skipped"][0]["reason"] == (
        "it is in a state herdr cannot tell; not interrupted")
    assert not ship.scenario()["herdr"].get("keys")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--force", "--json")
    assert rc == 0, doc
    assert any("herdr could not tell its state" in m for m in doc["messages"])


def test_an_agent_that_stops_but_does_not_start_again_says_how_to_start_it(running):
    ship = running
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
                          "--resume sess-main") for m in doc["messages"])
    starts = [c for c in ship.calls("herdr") if c[1:3] == ["agent", "start"]]
    assert len(starts) == 4  # two autostart launches, then a try and one retry


def test_set_notes_name_the_restart_command(running):
    ship = running
    rc, doc, _ = ship.ctl("set", "claude.remote_control=off", "claude.session_prefix=lab",
                          "--json")
    assert rc == 0, doc
    assert any("ship-fleet agent restart ship-01 --all" in m for m in doc["messages"])
    assert any("ship-fleet agent restart ship-01 --all --fresh" in m for m in doc["messages"])
