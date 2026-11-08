"""ship-ctl status, apply, set, version, logs and the JSON envelope (contracts/ship-ctl.md)."""

from __future__ import annotations

import json
import tomllib

import pytest
from ship_harness import BOOT_ID, assert_valid


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
        ({"claude": {"auth_garbage": True}}, "needs-sign-in"),
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


@pytest.mark.parametrize(
    "value,expected",
    [
        ("on", {"remoteControlAtStartup": True}),
        # Phase 0: startup false alone lets resumed sessions reconnect, so off must
        # also disable Remote Control. blocked is the older name for the same thing.
        ("off", {"remoteControlAtStartup": False, "disableRemoteControl": True}),
        ("blocked", {"remoteControlAtStartup": False, "disableRemoteControl": True}),
    ],
)
def test_remote_control_managed_settings(ship, value, expected):
    rc, doc, _ = ship.ctl("set", f"claude.remote_control={value}", "--json")
    assert rc == 0, doc
    managed = ship.json("/etc/claude-code/managed-settings.json")
    got = {k: v for k, v in managed.items() if "emoteControl" in k}
    assert got == expected
    # The ship starts with "on": only a real change warns that sessions must restart.
    assert any("restart" in m for m in doc["messages"]) == (value != "on")


def test_remote_control_off_to_blocked_is_no_change(ship):
    ship.ctl("set", "claude.remote_control=off", "--json")
    before = ship.p("/etc/claude-code/managed-settings.json").read_bytes()
    rc, doc, _ = ship.ctl("set", "claude.remote_control=blocked", "--json")
    assert rc == 0, doc
    assert ship.p("/etc/claude-code/managed-settings.json").read_bytes() == before
    assert not any("restart" in m for m in doc["messages"])


def test_session_prefix_change_says_running_sessions_keep_names(ship):
    rc, doc, _ = ship.ctl("set", "claude.session_prefix=lab", "--json")
    assert rc == 0, doc
    assert any("/rename" in m for m in doc["messages"])
    managed = ship.json("/etc/claude-code/managed-settings.json")
    assert managed["env"]["CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX"] == "lab"
    rc, doc, _ = ship.ctl("set", "claude.session_prefix=lab", "--json")
    assert rc == 0 and not any("/rename" in m for m in doc["messages"])
    rc, doc, _ = ship.ctl("set", "agent.sudo=true", "--json")
    assert not any("/rename" in m for m in doc["messages"])


def test_set_adds_and_removes_an_autostart_agent(ship):
    rc, doc, _ = ship.ctl("set", "autostart.add=extra:~/work", "autostart.add=docs:~/d:plan",
                          "--json")
    assert rc == 0, doc["error"]
    toml = tomllib.loads(ship.p("/etc/shipshape/ship.toml").read_text())
    assert {"name": "extra", "cwd": "~/work"} in toml["autostart"]
    assert {"name": "docs", "cwd": "~/d", "mode": "plan"} in toml["autostart"]
    rc, doc, _ = ship.ctl("set", "autostart.add=extra:~/other", "--json")
    assert rc != 0 and "already exists" in doc["error"]["message"]
    rc, doc, _ = ship.ctl("set", "autostart.remove=docs", "--json")
    assert rc == 0, doc
    toml = tomllib.loads(ship.p("/etc/shipshape/ship.toml").read_text())
    assert "docs" not in [e["name"] for e in toml["autostart"]]
    rc, doc, _ = ship.ctl("set", "autostart.remove=nobody", "--json")
    assert rc != 0 and "no autostart agent" in doc["error"]["message"]
    rc, doc, _ = ship.ctl("set", "autostart.add=bad:relative/path", "--json")
    assert rc == 4  # the schema refuses a cwd that is not ~/ or /


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
        "_SYSTEMD_USER_UNIT=herdr.service",  # plus _UID=<agent uid> on a Ship
    ] in ship.calls("journalctl")


def test_boot_id_fixture_matches(ship):
    assert ship.p("/proc/sys/kernel/random/boot_id").read_text().strip() == BOOT_ID


def test_unreadable_claude_status_is_an_error(ship):
    ship.set(claude={"auth_garbage": True})
    _, doc, _ = ship.ctl("status", "--json")
    assert doc["auth"]["claude"]["state"] == "error"
    assert "claude auth status: exit 0, unreadable output" in doc["errors"]


def test_timed_out_probe(ship):
    ship.set(claude={"hang": True})
    ship.env["SHIPSHAPE_PROBE_TIMEOUT"] = "1"
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert_valid("ship-status", doc)
    assert doc["auth"]["claude"]["state"] == "unknown"
    assert "claude auth status: timed out" in doc["errors"]


