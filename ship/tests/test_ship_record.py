"""The agent record: ShipShape resumes its agents itself (contracts/ship-ctl.md,
"The agent record"; contracts/agent-record.schema.json).

The sandbox Ship's ship.toml has two [[autostart]] agents, main (auto, the default
mode) and grinder (bypassPermissions). `ship.reboot()` is what a reboot looks like
to ship-ctl: a new boot id, and herdr back with every pane restored as a shell
under its old id, with no agent in it (herdr's own resume is off).
"""

from __future__ import annotations

import fcntl
import json

import pytest
from ship_harness import LEDGER, assert_valid
from test_ship_converge import pack

RECORD = "/var/lib/shipshape/agents.json"


def args(ship) -> dict[str, list[str]]:
    return {a["name"]: a["args"] for a in ship.scenario()["herdr"]["agents"]}


def starts(ship) -> list[list[str]]:
    return [c for c in ship.calls("herdr") if c[1:3] == ["agent", "start"]]


def recorded(ship) -> dict[str, dict]:
    return {a["name"]: a for a in ship.status()["agent_record"]["agents"]}


def add_live(ship, name, session, cwd, pane, mode=None, kind="claude", watched=True):
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
def started(ship):
    """main and grinder started, as at a Ship's first boot: by the status refresh
    that sees Claude signed in (so later refreshes see no new sign-in)."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.ctl("apply", "--json")
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert set(args(ship)) == {"main", "grinder"}
    return ship


# --- the record ------------------------------------------------------------------------
def test_autostart_writes_the_record(started):
    ship = started
    doc = ship.record()
    assert_valid("agent-record", doc)
    main = doc["agents"]["main"]
    assert main["cwd"] == "/home/agent/work/main" and main["session"] == "sess-main"
    assert main["mode"] == "auto" and main["prompt"] == "Get oriented."
    assert main["session_name"] == "ship-01-main" and main["origin"] == "autostart"
    assert main["place"]["id"] == "w1:p1" and main["want"] == "running"
    assert doc["agents"]["grinder"]["mode"] == "bypassPermissions"
    assert oct(ship.p(RECORD).stat().st_mode & 0o777) == "0o644"
    assert recorded(ship)["main"] == {
        "name": "main", "cwd": "/home/agent/work/main", "mode": "auto", "autostart": True,
        "state": "running", "relaunch_failures": 0, "created_at": main["created_at"],
        "updated_at": main["updated_at"]}


def test_status_never_shows_session_ids(started):
    _, _, text = started.ctl("status", "--json")
    assert "sess-main" not in text


# --- resume after a boot ----------------------------------------------------------------
def test_after_a_reboot_the_refresh_resumes_every_agent_in_its_folder_and_mode(started):
    ship = started
    panes = {a["name"]: a["pane_id"] for a in ship.scenario()["herdr"]["agents"]}
    ship.reboot()
    assert recorded(ship)["main"]["state"] == "starting"
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert args(ship) == {
        "main": ["--permission-mode", "auto", "--resume", "sess-main"],
        "grinder": ["--dangerously-skip-permissions", "--resume", "sess-grinder"],
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


def test_a_restored_pane_that_is_gone_gets_a_new_workspace(started):
    ship = started
    ship.reboot()
    s = ship.scenario()
    s["herdr"]["panes"] = [p for p in s["herdr"]["panes"] if p["pane_id"] != "w1:p1"]
    ship.set(herdr=s["herdr"])  # herdr lost main's pane (its layout was not saved)
    ship.ctl("refresh")
    main = ship.agent("main")
    assert main["args"] == ["--permission-mode", "auto", "--resume", "sess-main"]
    assert main["pane_id"] == "w3:p1" and main["cwd"] == "/home/agent/work/main"
    assert ship.agent("grinder")["pane_id"] == "w2:p1"


def test_another_agent_in_its_old_pane_never_becomes_it(started):
    """herdr's pane list cannot be read, and another, named agent sits in main's old
    pane: main must not take that agent's session (or its mode)."""
    ship = started
    ship.reboot()
    s = ship.scenario()
    s["herdr"]["panes"] = [p for p in s["herdr"]["panes"] if p["pane_id"] != "w1:p1"]
    ship.set(herdr=s["herdr"])
    add_live(ship, "other", "sess-other", "/home/agent/work/main", "w1:p1", mode="plan")
    ship.set(herdr={**ship.scenario()["herdr"], "pane_list_fails": True})
    ship.ctl("refresh")
    record = ship.record()["agents"]
    assert record["main"]["session"] == "sess-main" and record["main"]["mode"] == "auto"
    assert record["other"]["session"] == "sess-other"
    main = ship.agent("main")
    assert main["args"] == ["--permission-mode", "auto", "--resume", "sess-main"]
    assert main["pane_id"] == "w3:p1"  # a new workspace, not the busy pane
    assert not [c for c in starts(ship) if "sess-other" in c]
    ship.ctl("refresh")
    assert ship.record()["agents"]["main"]["session"] == "sess-main"


