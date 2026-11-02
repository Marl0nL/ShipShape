"""ship-ctl status, apply, set, version, logs and the JSON envelope (contracts/ship-ctl.md)."""

from __future__ import annotations

import json
import tomllib

import pytest
from conftest import BOOT_ID, assert_valid


# --- status ---------------------------------------------------------------------
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
    assert doc["herdr"]["last_resume"]["method"] == "hidden-client"
    assert {a["code"] for a in doc["attention"]} == {"agent-waiting"}
    assert doc["host"]["mem_used_pct"] == 50.0 and doc["host"]["uptime_s"] == 4000


def test_status_never_contains_a_token(ship):
    _, _, text = ship.ctl("status", "--json")
    assert "ya29" not in text and "gho_" not in text


@pytest.mark.parametrize(
    "change,state",
    [
        ({"claude": {"logged_in": False}}, "needs-sign-in"),
        ({"cloud_init": "running"}, "provisioning"),
        ({"herdr": {"server": False}}, "unknown"),
        ({}, "ready"),
    ],
)
def test_status_states(ship, change, state):
    ship.set(**change)
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert_valid("ship-status", doc)
    assert doc["state"] == state


def test_status_needs_sign_in_attention(ship):
    ship.set(claude={"logged_in": False}, gh={"login": None}, gcloud={})
    rc, doc, _ = ship.ctl("status", "--json")
    assert doc["state"] == "needs-sign-in"
    assert doc["auth"]["github"]["state"] == "missing"
    assert doc["auth"]["gcloud"]["state"] == "missing"
    assert {
        "code": "signin-missing",
        "message": "Claude Code is not signed in.",
        "subject": "claude",
    } in doc["attention"]


def test_status_provision_never_and_dead_run(ship):
    ship.p("/var/lib/shipshape/provision.json").unlink()
    _, doc, _ = ship.ctl("status", "--json")
    assert doc["provision"]["result"] == "never" and doc["state"] == "provisioning"
    # A "running" record from an earlier boot means that run died.
    ship.p("/var/lib/shipshape/provision.json").write_text(
        json.dumps(
            {"result": "running", "pid": 1, "boot_id": "other", "last_run": "2026-01-01T00:00:00Z"}
        )
    )
    _, doc, _ = ship.ctl("status", "--json")
    assert doc["provision"]["result"] == "failed"
    assert any(a["code"] == "provision-failed" for a in doc["attention"])


def test_status_missing_tools_fail_softly(ship):
    ship.set(claude={"installed": False})
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert_valid("ship-status", doc)
    assert doc["auth"]["claude"]["state"] == "unknown"
    assert doc["claude"]["version"] is None
    assert any("claude auth status" in e for e in doc["errors"])


def test_status_reports_failed_units_and_reboot(ship):
    ship.set(failed_units=["ship-status.service"], failed_user=["herdr.service"])
    ship.p("/var/run").mkdir(parents=True)
    ship.p("/var/run/reboot-required").write_text("")
    _, doc, _ = ship.ctl("status", "--json")
    codes = [(a["code"], a.get("subject")) for a in doc["attention"]]
    assert ("unit-failed", "ship-status.service") in codes
    assert ("unit-failed", "herdr.service") in codes
    assert ("reboot-required", None) in codes


def test_status_write_cache(ship):
    rc, doc, _ = ship.ctl("status", "--json", "--write", "/run/shipshape/status.json")
    assert rc == 0
    cached = ship.json("/run/shipshape/status.json")
    assert cached["schema"] == "shipshape.ship-status/1"
    assert oct(ship.p("/run/shipshape/status.json").stat().st_mode & 0o777) == "0o644"


def test_status_bad_ship_toml_gives_result_document(ship):
    ship.write_toml("schema = 1\n[ship]\nname = 'Bad Name'\n")
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 4
    assert_valid("ship-ctl-result", doc)
    assert doc["command"] == "status" and doc["error"]["code"] == "invalid-ship-toml"