def test_apply_fixes_a_mode_only_change(ship):
    ship.ctl("apply", "--json")
    ship.p("/etc/claude-code/managed-settings.json").chmod(0o600)
    rc, doc, _ = ship.ctl("apply", "--json")
    assert doc["changed"] == ["/etc/claude-code/managed-settings.json"]
    assert oct(ship.p("/etc/claude-code/managed-settings.json").stat().st_mode & 0o777) == "0o644"


def test_adc_probe_never_captures_the_token():
    """The ADC check's output is an access token: it must go to /dev/null."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    from shipctl.runner import Result
    from shipctl.status import probe_adc

    seen = {}

    class Runner:
        def run(self, argv, **kwargs):
            seen.update(argv=argv, **kwargs)
            return Result(0)

    class Ctx:
        runner = Runner()

    assert probe_adc(Ctx())[0]["state"] == "ok"
    assert seen["discard"] is True
    assert seen["argv"][-1] == "print-access-token"
    # gcloud copies command output into its log files: the token must not land there.
    assert seen["env"] == {"CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "1"}

    from shipctl.status import probe_gcloud_auth

    seen.clear()
    probe_gcloud_auth(Ctx())
    assert seen["env"] == {"CLOUDSDK_CORE_DISABLE_FILE_LOGGING": "1"}


# --- the owner's login --------------------------------------------------------------
OWNER_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGVvd25lcmtleWV4YW1wbGVvd25lcmtl you"


def test_an_owner_login_with_sudo_its_keys_and_certificates(ship):
    rc, doc, _ = ship.ctl("set", "owner.name=human", f"owner.keys={OWNER_KEY}", "--json")
    assert rc == 0, doc
    assert ["useradd", "--create-home", "--shell", "/bin/bash", "--comment",
            "ShipShape owner", "human"] in ship.calls("useradd")
    sudoers = ship.p("/etc/sudoers.d/90-shipshape-owner")
    assert sudoers.read_text() == "human ALL=(ALL:ALL) NOPASSWD: ALL\n"
    assert oct(sudoers.stat().st_mode & 0o777) == "0o440"
    assert ship.p("/etc/ssh/shipshape/keys/human").read_text() == OWNER_KEY + "\n"
    principals = ship.p("/etc/ssh/shipshape/principals")
    # The owner's own principals: a certificate for the agent's login never opens it.
    assert (principals / "human").read_text() == (
        "owner:all\nowner:tag:work\nowner:ship:ship-01\n")
    assert (principals / "agent").read_text() == (
        "agent:all\nagent:tag:work\nagent:ship:ship-01\n")
    dropin = next(ship.p("/etc/ssh/sshd_config.d").glob("*shipshape*")).read_text()
    assert "AuthorizedKeysFile .ssh/authorized_keys /etc/ssh/shipshape/keys/%u" in dropin
    assert "\nAllowUsers human\n" in dropin  # beside 10-shipshape.conf's fleet and agent
    assert not ship.p("/etc/sudoers.d/91-shipshape-agent").exists()  # agent: still no sudo
    # Applied again: the user is not made twice.
    ship.ctl("apply", "--json")
    assert len(ship.calls("useradd")) == 1


@pytest.mark.parametrize("name", ["agent", "fleet", "root", "nobody"])
def test_the_owner_cannot_be_a_shipshape_login(ship, name):
    rc, doc, _ = ship.ctl("set", f"owner.name={name}", "--json")
    assert rc == 4 and not ship.calls("useradd")
    assert not ship.p("/etc/sudoers.d/90-shipshape-owner").exists()


def test_the_owner_cannot_be_a_system_account(ship):
    passwd = ship.p("/etc/passwd")
    passwd.write_text("daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n")
    rc, doc, _ = ship.ctl("set", "owner.name=daemon", "--json")
    assert rc == 4 and "system account" in doc["error"]["message"]
    assert not ship.p("/etc/sudoers.d/90-shipshape-owner").exists()


def test_grow_root_fills_the_disk_once(ship):
    rc, doc, _ = ship.ctl("grow-root", "--json")
    assert rc == 0, doc
    assert_valid("ship-ctl-result", doc)
    assert doc["data"] == {"device": "/dev/sda1", "grown": True}
    assert ship.calls("growpart") == [["growpart", "/dev/sda", "1"]]
    assert ship.scenario()["disk"]["resized"] == ["/dev/sda1"]
    rc, doc, _ = ship.ctl("grow-root", "--json")  # already full: not an error
    assert rc == 0 and doc["data"]["grown"] is False and doc["changed"] == []


def test_the_owner_cannot_be_an_account_above_the_user_range(ship):
    ship.p("/etc/passwd").write_text("svc:x:65000:65000::/nonexistent:/usr/sbin/nologin\n")
    rc, doc, _ = ship.ctl("set", "owner.name=svc", "--json")
    assert rc == 4 and not ship.p("/etc/sudoers.d/90-shipshape-owner").exists()


AT_OPENSSH = "@" + "openssh.com"  # a key type's suffix (written apart: not an address)


@pytest.mark.parametrize("key", [
    "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTY= you",
    f"sk-ssh-ed25519{AT_OPENSSH} AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29t you",
    f"sk-ecdsa-sha2-nistp256{AT_OPENSSH} AAAAInNrLWVjZHNhLXNoYTItbmlzdHAyNTZAb3BlbnNzaC5jb20= you",
])
def test_the_owner_takes_any_key_type_fleetledger_takes(ship, key):
    rc, doc, _ = ship.ctl("set", "owner.name=human", f"owner.keys={key}", "--json")
    assert rc == 0, doc
    assert ship.p("/etc/ssh/shipshape/keys/human").read_text() == key + "\n"


def test_grow_root_grows_a_filesystem_its_partition_outgrew(ship):
    ship.set(disk={"full": True, "fs_behind": True})  # growpart ran, resize2fs did not
    rc, doc, _ = ship.ctl("grow-root", "--json")
    assert rc == 0 and doc["data"]["grown"] is True
    assert ship.scenario()["disk"]["resized"] == ["/dev/sda1"]


def test_a_ship_without_tags_still_gets_the_owners_principals(ship):
    assert ship.ctl("set", "ship.tags=", "--json")[0] == 0
    rc, doc, _ = ship.ctl("set", "owner.name=human", f"owner.keys={OWNER_KEY}", "--json")
    assert rc == 0, doc
    principals = ship.p("/etc/ssh/shipshape/principals")
    assert (principals / "human").read_text() == "owner:all\nowner:ship:ship-01\n"


def test_status_reports_the_settings_set_last_applied(ship):
    rc, doc, _ = ship.ctl("status", "--json")
    assert_valid("ship-status", doc)
    assert doc["settings"] == {  # BASIC_TOML, with every default filled in
        "ship": {"tags": ["work"], "timezone": "UTC"},
        "claude": {"remote_control": "on", "default_mode": "auto", "allow_bypass": True,
                   "update_channel": "latest", "session_prefix": "ship-01"},
        "agent": {"sudo": False},
        "network": {"lan_guard": False, "lan_guard_allow": [], "ssh_listen": "any"},
        "updates": {"auto_reboot": False, "reboot_time": "04:00"},
        "github": {"scopes": []},
        "owner": None,
        "autostart": [{"name": "main", "cwd": "~/work/main"},
                      {"name": "grinder", "cwd": "~/work/grinder",
                       "mode": "bypassPermissions"}],
    }
    rc, _, out = ship.ctl("set", "claude.remote_control=off", "updates.auto_reboot=true",
                    "updates.reboot_time=03:30", "ship.tags=work,lab", "agent.sudo=true",
                    "network.lan_guard=true", "network.lan_guard_allow=192.0.2.0/24",
                    "ship.timezone=Europe/Berlin", "claude.default_mode=plan",
                    "claude.update_channel=stable",
                    "owner.name=human", f"owner.keys={OWNER_KEY}",
                    "--json")
    assert rc == 0, out
    rc, doc, _ = ship.ctl("status", "--json")
    assert_valid("ship-status", doc)
    s = doc["settings"]
    assert s["ship"] == {"tags": ["work", "lab"], "timezone": "Europe/Berlin"}
    assert s["claude"] == {"remote_control": "off", "default_mode": "plan",
                           "allow_bypass": True, "update_channel": "stable",
                           "session_prefix": "ship-01"}
    assert s["agent"] == {"sudo": True}
    assert s["network"] == {"lan_guard": True, "lan_guard_allow": ["192.0.2.0/24"],
                            "ssh_listen": "any"}
    assert s["updates"] == {"auto_reboot": True, "reboot_time": "03:30"}
    assert s["owner"] == "human"
    assert [a["name"] for a in s["autostart"]] == ["main", "grinder"]
    # bypass may only go off once no agent starts in bypassPermissions
    toml = ship.p("/etc/shipshape/ship.toml")
    toml.write_text(toml.read_text().replace('mode = "bypassPermissions"\n', ""))
    rc, _, out = ship.ctl("set", "claude.allow_bypass=false", "claude.session_prefix=lab",
                          "--json")
    assert rc == 0, out
    s = ship.ctl("status", "--json")[1]["settings"]
    assert s["claude"]["allow_bypass"] is False and s["claude"]["session_prefix"] == "lab"
    assert s["autostart"][1] == {"name": "grinder", "cwd": "~/work/grinder"}
