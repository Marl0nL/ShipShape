"""The agent record: ShipShape resumes its agents itself (contracts/ship-ctl.md,
"The agent record"; contracts/agent-record.schema.json).

The sandbox Ship is a herdr-core Ship: its agents are terminals of the fake `hc`
(fakes/fake.py), each started with `hc run --keep` through the agent launcher in a
Claude session ShipShape chose (the record has it). Its ship.toml has two
[[autostart]] agents, main (auto, the default mode, terminal t1) and grinder
(bypassPermissions, t2). `ship.reboot()` is what a reboot looks like to ship-ctl: a
new boot id, and every kept terminal back under its address and name with no
program in it until its new shell starts; the resume types the launch into it.
What only classic herdr does (its panes, workspaces and renames, the move from
pre-record tooling) is in test_ship_herdr_legacy.py.
"""

from __future__ import annotations

import fcntl
import json
import sys
import time

import pytest
from ship_harness import LAUNCHER, LEDGER, SHIP, assert_valid

RECORD = "/var/lib/shipshape/agents.json"


def sid(ship, name: str) -> str:
    """The session the record has for the agent (on core, one ShipShape chose)."""
    return ship.record()["agents"][name]["session"]


def hc(ship) -> dict:
    return ship.scenario()["hc"]


def runs(ship) -> list[list[str]]:
    """Each `hc run` (a new terminal)."""
    return [c for c in ship.calls("hc") if c[1:2] == ["run"]]


def typed(ship, address: str) -> list[str]:
    """The launch lines typed into the terminal at address."""
    return [text for addr, text in hc(ship).get("sent", [])
            if addr == address and f"exec {LAUNCHER}" in text]


def launches(ship, name: str) -> int:
    """Each start of the agent's claude: a new terminal by its name, or the launch
    typed into one of its terminals."""
    new = [c for c in runs(ship) if "--name" in c and c[c.index("--name") + 1] == name]
    places = {t["address"] for t in ship.terminals(name)}
    return len(new) + sum(len(typed(ship, p)) for p in places)


def recorded(ship) -> dict[str, dict]:
    return {a["name"]: a for a in ship.status()["agent_record"]["agents"]}


def now_ts(delta_s: float = 0) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + delta_s))


def ledger_start(ship, session: str, address: str, mode: str | None) -> None:
    """The mode-ledger hook saw session start in the terminal at address (any
    session that started there before is older)."""
    path = ship.p(LEDGER)
    doc = json.loads(path.read_text()) if path.exists() else {
        "schema": "shipshape.modes/1", "sessions": {}}
    for e in doc["sessions"].values():
        if e.get("terminal") == address and not e.get("ended"):
            e["started_at"] = "2026-01-01T00:00:00Z"
    now = now_ts()
    doc["sessions"][session] = {**({"mode": mode} if mode else {}), "started_at": now,
                                "updated_at": now, "terminal": address}
    path.write_text(json.dumps(doc))


def add_live(ship, name, session, cwd, address, mode=None, kind="claude", keep=False):
    """An agent started some other way than ship-ctl (by hand, or by a firstmate):
    `hc run [--name NAME] [--keep] -- claude` in a new terminal at address. session:
    the one the mode-ledger hook saw start there (None: it never reported one)."""
    s = ship.scenario()
    claude = kind == "claude"
    s["hc"]["terminals"].append({
        "address": address, "name": name, "argv": [kind], "labels": {}, "cwd": cwd,
        "state": "live", "keep": keep, "created_ms": int(time.time() - 5) * 1000,
        "screen": "exact", "cols": 80, "rows": 24,
        "status": {"app": "claude-code", "state": "idle"} if claude else None, "turn": 0,
        "exit_code": None, "reason": None,
        "foreground": {"pid": 9, "name": kind, "argv": [kind], "cwd": cwd},
        **({"claude_args": [], "session": session} if claude else {})})
    ship.set(hc=s["hc"])
    if session:
        ledger_start(ship, session, address, mode)


