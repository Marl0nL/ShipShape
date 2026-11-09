"""Ships whose agents run in herdr-core (ship.toml runtime = "core"; runtime.Core).

`ship.use_core()` makes the sandbox Ship a core Ship; its `hc` is the fake in
fakes/fake.py. `ship.core_reboot()` is a reboot as ship-ctl sees it: a new boot id,
and every kept terminal back under its address and name with no program in it
until its new shell starts. Ships without the setting are herdr Ships, exactly as
before (the other test files).
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


def status(ship) -> dict:
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert_valid("ship-status", doc)
    return doc


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
def test_herdr_stays_the_default_and_never_meets_hc(ship):
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.ctl("apply", "--json")
    ship.ctl("refresh")
    assert {a["name"] for a in ship.scenario()["herdr"]["agents"]} == {"main", "grinder"}
    assert ship.calls("hc") == []
    doc = status(ship)
    assert doc["herdr"]["runtime"] == "herdr" and doc["agent_record"]["runtime"] == "herdr"
    assert ship.record()["runtime"] == "herdr"


def test_a_core_ship_never_meets_herdr(core):
    ship = core
    status(ship)
    assert ship.calls("herdr") == []
    assert ship.record()["runtime"] == "core"


def test_ship_toml_refuses_an_unknown_runtime(ship):
    text = ship.p("/etc/shipshape/ship.toml").read_text()
    ship.write_toml(text.replace("schema = 1\n", 'schema = 1\nruntime = "tmux"\n', 1))
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


def test_the_claude_floor_is_only_for_core(ship):
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
    ship.set(claude={"logged_in": True, "email": "work@example.com", "version": "2.1.290"})
    ship.ctl("apply", "--json")
    ship.ctl("refresh")
    assert {a["name"] for a in ship.scenario()["herdr"]["agents"]} == {"main", "grinder"}


# --- the status document ------------------------------------------------------------------
def test_status_maps_claudes_own_state(core):
    ship = core
    ship.core_status("main", "blocked", "permission", "approve Bash: curl -sI https://example.com")
    ship.core_status("grinder", None)  # not reporting yet
    doc = status(ship)
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


def test_terminals_that_are_not_agents_are_not_listed(core):
    ship = core
    s = ship.scenario()
    s["hc"]["terminals"].append({
        "address": "t9", "name": "scratch", "argv": ["bash"], "labels": {}, "cwd": "/tmp",
        "state": "live", "keep": False, "status": None,
        "foreground": {"pid": 9, "name": "vim", "argv": ["vim"], "cwd": "/tmp"}})
    ship.set(hc=s["hc"])
    assert {a["name"] for a in status(ship)["herdr"]["agents"]} == {"main", "grinder"}
    assert "scratch" not in ship.record()["agents"]


def test_status_says_when_hc_is_missing(core):
    ship = core
    os.unlink(Path(ship.env["PATH"].split(":")[0]) / "hc")
    doc = status(ship)
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
    assert {a["state"] for a in status(ship)["agent_record"]["agents"]} == {"starting"}
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
    doc = status(ship)
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


def test_a_ship_moved_from_herdr_resumes_its_agents_on_core(ship):
    """Sessions made under herdr continue under core; herdr's panes mean nothing there."""
    ship.env["SHIPCTL_TEST_WAIT_S"] = "0"
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
    doc = status(ship)
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
    assert {a["name"]: a["mode"] for a in status(ship)["herdr"]["agents"]} == {
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
    assert {a["name"]: a["state"] for a in status(ship)["herdr"]["agents"]}["main"] == "unknown"
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert not [x for x in hc(ship).get("sent", []) if "exec " in x[1]]
    assert len(runs(ship)) == 2