def test_an_unnamed_agent_in_its_pane_but_another_folder_never_becomes_it(started):
    """main crashed; someone uses its pane, `cd`s elsewhere and runs claude there by
    hand (no herdr name). That is another agent: main keeps its own session, mode
    and folder, and the other is recorded under its own name."""
    ship = started
    ship.crash("main")
    add_live(ship, None, "sess-x", "/home/agent/work/elsewhere", "w1:p1", mode="plan")
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
    assert args(ship)["elsewhere"] == ["--permission-mode", "plan", "--resume", "sess-x"]
    assert args(ship)["main"] == ["--permission-mode", "auto", "--name", "ship-01-main"]
    assert not [c for c in starts(ship) if c[3] == "main" and "sess-x" in c]


def test_claude_started_again_by_hand_in_its_pane_and_folder_is_still_it(started):
    """The same, in its own folder (claude started again there by hand, in a new
    session): that is the recorded agent, its name given back, its session followed."""
    ship = started
    ship.crash("main")
    add_live(ship, None, "sess-main-2", "/home/agent/work/main", "w1:p1", mode="auto")
    ship.ctl("refresh")
    assert set(ship.record()["agents"]) == {"main", "grinder"}
    assert ship.record()["agents"]["main"]["session"] == "sess-main-2"
    assert ["w1:p1", "main"] in [c[3:] for c in ship.calls("herdr") if c[1:3] == ["agent",
                                                                                   "rename"]]
    assert len([c for c in starts(ship) if c[3] == "main"]) == 1  # not started again


def test_a_pane_in_another_folder_is_not_used(started):
    ship = started
    ship.reboot()
    s = ship.scenario()
    for p in s["herdr"]["panes"]:
        if p["pane_id"] == "w1:p1":
            p["cwd"] = "/home/agent/elsewhere"
    ship.set(herdr=s["herdr"])
    ship.ctl("refresh")
    assert ship.agent("main")["pane_id"] == "w3:p1"  # claude --resume needs its folder


def test_without_herdrs_pane_list_the_agents_own_panes_are_tried_first(started):
    ship = started
    ship.reboot()
    ship.set(herdr={**ship.scenario()["herdr"], "pane_list_fails": True})
    ship.ctl("refresh")
    assert {a["name"]: a["pane_id"] for a in ship.scenario()["herdr"]["agents"]} == {
        "main": "w1:p1", "grinder": "w2:p1"}
    assert len(ship.scenario()["herdr"]["workspaces"]) == 2


def test_notices_wait_until_this_boots_resume_is_done(started):
    ship = started
    ship.reboot()
    rc, doc, _ = ship.ctl("notify-signin", "github", "--json")
    assert doc["data"]["sent"] == []
    assert {s["reason"] for s in doc["data"]["skipped"]} == {"resume-pending"}
    ship.ctl("refresh")
    rc, doc, _ = ship.ctl("notify-signin", "github", "--json")
    assert doc["data"]["sent"] == ["main", "grinder"]


def test_bypass_is_not_resumed_where_it_is_no_longer_allowed(started):
    ship = started
    text = ship.p("/etc/shipshape/ship.toml").read_text()
    ship.write_toml(text.replace("allow_bypass = true", "allow_bypass = false")
                    .replace('mode = "bypassPermissions"\n', ""))
    ship.reboot()
    ship.ctl("refresh")
    assert ship.agent("grinder")["args"] == ["--permission-mode", "auto",
                                             "--resume", "sess-grinder"]
    assert ship.record()["agents"]["grinder"]["mode"] == "auto"


def test_nothing_resumes_while_provisioning(started):
    ship = started
    ship.reboot()
    ship.set(cloud_init="running")
    ship.ctl("refresh")
    assert ship.scenario()["herdr"]["agents"] == []