def claude_by_hand_in(ship, name: str, session: str, cwd: str, mode: str,
                      reported: bool = True) -> None:
    """In the kept terminal by that name, whose claude crashed, someone `cd`s to cwd
    and runs claude by hand: a new session there. reported=False: its hook has not
    reported the session yet (the terminal still shows the one it was started with)."""
    s = ship.scenario()
    t = next(t for t in s["hc"]["terminals"] if t.get("name") == name and t["state"] == "live")
    t.pop("recover_in", None)
    t.update(cwd=cwd, status={"app": "claude-code", "state": "idle"}, claude_args=[],
             session=session,
             foreground={"pid": 10, "name": "claude", "argv": ["claude"], "cwd": cwd})
    ship.set(hc=s["hc"])
    if reported:
        ledger_start(ship, session, t["address"], mode)


def lose_sessions(ship, *sessions: str) -> None:
    """claude --resume of these finds no conversation: claude exits at once."""
    s = ship.scenario()
    s["hc"]["lost_sessions"] = list(sessions)
    ship.set(hc=s["hc"])


def fail_run(ship, *names: str) -> None:
    """hc run of a new terminal by these names fails."""
    s = ship.scenario()
    s["hc"]["fail_run"] = list(names)
    ship.set(hc=s["hc"])


@pytest.fixture
def started(ship):
    """main and grinder started, as at a Ship's first boot: by the status refresh
    that sees Claude signed in (so later refreshes see no new sign-in)."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.ctl("apply", "--json")
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert set(ship.args()) == {"main", "grinder"}
    return ship


# --- the record ------------------------------------------------------------------------
def test_autostart_writes_the_record(started):
    ship = started
    doc = ship.record()
    assert_valid("agent-record", doc)
    main = doc["agents"]["main"]
    assert main["cwd"] == "/home/agent/work/main" and main["session"] == ship.session("main")
    assert len(main["session"]) == 36  # a uuid ShipShape chose
    assert main["mode"] == "auto" and main["prompt"] == "Get oriented."
    assert main["session_name"] == "ship-01-main" and main["origin"] == "autostart"
    assert main["place"]["id"] == "t1" and main["want"] == "running"
    assert doc["agents"]["grinder"]["mode"] == "bypassPermissions"
    assert oct(ship.p(RECORD).stat().st_mode & 0o777) == "0o644"
    assert recorded(ship)["main"] == {
        "name": "main", "cwd": "/home/agent/work/main", "mode": "auto", "autostart": True,
        "state": "running", "relaunch_failures": 0, "created_at": main["created_at"],
        "updated_at": main["updated_at"]}


def test_status_never_shows_session_ids(started):
    _, _, text = started.ctl("status", "--json")
    assert sid(started, "main") not in text and sid(started, "grinder") not in text


# --- resume after a boot ----------------------------------------------------------------
def test_after_a_reboot_the_refresh_resumes_every_agent_in_its_folder_and_mode(started):
    ship = started
    places = {n: ship.place(n) for n in ("main", "grinder")}
    ship.reboot()
    assert recorded(ship)["main"]["state"] == "starting"
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert ship.args() == {
        "main": ["--permission-mode", "auto", "--resume", sid(ship, "main")],
        "grinder": ["--dangerously-skip-permissions", "--resume", sid(ship, "grinder")],
    }
    # In the kept terminals the core restored, under their names: no new terminal, no
    # first prompt.
    assert {n: ship.place(n) for n in ("main", "grinder")} == places
    assert len(runs(ship)) == 2
    assert ship.prompts() == [
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


def test_a_kept_terminal_that_is_gone_gets_a_new_one(started):
    ship = started
    ship.reboot()
    s = ship.scenario()
    s["hc"]["terminals"] = [t for t in s["hc"]["terminals"] if t["address"] != "t1"]
    ship.set(hc=s["hc"])  # the core lost main's terminal (its state was not saved)
    ship.ctl("refresh")
    main = ship.agent("main")
    assert main["claude_args"] == ["--permission-mode", "auto", "--resume", sid(ship, "main")]
    assert main["address"] == "t3" and main["cwd"] == "/home/agent/work/main"
    assert ship.place("grinder") == "t2"


def test_another_agent_in_its_old_terminal_never_becomes_it(started):
    """After a boot main's kept terminal is gone, and another, named agent sits at
    its old address (a core whose state was wiped gives addresses out again): main
    must not take that agent's session (or its mode)."""
    ship = started
    main_sid = sid(ship, "main")
    ship.reboot()
    s = ship.scenario()
    s["hc"]["terminals"] = [t for t in s["hc"]["terminals"] if t["address"] != "t1"]
    ship.set(hc=s["hc"])
    add_live(ship, "other", "sess-other", "/home/agent/work/main", "t1", mode="plan")
    ship.ctl("refresh")
    record = ship.record()["agents"]
    assert record["main"]["session"] == main_sid and record["main"]["mode"] == "auto"
    assert record["other"]["session"] == "sess-other"
    main = ship.agent("main")
    assert main["claude_args"] == ["--permission-mode", "auto", "--resume", main_sid]
    assert main["address"] == "t3"  # a new terminal, not the busy one
    assert not typed(ship, "t1")
    assert not [c for c in runs(ship) if "sess-other" in c]
    ship.ctl("refresh")
    assert ship.record()["agents"]["main"]["session"] == main_sid


