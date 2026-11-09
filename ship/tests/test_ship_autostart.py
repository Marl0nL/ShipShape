"""ship-ctl autostart and the status timer's reaction to sign-ins (contracts/ship-ctl.md)."""

from __future__ import annotations

from ship_harness import assert_valid


def started_args(ship) -> dict[str, list[str]]:
    return {a["name"]: a["args"] for a in ship.scenario()["herdr"]["agents"]}


def test_autostart_starts_each_agent_in_its_mode(ship):
    ship.ctl("apply", "--json")
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0, doc
    assert_valid("ship-ctl-result", doc)
    assert doc["data"] == {"started": ["main", "grinder"], "skipped": []}
    assert started_args(ship) == {
        "main": ["--permission-mode", "auto", "--name", "ship-01-main"],
        "grinder": ["--dangerously-skip-permissions", "--name", "ship-01-grinder"],
    }
    workspaces = ship.scenario()["herdr"]["workspaces"]
    assert workspaces[0]["args"] == [
        "--cwd",
        "/home/agent/work/main",
        "--label",
        "main",
        "--no-focus",
    ]
    assert ship.p("/home/agent/work/main").is_dir()
    # grinder has no prompt of its own: it gets the default first prompt.
    assert ship.scenario()["herdr"]["prompts"] == [
        ["main", "Get oriented."],
        ["grinder", "Get oriented and ready to work."],
    ]
    # A second run starts nothing.
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert doc["data"]["started"] == []
    assert {s["reason"] for s in doc["data"]["skipped"]} == {"running"}


def test_autostart_waits_while_signed_out(ship):
    ship.set(claude={"logged_in": False})
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0
    assert doc["data"]["started"] == []
    assert {s["reason"] for s in doc["data"]["skipped"]} == {"needs-sign-in"}
    assert not ship.calls("herdr") or all(c[1:3] != ["agent", "start"] for c in ship.calls("herdr"))


def test_after_a_boot_autostart_resumes_its_agents_instead_of_starting_new_ones(ship):
    ship.ctl("autostart", "--json")
    ship.reboot()
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0, doc
    assert doc["data"] == {"started": [], "skipped": [{"name": "main", "reason": "running"},
                                                      {"name": "grinder", "reason": "running"}]}
    assert started_args(ship) == {
        "main": ["--permission-mode", "auto", "--resume", "sess-main"],
        "grinder": ["--dangerously-skip-permissions", "--resume", "sess-grinder"],
    }
    assert len(ship.scenario()["herdr"]["workspaces"]) == 2  # back in their own panes
    assert any("main started again, continuing its session." in m for m in doc["messages"])


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


def test_autostart_reports_start_failure(ship):
    ship.set(herdr={"server": True, "agents": [], "fail_start": ["grinder"]})
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 1
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "herdr-unavailable"
    assert doc["data"]["started"] == ["main"]
    assert doc["data"]["skipped"][0]["name"] == "grinder"


def test_autostart_without_herdr(ship):
    ship.set(herdr={"server": False})
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 1 and doc["error"]["code"] == "herdr-unavailable"


def test_refresh_runs_autostart_once_when_claude_signs_in(ship):
    ship.set(claude={"logged_in": False})
    rc, _, out = ship.ctl("refresh")
    assert rc == 0, out
    assert ship.json("/run/shipshape/status.json")["state"] == "needs-sign-in"
    assert ship.scenario()["herdr"]["agents"] == []

    ship.set(claude={"logged_in": True, "email": "work@example.com"})
    ship.ctl("refresh")
    assert sorted(started_args(ship)) == ["grinder", "main"]

    # Exit an agent by hand (/exit): the next refresh must not start it again.
    ship.exit("grinder")
    ship.ctl("refresh")
    assert sorted(started_args(ship)) == ["main"]
    assert ship.record()["agents"]["grinder"]["want"] == "stopped"


