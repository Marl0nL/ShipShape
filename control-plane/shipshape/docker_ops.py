"""Thin subprocess wrappers around docker. No docker socket is mounted
anywhere — the control plane runs as the host user and shells out to the CLI."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

PROXY = "egress-proxy"


@dataclass
class Result:
    ok: bool
    output: str


def run(args: list[str], timeout: int = 30) -> Result:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return Result(p.returncode == 0, (p.stdout + p.stderr).strip())
    except FileNotFoundError:
        return Result(False, f"command not found: {args[0]}")
    except subprocess.TimeoutExpired:
        return Result(False, f"timed out: {' '.join(args)}")


def container_running(name: str = PROXY) -> bool:
    r = run(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=10)
    return r.ok and r.output.strip() == "true"


def reconfigure(name: str = PROXY) -> Result:
    """Hot-reload Squid's config (picks up allow-list changes, no restart)."""
    return run(["docker", "exec", name, "squid", "-k", "reconfigure"])


def logs_tail(name: str = PROXY, n: int = 2000) -> Result:
    return run(["docker", "logs", f"--tail={n}", name], timeout=20)


def logs_popen(name: str = PROXY, tail: str = "0") -> subprocess.Popen:
    """Stream the proxy log (for the live denied-domain harvester)."""
    return subprocess.Popen(
        ["docker", "logs", f"--tail={tail}", "--follow", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