def test_an_unnamed_agent_in_its_terminal_but_another_folder_never_becomes_it(started):
    """main crashed; someone uses its kept terminal, `cd`s elsewhere and runs claude
    there by hand. That is another agent: main keeps its own session, mode and folder,
    and the other is recorded under its own name."""
    ship = started
    main_sid = sid(ship, "main")
    ship.crash("main")
    claude_by_hand_in(ship, "main", "sess-x", "/home/agent/work/elsewhere", "plan")
    ship.ctl("refresh")
    record = ship.record()["agents"]
    assert (record["main"]["session"], record["main"]["mode"], record["main"]["cwd"]) == (
        main_sid, "auto", "/home/agent/work/main")
    assert record["elsewhere"]["session"] == "sess-x"  # adopted, named after its folder
    assert record["main"]["want"] == "stopped"  # its terminal holds another agent now
    # An [[autostart]] agent, so the next boot starts it afresh, and the other resumes.
    ship.p("/home/agent/work/elsewhere").mkdir(parents=True)
    ship.reboot()
    ship.ctl("refresh")
    assert ship.args()["elsewhere"] == ["--permission-mode", "plan", "--resume", "sess-x"]
    assert ship.args()["main"] == ["--permission-mode", "auto", "--name", "ship-01-main",
                                   "--session-id", sid(ship, "main")]
    assert sid(ship, "main") not in (main_sid, "sess-x")


def test_the_crashed_agent_resumes_its_own_session_once_its_terminal_is_free(started):
    """The same, and nothing is lost while both exist: a restart of main never takes
    the other's Claude for it (hc gives no second terminal main's name), and once
    that one is gone main resumes its own session, in its own folder and mode, and
    the other its own, in a terminal of its own."""
    ship = started
    main_sid = sid(ship, "main")
    ship.crash("main")
    claude_by_hand_in(ship, "main", "sess-x", "/home/agent/work/elsewhere", "plan")
    ship.ctl("refresh")
    rc, _, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc != 0  # its terminal runs another Claude
    record = ship.record()["agents"]
    assert set(record) == {"main", "grinder", "elsewhere"}
    assert (record["main"]["session"], record["main"]["mode"], record["main"]["cwd"]) == (
        main_sid, "auto", "/home/agent/work/main")
    assert (record["elsewhere"]["session"], record["elsewhere"]["cwd"]) == (
        "sess-x", "/home/agent/work/elsewhere")
    assert not typed(ship, "t1") and len(runs(ship)) == 2
    # The other one crashes too: it is started again in a new terminal, by its name.
    ship.p("/home/agent/work/elsewhere").mkdir(parents=True)
    ship.core_crash("main")  # (the terminal is still called main)
    ship.ctl("refresh")
    assert ship.args()["elsewhere"] == ["--permission-mode", "plan", "--resume", "sess-x"]
    assert ship.agent("elsewhere")["cwd"] == "/home/agent/work/elsewhere"
    assert not typed(ship, "t1")
    # main, back by hand, resumes its own conversation in its kept terminal.
    rc, _, out = ship.ctl("agent", "restart", "main", "--json")
    assert rc == 0, out
    assert ship.args()["main"] == ["--permission-mode", "auto", "--resume", main_sid]
    assert ship.place("main") == "t1" and ship.agent("main")["cwd"] == "/home/agent/work/main"
    ship.ctl("refresh")
    record = ship.record()["agents"]
    assert set(record) == {"main", "grinder", "elsewhere"}
    assert (record["main"]["session"], record["main"]["mode"], record["main"]["cwd"],
            record["main"]["want"]) == (main_sid, "auto", "/home/agent/work/main", "running")
    assert (record["elsewhere"]["session"], record["elsewhere"]["mode"],
            record["elsewhere"]["want"]) == ("sess-x", "plan", "running")
    assert record["elsewhere"]["place"]["id"] != "t1"


