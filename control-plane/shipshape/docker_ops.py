"""Thin subprocess wrappers around docker. No docker socket is mounted
anywhere — the control plane runs as the host user and shells out to the CLI."""

from __future__ import annotations

import os
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
    except OSError as e:
        return Result(False, f"{args[0]}: {e}")


def container_running(name: str = PROXY) -> bool:
    r = run(["docker", "inspect", "-f", "{{.State.Running}}", name], timeout=10)
    return r.ok and r.output.strip() == "true"


def reconfigure(name: str = PROXY) -> Result:
    """Hot-reload Squid's config (picks up allow-list changes, no restart)."""
    return run(["docker", "exec", name, "squid", "-k", "reconfigure"])


def logs_tail(name: str = PROXY, n: int = 2000) -> Result:
    return run(["docker", "logs", f"--tail={n}", name], timeout=20)


def compose(root, args: list[str], timeout: int = 1800) -> Result:
    """Run `docker compose` for the ShipShape project. Passes HOST_UID so a build
    matches the host operator's uid (keeps the 0600 /auth creds readable)."""
    env = {**os.environ, "HOST_UID": str(os.getuid())}
    cmd = ["docker", "compose", "-f", f"{root}/docker-compose.yml", *args]
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(root), env=env
        )
        return Result(p.returncode == 0, (p.stdout + p.stderr).strip())
    except FileNotFoundError:
        return Result(False, "docker not found")
    except subprocess.TimeoutExpired:
        return Result(False, f"timed out after {timeout}s")
    except OSError as e:
        return Result(False, str(e))


def logs_popen(name: str = PROXY, tail: str = "0") -> subprocess.Popen:
    """Stream the proxy log (for the live denied-domain harvester)."""
    return subprocess.Popen(
        ["docker", "logs", f"--tail={tail}", "--follow", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