# --- apply ----------------------------------------------------------------------
def test_apply_writes_settings_and_is_idempotent(ship):
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0, doc
    assert_valid("ship-ctl-result", doc)
    assert "/etc/claude-code/managed-settings.json" in doc["changed"]
    managed = ship.json("/etc/claude-code/managed-settings.json")
    assert managed == {
        "remoteControlAtStartup": True,
        "permissions": {"defaultMode": "auto"},
        "autoUpdatesChannel": "latest",
        "minimumVersion": "2.1.284",
        "env": {"CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX": "ship-01"},
    }
    user = ship.json("/home/agent/.claude/settings.json")
    assert user["skipDangerousModePermissionPrompt"] is True
    for event in ("UserPromptSubmit", "SessionStart"):
        cmds = [h["command"] for e in user["hooks"][event] for h in e["hooks"]]
        assert cmds == ["/opt/shipshape/hooks/mode-ledger"]
    claude_json = ship.json("/home/agent/.claude.json")
    assert claude_json["projects"]["/home/agent/work/main"]["hasTrustDialogAccepted"] is True
    assert claude_json["projects"]["/home/agent/work/grinder"]["hasTrustDialogAccepted"] is True
    herdr = tomllib.loads(ship.p("/home/agent/.config/herdr/config.toml").read_text())
    assert herdr == {
        "onboarding": False,
        "update": {"version_check": False},
        "session": {"resume_agents_on_restore": True},
    }
    assert not ship.p("/etc/sudoers.d/91-shipshape-agent").exists()
    avahi = ship.p("/etc/avahi/services/shipshape.service").read_text()
    assert "<name>ship-01</name>" in avahi and "<type>_shipshape._tcp</type>" in avahi
    assert "<port>22</port>" in avahi
    for txt in ("schema=1", "name=ship-01", "id=7c9e6679-7425-40de-944b-e07fc1f90ae7"):
        assert f"<txt-record>{txt}</txt-record>" in avahi

    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0 and doc["changed"] == []


def test_apply_keeps_other_keys(ship):
    settings = ship.p("/home/agent/.claude/settings.json")
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "Stop": [{"hooks": [{"type": "command", "command": "herdr-hook"}]}],
                    "SessionStart": [{"hooks": [{"type": "command", "command": "ship-axi hook"}]}],
                },
            }
        )
    )
    ship.p("/home/agent/.claude.json").write_text(
        json.dumps({"numStartups": 3, "projects": {"/x": {"allowedTools": ["a"]}}})
    )
    ship.ctl("apply", "--json")
    user = ship.json("/home/agent/.claude/settings.json")
    assert user["theme"] == "dark"
    assert user["hooks"]["Stop"][0]["hooks"][0]["command"] == "herdr-hook"
    assert len(user["hooks"]["SessionStart"]) == 2
    claude_json = ship.json("/home/agent/.claude.json")
    assert claude_json["numStartups"] == 3
    assert claude_json["projects"]["/x"] == {"allowedTools": ["a"]}


def test_apply_dry_run_writes_nothing(ship):
    rc, doc, _ = ship.ctl("apply", "--dry-run", "--json")
    assert rc == 0 and doc["changed"]
    assert not ship.p("/etc/claude-code/managed-settings.json").exists()


def test_apply_refuses_corrupt_agent_json(ship):
    ship.p("/home/agent/.claude.json").write_text("{not json")
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 1 and doc["error"]["code"] == "internal"
    assert ship.p("/home/agent/.claude.json").read_text() == "{not json"


# --- set ------------------------------------------------------------------------
def test_set_changes_and_applies(ship):
    rc, doc, _ = ship.ctl(
        "set", "claude.remote_control=blocked", "agent.sudo=true", "ship.tags=work,lab", "--json"
    )
    assert rc == 0, doc
    assert_valid("ship-ctl-result", doc)
    toml = tomllib.loads(ship.p("/etc/shipshape/ship.toml").read_text())
    assert toml["claude"]["remote_control"] == "blocked"
    assert toml["ship"]["tags"] == ["work", "lab"]
    managed = ship.json("/etc/claude-code/managed-settings.json")
    assert managed["disableRemoteControl"] is True and managed["remoteControlAtStartup"] is False
    sudoers = ship.p("/etc/sudoers.d/91-shipshape-agent")
    assert sudoers.read_text() == "agent ALL=(ALL:ALL) NOPASSWD: ALL\n"
    assert oct(sudoers.stat().st_mode & 0o777) == "0o440"
    rc, _, _ = ship.ctl("set", "agent.sudo=false", "--json")
    assert rc == 0 and not sudoers.exists()