def test_before_its_hook_reports_the_other_claude_still_never_becomes_it(started):
    """The same, before the other Claude's hook has reported its session: its
    terminal still shows main's, which is no proof it is main."""
    ship = started
    main_sid = sid(ship, "main")
    ship.crash("main")
    claude_by_hand_in(ship, "main", "sess-x", "/home/agent/work/elsewhere", "plan",
                      reported=False)
    for reported in (False, True):
        if reported:
            ledger_start(ship, "sess-x", "t1", "plan")
        ship.ctl("refresh")
        record = ship.record()["agents"]
        assert (record["main"]["session"], record["main"]["mode"], record["main"]["cwd"]) == (
            main_sid, "auto", "/home/agent/work/main")
        assert [n for n, e in record.items() if e["session"] == main_sid] == ["main"]
    assert (record["elsewhere"]["session"], record["elsewhere"]["mode"]) == ("sess-x", "plan")
    assert not typed(ship, "t1")


def test_the_other_claude_keeps_its_own_record_in_a_let_go_agents_terminal(started):
    """An agent not in [[autostart]] whose kept terminal someone used is let go;
    the Claude started there by hand, in another folder, stays recorded as itself
    (not, once that name is free, under the name of the terminal it runs in)."""
    ship = started
    ship.p("/home/agent/work/foo").mkdir(parents=True)
    add_live(ship, "foo", "sess-foo", "/home/agent/work/foo", "t5", mode="plan", keep=True)
    ship.ctl("refresh")
    assert sid(ship, "foo") == "sess-foo"
    ship.core_crash("foo")
    claude_by_hand_in(ship, "foo", "sess-x", "/home/agent/work/elsewhere", "auto")
    for _ in range(3):
        ship.ctl("refresh")
        record = ship.record()["agents"]
        assert (record["elsewhere"]["session"], record["elsewhere"]["want"]) == ("sess-x",
                                                                                  "running")
        assert [n for n, e in record.items() if e["session"] == "sess-x"] == ["elsewhere"]


def test_a_folder_spelt_another_way_is_still_its_folder(tmp_path):
    """A runtime reports the folder a process runs in with its links resolved: a
    recorded folder reached through a link is the same folder, so the agent by its
    name is still itself (here with no session known yet, as on classic herdr
    until it reports one), and a folder that is really another is not."""
    sys.path.insert(0, str(SHIP / "lib"))
    from shipctl import record

    (tmp_path / "real" / "main").mkdir(parents=True)
    (tmp_path / "real" / "elsewhere").mkdir()
    (tmp_path / "work").symlink_to(tmp_path / "real")
    doc = {"agents": {"main": {"cwd": str(tmp_path / "work" / "main"), "session": None,
                               "place": None}}}

    def live(cwd):
        return [{"name": "main", "kind": "claude", "session": "sess-1", "place": "t1",
                 "cwd": str(cwd)}]

    assert record.match(doc, live(tmp_path / "real" / "main"))["main"]["place"] == "t1"
    assert record.match(doc, live(tmp_path / "real" / "elsewhere")) == {}


def test_a_relaunch_in_its_own_terminal_and_folder_is_still_it(started):
    """A normal relaunch after a crash: in its kept terminal, in its folder, by its
    name; the record keeps it as itself, and nothing is picked up beside it."""
    ship = started
    main_sid = sid(ship, "main")
    ship.crash("main")
    ship.ctl("refresh")
    assert ship.args()["main"] == ["--permission-mode", "auto", "--resume", main_sid]
    assert ship.place("main") == "t1"
    ship.ctl("refresh")
    record = ship.record()["agents"]
    assert set(record) == {"main", "grinder"}
    assert (record["main"]["session"], record["main"]["cwd"], record["main"]["want"],
            record["main"]["place"]["id"]) == (main_sid, "/home/agent/work/main", "running", "t1")
    assert launches(ship, "main") == 2  # its first start and this one, never a third


