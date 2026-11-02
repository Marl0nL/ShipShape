#!/usr/bin/python3 -I
"""Mode ledger: a Claude Code hook that records each session's permission mode.

Installed as /opt/shipshape/hooks/mode-ledger and registered by `ship-ctl apply`
for UserPromptSubmit and SessionStart in the agent's ~/.claude/settings.json.

Claude Code restores a resumed session's mode except bypass, and herdr resumes
with a bare `claude --resume <id>`. This ledger lets the resume shim put bypass
back (contracts/settings.md, "Permission modes across reboots").

- UserPromptSubmit carries `permission_mode`: recorded as is.
- SessionStart may not carry it; then the mode comes from the launching claude
  process's own flags (--dangerously-skip-permissions or --permission-mode), so an
  agent that was started but never prompted is still covered. A launch without
  flags records nothing, keeping what an earlier prompt recorded.

The hook never prints (SessionStart output would be added to Claude's context)
and always exits 0: a broken ledger must never block a prompt.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time

MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
SCHEMA = "shipshape.modes/1"
MAX_SESSIONS = 500


def ledger_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".local", "state", "shipshape")


def mode_from_argv(args: list[str]) -> str | None:
    if "--dangerously-skip-permissions" in args:
        return "bypassPermissions"
    for i, a in enumerate(args):
        if a == "--permission-mode" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--permission-mode="):
            return a.split("=", 1)[1]
    return None


def _is_claude(args: list[str]) -> bool:
    return any(os.path.basename(a) == "claude" for a in args[:2])


def launch_mode(proc: str = "/proc", pid: int | None = None) -> str | None:
    """Walk up from the hook's parent to the claude process and read its flags."""
    pid = os.getppid() if pid is None else pid
    for _ in range(8):
        try:
            with open(f"{proc}/{pid}/cmdline", "rb") as fh:
                args = [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
            with open(f"{proc}/{pid}/stat") as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
        if _is_claude(args):
            return mode_from_argv(args)
        if ppid <= 1:
            return None
        pid = ppid
    return None


def record(directory: str, session_id: str, mode: str, now: float | None = None) -> None:
    os.makedirs(directory, mode=0o700, exist_ok=True)
    path = os.path.join(directory, "modes.json")
    with open(os.path.join(directory, "modes.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(path) as fh:
                doc = json.load(fh)
            sessions = doc.get("sessions") if isinstance(doc, dict) else None
            if not isinstance(sessions, dict):
                sessions = {}
        except (OSError, ValueError):
            sessions = {}
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        sessions[session_id] = {"mode": mode, "updated_at": stamp}
        if len(sessions) > MAX_SESSIONS:
            newest = sorted(sessions.items(), key=lambda kv: str(kv[1].get("updated_at")))
            sessions = dict(newest[-MAX_SESSIONS:])
        fd, tmp = tempfile.mkstemp(prefix=".modes.", dir=directory)
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"schema": SCHEMA, "sessions": sessions}, fh, indent=1)
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


def handle(event: dict, directory: str, proc: str = "/proc") -> str | None:
    """Record the event's mode; returns the mode recorded, if any."""
    sid = event.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    mode = event.get("permission_mode")
    if mode not in MODES and event.get("hook_event_name") == "SessionStart":
        mode = launch_mode(proc)
    if mode not in MODES:
        return None
    record(directory, sid, mode)
    return mode


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if isinstance(event, dict):
            handle(event, ledger_dir())
    except Exception:  # never block Claude Code
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