def test_a_boot_while_signed_out_resumes_once_claude_signs_in(ship):
    ship.ctl("refresh")  # signed in: main and grinder start
    ship.set(claude={"logged_in": False})
    ship.reboot()
    ship.ctl("refresh")
    assert ship.scenario()["herdr"]["agents"] == []  # nothing starts before the sign-in
    assert ship.record()["resume"]["boot_id"] != ship.p(
        "/proc/sys/kernel/random/boot_id").read_text().strip()
    ship.set(claude={"logged_in": True, "email": "work@example.com"})
    ship.ctl("refresh")
    assert started_args(ship) == {  # resumed, not started afresh
        "main": ["--permission-mode", "auto", "--resume", "sess-main"],
        "grinder": ["--dangerously-skip-permissions", "--resume", "sess-grinder"],
    }
    assert ship.scenario()["herdr"].get("prompts") == [
        ["main", "Get oriented."], ["grinder", "Get oriented and ready to work."]]  # once


def test_refresh_does_nothing_while_provisioning(ship):
    ship.set(cloud_init="running")
    ship.ctl("refresh")
    assert ship.json("/run/shipshape/status.json")["state"] == "provisioning"
    assert not ship.p("/run/shipshape/auth-previous.json").exists()
    assert ship.scenario()["herdr"]["agents"] == []


def test_autostart_records_session_names(ship):
    ship.ctl("autostart", "--json")
    record = ship.record()["agents"]
    assert record["main"]["session_name"] == "ship-01-main"
    assert record["grinder"]["session_name"] == "ship-01-grinder"
    ship.ctl("set", "claude.session_prefix=lab", "--json")
    import sys

    sys.path.insert(0, str(ship.bundle / "lib"))
    from shipctl import naming, shiptoml
    from shipctl.paths import Paths

    toml = shiptoml.load(Paths(ship.root, ship.bundle))
    assert naming.session_name(toml, "main") == "lab-main"


def test_autostart_without_name_flag(ship):
    ship.set(claude={"logged_in": True, "email": "work@example.com", "name_flag": False})
    ship.ctl("autostart", "--json")
    assert started_args(ship)["main"] == ["--permission-mode", "auto"]


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


# --- an agent that works in a GitHub repo -------------------------------------------


def _with_repo_agent(ship, repo="someone/agent-setup"):
    rc, doc, _ = ship.ctl("set", f"autostart.add=setup:github:{repo}", "--json")
    assert rc == 0, doc
    toml = ship.p("/etc/shipshape/ship.toml").read_text()
    assert f'repo = "{repo}"' in toml and 'cwd = "~/work/agent-setup"' in toml


def test_a_public_repo_is_cloned_and_the_agent_starts_in_it(ship):
    ship.set(git={"public": ["someone/agent-setup"]})
    _with_repo_agent(ship)
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0, doc
    assert "setup" in doc["data"]["started"]
    assert ship.p("/home/agent/work/agent-setup/CLAUDE.md").exists()
    cwds = [w["args"][1] for w in ship.scenario()["herdr"]["workspaces"]]
    assert "/home/agent/work/agent-setup" in cwds


def test_a_private_repo_waits_for_the_github_sign_in_then_starts(ship):
    ship.set(git={"private": ["someone/agent-setup"]}, gh={"login": None})
    _with_repo_agent(ship)
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0, doc
    assert "setup" not in doc["data"]["started"] and "main" in doc["data"]["started"]
    waiting = next(s for s in doc["data"]["skipped"] if s["name"] == "setup")
    assert "waits for the GitHub sign-in" in waiting["reason"]
    rc, status_doc, _ = ship.ctl("status", "--json")
    assert_valid("ship-status", status_doc)
    codes = {a["code"] for a in status_doc["attention"]}
    assert "agent-repo-waiting" in codes
    # GitHub signed in: the next refresh notices and starts it.
    ship.ctl("refresh", "--json")
    ship.set(gh={"login": "example-user"})
    ship.ctl("refresh", "--json")
    assert "setup" in started_args(ship)
    rc, status_doc, _ = ship.ctl("status", "--json")
    assert "agent-repo-waiting" not in {a["code"] for a in status_doc["attention"]}


