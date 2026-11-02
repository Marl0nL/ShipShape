"""ship-ctl overlay (phase 5): join, leave, status, and the sshd listen policy.

A fake `tailscale` and `sshd` stand in for the real ones (fakes/fake.py). The auth
key is a secret: every test that handles one checks it never reaches a command
line, a result, a log or a file that outlives the join.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ship_harness import FAKE, assert_valid

KEY = "tskey-auth-kExample1CNTRL-0123456789abcdefSECRETPART"
JOIN = ("overlay", "join", "--hostname", "ship-01", "--tag", "tag:ship", "--json")


@pytest.fixture
def ov(ship):
    bindir = Path(ship.env["PATH"].split(":")[0])
    (bindir / "tailscale").symlink_to(FAKE)  # sshd is faked for every Ship test
    return ship


def everything_written(ship) -> str:
    """Every file under the sandbox root plus every recorded command line."""
    parts = [(ship.fake_dir / "calls.jsonl").read_text()]
    for path in ship.root.rglob("*"):
        if path.is_file():
            parts.append(path.read_text(errors="replace"))
    return "\n".join(parts)


def test_join(ov):
    code, doc, out = ov.ctl(*JOIN, input=KEY + "\n")
    assert code == 0, out
    assert_valid("ship-ctl-result", doc)
    overlay = doc["data"]["overlay"]
    assert overlay["joined"] and overlay["state"] == "running"
    assert overlay["name"] == "ship-01.example-tailnet.ts.net"
    assert overlay["addresses"] == ["192.0.2.10", "fd7a:115c:a1e0::a"]
    assert overlay["tags"] == ["tag:ship"] and overlay["ssh_listen"] == "any"
    (up,) = [c for c in ov.calls("tailscale") if c[1] == "up"]
    assert "--ssh=false" in up and "--accept-dns=false" in up and "--accept-routes=false" in up
    assert "--hostname=ship-01" in up and "--advertise-tags=tag:ship" in up and "--reset" in up
    assert ["systemctl", "enable", "--now", "tailscaled"] in ov.calls("systemctl")
    dropin = ov.p("/etc/systemd/system/tailscaled.service.d/50-shipshape.conf").read_text()
    assert "TS_NO_LOGS_NO_SUPPORT=true" in dropin


def test_the_key_goes_through_a_file_that_is_removed(ov):
    code, doc, out = ov.ctl(*JOIN, input=KEY + "\n")
    assert code == 0, out
    (up,) = [c for c in ov.calls("tailscale") if c[1] == "up"]
    key_arg = next(a for a in up if a.startswith("--auth-key="))
    assert key_arg.startswith("--auth-key=file:")
    assert ov.scenario()["tailscale"]["key_seen"] == KEY  # it was there while up ran
    key_file = Path(key_arg.removeprefix("--auth-key=file:"))
    assert not key_file.exists()
    assert oct(key_file.parent.stat().st_mode & 0o777) == "0o700"
    # Not on any command line, in the result, in the state file or anywhere else.
    assert KEY not in everything_written(ov).replace(f'"key_seen": "{KEY}"', "")
    assert KEY not in out
    state = ov.p("/var/lib/shipshape/overlay.json")
    assert oct(state.stat().st_mode & 0o777) == "0o600"


def test_a_failed_join_reports_without_the_key(ov):
    ov.set(tailscale={"up_fails": True})
    code, doc, out = ov.ctl(*JOIN, input=KEY + "\n")
    assert code == 1 and doc["error"]["code"] == "overlay-failed"
    assert KEY not in out and "SECRETPART" not in out  # tailscale echoed it; we did not
    assert "[key]" in doc["error"]["message"] or "redacted" in doc["error"]["message"]
    assert list((ov.root / "run/shipshape/overlay").iterdir()) == []
    assert not ov.p("/var/lib/shipshape/overlay.json").exists()


@pytest.mark.parametrize("stdin", ["", "short", KEY + " " + KEY, "x" * 600, "tskey-auth-ü" * 3])
def test_join_refuses_anything_but_one_key(ov, stdin):
    code, doc, out = ov.ctl(*JOIN, input=stdin)
    assert code == 2 and doc["error"]["code"] == "usage"
    assert not [c for c in ov.calls("tailscale") if c[1] == "up"]
    if len(stdin) > 8:
        assert stdin[:20] not in doc["error"]["message"]


@pytest.mark.parametrize("args", [
    ("overlay", "join", "--tag", "tag:ship", "--json"),
    ("overlay", "join", "--hostname", "ship-01", "--json"),
    ("overlay", "join", "--hostname", "ship-01", "--tag", "ship", "--json"),
    ("overlay", "join", "--hostname", "Ship 01", "--tag", "tag:ship", "--json"),
    ("overlay", "join", "--hostname", "ship-01", "--tag", "tag:ship",
     "--login-server", "http://plain.example.com", "--json"),
])
def test_join_arguments_are_checked(ov, args):
    code, doc, out = ov.ctl(*args, input=KEY)
    assert code == 2 and doc["error"]["code"] == "usage"


def test_join_with_a_login_server(ov):
    code, doc, out = ov.ctl(*JOIN[:-1], "--login-server", "https://hs.example.com", "--json",
                            input=KEY)
    assert code == 0, out
    (up,) = [c for c in ov.calls("tailscale") if c[1] == "up"]
    assert "--login-server=https://hs.example.com" in up


def test_ssh_listen_overlay_needs_the_overlay(ov):
    before = ov.p("/etc/shipshape/ship.toml").read_text()
    code, doc, out = ov.ctl("set", "network.ssh_listen=overlay", "--json")
    assert code == 1 and doc["error"]["code"] == "overlay-not-joined"
    assert ov.p("/etc/shipshape/ship.toml").read_text() == before
    assert not ov.p("/etc/ssh/sshd_config.d/20-shipshape-listen.conf").exists()


def test_ssh_listen_overlay_after_joining(ov):
    assert ov.ctl(*JOIN, input=KEY)[0] == 0
    code, doc, out = ov.ctl("set", "network.ssh_listen=overlay", "--json")
    assert code == 0, out
    conf = ov.p("/etc/ssh/sshd_config.d/20-shipshape-listen.conf").read_text()
    lines = conf.strip().splitlines()
    assert lines[-3] == ("Match LocalAddress *,!127.0.0.1,!::1,!192.0.2.10,!fd7a:115c:a1e0::a")
    assert lines[-2].strip() == "DenyUsers *"
    assert lines[-1] == "Match all"  # nothing after this file is pulled into the block
    code, status, _ = ov.ctl("status", "--json")
    assert_valid("ship-status", status)
    assert status["overlay"]["ssh_listen"] == "overlay"


def test_leave_listens_everywhere_first(ov):
    assert ov.ctl(*JOIN, input=KEY)[0] == 0
    assert ov.ctl("set", "network.ssh_listen=overlay", "--json")[0] == 0
    code, doc, out = ov.ctl("overlay", "leave", "--json")
    assert code == 0, out
    assert 'ssh_listen = "any"' in ov.p("/etc/shipshape/ship.toml").read_text()
    assert not ov.p("/etc/ssh/sshd_config.d/20-shipshape-listen.conf").exists()
    calls = ov.calls()
    logout = calls.index(["tailscale", "logout"])
    assert ["systemctl", "disable", "--now", "tailscaled"] in calls[logout:]
    assert not ov.p("/var/lib/shipshape/overlay.json").exists()
    assert doc["data"]["overlay"]["joined"] is False


def test_leave_when_never_joined_changes_nothing(ship):
    code, doc, out = ship.ctl("overlay", "leave", "--json")
    assert code == 0 and doc["changed"] == []
    assert "not joined" in doc["messages"][0]


def test_status_without_the_overlay_is_null(ship):
    code, doc, out = ship.ctl("status", "--json")
    assert code == 0 and doc["overlay"] is None
    assert_valid("ship-status", doc)


def test_status_flags_a_joined_overlay_that_is_down(ov):
    assert ov.ctl(*JOIN, input=KEY)[0] == 0
    ov.set(tailscale={**ov.scenario()["tailscale"], "backend": "Stopped"})
    code, doc, out = ov.ctl("status", "--json")
    assert_valid("ship-status", doc)
    assert doc["overlay"]["state"] == "stopped"
    assert any(a["code"] == "overlay-down" for a in doc["attention"])
    ov.set(tailscale={**ov.scenario()["tailscale"], "daemon": False})
    code, doc, out = ov.ctl("status", "--json")
    assert doc["overlay"]["state"] == "off"


def test_apply_never_locks_the_ship_out(ov):
    """ssh_listen = overlay written by hand (set --file) without an overlay: no deny rule."""
    toml = ov.p("/etc/shipshape/ship.toml").read_text() + '\n[network]\nssh_listen = "overlay"\n'
    ov.p("/etc/shipshape/ship.toml").write_text(toml)
    code, doc, out = ov.ctl("apply", "--json")
    assert code == 0
    assert not ov.p("/etc/ssh/sshd_config.d/20-shipshape-listen.conf").exists()
    assert any("has not joined" in m for m in doc["messages"])


def test_overlay_status_command(ov):
    code, doc, out = ov.ctl("overlay", "status", "--json")
    assert code == 0 and doc["data"]["overlay"]["state"] == "off"
    assert ov.ctl(*JOIN, input=KEY)[0] == 0
    code, doc, out = ov.ctl("overlay", "status", "--json")
    assert doc["data"]["overlay"]["state"] == "running"
    assert KEY not in json.dumps(doc)