# --- crashes, and agents stopped on purpose ---------------------------------------------------
def test_an_agent_that_crashed_is_started_again_in_its_pane(started):
    ship = started
    ship.crash("grinder")
    assert recorded(ship)["grinder"]["state"] == "starting"
    ship.ctl("refresh")
    grinder = ship.agent("grinder")
    assert grinder["args"] == ["--dangerously-skip-permissions", "--resume", "sess-grinder"]
    assert grinder["pane_id"] == "w2:p1"
    assert recorded(ship)["grinder"]["state"] == "running"


def test_after_three_failed_tries_it_is_left_alone_with_attention(started):
    ship = started
    ship.crash("grinder")  # within 10 minutes of its start: the first failed try
    s = ship.scenario()
    s["herdr"]["fail_start"] = ["grinder"]
    ship.set(herdr=s["herdr"])
    for _ in range(4):
        ship.ctl("refresh")
    # Its first start, then two failed starts (each tried twice), then it is let be.
    assert len([c for c in starts(ship) if c[3] == "grinder"]) == 1 + 2 * 2
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
                                             "--resume", "sess-grinder"]
    assert ship.record()["agents"]["grinder"]["mode"] == "dontAsk"
    assert "agent-relaunch-failed" not in {a["code"] for a in ship.status()["attention"]}


def test_an_agent_that_keeps_dying_soon_after_it_starts_is_not_started_forever(started):
    """Its starts succeed, but it stops each time before the next refresh (a transcript
    too big to load, say): every early stop is a failed try."""
    ship = started
    for _ in range(5):
        ship.crash("grinder")
        ship.ctl("refresh")
    assert len([c for c in starts(ship) if c[3] == "grinder"]) == 1 + 2  # then let be
    grinder = recorded(ship)["grinder"]
    assert grinder["state"] == "failed" and grinder["relaunch_failures"] == 3
    assert grinder["last_error"].startswith("it stopped ") and "after it started" in (
        grinder["last_error"])
    assert "agent-relaunch-failed" in {a["code"] for a in ship.status()["attention"]}


def age_launch(ship, name: str, minutes: int) -> None:
    """As if `name` was started that long ago."""
    from datetime import UTC, datetime, timedelta

    doc = ship.record()
    when = datetime.now(UTC) - timedelta(minutes=minutes)
    doc["agents"][name]["launched_at"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    ship.p(RECORD).write_text(json.dumps(doc))


def test_tries_reset_once_an_agent_has_run_ten_minutes(started):
    ship = started
    for _ in range(2):  # two early stops: two tries spent
        ship.crash("grinder")
        ship.ctl("refresh")
    assert recorded(ship)["grinder"]["relaunch_failures"] == 2
    ship.ctl("refresh")  # running, but only just: nothing is given back yet
    assert recorded(ship)["grinder"]["relaunch_failures"] == 2
    age_launch(ship, "grinder", 11)
    ship.ctl("refresh")  # it has run long enough
    assert recorded(ship)["grinder"]["relaunch_failures"] == 0
    age_launch(ship, "grinder", 11)
    ship.crash("grinder")  # a crash after a long run is not an early stop
    ship.ctl("refresh")
    assert "grinder" in args(ship) and recorded(ship)["grinder"]["relaunch_failures"] == 0


def test_a_crashed_agent_comes_back_in_the_mode_it_was_switched_to_in_claude(started):
    """Its mode changed in Claude (the ledger has it) just before it crashed, before
    a refresh brought the record up to date: the ledger's mode wins."""
    ship = started
    doc = ship.json(LEDGER)
    doc["sessions"]["sess-main"]["mode"] = "plan"
    ship.p(LEDGER).write_text(json.dumps(doc))
    ship.crash("main")
    ship.ctl("refresh")
    assert ship.agent("main")["args"] == ["--permission-mode", "plan", "--resume", "sess-main"]


def test_resume_waits_for_dns_after_a_boot(started):
    ship = started
    ship.reboot()
    ship.set(dns=False)
    rc, _, out = ship.ctl("refresh")
    assert ship.scenario()["herdr"]["agents"] == []
    assert "agent record: resume waits for DNS" in out
    rc, doc, _ = ship.ctl("notify-signin", "github", "--json")
    assert {s["reason"] for s in doc["data"]["skipped"]} == {"resume-pending"}
    ship.set(dns=True)
    ship.ctl("refresh")
    assert set(args(ship)) == {"main", "grinder"}
    # Ten minutes into a boot nothing waits any more, DNS or not.
    ship.reboot(uptime=700.0)
    ship.set(dns=False)
    ship.ctl("refresh")
    assert set(args(ship)) == {"main", "grinder"}


def test_a_reboot_gives_a_failed_agent_new_tries(started):
    ship = started
    ship.crash("grinder")
    s = ship.scenario()
    s["herdr"]["fail_start"] = ["grinder"]
    ship.set(herdr=s["herdr"])
    for _ in range(3):
        ship.ctl("refresh")
    s = ship.scenario()
    s["herdr"]["fail_start"] = []
    ship.set(herdr=s["herdr"])
    ship.reboot()
    ship.ctl("refresh")
    assert ship.agent("grinder")["args"][-2:] == ["--resume", "sess-grinder"]


def test_a_lost_conversation_counts_as_a_failed_start(started):
    ship = started
    s = ship.scenario()
    s["herdr"]["lost_sessions"] = ["sess-main"]  # claude --resume: No conversation found
    ship.set(herdr=s["herdr"])
    ship.reboot()
    ship.ctl("refresh")
    assert "main" not in args(ship) and "grinder" in args(ship)
    assert ship.status()["herdr"]["last_resume"] == {
        "at": ship.record()["resume"]["at"], "method": "api-fallback", "resumed": 1,
        "failed": 1}
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--fresh", "--json")
    assert rc == 0, doc
    assert ship.agent("main")["args"] == ["--permission-mode", "auto", "--name", "ship-01-main"]
    assert ["main", "Get oriented."] in ship.scenario()["herdr"]["prompts"]


def test_a_restart_that_did_not_start_again_is_started_by_the_next_refresh(started):
    ship = started
    s = ship.scenario()
    s["herdr"]["fail_start"] = ["main"]
    ship.set(herdr=s["herdr"])
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "plan", "--json")
    assert rc == 1 and doc["error"]["code"] == "agent-failed"
    assert recorded(ship)["main"]["state"] == "starting"
    s = ship.scenario()
    s["herdr"]["fail_start"] = []
    ship.set(herdr=s["herdr"])
    ship.ctl("refresh")  # its /exit was the restart's own, not someone's: a crash
    assert recorded(ship)["main"]["relaunch_failures"] == 1  # it stopped at once
    assert ship.agent("main")["args"] == ["--permission-mode", "plan", "--resume", "sess-main"]
    assert ship.agent("main")["pane_id"] == "w1:p1"


