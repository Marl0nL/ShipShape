#!/usr/bin/python3 -I
"""ship-resume: let herdr resume its agents after a boot, with nobody attached.

herdr restores its layout when the server starts, but resumes agent panes only
"after a client attaches and provides terminal size and theme context". So, as
the agent user (ship-resume.service, after herdr.service):

1. wait until the herdr socket answers and DNS works (user services cannot wait
   on the system's network target), at most 2 minutes;
2. attach a herdr client inside a detached tmux terminal of a fixed size;
3. wait until `herdr agent list` stops changing (at most 2 minutes);
4. detach the client and end tmux;
5. record the result in ~/.local/state/shipshape/last-resume.json with this
   boot's id, which ship-ctl reports as `herdr.last_resume` and which tells
   `ship-ctl autostart` that herdr's own resume is finished.

Exit 0 even when nothing was resumed; exit 1 only if herdr never answered.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

TMUX_SOCKET = "ship-resume"
READY_TIMEOUT = 120
SETTLE_TIMEOUT = 120
POLL = 3
STABLE_POLLS = 3
DNS_PROBE = "api.anthropic.com"


def log(msg: str) -> None:
    print(f"ship-resume: {msg}", flush=True)


def run(argv: list[str], timeout: float = 15) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


def agent_list() -> list[dict] | None:
    r = run(["herdr", "agent", "list"])
    if r.returncode != 0:
        return None
    try:
        agents = json.loads(r.stdout)["result"]["agents"]
        return [a for a in agents if isinstance(a, dict)]
    except (ValueError, KeyError, TypeError):
        return None


def dns_ok() -> bool:
    try:
        socket.getaddrinfo(DNS_PROBE, 443)
        return True
    except OSError:
        return False


def snapshot(agents: list[dict]) -> list[tuple]:
    return sorted(
        (a.get("pane_id"), a.get("agent"), a.get("agent_status"), bool(a.get("launch_pending")))
        for a in agents
    )


def settled(agents: list[dict]) -> bool:
    return not any(a.get("launch_pending") or a.get("agent_status") == "unknown" for a in agents)


def boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def write_record(resumed: int, failed: int) -> None:
    d = os.path.join(os.path.expanduser("~"), ".local", "state", "shipshape")
    os.makedirs(d, mode=0o700, exist_ok=True)
    rec = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": "hidden-client",
        "resumed": resumed,
        "failed": failed,
        "boot_id": boot_id(),
    }
    tmp = os.path.join(d, ".last-resume.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(rec, fh)
    os.replace(tmp, os.path.join(d, "last-resume.json"))


def main(clock=time.monotonic, sleep=time.sleep) -> int:
    start = clock()
    while agent_list() is None or not dns_ok():
        if clock() - start > READY_TIMEOUT:
            log(f"herdr or DNS not ready after {READY_TIMEOUT}s; giving up")
            return 1
        sleep(1)
    log(f"herdr answering after {clock() - start:.0f}s; attaching a hidden client")

    tmux = ["tmux", "-L", TMUX_SOCKET]
    run([*tmux, "kill-server"])
    r = run(
        [
            *tmux,
            "-f",
            "/dev/null",
            "new-session",
            "-d",
            "-s",
            "resume",
            "-x",
            "200",
            "-y",
            "50",
            "herdr",
        ]
    )
    if r.returncode != 0:
        log(f"could not start the hidden client: {r.stderr.strip()[:200]}")

    begin = clock()
    previous = None
    stable = 0
    agents: list[dict] = []
    while clock() - begin < SETTLE_TIMEOUT:
        sleep(POLL)
        agents = agent_list() or []
        snap = snapshot(agents)
        stable = stable + 1 if snap == previous and settled(agents) else 0
        previous = snap
        if stable >= STABLE_POLLS:
            break
    log(f"agent list settled after {clock() - begin:.0f}s: {len(agents)} agent(s)")

    # herdr detaches on its prefix then q; ending tmux is the fallback.
    run([*tmux, "send-keys", "-t", "resume", "C-b"])
    run([*tmux, "send-keys", "-t", "resume", "q"])
    for _ in range(5):
        if run([*tmux, "has-session", "-t", "resume"]).returncode != 0:
            break
        sleep(1)
    run([*tmux, "kill-server"])
    log("hidden client gone")

    unresumed = sum(1 for a in agents if a.get("agent_status") == "unknown")
    write_record(resumed=len(agents) - unresumed, failed=unresumed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