def test_claude_started_again_by_hand_in_its_terminal_and_folder_is_still_it(started):
    """claude started again by hand, in a new session, in its own kept terminal and
    folder: that is the recorded agent, under its name, its session followed."""
    ship = started
    ship.crash("main")
    claude_by_hand_in(ship, "main", "sess-main-2", "/home/agent/work/main", "auto")
    ship.ctl("refresh")
    assert set(ship.record()["agents"]) == {"main", "grinder"}
    assert ship.record()["agents"]["main"]["session"] == "sess-main-2"
    assert ship.record()["agents"]["main"]["place"]["id"] == "t1"
    assert {a["name"] for a in ship.status()["herdr"]["agents"]} == {"main", "grinder"}
    assert launches(ship, "main") == 1  # not started again


def test_a_kept_terminal_in_another_folder_resumes_in_the_agents_folder(started):
    """Its shell came back in another folder: claude --resume needs its own, so the
    launch typed into the kept terminal goes there first."""
    ship = started
    ship.reboot()
    s = ship.scenario()
    for t in s["hc"]["terminals"]:
        if t["address"] == "t1":
            t["cwd"] = "/home/agent/elsewhere"
    ship.set(hc=s["hc"])
    ship.ctl("refresh")
    main = ship.agent("main")
    assert main["address"] == "t1" and main["cwd"] == "/home/agent/work/main"
    assert typed(ship, "t1") == [f"cd -- /home/agent/work/main && exec {LAUNCHER} "
                                 f"--permission-mode auto --resume {sid(ship, 'main')}"]
    assert len(runs(ship)) == 2


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
    assert ship.args()["grinder"] == ["--permission-mode", "auto",
                                      "--resume", sid(ship, "grinder")]
    assert ship.record()["agents"]["grinder"]["mode"] == "auto"


def test_nothing_resumes_while_provisioning(started):
    ship = started
    ship.reboot()
    ship.set(cloud_init="running")
    ship.ctl("refresh")
    assert ship.args() == {}
    assert len(runs(ship)) == 2 and not [x for x in hc(ship)["sent"] if "exec " in x[1]]


# --- crashes, and agents stopped on purpose ---------------------------------------------------
def test_an_agent_that_crashed_is_started_again_in_its_terminal(started):
    ship = started
    ship.crash("grinder")
    assert recorded(ship)["grinder"]["state"] == "starting"
    ship.ctl("refresh")
    assert ship.args()["grinder"] == ["--dangerously-skip-permissions",
                                      "--resume", sid(ship, "grinder")]
    assert ship.place("grinder") == "t2"
    assert recorded(ship)["grinder"]["state"] == "running"


def test_after_three_failed_tries_it_is_left_alone_with_attention(started):
    ship = started
    grinder_sid = sid(ship, "grinder")
    ship.crash("grinder")  # within 10 minutes of its start: the first failed try
    lose_sessions(ship, grinder_sid)  # each start of it exits at once
    for _ in range(4):
        ship.ctl("refresh")
    # Its first start, then two failed starts (the launch typed into its kept terminal,
    # then a new terminal once that one finished; core does not retry a start), then
    # it is let be.
    assert launches(ship, "grinder") == 1 + 2
    doc = ship.status()
    grinder = next(a for a in doc["agent_record"]["agents"] if a["name"] == "grinder")
    assert grinder["state"] == "failed" and grinder["relaunch_failures"] == 3
    assert grinder["last_error"] == "claude exited at once (exit 1)"
    item = next(a for a in doc["attention"] if a["code"] == "agent-relaunch-failed")
    assert item["subject"] == "grinder" and "ship-ctl agent forget grinder" in item["message"]
    # Restarting it by hand starts it again, and the attention goes.
    lose_sessions(ship)
    rc, doc, _ = ship.ctl("agent", "restart", "grinder", "--mode", "dontAsk", "--json")
    assert rc == 0, doc
    assert doc["data"]["restarted"][0]["was"] == "failed"
    assert doc["data"]["restarted"][0]["previous_mode"] == "bypassPermissions"
    assert ship.args()["grinder"] == ["--permission-mode", "dontAsk", "--resume", grinder_sid]
    assert ship.record()["agents"]["grinder"]["mode"] == "dontAsk"
    assert "agent-relaunch-failed" not in {a["code"] for a in ship.status()["attention"]}


