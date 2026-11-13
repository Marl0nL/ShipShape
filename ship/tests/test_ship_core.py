"""Ships whose agents run in herdr-core (ship.toml runtime = "core"; runtime.Core).

Every sandbox Ship is a core Ship (the harness's default; `ship.use_core()` says so
again); its `hc` is the fake in fakes/fake.py. `ship.core_reboot()` is a reboot as
ship-ctl sees it: a new boot id, and every kept terminal back under its address and
name with no program in it until its new shell starts. The tests here that start on
herdr (moving a Ship between runtimes) say so with `ship.use_herdr()`; what only
classic herdr does is test_ship_herdr_legacy.py.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

import pytest
from ship_harness import assert_valid

LAUNCHER = "/opt/shipshape/bin/agent-launch"
SHIP_FILES = Path(__file__).resolve().parents[1] / "files"


def hc(ship) -> dict:
    return ship.scenario()["hc"]


def runs(ship) -> list[list[str]]:
    return [c for c in ship.calls("hc") if c[1:2] == ["run"]]


def launched(ship, name: str) -> list[str]:
    """claude's arguments in the live terminal by that name."""
    return ship.terminal(name)["claude_args"]


def never_killed(ship) -> None:
    """Nothing ended an agent the hard way, and nothing typed into a blocked one."""
    assert not hc(ship).get("forbidden"), hc(ship).get("forbidden")
    assert not [c for c in ship.calls("hc") if c[1:2] == ["kill"] or c[1:3] == ["server", "stop"]
                or "--force" in c]


