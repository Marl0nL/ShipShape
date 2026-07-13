"""Run operator-approved commands on the host and reply to the agent via the spool.

Commands run on the HOST as the operator (their gcloud/gh identity). Per the
locked decision, agent-proposed commands are unconstrained in shape — the human
review (accept / edit / decline in the TUI or CLI) is the only gate. Nothing runs
without an explicit operator decision.
"""

from __future__ import annotations

import re
import subprocess
import time

from . import spool
from .config import Config, Paths
from .state import CommandStore

_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def sanitize(s: str) -> str:
    """Strip control chars so agent-supplied text can't spoof the terminal display
    (ANSI/escape sequences) that the operator relies on to review a command."""
    return _CTRL.sub("?", s or "")


def execute(command: str, cwd: str, timeout: int = 300) -> tuple[int, str]:
    try:
        p = subprocess.run(
            command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def _cwd(paths: Paths, cfg: Config) -> str:
    return cfg.command_cwd or str(paths.root)


def accept(paths: Paths, cfg: Config, rid: str) -> tuple[bool, str]:
    store = CommandStore(paths.commands)
    entry = store.get(rid)
    if not entry:
        return False, f"no such command {rid}"
    command = entry["command"]
    if not store.claim(rid):  # atomically claim; blocks a second accept / re-ingest
        return False, f"command {rid} is not pending (already handled)"
    rc, output = execute(command, _cwd(paths, cfg))
    status = "ok" if rc == 0 else "error"
    tail = output[-4000:]
    store.set_result(rid, status, tail)
    spool.write_response(paths.spool, rid, status, f"exit {rc}", time.time(), {"output": tail})
    return rc == 0, output


def edit(paths: Paths, rid: str, new_command: str) -> bool:
    return CommandStore(paths.commands).edit(rid, new_command)


def decline(paths: Paths, rid: str) -> bool:
    store = CommandStore(paths.commands)
    if not store.get(rid):
        return False
    store.set_result(rid, "declined", "")
    spool.write_response(paths.spool, rid, "denied", "declined by operator", time.time())
    return True