def test_a_folder_that_holds_something_else_is_never_overwritten(ship):
    ship.set(git={"public": ["someone/agent-setup"]})
    folder = ship.p("/home/agent/work/agent-setup")
    folder.mkdir(parents=True)
    (folder / "notes.txt").write_text("mine")
    _with_repo_agent(ship)
    rc, doc, _ = ship.ctl("autostart", "--json")
    skipped = next(s for s in doc["data"]["skipped"] if s["name"] == "setup")
    assert "not a checkout" in skipped["reason"]
    assert (folder / "notes.txt").read_text() == "mine" and not (folder / ".git").exists()


def test_a_public_repo_is_cloned_before_any_sign_in(ship):
    ship.set(git={"public": ["someone/agent-setup"]}, claude={"logged_in": False})
    _with_repo_agent(ship)
    ship.ctl("refresh", "--json")
    assert ship.p("/home/agent/work/agent-setup/CLAUDE.md").exists()  # ready for the agent
    assert "setup" not in started_args(ship)  # which starts once Claude is signed in
    clones = [c for c in ship.calls("git") if c[1:2] == ["clone"]]
    ship.ctl("refresh", "--json")
    assert [c for c in ship.calls("git") if c[1:2] == ["clone"]] == clones  # not again


def test_a_github_sign_in_starts_only_the_agents_that_waited_for_it(ship):
    ship.set(git={"private": ["someone/agent-setup"]}, gh={"login": None})
    _with_repo_agent(ship)
    ship.ctl("refresh", "--json")  # the first after boot: main and grinder; setup waits
    ship.exit("main")  # then you stopped main
    ship.set(gh={"login": "example-user"})
    ship.ctl("refresh", "--json")
    started = started_args(ship)
    assert "setup" in started and "main" not in started  # main stays stopped


def test_a_waiting_private_repo_is_not_tried_at_every_refresh(ship):
    ship.set(git={"private": ["someone/agent-setup"]}, gh={"login": None})
    _with_repo_agent(ship)
    ship.ctl("refresh", "--json")
    clones = len([c for c in ship.calls("git") if c[1:2] == ["clone"]])
    ship.ctl("refresh", "--json")
    ship.ctl("refresh", "--json")
    assert len([c for c in ship.calls("git") if c[1:2] == ["clone"]]) == clones


def test_github_out_of_reach_is_said_and_tried_again(ship):
    ship.set(git={"public": ["someone/agent-setup"], "offline": True},
             claude={"logged_in": False})
    _with_repo_agent(ship)
    ship.ctl("refresh", "--json")
    rc, doc, _ = ship.ctl("status", "--json")
    item = next(a for a in doc["attention"] if a.get("subject") == "setup")
    assert "could not reach github.com" in item["message"]
    assert "sign-in" not in item["message"]
    s = ship.scenario()
    s["git"]["offline"] = False
    ship.set(git=s["git"])
    ship.ctl("refresh", "--json")  # no sign-in changed: retried anyway
    assert ship.p("/home/agent/work/agent-setup/CLAUDE.md").exists()


def test_removing_an_agent_drops_its_repo_attention(ship):
    ship.set(git={"private": ["someone/agent-setup"]}, gh={"login": None})
    _with_repo_agent(ship)
    ship.ctl("refresh", "--json")
    rc, doc, _ = ship.ctl("status", "--json")
    assert "agent-repo-waiting" in {a["code"] for a in doc["attention"]}
    assert ship.ctl("set", "autostart.remove=setup", "--json")[0] == 0
    rc, doc, _ = ship.ctl("status", "--json")
    assert "agent-repo-waiting" not in {a["code"] for a in doc["attention"]}


def test_f8_github_signin_alone_never_starts_when_claude_signed_out(ship):
    """A GitHub sign-in with a repo agent waiting, while Claude is signed out, starts
    nothing: only a Claude sign-in (or a boot while signed in) lets agents start."""
    ship.set(git={"private": ["someone/agent-setup"]}, gh={"login": None},
             claude={"logged_in": False})
    _with_repo_agent(ship)
    ship.ctl("refresh", "--json")
    ship.set(gh={"login": "example-user"})
    ship.ctl("refresh", "--json")
    assert ship.p("/home/agent/work/agent-setup/CLAUDE.md").exists()  # cloned
    assert started_args(ship) == {}  # but nothing started
    rc, doc, _ = ship.ctl("status", "--json")
    assert doc["state"] == "needs-sign-in"