def test_set_disallowing_bypass(ship):
    ship.write_toml(
        'schema = 1\n[ship]\nname = "ship-01"\nid = "7c9e6679-7425-40de-944b-e07fc1f90ae7"\n'
    )
    ship.ctl("set", "claude.allow_bypass=true", "--json")
    assert ship.json("/home/agent/.claude/settings.json")["skipDangerousModePermissionPrompt"]
    ship.ctl("set", "claude.allow_bypass=false", "--json")
    assert "skipDangerousModePermissionPrompt" not in ship.json("/home/agent/.claude/settings.json")
    managed = ship.json("/etc/claude-code/managed-settings.json")
    assert managed["permissions"]["disableBypassPermissionsMode"] == "disable"


def test_set_invalid_changes_nothing(ship):
    before = ship.p("/etc/shipshape/ship.toml").read_text()
    # grinder runs in bypass, so bypass cannot be switched off.
    rc, doc, _ = ship.ctl("set", "claude.allow_bypass=false", "--json")
    assert rc == 4 and doc["error"]["code"] == "invalid-ship-toml"
    assert ship.p("/etc/shipshape/ship.toml").read_text() == before
    rc, doc, _ = ship.ctl("set", "claude.nope=1", "--json")
    assert rc == 2 and doc["error"]["code"] == "usage"
    rc, doc, _ = ship.ctl("set", "agent.sudo=maybe", "--json")
    assert rc == 2


def test_set_file_replaces_whole_toml(ship):
    new = ship.p("/etc/shipshape/ship.toml").read_text().replace('name = "grinder"', 'name = "g2"')
    rc, doc, _ = ship.ctl("set", "--file", "-", "--json", input=new)
    assert rc == 0, doc
    names = [
        a["name"]
        for a in tomllib.loads(ship.p("/etc/shipshape/ship.toml").read_text())["autostart"]
    ]
    assert names == ["main", "g2"]
    other = new.replace('name = "ship-01"', 'name = "ship-09"')
    rc, doc, _ = ship.ctl("set", "--file", "-", "--json", input=other)
    assert rc == 4
    dupes = new.replace('name = "g2"', 'name = "main"')
    rc, doc, _ = ship.ctl("set", "--file", "-", "--json", input=dupes)
    assert rc == 4 and "duplicate" in doc["error"]["message"]


# --- envelope, version, logs -----------------------------------------------------
def test_version_needs_no_root_and_lists_contracts(ship):
    rc, doc, _ = ship.ctl("version", "--json")
    assert rc == 0
    assert_valid("ship-ctl-result", doc)
    assert doc["data"]["contracts"]["ship-status"] == 1
    assert doc["data"]["tooling"] == "0.1.0"


def test_usage_errors_are_json(ship):
    rc, doc, _ = ship.ctl("tools", "explode", "--json")
    assert rc == 2
    assert_valid("ship-ctl-result", doc)
    assert doc["command"] == "tools" and doc["error"]["code"] == "usage"


def test_not_root_outside_sandbox(ship):
    import subprocess
    import sys

    env = dict(ship.env)
    del env["SHIPSHAPE_ROOT"]
    proc = subprocess.run(
        [sys.executable, "-I", str(ship.bundle / "bin/ship-ctl"), "status", "--json"],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode == 0:
        pytest.skip("running as root")
    doc = json.loads(proc.stdout)
    assert proc.returncode == 3
    assert_valid("ship-ctl-result", doc)
    assert doc["error"]["code"] == "not-root"


def test_logs(ship):
    ship.p("/var/log/shipshape").mkdir(parents=True)
    ship.p("/var/log/shipshape/provision.log").write_text("".join(f"line {i}\n" for i in range(9)))
    rc, doc, _ = ship.ctl("logs", "provision", "--lines", "3", "--json")
    assert rc == 0
    assert_valid("ship-ctl-result", doc)
    assert doc["data"]["lines"] == ["line 6", "line 7", "line 8"]
    ship.set(journal=["Jan 01 herdr[1]: started"])
    rc, doc, _ = ship.ctl("logs", "herdr", "--json")
    assert doc["data"]["lines"] == ["Jan 01 herdr[1]: started"]
    assert [
        "journalctl",
        "--no-pager",
        "-o",
        "short-iso",
        "-n",
        "200",
        "--user-unit",
        "herdr.service",
    ] in ship.calls("journalctl")


def test_boot_id_fixture_matches(ship):
    assert ship.p("/proc/sys/kernel/random/boot_id").read_text().strip() == BOOT_ID