def test_an_agent_that_keeps_dying_soon_after_it_starts_is_not_started_forever(started):
    """Its starts succeed, but it stops each time before the next refresh (a transcript
    too big to load, say: claude exits with an error, which finishes its terminal):
    every early stop is a failed try."""
    ship = started
    for _ in range(5):
        ship.core_crash("grinder", how="exit")  # nothing, once none runs
        ship.ctl("refresh")
    assert launches(ship, "grinder") == 1 + 2  # then let be
    grinder = recorded(ship)["grinder"]
    assert grinder["state"] == "failed" and grinder["relaunch_failures"] == 3
    assert grinder["last_error"].startswith("it stopped ") and "after it started" in (
        grinder["last_error"])
    assert "agent-relaunch-failed" in {a["code"] for a in ship.status()["attention"]}


@pytest.mark.xfail(strict=True, reason=(
    "core gap: the shipshape.launching label stays on a kept terminal once claude is up "
    "(runtime.Core._exec_in), so within LAUNCH_GRACE_S (120 s) of a launch typed into it "
    "a new crash is reported as started again but nothing is typed"))
def test_an_agent_killed_again_soon_after_it_came_back_is_started_again(started):
    """claude is killed in its kept terminal, comes back there, and is killed again
    a minute later: it comes back again (an early stop, a failed try)."""
    ship = started
    grinder_sid = sid(ship, "grinder")
    want = ["--dangerously-skip-permissions", "--resume", grinder_sid]
    for n in (1, 2):
        ship.crash("grinder")
        ship.ctl("refresh")
        assert recorded(ship)["grinder"]["relaunch_failures"] == n
        assert ship.args().get("grinder") == want
    assert launches(ship, "grinder") == 1 + 2 and ship.place("grinder") == "t2"


def age_launch(ship, name: str, minutes: int) -> None:
    """As if `name` was started that long ago."""
    from datetime import UTC, datetime, timedelta

    doc = ship.record()
    when = datetime.now(UTC) - timedelta(minutes=minutes)
    doc["agents"][name]["launched_at"] = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    ship.p(RECORD).write_text(json.dumps(doc))


def test_tries_reset_once_an_agent_has_run_ten_minutes(started):
    ship = started
    for _ in range(2):  # two early stops (claude exits with an error): two tries spent
        ship.core_crash("grinder", how="exit")
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
    assert "grinder" in ship.args() and recorded(ship)["grinder"]["relaunch_failures"] == 0


def test_a_crashed_agent_comes_back_in_the_mode_it_was_switched_to_in_claude(started):
    """Its mode changed in Claude (the ledger has it) just before it crashed, before
    a refresh brought the record up to date: the ledger's mode wins."""
    ship = started
    main_sid = sid(ship, "main")
    doc = ship.json(LEDGER)
    doc["sessions"][main_sid]["mode"] = "plan"
    ship.p(LEDGER).write_text(json.dumps(doc))
    ship.crash("main")
    ship.ctl("refresh")
    assert ship.args()["main"] == ["--permission-mode", "plan", "--resume", main_sid]


def age_typed_launches(ship, seconds: int) -> None:
    """As if the launches typed into kept terminals (the shipshape.launching label)
    were typed that long ago."""
    s = ship.scenario()
    for t in s["hc"]["terminals"]:
        typed_at = t.get("labels", {}).get("shipshape.launching")
        if typed_at:
            t["labels"]["shipshape.launching"] = str(int(typed_at) - seconds)
    ship.set(hc=s["hc"])


def test_resume_waits_for_dns_after_a_boot(started):
    ship = started
    ship.reboot()
    ship.set(dns=False)
    rc, _, out = ship.ctl("refresh")
    assert ship.args() == {}
    assert "agent record: resume waits for DNS" in out
    rc, doc, _ = ship.ctl("notify-signin", "github", "--json")
    assert {s["reason"] for s in doc["data"]["skipped"]} == {"resume-pending"}
    ship.set(dns=True)
    ship.ctl("refresh")
    assert set(ship.args()) == {"main", "grinder"}
    # Ten minutes into a boot nothing waits any more, DNS or not.
    age_typed_launches(ship, 700)  # the launches typed in the last boot are older still
    ship.reboot(uptime=700.0)
    ship.set(dns=False)
    ship.ctl("refresh")
    assert set(ship.args()) == {"main", "grinder"}