def test_an_agent_someone_exited_is_not_started_again(started):
    ship = started
    ship.exit("grinder")
    ship.ctl("refresh")
    assert "grinder" not in args(ship)
    assert recorded(ship)["grinder"]["state"] == "stopped"
    # An [[autostart]] agent starts afresh at the next boot, as it always did.
    ship.reboot()
    ship.ctl("refresh")
    assert args(ship) == {
        "main": ["--permission-mode", "auto", "--resume", "sess-main"],
        "grinder": ["--dangerously-skip-permissions", "--name", "ship-01-grinder"],
    }


def test_an_exit_in_an_earlier_boot_does_not_stop_the_resume(started):
    """Claude may report a shutdown's SIGTERM however it likes: only an exit seen in
    this boot counts as on purpose."""
    ship = started
    ship.reboot()
    ship.end_session("sess-main", "prompt_input_exit")
    s = ship.json(LEDGER)
    s["sessions"]["sess-main"]["ended"]["at"] = "2026-01-01T00:00:00Z"  # before this boot
    ship.p(LEDGER).write_text(json.dumps(s))
    ship.ctl("refresh")
    assert args(ship)["main"] == ["--permission-mode", "auto", "--resume", "sess-main"]


def test_a_closed_pane_is_a_stopped_agent(started):
    ship = started
    s = ship.scenario()
    s["herdr"]["agents"] = [a for a in s["herdr"]["agents"] if a["name"] != "main"]
    s["herdr"]["panes"] = [p for p in s["herdr"]["panes"] if p["pane_id"] != "w1:p1"]
    ship.set(herdr=s["herdr"])  # someone closed main's pane: SIGHUP, reason "other"
    ship.ctl("refresh")
    assert "main" not in args(ship)
    assert ship.record()["agents"]["main"]["want"] == "stopped"


