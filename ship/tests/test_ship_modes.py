"""The mode-ledger hook (contracts/settings.md, "Permission modes across reboots")."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

FILES = Path(__file__).resolve().parents[1] / "files"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ledger = load("mode_ledger", FILES / "hooks/mode_ledger.py")


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


def test_session_end_is_recorded_until_the_session_runs_again(tmp_path):
    """How a session ended tells an agent someone exited from one that died."""
    d = str(tmp_path / "state")
    ledger.handle({"session_id": "s1", "hook_event_name": "UserPromptSubmit",
                   "permission_mode": "auto"}, d)
    assert ledger.handle({"session_id": "s1", "hook_event_name": "SessionEnd",
                          "reason": "prompt_input_exit"}, d) == "prompt_input_exit"
    entry = json.loads((tmp_path / "state/modes.json").read_text())["sessions"]["s1"]
    assert entry["mode"] == "auto"  # the mode stays: it is resumed in it
    assert entry["ended"]["reason"] == "prompt_input_exit" and entry["ended"]["at"].endswith("Z")
    # Resumed (a launch without mode flags): it runs again, so it no longer counts as ended.
    ledger.handle({"session_id": "s1", "hook_event_name": "SessionStart"}, d, str(tmp_path))
    entry = json.loads((tmp_path / "state/modes.json").read_text())["sessions"]["s1"]
    assert "ended" not in entry and entry["mode"] == "auto"


def test_session_end_of_an_unknown_session_and_odd_reasons(tmp_path):
    d = str(tmp_path / "state")
    assert ledger.handle({"session_id": "s2", "hook_event_name": "SessionEnd",
                          "reason": "Weird Reason!"}, d) == "other"
    entry = json.loads((tmp_path / "state/modes.json").read_text())["sessions"]["s2"]
    assert "mode" not in entry and entry["ended"]["reason"] == "other"
    # A start without flags records no mode, but that the session started under these
    # hooks (so its end will be recorded too).
    ledger.handle({"session_id": "s3", "hook_event_name": "SessionStart"}, d, str(tmp_path))
    entry = json.loads((tmp_path / "state/modes.json").read_text())["sessions"]["s3"]
    assert "mode" not in entry and entry["started_at"].endswith("Z")
    # A prompt of a session the ledger never saw start (it predates the hooks) does not.
    ledger.handle({"session_id": "s4", "hook_event_name": "UserPromptSubmit",
                   "permission_mode": "plan"}, d)
    assert "started_at" not in json.loads(
        (tmp_path / "state/modes.json").read_text())["sessions"]["s4"]
