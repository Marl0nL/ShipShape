#!/usr/bin/python3 -I
"""Mode ledger: a Claude Code hook that records each session's permission mode.

Installed as /opt/shipshape/hooks/mode-ledger and registered by `ship-ctl apply`
for UserPromptSubmit, SessionStart and SessionEnd in the agent's
~/.claude/settings.json.

ship-ctl reads it to know each session's mode (the status document, and the mode
an agent is resumed in after a reboot: contracts/settings.md, "Permission modes
across reboots"), and how a session ended, which tells an agent someone exited
from one that died (ship-ctl's agent record).

- UserPromptSubmit carries `permission_mode`: recorded as is.
- SessionStart may not carry it; then the mode comes from the launching claude
  process's own flags (--dangerously-skip-permissions or --permission-mode), so an
  agent that was started but never prompted is still covered. A launch without
  flags records no mode, keeping what an earlier prompt recorded.
- SessionEnd carries `reason` (clear, resume, logout, prompt_input_exit, other):
  recorded as the session's `ended`, until the session starts again.
- SessionStart also records `started_at`: this session runs under these hooks, so
  its end will be recorded (Claude Code reads hooks when a session starts).

The hook never prints (SessionStart output would be added to Claude's context)
and always exits 0: a broken ledger must never block a prompt.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
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


def record(directory: str, session_id: str, mode: str | None = None,
           ended: str | None = None, started: bool = False, now: float | None = None) -> None:
    """Set the session's mode (if given) and how it ended: `ended` given, or else
    cleared, since any other event means the session runs (again). started: it has
    just started (SessionStart)."""
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
        entry = sessions.get(session_id)
        entry = dict(entry) if isinstance(entry, dict) else {}
        before = dict(entry)
        if mode is not None:
            entry["mode"] = mode
        if ended is not None:
            entry["ended"] = {"reason": ended, "at": stamp}
        else:
            entry.pop("ended", None)
        if started:
            entry["started_at"] = stamp
        if entry == before and mode is None:
            return  # nothing to say
        entry["updated_at"] = stamp
        sessions[session_id] = entry
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


REASON = re.compile(r"^[a-z_]{1,40}$")


def handle(event: dict, directory: str, proc: str = "/proc") -> str | None:
    """Record the event; returns the mode recorded, or for SessionEnd the reason."""
    sid = event.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    name = event.get("hook_event_name")
    if name == "SessionEnd":
        reason = event.get("reason")
        reason = reason if isinstance(reason, str) and REASON.match(reason) else "other"
        record(directory, sid, ended=reason)
        return reason
    mode = event.get("permission_mode")
    if mode not in MODES and name == "SessionStart":
        mode = launch_mode(proc)
    mode = mode if mode in MODES else None
    if mode is not None or name == "SessionStart":
        record(directory, sid, mode, started=name == "SessionStart")
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