@pytest.fixture
def core(ship):
    """main and grinder started on a core Ship, as at its first boot."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.use_core()
    ship.ctl("apply", "--json")
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert {t["name"] for t in ship.terminals() if t["state"] == "live"} == {"main", "grinder"}
    return ship


# --- choosing the runtime ---------------------------------------------------------------
def test_a_core_ship_never_meets_herdr(core):
    ship = core
    ship.status()
    assert ship.calls("herdr") == []
    assert ship.record()["runtime"] == "core"


def test_ship_toml_refuses_an_unknown_runtime(ship):
    text = ship.p("/etc/shipshape/ship.toml").read_text()
    ship.write_toml(text.replace('runtime = "core"\n', 'runtime = "tmux"\n', 1))
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc != 0 and doc["error"]["code"] == "invalid-ship-toml"


# --- starting agents ----------------------------------------------------------------------
def test_autostart_runs_each_agent_through_the_launcher_with_a_chosen_session(core):
    ship = core
    main = ship.terminal("main")
    sid = ship.record()["agents"]["main"]["session"]
    assert len(sid) == 36 and sid.count("-") == 4  # a uuid ShipShape chose
    [run] = [c for c in runs(ship) if "main" in c]
    assert run == ["hc", "run", "--json", "--bg", "--keep", "--name", "main",
                   "--cwd", "/home/agent/work/main",
                   "--label", f"shipshape.session={sid}", "--label", "herdr.space=shipshape",
                   "--label", "herdr.tab=main",
                   "--", LAUNCHER, "--permission-mode", "auto", "--name", "ship-01-main",
                   "--session-id", sid]
    assert launched(ship, "grinder")[:1] == ["--dangerously-skip-permissions"]
    # The record had the session before the agent started.
    assert hc(ship)["recorded_at_run"] == {"main": sid,
                                           "grinder": ship.record()["agents"]["grinder"]["session"]}
    assert main["labels"]["shipshape.session"] == sid and main["session"] == sid
    place = ship.record()["agents"]["main"]["place"]
    assert place["runtime"] == "core" and place["id"] == main["address"]
    assert place["workspace"] == "shipshape"
    # The first prompt is pasted with hc send, never forced.
    assert [main["address"], "Get oriented."] in hc(ship)["sent"]
    assert ["hc", "send", "--json", "main", "--", "Get oriented."] in ship.calls("hc")
    assert_valid("agent-record", ship.record())
    never_killed(ship)


def test_claude_too_old_for_core_starts_nothing(ship):
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.set(claude={"logged_in": True, "email": "work@example.com", "version": "2.1.290"})
    ship.use_core()
    ship.ctl("apply", "--json")
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc != 0 and runs(ship) == []
    reasons = {s["name"]: s["reason"] for s in doc["data"]["skipped"]}
    assert "older than 2.1.295" in reasons["main"]
    assert "agents" not in ship.record() or not ship.record()["agents"]


# --- the status document ------------------------------------------------------------------
def test_status_maps_claudes_own_state(core):
    ship = core
    ship.core_status("main", "blocked", "permission", "approve Bash: curl -sI https://example.com")
    ship.core_status("grinder", None)  # not reporting yet
    doc = ship.status()
    herdr = doc["herdr"]
    assert herdr["runtime"] == "core" and herdr["server"] == "running"
    assert herdr["version"] == "0.1.19"
    agents = {a["name"]: a for a in herdr["agents"]}
    main = ship.terminal("main")
    assert agents["main"] == {
        "name": "main", "kind": "claude", "state": "blocked", "mode": "auto",
        "pane": main["address"], "workspace": "shipshape", "cwd": "/home/agent/work/main",
        "blocked_kind": "permission", "message": "approve Bash: curl -sI https://example.com"}
    assert agents["grinder"]["state"] == "unknown"
    assert agents["grinder"]["mode"] == "bypassPermissions"
    [waiting] = [a for a in doc["attention"] if a["code"] == "agent-waiting"]
    assert waiting == {"code": "agent-waiting", "subject": "main", "message":
                       "Agent 'main' is waiting for you to approve something: "
                       "approve Bash: curl -sI https://example.com"}
    assert doc["state"] == "running"
    assert {a["state"] for a in doc["agent_record"]["agents"]} == {"running"}
    assert "runtime-deprecated" not in {a["code"] for a in doc["attention"]}


def test_a_core_ship_is_never_told_herdr_is_deprecated(core):
    """runtime-deprecated is for classic herdr only, even with herdr installed and a
    herdr server (another tool's, say) running beside the core."""
    ship = core
    s = ship.scenario()
    s["herdr"]["server"] = True
    ship.set(herdr=s["herdr"])
    doc = ship.status()
    assert doc["herdr"]["runtime"] == "core"
    assert "runtime-deprecated" not in {a["code"] for a in doc["attention"]}


def test_terminals_that_are_not_agents_are_not_listed(core):
    ship = core
    s = ship.scenario()
    s["hc"]["terminals"].append({
        "address": "t9", "name": "scratch", "argv": ["bash"], "labels": {}, "cwd": "/tmp",
        "state": "live", "keep": False, "status": None,
        "foreground": {"pid": 9, "name": "vim", "argv": ["vim"], "cwd": "/tmp"}})
    ship.set(hc=s["hc"])
    assert {a["name"] for a in ship.status()["herdr"]["agents"]} == {"main", "grinder"}
    assert "scratch" not in ship.record()["agents"]


def test_status_says_when_hc_is_missing(core):
    ship = core
    os.unlink(Path(ship.env["PATH"].split(":")[0]) / "hc")
    doc = ship.status()
    assert doc["herdr"]["server"] == "unknown" and doc["herdr"]["agents"] == []
    assert "herdr-core: not installed" in doc["errors"]
    assert {a["state"] for a in doc["agent_record"]["agents"]} == {"unknown"}


# --- after a boot -------------------------------------------------------------------------
def test_after_a_reboot_each_kept_terminal_gets_its_agent_back(core):
    """A kept terminal comes back with no program for a moment: the relaunch waits
    for its shell (typing earlier fails), then types the launch into it."""
    ship = core
    ship.env["SHIPCTL_TEST_WAIT_S"] = "4"
    before = {t["name"]: t["address"] for t in ship.terminals()}
    sids = {n: e["session"] for n, e in ship.record()["agents"].items()}
    ship.core_reboot(recover_in=10)
    assert {a["state"] for a in ship.status()["agent_record"]["agents"]} == {"starting"}
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert "agent record: main started again, continuing its session." in out
    line = (f"cd -- /home/agent/work/main && exec {LAUNCHER} --permission-mode auto "
            f"--resume {sids['main']}")
    assert [before["main"], line] in hc(ship)["sent"]
    assert launched(ship, "grinder") == ["--dangerously-skip-permissions",
                                         "--resume", sids["grinder"]]
    # The same terminals, under the same addresses (an app pane on one stays put).
    assert {t["name"]: t["address"] for t in ship.terminals()} == before
    assert len(runs(ship)) == 2  # only the first boot's
    assert ship.terminal("main")["labels"]["shipshape.session"] == sids["main"]
    doc = ship.status()
    assert doc["agent_record"]["resume"]["resumed"] == 2
    assert {a["state"] for a in doc["agent_record"]["agents"]} == {"running"}
    assert {a["name"]: a["mode"] for a in doc["herdr"]["agents"]} == {
        "main": "auto", "grinder": "bypassPermissions"}
    never_killed(ship)


def test_a_terminal_that_did_not_come_back_is_run_again(core):
    ship = core
    sid = ship.record()["agents"]["main"]["session"]
    ship.core_reboot()
    s = ship.scenario()
    s["hc"]["terminals"] = [t for t in s["hc"]["terminals"] if t["name"] != "main"]
    ship.set(hc=s["hc"])
    ship.ctl("refresh")
    main = ship.terminal("main")
    assert main["argv"] == [LAUNCHER, "--permission-mode", "auto", "--resume", sid]
    assert main["keep"] and main["labels"] == {
        "shipshape.session": sid, "herdr.space": "shipshape", "herdr.tab": "main"}
    assert ship.record()["agents"]["main"]["place"]["id"] == main["address"]


def test_name_taken_means_it_is_running_already(core):
    """Another ship-ctl started it a moment before: hc refuses a second terminal by
    that name, and that is not a failure."""
    ship = core
    ship.core_reboot()
    s = ship.scenario()
    s["hc"]["terminals"] = [t for t in s["hc"]["terminals"] if t["name"] != "main"]
    s["hc"]["race_run"] = ["main"]
    ship.set(hc=s["hc"])
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert "main started again" in out and "did not start" not in out
    assert len([t for t in ship.terminals("main") if t["state"] == "live"]) == 1
    assert ship.record()["agents"]["main"]["relaunch"]["failures"] == 0


# Classic herdr only (a Ship moving between herdr and core): classic_herdr in
# pytest.ini; CI runs it, a local scripts/check does not. Deleted with classic herdr.
@pytest.mark.classic_herdr
def test_a_ship_moved_from_herdr_resumes_its_agents_on_core(ship):
    """Sessions made under herdr continue under core; herdr's panes mean nothing there."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.use_herdr()
    ship.ctl("apply", "--json")
    ship.ctl("refresh")
    ship.reboot()
    ship.use_core()
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert launched(ship, "main") == ["--permission-mode", "auto", "--resume", "sess-main"]
    assert launched(ship, "grinder")[-2:] == ["--resume", "sess-grinder"]
    record = ship.record()
    assert record["runtime"] == "core"
    assert {e["place"]["runtime"] for e in record["agents"].values()} == {"core"}


# --- the crash policy ---------------------------------------------------------------------
def test_an_agent_killed_by_a_signal_comes_back_in_its_kept_terminal(core):
    ship = core
    address = ship.terminal("main")["address"]
    sid = ship.record()["agents"]["main"]["session"]
    ship.core_crash("main", recover_in=1)
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert "main started again, continuing its session (it had stopped)." in out
    assert [address, f"cd -- /home/agent/work/main && exec {LAUNCHER} --permission-mode auto "
            f"--resume {sid}"] in hc(ship)["sent"]
    assert ship.terminal("main")["address"] == address
    assert launched(ship, "main")[-2:] == ["--resume", sid]
    never_killed(ship)


def test_an_agent_that_exited_with_a_code_gets_a_new_terminal(core):
    ship = core
    old = ship.terminal("main")["address"]
    ship.core_crash("main", how="exit")
    ship.ctl("refresh")
    main = ship.terminal("main")
    assert main["state"] == "live" and main["address"] != old
    assert hc(ship)["removed"] == [old]  # the finished one, never a live terminal
    assert ship.record()["agents"]["main"]["place"]["id"] == main["address"]


def test_all_agents_dying_at_once_all_come_back(core):
    """One holder process carries every terminal: when it dies, every agent goes."""
    ship = core
    ship.core_holder_lost()
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert {t["name"] for t in ship.terminals() if t["foreground"] and
            t["foreground"]["name"] == "claude"} == {"main", "grinder"}
    said = {line for line in out.splitlines() if line.startswith("agent record:")}
    assert said == {f"agent record: {n} started again, continuing its session (it had stopped)."
                    for n in ("main", "grinder")}


def test_a_deliberate_exit_is_not_a_crash(core):
    """Someone typed /exit: Claude ended its session (SessionEnd) and its terminal
    finished with exit code 0. It stays stopped until the next boot."""
    ship = core
    sid = ship.record()["agents"]["main"]["session"]
    s = ship.scenario()
    for t in s["hc"]["terminals"]:
        if t["name"] == "main":
            t.update(state="finished", exit_code=0, reason="exited", foreground=None, status=None)
    ship.set(hc=s["hc"])
    ship.end_session(sid, "prompt_input_exit")
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert "main stopped (it was exited (prompt_input_exit)); not started again." in out
    assert len(runs(ship)) == 2 and ship.record()["agents"]["main"]["want"] == "stopped"


def test_attention_after_three_failed_relaunches(core):
    ship = core
    sid = ship.record()["agents"]["main"]["session"]
    s = ship.scenario()
    s["hc"]["lost_sessions"] = [sid]  # its transcript cannot be loaded: claude exits at once
    ship.set(hc=s["hc"])
    ship.core_crash("main", how="exit")
    ship.ctl("refresh")  # stopped soon after it started (1), and the relaunch failed (2)
    ship.ctl("refresh")  # failed again (3)
    first = len(runs(ship))
    ship.ctl("refresh")  # left alone
    assert len(runs(ship)) == first
    doc = ship.status()
    main = {a["name"]: a for a in doc["agent_record"]["agents"]}["main"]
    assert main["state"] == "failed" and main["relaunch_failures"] == 3
    assert "exited at once" in main["last_error"]
    assert "agent-relaunch-failed" in {a["code"] for a in doc["attention"]}


# --- restart and notices ------------------------------------------------------------------
def test_restart_is_graceful_and_never_kills(core):
    ship = core
    old = ship.terminal("main")["address"]
    sid = ship.record()["agents"]["main"]["session"]
    rc, doc, out = ship.ctl("agent", "restart", "main", "--mode", "acceptEdits", "--json")
    assert rc == 0, out
    assert doc["data"]["restarted"][0]["mode"] == "acceptEdits"
    assert [old, "ctrl+c"] in hc(ship)["keys"] and [old, "/exit"] in hc(ship)["sent"]
    main = ship.terminal("main")
    assert main["address"] != old  # /exit finished the old terminal
    assert main["claude_args"] == ["--permission-mode", "acceptEdits", "--resume", sid]
    assert {a["name"]: a["mode"] for a in ship.status()["herdr"]["agents"]} == {
        "main": "acceptEdits", "grinder": "bypassPermissions"}
    never_killed(ship)


def test_restart_of_a_working_agent_presses_escape_first(core):
    ship = core
    ship.core_status("main", "working")
    old = ship.terminal("main")["address"]
    rc, doc, out = ship.ctl("agent", "restart", "main", "--force", "--json")
    assert rc == 0, out
    keys = hc(ship)["keys"]
    assert keys.index([old, "esc"]) < keys.index([old, "ctrl+c"])
    never_killed(ship)


def test_restart_fresh_gives_a_new_session_id(core):
    ship = core
    old = ship.record()["agents"]["main"]["session"]
    rc, _, out = ship.ctl("agent", "restart", "main", "--fresh", "--json")
    assert rc == 0, out
    new = ship.record()["agents"]["main"]["session"]
    assert new != old and len(new) == 36
    assert launched(ship, "main")[-2:] == ["--session-id", new]
    assert ship.terminal("main")["labels"]["shipshape.session"] == new
    assert hc(ship)["recorded_at_run"]["main"] == new


def test_a_stubborn_agent_is_left_running(core):
    ship = core
    s = ship.scenario()
    s["hc"]["stubborn"] = ["main"]
    ship.set(hc=s["hc"])
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc != 0 and doc["error"]["code"] == "agent-failed"
    assert [ship.terminal("main")["address"], "ctrl+c", "ctrl+c"] in hc(ship)["keys"]
    assert ship.terminal("main")["state"] == "live"
    never_killed(ship)


def test_notices_never_type_into_a_blocked_agent(core):
    ship = core
    ship.core_status("main", "blocked", "question", "Which color?")
    rc, doc, out = ship.ctl("notify-signin", "github", "--json")
    assert rc == 0, out
    assert doc["data"]["sent"] == ["grinder"]
    assert doc["data"]["skipped"] == [{"name": "main", "reason": "waiting-for-permission"}]
    grinder = ship.terminal("grinder")["address"]
    assert [grinder, "ShipShape: GitHub is now signed in as example-user. Re-check it if "
            "you need it."] in hc(ship)["sent"]
    never_killed(ship)


# --- the launcher -------------------------------------------------------------------------
def test_the_launcher_gives_claude_the_ships_environment(tmp_path):
    """Whatever environment the core has (a laptop's bare SSH login), claude gets the
    Ship's PATH, npm prefix and the catalogue tools' environment."""
    home = tmp_path / "home"
    (home / ".local/bin").mkdir(parents=True)
    (home / ".config/shipshape").mkdir(parents=True)
    (home / ".config/shipshape/env").write_text("EXAMPLE_TOOL_HOME='/opt/example tool'\n")
    claude = home / ".local/bin/claude"
    claude.write_text('#!/bin/sh\nprintf "%s\\n" "$PATH" "$npm_config_prefix" '
                      '"$EXAMPLE_TOOL_HOME" "$@"\n')
    claude.chmod(0o755)
    bare = {"HOME": str(home), "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:"
            "/bin:/usr/games:/usr/local/games:/snap/bin"}
    out = subprocess.run([str(SHIP_FILES / "bin/agent-launch"), "--permission-mode", "auto",
                          "--resume", "a b"], env=bare, capture_output=True, text=True,
                         check=True).stdout.splitlines()
    assert out == [f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin", f"{home}/.local",
                   "/opt/example tool", "--permission-mode", "auto", "--resume", "a b"]


def test_the_launch_typed_into_a_shell_is_one_quoted_line(core):
    ship = core
    sid = ship.record()["agents"]["main"]["session"]
    ship.core_crash("main")
    ship.ctl("refresh")
    [line] = [text for _, text in hc(ship)["sent"] if "exec " in text]
    assert shlex.split(line) == ["cd", "--", "/home/agent/work/main", "&&", "exec", LAUNCHER,
                                 "--permission-mode", "auto", "--resume", sid]


def test_the_core_unit_only_starts_the_core():
    text = (SHIP_FILES / "systemd/user/ship-core.service").read_text()
    assert "ExecStart=/usr/local/bin/hc server start" in text
    assert "Type=oneshot" in text and "ConditionUser=agent" in text
    assert "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin" in text
    assert "EnvironmentFile=-%h/.config/shipshape/env" in text
    assert "ExecStop" not in text and "kill" not in text.lower().replace("killed", "")


def test_schemas_are_kept_in_step():
    repo = SHIP_FILES.parents[1]
    for name in ("ship-status", "ship.toml"):
        a = json.loads((repo / "contracts" / f"{name}.schema.json").read_text())
        b = json.loads((repo / "ship/schemas" / f"{name}.schema.json").read_text())
        assert a == b


def test_a_terminal_still_running_the_launcher_is_never_typed_into(core):
    """agent-launch is a sh script: until it execs claude its terminal's foreground is
    sh, which is not a free shell, so a second launch never lands in claude's prompt."""
    ship = core
    s = ship.scenario()
    for t in s["hc"]["terminals"]:
        if t["name"] == "main":
            t.update(status=None, foreground={"pid": 7, "name": "sh", "cwd": t["cwd"],
                                              "argv": ["/bin/sh", LAUNCHER, "--resume", "x"]})
    ship.set(hc=s["hc"])
    assert {a["name"]: a["state"] for a in ship.status()["herdr"]["agents"]}["main"] == "unknown"
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert not [x for x in hc(ship).get("sent", []) if "exec " in x[1]]
    assert len(runs(ship)) == 2



# --- the wake-up after a boot -------------------------------------------------------------
WAKE_LINE = ("Your Ship rebooted 10 minutes ago. Make sure you've resumed any background "
             "processes or work that you should be doing, and review whether subagents or "
             "crews may have been interrupted by the reboot too.")


def woken(ship) -> dict:
    rc, doc, out = ship.ctl("set", "wake.after_boot=true", "--json")
    assert rc == 0, out
    ship.p("/proc/uptime").write_text("605 1.0\n")
    rc, doc, out = ship.ctl("wake", "--json")
    assert rc == 0, out
    return doc["data"]


def test_the_wake_up_reaches_the_primary_through_hc_send(core):
    ship = core
    data = woken(ship)
    assert data == {"outcome": "sent", "agent": "main", "tries": 1, "reason": None}
    assert [ship.terminal("main")["address"], WAKE_LINE] in hc(ship)["sent"]
    assert ["hc", "send", "--json", "main", "--", WAKE_LINE] in ship.calls("hc")
    assert ship.calls("herdr") == []
    never_killed(ship)


def test_the_wake_up_never_types_into_a_blocked_primary(core):
    ship = core
    ship.core_status("main", "blocked", "permission", "approve Bash: make")
    data = woken(ship)
    assert data["outcome"] == "gave-up" and data["reason"] == "waiting-for-permission"
    assert not [x for x in hc(ship)["sent"] if x[1] == WAKE_LINE]
    never_killed(ship)


# --- following the session, a live runtime switch, flaky reads (the Fable review) ---------
def test_the_record_follows_clear_and_resumes_in_the_new_sessions_mode(core):
    """/clear in grinder's terminal (bypass) starts a session the person then moves to
    plan: the record follows it, so a crash brings back that session in plan, never
    the cleared one in bypass."""
    ship = core
    old = ship.record()["agents"]["grinder"]["session"]
    ship.core_clear("grinder", "sess-cleared", "plan")
    agents = {a["name"]: a for a in ship.status()["herdr"]["agents"]}
    assert agents["grinder"]["mode"] == "plan"
    ship.ctl("refresh")
    assert ship.record()["agents"]["grinder"]["session"] == "sess-cleared"
    assert ship.record()["agents"]["grinder"]["mode"] == "plan"
    ship.core_crash("grinder")
    ship.ctl("refresh")
    assert launched(ship, "grinder") == ["--permission-mode", "plan", "--resume", "sess-cleared"]
    assert old not in launched(ship, "grinder")


def test_a_claude_started_by_hand_in_a_terminal_is_picked_up(core):
    ship = core
    s = ship.scenario()
    s["hc"]["terminals"].append({
        "address": "t9", "name": None, "argv": ["claude"], "labels": {}, "cwd": "/home/agent/x",
        "state": "live", "keep": False, "created_ms": 1,
        "status": {"app": "claude-code", "state": "idle"},
        "foreground": {"pid": 9, "name": "claude", "argv": ["claude"], "cwd": "/home/agent/x"}})
    ship.set(hc=s["hc"])
    path = ship.p("/home/agent/.local/state/shipshape/modes.json")
    doc = json.loads(path.read_text())
    doc["sessions"]["sess-hand"] = {"mode": "acceptEdits", "started_at": "2026-01-01T00:00:00Z",
                                    "updated_at": "2026-01-01T00:00:00Z", "terminal": "t9"}
    path.write_text(json.dumps(doc))
    ship.ctl("refresh")
    e = ship.record()["agents"]["x"]
    assert e["session"] == "sess-hand" and e["mode"] == "acceptEdits"
    assert e["origin"] == "adopted" and e["place"]["id"] == "t9"


def test_the_hook_records_the_core_terminal(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("mode_ledger",
                                                  SHIP_FILES / "hooks/mode_ledger.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setenv("HC_TERMINAL_ID", "t12")
    mod.handle({"hook_event_name": "SessionStart", "session_id": "s1",
                "permission_mode": "plan"}, str(tmp_path))
    monkeypatch.setenv("HC_TERMINAL_ID", "$(reboot)")  # only an address is kept
    mod.handle({"hook_event_name": "SessionStart", "session_id": "s2",
                "permission_mode": "plan"}, str(tmp_path))
    monkeypatch.delenv("HC_TERMINAL_ID")
    mod.handle({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                "permission_mode": "auto"}, str(tmp_path))
    sessions = json.loads((tmp_path / "modes.json").read_text())["sessions"]
    assert sessions["s1"]["terminal"] == "t12" and sessions["s1"]["mode"] == "auto"
    assert "terminal" not in sessions["s2"]


# Classic herdr only (a Ship moving between herdr and core): classic_herdr in
# pytest.ini; CI runs it, a local scripts/check does not. Deleted with classic herdr.
@pytest.mark.classic_herdr
def test_switching_a_live_herdr_ship_to_core_starts_nothing_twice(ship):
    """ship.toml says core while the herdr agents still run (no reboot): core starts
    none of them this boot; the next boot resumes them on core."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.use_herdr()
    ship.ctl("apply", "--json")
    ship.ctl("refresh")
    ship.use_core()
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert runs(ship) == [] and len(ship.scenario()["herdr"]["agents"]) == 2
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert runs(ship) == []
    assert {a["state"] for a in ship.status()["agent_record"]["agents"]} == {"unknown"}
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--json")
    assert rc != 0 and runs(ship) == []
    ship.reboot()
    ship.ctl("refresh")
    assert launched(ship, "main")[-2:] == ["--resume", "sess-main"]


# Classic herdr only (a Ship moving between herdr and core): classic_herdr in
# pytest.ini; CI runs it, a local scripts/check does not. Deleted with classic herdr.
@pytest.mark.classic_herdr
def test_switching_back_to_herdr_while_core_agents_run_starts_nothing_twice(core):
    ship = core
    text = ship.p("/etc/shipshape/ship.toml").read_text()
    ship.runtime = None  # ship.toml as written: no runtime, so herdr
    ship.write_toml(text.replace('runtime = "core"\n', ""))
    ship.ctl("refresh")
    assert [c for c in ship.calls("herdr") if c[1:3] == ["agent", "start"]] == []


def test_one_failed_hc_get_never_stops_or_drops_a_live_agent(core):
    ship = core
    before = ship.record()["agents"]["main"]
    s = ship.scenario()
    s["hc"]["get_fails"] = [ship.terminal("main")["address"]]
    ship.set(hc=s["hc"])
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    after = ship.record()["agents"]["main"]
    assert after["want"] == "running" and after["origin"] == before["origin"] == "autostart"
    assert after["prompt"] == before["prompt"]
    assert len(runs(ship)) == 2
    doc = ship.status()
    assert doc["herdr"]["agents"] == [] and "hc get t1: no answer" in doc["errors"]


def test_a_launch_is_typed_once_even_when_the_shell_is_slow(core):
    """bash has not run the typed line yet: no second line in the same refresh (two
    passes, two places to try) nor in the next one."""
    ship = core
    s = ship.scenario()
    s["hc"]["slow_shell"] = ["main"]
    ship.set(hc=s["hc"])
    ship.core_reboot()
    ship.ctl("refresh")
    ship.ctl("refresh")
    address = ship.terminal("main")["address"]
    typed = [x for x in hc(ship)["sent"] if "exec " in x[1] and x[0] == address]
    assert len(typed) == 1
    assert ship.terminal("main")["labels"]["shipshape.launching"].isdigit()
    assert len(runs(ship)) == 2


def test_another_program_in_a_kept_terminal_is_not_claude(core):
    ship = core
    s = ship.scenario()
    for t in s["hc"]["terminals"]:
        if t["name"] == "main":
            t.update(status=None, foreground={"pid": 5, "name": "vim", "argv": ["vim", "x"],
                                              "cwd": t["cwd"]})
    ship.set(hc=s["hc"])
    doc = ship.status()
    assert "main" not in {a["name"] for a in doc["herdr"]["agents"]}
    assert {a["name"]: a["state"] for a in doc["agent_record"]["agents"]}["main"] == "starting"
    ship.ctl("refresh")
    assert not [x for x in hc(ship)["sent"] if "exec " in x[1]]  # never typed into vim


def test_a_terminal_with_no_program_is_lost_whatever_its_last_word(core):
    ship = core
    s = ship.scenario()
    for t in s["hc"]["terminals"]:
        if t["name"] == "main":
            t.update(foreground=None, recover_in=1)  # status left as Claude last said
    ship.set(hc=s["hc"])
    ship.ctl("refresh")
    assert launched(ship, "main")[-2:] == ["--resume", ship.record()["agents"]["main"]["session"]]


# --- core-restart-pending and ship-ctl logs core -------------------------------------


def _fake_process(ship, pid: int, argv: list[str], exe: Path) -> None:
    proc = ship.p(f"/proc/{pid}")
    proc.mkdir(parents=True)
    (proc / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (proc / "exe").symlink_to(exe)


def _install_core(ship) -> Path:
    path = ship.p("/usr/local/bin/herdr-core")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("new herdr-core")
    return path


def _codes(doc) -> list[str]:
    return [a["code"] for a in doc["attention"]]


def test_core_restart_pending_when_the_core_runs_an_old_binary(core, tmp_path):
    """A converge replaced /usr/local/bin/herdr-core; the core keeps the old one. Its
    holders (which keep each terminal's program) never count."""
    ship = core
    installed = _install_core(ship)
    old = tmp_path / "old-core"
    old.write_text("old herdr-core")
    _fake_process(ship, 4300, ["/usr/local/bin/herdr-core", "hold", "--socket", "x"], old)
    assert "core-restart-pending" not in _codes(ship.status())
    _fake_process(ship, 4301, ["/usr/local/bin/herdr-core", "serve"], installed)
    assert "core-restart-pending" not in _codes(ship.status())
    _fake_process(ship, 4302, ["/usr/local/bin/herdr-core", "serve"], old)
    doc = ship.status()
    [item] = [a for a in doc["attention"] if a["code"] == "core-restart-pending"]
    assert item["subject"] == "herdr-core" and "Agents are not affected" in item["message"]
    assert "herdr-restart-pending" not in _codes(doc)


# A herdr server another tool runs on a herdr-core Ship: a named session (a supervising
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
    _fake_process(ship, 4500, argv, old)


@pytest.mark.parametrize("server", OTHER_HERDR_SERVERS)
def test_a_core_ship_never_reports_a_herdr_restart_pending(core, tmp_path, server):
    """herdr is not a core Ship's runtime: no running herdr server, on any version,
    makes status say a restart is pending (FleetLedger's update reboots on it)."""
    ship = core
    _old_herdr_server(ship, tmp_path, OTHER_HERDR_SERVERS[server])
    doc = ship.status()
    assert_valid("ship-status", doc)
    assert doc["herdr"]["runtime"] == "core"
    assert "herdr-restart-pending" not in _codes(doc)


def test_a_core_ships_own_restart_pending_is_unchanged_beside_an_old_herdr(core, tmp_path):
    ship = core
    _old_herdr_server(ship, tmp_path, OTHER_HERDR_SERVERS["named-session"])
    _install_core(ship)
    old_core = tmp_path / "old-core"
    old_core.write_text("old herdr-core")
    _fake_process(ship, 4501, ["/usr/local/bin/herdr-core", "serve"], old_core)
    codes = _codes(ship.status())
    assert "core-restart-pending" in codes and "herdr-restart-pending" not in codes


def test_logs_core_reads_the_boot_unit_and_the_cores_own_logs(core):
    ship = core
    ship.set(journal=["2026-01-01T00:00:00+0000 hc[9]: core started"])
    state = ship.p("/home/agent/.local/state/herdr-core/default")
    state.mkdir(parents=True, exist_ok=True)
    (state / "core.log").write_text("".join(f"core {i}\n" for i in range(5)))
    (state / "holder.log").write_text("holder 0\n")
    rc, doc, _ = ship.ctl("logs", "core", "--lines", "2", "--json")
    assert rc == 0
    assert_valid("ship-ctl-result", doc)
    assert doc["data"]["lines"] == ["2026-01-01T00:00:00+0000 hc[9]: core started",
                                    "core.log: core 3", "core.log: core 4",
                                    "holder.log: holder 0"]
    journal = [c for c in ship.calls("journalctl") if "_SYSTEMD_USER_UNIT=ship-core.service" in c]
    assert journal and journal[-1][-1] == "_SYSTEMD_USER_UNIT=ship-core.service"


def test_logs_core_never_follows_a_planted_link(core, tmp_path):
    """The agent owns its state folder: a core.log that is a link to a root-only file
    is not read through."""
    ship = core
    secret = tmp_path / "secret"
    secret.write_text("not for agents\n")
    state = ship.p("/home/agent/.local/state/herdr-core/default")
    state.mkdir(parents=True, exist_ok=True)
    (state / "core.log").symlink_to(secret)
    rc, doc, _ = ship.ctl("logs", "core", "--json")
    assert rc == 0
    assert not [line for line in doc["data"]["lines"] if "not for agents" in line]


def test_logs_core_reads_only_the_end_of_a_long_log(tmp_path, monkeypatch):
    """herdr-core never rotates core.log: only its end is read, never a cut line."""
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, str(SHIP_FILES.parent / "lib"))
    from shipctl import logs
    from shipctl.homefs import HomeFS

    state = tmp_path / ".local/state/herdr-core/default"
    state.mkdir(parents=True)
    (state / "core.log").write_text("x" * 50 + "\nlast line\n")
    monkeypatch.setattr(logs, "CORE_LOG_BYTES", 20)
    ctx = SimpleNamespace(home=HomeFS(tmp_path))
    rel = ".local/state/herdr-core/default/core.log"
    assert logs._home_tail(ctx, rel, 10) == ["last line"]
    # Exactly CORE_LOG_BYTES long: nothing was cut, so no line is dropped.
    (state / "core.log").write_text("first 789\nlast line\n")  # 20 bytes
    assert logs._home_tail(ctx, rel, 10) == ["first 789", "last line"]


# --- what a failed start leaves in the record (characterisation) -------------------------
def _fail_run(ship, *names: str) -> None:
    s = ship.scenario()
    s["hc"]["fail_run"] = list(names)
    ship.set(hc=s["hc"])


def _exited(ship, name: str) -> str:
    """name was exited on purpose (its terminal finished, its session ended): the
    refresh leaves it in the record, stopped. Returns its session."""
    sid = ship.record()["agents"][name]["session"]
    s = ship.scenario()
    for t in s["hc"]["terminals"]:
        if t["name"] == name:
            t.update(state="finished", exit_code=0, reason="exited", foreground=None, status=None)
    ship.set(hc=s["hc"])
    ship.end_session(sid, "prompt_input_exit")
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert ship.record()["agents"][name]["want"] == "stopped"
    return sid


def test_a_failed_autostart_on_core_leaves_no_record_of_it(ship):
    """The record had the new session before the launch; nothing started, so the
    agent is not recorded at all (it was not before)."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.use_core()
    ship.ctl("apply", "--json")
    _fail_run(ship, "grinder")
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc != 0 and doc["data"]["started"] == ["main"]
    assert doc["data"]["skipped"][0]["name"] == "grinder"
    assert set(ship.record()["agents"]) == {"main"}
    never_killed(ship)


def test_a_failed_autostart_on_core_puts_a_stopped_agent_back_as_it_was(core):
    ship = core
    _exited(ship, "grinder")
    before = ship.record()["agents"]["grinder"]
    _fail_run(ship, "grinder")
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc != 0
    reasons = {s["name"]: s["reason"] for s in doc["data"]["skipped"]}
    assert reasons["grinder"].startswith("agent start failed:"), reasons
    assert ship.record()["agents"]["grinder"] == before
    never_killed(ship)


def test_a_failed_fresh_restart_on_core_keeps_the_old_session(core):
    """A running agent stopped for a fresh restart whose new terminal did not start:
    the record goes back to its old session and is not marked as a crash (a fresh
    conversation is not resumed by the refresh)."""
    ship = core
    before = ship.record()["agents"]["main"]
    _fail_run(ship, "main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--fresh", "--json")
    assert rc != 0 and doc["error"]["code"] == "agent-failed"
    after = ship.record()["agents"]["main"]
    assert after["session"] == before["session"]
    assert after["relaunch"]["last_error"] is None
    assert hc(ship)["recorded_at_run"]["main"] == before["session"]  # only the first start
    never_killed(ship)


def test_a_failed_restart_on_core_is_recorded_as_a_crash(core):
    """Not fresh: the record keeps its session in the new mode, as a crash, so the
    next refresh resumes it."""
    ship = core
    sid = ship.record()["agents"]["main"]["session"]
    _fail_run(ship, "main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--mode", "plan", "--json")
    assert rc != 0 and doc["error"]["code"] == "agent-failed"
    after = ship.record()["agents"]["main"]
    assert after["session"] == sid and after["mode"] == "plan" and after["want"] == "running"
    assert after["relaunch"]["last_error"]
    _fail_run(ship)
    ship.ctl("refresh")
    assert launched(ship, "main") == ["--permission-mode", "plan", "--resume", sid]
    never_killed(ship)


def test_a_failed_fresh_start_of_a_stopped_agent_on_core_keeps_its_session(core):
    """A recorded agent that is not running, started fresh (record.relaunch): the
    record had the new session before the launch, and gets its old one back."""
    ship = core
    sid = _exited(ship, "main")
    before = ship.record()["agents"]["main"]
    _fail_run(ship, "main")
    rc, doc, _ = ship.ctl("agent", "restart", "main", "--fresh", "--json")
    assert rc != 0 and doc["error"]["code"] == "agent-failed"
    after = ship.record()["agents"]["main"]
    assert after["session"] == sid
    assert after == before
    never_killed(ship)


@pytest.fixture(scope="module")
def pane_env():
    """A pane's settings, set before any test's own fixtures run."""
    mp = pytest.MonkeyPatch()
    for key in ("HERDR_SOCKET_PATH", "HERDR_SESSION", "HC_TERMINAL_ID", "SHIPSHAPE_ROOT"):
        mp.setenv(key, "/run/example-pane")
    yield
    mp.undo()


def test_no_ship_test_sees_the_panes_herdr_settings(pane_env):
    assert [k for k in os.environ if k.startswith(("HERDR_", "HC_", "SHIPSHAPE_"))] == []
