"""The mode-ledger hook and the resume shim (contracts/settings.md, permission modes)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FILES = Path(__file__).resolve().parents[1] / "files"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ledger = load("mode_ledger", FILES / "hooks/mode_ledger.py")
shim = load("claude_shim", FILES / "shim/claude.py")


# --- ledger -----------------------------------------------------------------------
def test_ledger_records_prompt_mode(tmp_path):
    d = str(tmp_path / "state")
    assert (
        ledger.handle(
            {
                "session_id": "s1",
                "hook_event_name": "UserPromptSubmit",
                "permission_mode": "bypassPermissions",
            },
            d,
        )
        == "bypassPermissions"
    )
    ledger.handle(
        {"session_id": "s2", "hook_event_name": "UserPromptSubmit", "permission_mode": "auto"}, d
    )
    doc = json.loads((tmp_path / "state/modes.json").read_text())
    assert doc["schema"] == "shipshape.modes/1"
    assert doc["sessions"]["s1"]["mode"] == "bypassPermissions"
    assert doc["sessions"]["s2"]["mode"] == "auto"
    assert doc["sessions"]["s1"]["updated_at"].endswith("Z")
    assert oct((tmp_path / "state/modes.json").stat().st_mode & 0o777) == "0o600"


def test_ledger_ignores_junk(tmp_path):
    d = str(tmp_path / "state")
    assert (
        ledger.handle({"hook_event_name": "UserPromptSubmit", "permission_mode": "auto"}, d) is None
    )
    assert ledger.handle({"session_id": "s", "permission_mode": "sudo"}, d) is None
    assert not (tmp_path / "state").exists()


def fake_proc(root: Path, chain: list[tuple[int, int, list[str]]]) -> str:
    """chain: (pid, ppid, argv) entries."""
    for pid, ppid, argv in chain:
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (d / "stat").write_text(f"{pid} (x) S {ppid} 0 0\n")
    return str(root)


def test_session_start_reads_launch_flags(tmp_path, monkeypatch):
    proc = fake_proc(
        tmp_path / "proc",
        [
            (30, 20, ["/bin/sh", "-c", "/opt/shipshape/hooks/mode-ledger"]),
            (
                20,
                1,
                [
                    "/home/agent/.local/bin/claude",
                    "--dangerously-skip-permissions",
                    "--resume",
                    "s9",
                ],
            ),
        ],
    )
    monkeypatch.setattr(os, "getppid", lambda: 30)
    d = str(tmp_path / "state")
    assert (
        ledger.handle({"session_id": "s9", "hook_event_name": "SessionStart"}, d, proc)
        == "bypassPermissions"
    )
    assert (
        ledger.launch_mode(
            fake_proc(tmp_path / "p2", [(5, 1, ["claude", "--permission-mode", "plan"])]), pid=5
        )
        == "plan"
    )
    # A plain launch says nothing about the mode: nothing is recorded.
    assert ledger.launch_mode(fake_proc(tmp_path / "p3", [(7, 1, ["claude"])]), pid=7) is None


def test_hook_script_is_silent_and_never_fails(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    for stdin in (
        '{"session_id": "a", "hook_event_name": "UserPromptSubmit", "permission_mode": "auto"}',
        "not json",
        "",
    ):
        r = subprocess.run(
            [sys.executable, str(FILES / "hooks/mode_ledger.py")],
            input=stdin,
            env=env,
            capture_output=True,
            text=True,
        )
        assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    doc = json.loads((tmp_path / ".local/state/shipshape/modes.json").read_text())
    assert doc["sessions"]["a"]["mode"] == "auto"


# --- shim -------------------------------------------------------------------------
@pytest.fixture
def files(tmp_path):
    led = tmp_path / "modes.json"
    led.write_text(
        json.dumps(
            {
                "schema": "shipshape.modes/1",
                "sessions": {
                    "byp": {"mode": "bypassPermissions", "updated_at": "2026-01-01T00:00:00Z"},
                    "aut": {"mode": "auto", "updated_at": "2026-01-01T00:00:00Z"},
                },
            }
        )
    )
    toml = tmp_path / "ship.toml"
    toml.write_text("schema = 1\n[claude]\nallow_bypass = true\n")
    return str(led), toml


def test_shim_restores_bypass_on_resume(files):
    led, toml = files
    args, note = shim.decide(["--resume", "byp"], led, str(toml))
    assert args == ["--dangerously-skip-permissions", "--resume", "byp"]
    assert "restored" in note


@pytest.mark.parametrize(
    "argv",
    [
        ["--resume", "aut"],
        ["--resume", "unknown"],
        ["--resume", "byp", "--permission-mode", "plan"],
        ["--resume", "byp", "--dangerously-skip-permissions"],
        ["-p", "hello"],
        [],
    ],
)
def test_shim_passes_everything_else_through(files, argv):
    led, toml = files
    assert shim.decide(argv, led, str(toml))[0] == argv


def test_shim_respects_allow_bypass(files):
    led, toml = files
    toml.write_text("schema = 1\n[claude]\nallow_bypass = false\n")
    assert shim.decide(["--resume", "byp"], led, str(toml))[0] == ["--resume", "byp"]
    toml.unlink()
    assert shim.decide(["--resume", "byp"], led, str(toml))[0] == ["--resume", "byp"]


def test_shim_execs_the_real_launcher(tmp_path, files):
    led, _ = files
    home = tmp_path / "home"
    real = home / ".local/bin/claude"
    real.parent.mkdir(parents=True)
    real.write_text('#!/bin/sh\necho "real: $*"\n')
    real.chmod(0o755)
    state = home / ".local/state/shipshape"
    state.mkdir(parents=True)
    Path(led).rename(state / "modes.json")
    root = tmp_path / "root"
    (root / "etc/shipshape").mkdir(parents=True)
    (root / "etc/shipshape/ship.toml").write_text("schema = 1\n[claude]\nallow_bypass = true\n")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SHIPSHAPE_ROOT": str(root)}
    r = subprocess.run(
        [sys.executable, str(FILES / "shim/claude.py"), "--resume", "byp"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.stdout == "real: --dangerously-skip-permissions --resume byp\n"
    assert "bypass restored" in (state / "shim.log").read_text()
    real.unlink()
    r = subprocess.run(
        [sys.executable, str(FILES / "shim/claude.py")], env=env, capture_output=True, text=True
    )
    assert r.returncode == 127