def test_a_reboot_gives_a_failed_agent_new_tries(started):
    ship = started
    grinder_sid = sid(ship, "grinder")
    ship.crash("grinder")
    lose_sessions(ship, grinder_sid)
    for _ in range(3):
        ship.ctl("refresh")
    assert recorded(ship)["grinder"]["state"] == "failed"
    lose_sessions(ship)
    ship.reboot()
    ship.ctl("refresh")
    assert ship.args()["grinder"][-2:] == ["--resume", grinder_sid]


def test_a_lost_conversation_counts_as_a_failed_start(started):
    ship = started
    lose_sessions(ship, sid(ship, "main"))  # claude --resume: No conversation found
    ship.reboot()
    ship.ctl("refresh")
    assert "main" not in ship.args() and "grinder" in ship.args()
    assert ship.status()["herdr"]["last_resume"] == {
        "at": ship.record()["resume"]["at"], "method": "api-fallback", "resumed": 1,
        "failed": 1}
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--fresh", "--json")
    assert rc == 0, doc
    assert ship.args()["main"] == ["--permission-mode", "auto", "--name", "ship-01-main",
                                   "--session-id", sid(ship, "main")]
    assert [ship.place("main"), "Get oriented."] in hc(ship)["sent"]


def test_a_restart_that_did_not_start_again_is_started_by_the_next_refresh(started):
    ship = started
    main_sid = sid(ship, "main")
    fail_run(ship, "main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "plan", "--json")
    assert rc == 1 and doc["error"]["code"] == "agent-failed"
    assert recorded(ship)["main"]["state"] == "starting"
    fail_run(ship)
    ship.ctl("refresh")  # its /exit was the restart's own, not someone's: a crash
    assert recorded(ship)["main"]["relaunch_failures"] == 1  # it stopped at once
    assert ship.args()["main"] == ["--permission-mode", "plan", "--resume", main_sid]
    # /exit finished its terminal: it runs in a new one, and the finished one goes.
    assert ship.place("main") == "t3" and hc(ship)["removed"] == ["t1"]


def test_an_agent_someone_exited_is_not_started_again(started):
    ship = started
    main_sid, grinder_sid = sid(ship, "main"), sid(ship, "grinder")
    ship.exit("grinder")
    ship.ctl("refresh")
    assert "grinder" not in ship.args()
    assert recorded(ship)["grinder"]["state"] == "stopped"
    # An [[autostart]] agent starts afresh at the next boot, as it always did.
    ship.reboot()
    ship.ctl("refresh")
    assert sid(ship, "grinder") != grinder_sid
    assert ship.args() == {
        "main": ["--permission-mode", "auto", "--resume", main_sid],
        "grinder": ["--dangerously-skip-permissions", "--name", "ship-01-grinder",
                    "--session-id", sid(ship, "grinder")],
    }


def test_an_exit_in_an_earlier_boot_does_not_stop_the_resume(started):
    """Claude may report a shutdown's SIGTERM however it likes: only an exit seen in
    this boot counts as on purpose."""
    ship = started
    main_sid = sid(ship, "main")
    ship.reboot()
    ship.end_session(main_sid, "prompt_input_exit")
    s = ship.json(LEDGER)
    s["sessions"][main_sid]["ended"]["at"] = "2026-01-01T00:00:00Z"  # before this boot
    ship.p(LEDGER).write_text(json.dumps(s))
    ship.ctl("refresh")
    assert ship.args()["main"] == ["--permission-mode", "auto", "--resume", main_sid]


def test_a_closed_terminal_is_a_stopped_agent(started):
    ship = started
    s = ship.scenario()
    s["hc"]["terminals"] = [t for t in s["hc"]["terminals"] if t["address"] != "t1"]
    ship.set(hc=s["hc"])  # someone ended main's terminal and removed it: no SessionEnd
    ship.ctl("refresh")
    assert "main" not in ship.args()
    assert ship.record()["agents"]["main"]["want"] == "stopped"
    assert len(runs(ship)) == 2


def test_a_refresh_leaves_the_record_alone_while_another_ship_ctl_holds_it(started):
    ship = started
    ship.crash("grinder")
    with open(ship.p("/var/lib/shipshape/agents.lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        ship.ctl("refresh")  # say an agent restart is between /exit and its new start
        assert "grinder" not in ship.args()
    ship.ctl("refresh")
    assert "grinder" in ship.args()


# --- agents started by hand ---------------------------------------------------------------------
def test_an_agent_started_by_hand_is_recorded_and_resumed(started):
    ship = started
    add_live(ship, "helper", "sess-helper", "/home/agent/work/side", "t7", mode="plan",
             keep=True)
    ship.p("/home/agent/work/side").mkdir(parents=True)
    ship.ctl("refresh")
    helper = ship.record()["agents"]["helper"]
    assert helper["origin"] == "adopted" and helper["mode"] == "plan"
    assert helper["cwd"] == "/home/agent/work/side" and helper["prompt"] is None
    assert helper["session"] == "sess-helper" and helper["place"]["id"] == "t7"
    assert recorded(ship)["helper"]["autostart"] is False
    ship.reboot()
    ship.ctl("refresh")
    assert ship.args()["helper"] == ["--permission-mode", "plan", "--resume", "sess-helper"]
    assert ship.place("helper") == "t7"


def test_an_unnamed_agent_is_named_after_its_folder(started):
    """The record names it; herdr-core names a terminal only when it starts, so the
    record knows it by its session from then on."""
    ship = started
    add_live(ship, None, "sess-x", "/home/agent/work/My Project", "t8")
    add_live(ship, None, "sess-y", "/home/agent/work/main", "t9")  # main is taken
    ship.ctl("refresh")
    record = ship.record()["agents"]
    assert (record["my-project"]["session"], record["my-project"]["place"]["id"]) == (
        "sess-x", "t8")
    assert (record["main-2"]["session"], record["main-2"]["place"]["id"]) == ("sess-y", "t9")
    assert record["main"]["session"] == ship.session("main")
    ship.ctl("refresh")
    assert set(ship.record()["agents"]) == {"main", "grinder", "my-project", "main-2"}
    assert {a["state"] for a in recorded(ship).values()} == {"running"}
    assert len(runs(ship)) == 2


def test_an_agent_without_a_session_or_another_kind_is_not_recorded(started):
    ship = started
    add_live(ship, "worker", None, "/home/agent/work/w", "t5")
    add_live(ship, "coder", "sess-c", "/home/agent/work/c", "t6", kind="codex")
    ship.ctl("refresh")
    assert set(ship.record()["agents"]) == {"main", "grinder"}


def test_an_adopted_agent_that_exits_leaves_the_record(started):
    ship = started
    add_live(ship, "helper", "sess-helper", "/home/agent/work/side", "t7", mode="plan")
    ship.ctl("refresh")
    assert "helper" in ship.record()["agents"]
    ship.exit("helper")
    rc, _, out = ship.ctl("refresh")
    assert "helper" not in ship.record()["agents"]
    assert "agent record: helper stopped (it was exited (prompt_input_exit))" in out


def test_the_record_follows_a_new_session_and_mode(started):
    """/clear (a new session in the same agent), then a mode change in Claude."""
    ship = started
    ship.core_clear("main", "sess-main-2", "acceptEdits")
    ship.ctl("refresh")
    main = ship.record()["agents"]["main"]
    assert main["session"] == "sess-main-2" and main["mode"] == "acceptEdits"
    ship.reboot()
    ship.ctl("refresh")
    assert ship.args()["main"] == ["--permission-mode", "acceptEdits",
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
    assert ship.args()["main"][:2] == ["--permission-mode", "auto"]  # untouched
    assert ship.place("main") == "t1" and len(runs(ship)) == 2


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
    main_sid = sid(ship, "main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "plan", "--json")
    assert rc == 0, doc
    ship.exit("main")
    ship.ctl("refresh")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")  # no --mode
    assert rc == 0, doc
    assert ship.args()["main"] == ["--permission-mode", "plan", "--resume", main_sid]
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


def test_forget_refuses_while_the_core_does_not_answer(started):
    """Nothing tells whether the agent runs: it stays recorded."""
    ship = started
    ship.set(hc={**ship.scenario()["hc"], "server": False})
    rc, doc, _ = ship.ctl("agent", "forget", "main", "--json")
    assert rc == 1 and doc["error"]["code"] == "herdr-unavailable"
    assert "main" in ship.record()["agents"]
