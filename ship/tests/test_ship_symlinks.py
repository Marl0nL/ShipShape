"""ship-ctl runs as root but writes into /home/agent, which the agent controls.

A symlink the agent planted must never let ship-ctl read, overwrite or take over a
file outside the home. Each test plants one, runs the command that used to follow
it, and checks the target is untouched.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ship" / "lib"))

from shipctl.homefs import HomeFS, UnsafePath  # noqa: E402

SKILL = """\
schema = 1
id = "example-skill"
description = "A skill folder"
kind = "skill-dir"
skill_dir = "skills/example"
check = "true"
"""


@pytest.fixture
def victim(tmp_path) -> Path:
    """A file outside the home standing in for /etc/passwd or a root-only file."""
    path = tmp_path / "outside" / "victim"
    path.parent.mkdir()
    path.write_text("ROOT-ONLY CONTENT\n")
    path.chmod(0o600)
    return path


def untouched(victim: Path) -> bool:
    return (
        victim.read_text() == "ROOT-ONLY CONTENT\n"
        and oct(victim.stat().st_mode & 0o777) == "0o600"
    )


def test_skill_file_symlink_is_replaced_not_followed(ship, victim):
    """Repro A: ~/.claude/skills/<id>/SKILL.md -> /etc/passwd."""
    (ship.bundle / "tools/example-skill.toml").write_text(SKILL)
    (ship.bundle / "skills/example").mkdir(parents=True)
    (ship.bundle / "skills/example/SKILL.md").write_text("SKILL TEXT\n")
    dest = ship.p("/home/agent/.claude/skills/example-skill")
    dest.mkdir(parents=True)
    (dest / "SKILL.md").symlink_to(victim)
    (dest / "extra").symlink_to(victim.parent)
    rc, doc, out = ship.ctl("tools", "run", "example-skill", "--json")
    assert rc == 0, out
    assert untouched(victim)
    assert not (dest / "SKILL.md").is_symlink()
    assert (dest / "SKILL.md").read_text() == "SKILL TEXT\n"
    assert not (dest / "extra").exists() and victim.exists()


def test_skill_folder_symlink_is_refused(ship, victim):
    (ship.bundle / "tools/example-skill.toml").write_text(SKILL)
    (ship.bundle / "skills/example").mkdir(parents=True)
    (ship.bundle / "skills/example/SKILL.md").write_text("SKILL TEXT\n")
    ship.p("/home/agent/.claude").mkdir()
    ship.p("/home/agent/.claude/skills").symlink_to(victim.parent)
    rc, doc, _ = ship.ctl("tools", "run", "example-skill", "--json")
    assert rc == 1 and doc["error"]["code"] == "tool-failed"
    assert any("symlink" in m for m in doc["messages"])
    assert sorted(p.name for p in victim.parent.iterdir()) == ["victim"]


def test_apply_does_not_write_through_a_symlinked_folder(ship, tmp_path):
    """Repro B: ~/.config/herdr -> /etc/apt/apt.conf.d."""
    apt = tmp_path / "apt.conf.d"
    apt.mkdir()
    ship.p("/home/agent/.config").mkdir()
    ship.p("/home/agent/.config/herdr").symlink_to(apt)
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    assert list(apt.iterdir()) == []
    assert any("~/.config/herdr: a symlink" in m for m in doc["messages"])
    assert "/home/agent/.claude/settings.json" in doc["changed"]  # the rest still applied


def test_apply_does_not_read_through_a_symlink(ship, victim):
    """Repro C: ~/.claude.json -> a root-only file."""
    victim.write_text('{"token": "SECRET-VALUE"}')
    victim.chmod(0o600)
    ship.p("/home/agent/.claude.json").symlink_to(victim)
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0
    assert victim.read_text() == '{"token": "SECRET-VALUE"}'
    assert ship.p("/home/agent/.claude.json").is_symlink()  # left alone, reported
    assert any(".claude.json: a symlink" in m for m in doc["messages"])
    assert "SECRET" not in json.dumps(doc)


def test_settings_file_symlink_is_left_alone(ship, victim):
    ship.p("/home/agent/.claude").mkdir()
    ship.p("/home/agent/.claude/settings.json").symlink_to(victim)
    rc, doc, _ = ship.ctl("apply", "--json")
    assert rc == 0 and untouched(victim)


def test_env_file_symlink(ship, victim):
    """The env file is written by tools run and read into every agent command."""
    victim.write_text("SECRET_KEY='from-root'\n")
    ship.p("/home/agent/.config/shipshape").mkdir(parents=True)
    ship.p("/home/agent/.config/shipshape/env").symlink_to(victim)
    rc, doc, _ = ship.ctl("tools", "run", "--json")
    assert victim.read_text() == "SECRET_KEY='from-root'\n"
    assert any("symlink" in m for m in doc["messages"])
    # And it is not read: no agent command gets the value.
    sys.path.insert(0, str(ship.bundle / "lib"))
    from shipctl.runner import read_env_file

    assert read_env_file(ship.p("/home/agent"), ".config/shipshape/env") == {}


def test_status_does_not_read_ledger_through_symlink(ship, tmp_path):
    other = tmp_path / "ledger.json"
    other.write_text(json.dumps({"sessions": {"s": {"mode": "bypassPermissions"}}}))
    ledger = ship.p("/home/agent/.local/state/shipshape/modes.json")
    ledger.symlink_to(other)
    ship.set(
        herdr={
            "server": True,
            "agents": [
                {
                    "agent": "claude",
                    "agent_status": "idle",
                    "pane_id": "w1:p1",
                    "workspace_id": "w1",
                    "tab_id": "t",
                    "terminal_id": "t",
                    "focused": False,
                    "revision": 1,
                    "agent_session": {"source": "x", "agent": "claude", "kind": "id", "value": "s"},
                }
            ],
        }
    )
    _, doc, _ = ship.ctl("status", "--json")
    assert doc["herdr"]["agents"][0]["mode"] is None


# --- HomeFS directly -----------------------------------------------------------------
def test_homefs_refuses_links_at_every_level(tmp_path, victim):
    home = tmp_path / "home"
    (home / "a").mkdir(parents=True)
    (home / "a/link").symlink_to(victim.parent)
    (home / "file-link").symlink_to(victim)
    fs = HomeFS(home)
    for rel in ("a/link/victim", "file-link"):
        with pytest.raises(UnsafePath):
            fs.read(rel)
        with pytest.raises(UnsafePath):
            fs.write(rel, "x")
    with pytest.raises(UnsafePath):
        fs.mkdirs("a/link/new")
    assert untouched(victim) and sorted(os.listdir(victim.parent)) == ["victim"]


def test_homefs_refuses_fifos(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    os.mkfifo(home / "pipe")
    with pytest.raises(UnsafePath):
        HomeFS(home).read("pipe")


def test_homefs_writes_atomically_and_reports_changes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    fs = HomeFS(home)
    assert fs.write("x/y/z.json", "1", 0o600, dir_mode=0o700) is True
    assert oct((home / "x").stat().st_mode & 0o777) == "0o700"
    assert oct((home / "x/y/z.json").stat().st_mode & 0o777) == "0o600"
    assert fs.write("x/y/z.json", "1", 0o600) is False
    assert fs.write("x/y/z.json", "1", 0o644) is True  # a mode-only change counts
    assert fs.write("x/y/z.json", "2", 0o600, keep_mode=True) is True
    assert oct((home / "x/y/z.json").stat().st_mode & 0o777) == "0o644"
    assert fs.write("x/y/z.json", "3", dry_run=True) is True
    assert fs.read("x/y/z.json") == b"2"
    assert fs.read("x/missing/z") is None
    assert [p.name for p in (home / "x/y").iterdir()] == ["z.json"]  # no temp files left
    with pytest.raises(ValueError):
        fs.write("../escape", "x")
