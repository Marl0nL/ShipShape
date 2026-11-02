"""ship-ctl autostart and the status timer's reaction to sign-ins (contracts/ship-ctl.md)."""

from __future__ import annotations

import json

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
    assert ship.scenario()["herdr"]["prompts"] == [["main", "Get oriented."]]
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


def test_autostart_waits_for_resume_after_boot(ship):
    ship.p("/home/agent/.local/state/shipshape/last-resume.json").write_text(
        json.dumps(
            {
                "at": "2026-01-01T00:00:00Z",
                "method": "hidden-client",
                "resumed": 1,
                "boot_id": "an-earlier-boot",
            }
        )
    )
    ship.p("/proc/uptime").write_text("30.0 1.0\n")
    rc, doc, _ = ship.ctl("autostart", "--json")
    assert rc == 0
    assert {s["reason"] for s in doc["data"]["skipped"]} == {"resume-pending"}
    # Long after boot, autostart stops waiting.
    ship.p("/proc/uptime").write_text("900.0 1.0\n")
    _, doc, _ = ship.ctl("autostart", "--json")
    assert doc["data"]["started"] == ["main", "grinder"]


def test_autostart_reattaches_name_after_reboot(ship):
    """herdr resumed the pane but not the name: match by recorded session, then rename."""
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

    # Stop an agent by hand: the next refresh must not restart it.
    s = ship.scenario()
    s["herdr"]["agents"] = [a for a in s["herdr"]["agents"] if a["name"] != "grinder"]
    ship.set(herdr=s["herdr"])
    ship.ctl("refresh")
    assert sorted(started_args(ship)) == ["main"]


def test_refresh_retries_autostart_after_resume(ship):
    ship.p("/home/agent/.local/state/shipshape/last-resume.json").unlink()
    ship.p("/proc/uptime").write_text("20.0 1.0\n")
    ship.ctl("refresh")
    assert ship.scenario()["herdr"]["agents"] == []
    ship.p("/home/agent/.local/state/shipshape/last-resume.json").write_text(
        json.dumps(
            {
                "at": "2026-01-01T00:00:00Z",
                "method": "hidden-client",
                "resumed": 0,
                "boot_id": ship.p("/proc/sys/kernel/random/boot_id").read_text().strip(),
            }
        )
    )
    ship.ctl("refresh")
    assert sorted(started_args(ship)) == ["grinder", "main"]


def test_refresh_does_nothing_while_provisioning(ship):
    ship.set(cloud_init="running")
    ship.ctl("refresh")
    assert ship.json("/run/shipshape/status.json")["state"] == "provisioning"
    assert not ship.p("/run/shipshape/auth-previous.json").exists()
    assert ship.scenario()["herdr"]["agents"] == []


def test_autostart_records_session_names(ship):
    ship.ctl("autostart", "--json")
    record = ship.json("/var/lib/shipshape/autostart.json")
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