def test_a_refresh_leaves_the_record_alone_while_another_ship_ctl_holds_it(started):
    ship = started
    ship.crash("grinder")
    with open(ship.p("/var/lib/shipshape/agents.lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        ship.ctl("refresh")  # say an agent restart is between /exit and its new start
        assert "grinder" not in args(ship)
    ship.ctl("refresh")
    assert "grinder" in args(ship)


# --- agents started by hand ---------------------------------------------------------------------
def test_an_agent_started_by_hand_is_recorded_and_resumed(started):
    ship = started
    add_live(ship, "helper", "sess-helper", "/home/agent/work/side", "w7:p1", mode="plan")
    ship.p("/home/agent/work/side").mkdir(parents=True)
    ship.ctl("refresh")
    helper = ship.record()["agents"]["helper"]
    assert helper["origin"] == "adopted" and helper["mode"] == "plan"
    assert helper["cwd"] == "/home/agent/work/side" and helper["prompt"] is None
    assert recorded(ship)["helper"]["autostart"] is False
    ship.reboot()
    ship.ctl("refresh")
    assert ship.agent("helper")["args"] == ["--permission-mode", "plan", "--resume",
                                            "sess-helper"]
    assert ship.agent("helper")["pane_id"] == "w7:p1"


def test_an_unnamed_agent_is_named_after_its_folder(started):
    ship = started
    add_live(ship, None, "sess-x", "/home/agent/work/My Project", "w8:p1")
    add_live(ship, None, "sess-y", "/home/agent/work/main", "w9:p1")  # main is taken
    ship.ctl("refresh")
    renames = sorted(c[3:] for c in ship.calls("herdr") if c[1:3] == ["agent", "rename"])
    assert renames == [["w8:p1", "my-project"], ["w9:p1", "main-2"]]
    assert {"my-project", "main-2"} <= set(ship.record()["agents"])


def test_an_agent_without_a_session_or_another_kind_is_not_recorded(started):
    ship = started
    add_live(ship, "worker", None, "/home/agent/work/w", "w5:p1")
    add_live(ship, "coder", "sess-c", "/home/agent/work/c", "w6:p1", kind="codex")
    ship.ctl("refresh")
    assert set(ship.record()["agents"]) == {"main", "grinder"}


def test_an_adopted_agent_that_exits_leaves_the_record(started):
    ship = started
    add_live(ship, "helper", "sess-helper", "/home/agent/work/side", "w7:p1", mode="plan")
    ship.ctl("refresh")
    ship.exit("helper")
    rc, _, out = ship.ctl("refresh")
    assert "helper" not in ship.record()["agents"]
    assert "agent record: helper stopped (it was exited (prompt_input_exit))" in out


def test_the_record_follows_a_new_session_and_mode(started):
    """/clear (a new session in the same agent), then a mode change in Claude."""
    ship = started
    s = ship.scenario()
    for a in s["herdr"]["agents"]:
        if a["name"] == "main":
            a["agent_session"]["value"] = "sess-main-2"
    ship.set(herdr=s["herdr"])
    doc = ship.json(LEDGER)
    doc["sessions"]["sess-main-2"] = {"mode": "acceptEdits", "updated_at": "2026-01-01T00:00:00Z"}
    ship.p(LEDGER).write_text(json.dumps(doc))
    ship.ctl("refresh")
    main = ship.record()["agents"]["main"]
    assert main["session"] == "sess-main-2" and main["mode"] == "acceptEdits"
    ship.reboot()
    ship.ctl("refresh")
    assert ship.agent("main")["args"] == ["--permission-mode", "acceptEdits",
                                          "--resume", "sess-main-2"]


# --- the lock, and agents leaving -------------------------------------------------------------
def test_a_command_waits_a_bounded_time_for_the_record(started):
    ship = started
    ship.env["SHIPCTL_TEST_WAIT_S"] = "1"
    with open(ship.p("/var/lib/shipshape/agents.lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # a boot's resume, say, starting agents
        for argv in (("agent", "restart", "main"), ("agent", "forget", "main"),
                     ("autostart",)):
            rc, doc, _ = ship.ctl(*argv, "--json")
            assert rc == 1 and doc["error"]["code"] == "agent-busy", argv
            assert "another ship-ctl is starting or stopping agents" in doc["error"]["message"]
    assert ship.agent("main")["args"][:2] == ["--permission-mode", "auto"]  # untouched


def test_an_agent_no_longer_in_autostart_leaves_the_record(started):
    ship = started
    assert ship.ctl("set", "autostart.remove=grinder", "--json")[0] == 0
    ship.exit("grinder")  # removed first, then exited
    ship.ctl("refresh")
    assert "grinder" not in ship.record()["agents"]
    ship.exit("main")  # exited first: stopped, an [[autostart]] agent still...
    ship.ctl("refresh")
    assert ship.record()["agents"]["main"]["want"] == "stopped"
    assert ship.ctl("set", "autostart.remove=main", "--json")[0] == 0  # ...then removed
    assert "main" not in ship.record()["agents"]


def test_restarting_an_agent_that_is_not_running_brings_it_back_in_its_mode(started):
    ship = started
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "plan", "--json")
    assert rc == 0, doc
    ship.exit("main")
    ship.ctl("refresh")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")  # no --mode
    assert rc == 0, doc
    assert ship.agent("main")["args"] == ["--permission-mode", "plan", "--resume", "sess-main"]
    assert doc["data"]["restarted"][0]["mode"] == "plan"


# --- forget ---------------------------------------------------------------------------------
def test_forget_drops_an_agent_that_is_not_running(started):
    ship = started
    rc, doc, _ = ship.ctl("agent", "forget", "main", "--json")
    assert rc == 1 and doc["error"]["code"] == "invalid-input"
    assert "main is running" in doc["error"]["message"]
    ship.exit("main")
    ship.ctl("refresh")
    rc, doc, _ = ship.ctl("agent", "forget", "main", "--json")
    assert rc == 0, doc
    assert_valid("ship-ctl-result", doc)
    assert doc["changed"] == ["main"] and "main" not in ship.record()["agents"]
    rc, doc, _ = ship.ctl("agent", "forget", "nobody", "--json")
    assert rc == 1 and "recorded: grinder" in doc["error"]["message"]
    rc, doc, _ = ship.ctl("agent", "forget", "--json")
    assert rc == 2 and doc["error"]["code"] == "usage"


def test_forget_refuses_while_herdr_does_not_answer(started):
    """Nothing tells whether the agent runs: it stays recorded."""
    ship = started
    ship.set(herdr={**ship.scenario()["herdr"], "server": False})
    rc, doc, _ = ship.ctl("agent", "forget", "main", "--json")
    assert rc == 1 and doc["error"]["code"] == "herdr-unavailable"
    assert "main" in ship.record()["agents"]


# --- moving a Ship onto the record (migration) -------------------------------------------------
def old_ship(ship):
    """A Ship on tooling from before the agent record, as an update finds it: herdr
    resumed its agents (resume_agents_on_restore = true) and autostart kept its own
    record. Running: main (auto) and grinder (bypassPermissions) from autostart, a
    hand-started helper in bypassPermissions, and one in auto whose name herdr lost
    after an earlier resume (autostart's record still knew its pane)."""
    cfg = ship.p("/home/agent/.config/herdr/config.toml")
    cfg.parent.mkdir(parents=True)
    cfg.write_text("onboarding = false\n\n[session]\nresume_agents_on_restore = true\n")
    for i, (name, mode, cwd) in enumerate([
        ("main", "auto", "/home/agent/work/main"),
        ("grinder", "bypassPermissions", "/home/agent/work/grinder"),
        ("helper", "bypassPermissions", "/home/agent/work/side"),
    ], 1):
        ship.p(cwd).mkdir(parents=True, exist_ok=True)
        add_live(ship, None if name == "main" else name, f"sess-{name}", cwd, f"w{i}:p1", mode,
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
    assert args(ship) == {
        "main": ["--permission-mode", "auto", "--resume", "sess-main"],
        "grinder": ["--dangerously-skip-permissions", "--resume", "sess-grinder"],
        "helper": ["--dangerously-skip-permissions", "--resume", "sess-helper"],
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
    assert not starts(ship)
    ship.crash("helper")
    ship.crash("grinder")
    rc, _, out = ship.ctl("refresh")
    assert not starts(ship)
    assert "helper" not in ship.record()["agents"]  # let go, as herdr would have
    assert ship.record()["agents"]["grinder"]["want"] == "stopped"  # autostart: next boot
    assert "it started before the Ship could tell an exit from a crash" in out
    ship.reboot()
    ship.ctl("refresh")
    assert args(ship)["main"] == ["--permission-mode", "auto", "--resume", "sess-main"]
    assert args(ship)["grinder"] == ["--dangerously-skip-permissions", "--name",
                                     "ship-01-grinder"]  # afresh, as autostart did
    ship.crash("main")  # resumed under the new hooks: now a crash is one
    ship.ctl("refresh")
    assert args(ship)["main"] == ["--permission-mode", "auto", "--resume", "sess-main"]
    assert len([c for c in starts(ship) if c[3] == "main"]) == 2


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
