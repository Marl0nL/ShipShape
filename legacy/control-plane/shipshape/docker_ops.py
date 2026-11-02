"""Thin subprocess wrappers around docker. No docker socket is mounted anywhere —
the control plane runs as the host user and shells out to the CLI.

Container targets default to the ACTIVE instance (config.set_active), so callers
usually pass no name: `reconfigure()` hits this instance's proxy, `open_shell()`
this instance's agent, `compose([...])` this instance's compose project.
"""

from __future__ import annotations

import json
import os
import subprocess


from dataclasses import dataclass


@dataclass
class Result:
    ok: bool
    output: str


def _active():
    from . import config

    return config.active_paths(), config.active_config()


def _proxy(name: str | None) -> str:
    if name:
        return name
    from . import config

    return config.active_config().proxy_container


def _agent(name: str | None) -> str:
    if name:
        return name
    from . import config

    return config.active_config().agent_container


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


def container_running(name: str | None = None) -> bool:
    r = run(["docker", "inspect", "-f", "{{.State.Running}}", _proxy(name)], timeout=10)
    return r.ok and r.output.strip() == "true"


def running_containers() -> set[str]:
    """Names of all currently-running containers (one `docker ps`, for the dashboard
    service dots — cheaper than inspecting each container separately)."""
    r = run(["docker", "ps", "--format", "{{.Names}}"], timeout=10)
    return set(r.output.split()) if r.ok else set()


def reconfigure(name: str | None = None) -> Result:
    """Hot-reload this instance's Squid config (picks up allow-list changes, no restart)."""
    return run(["docker", "exec", _proxy(name), "squid", "-k", "reconfigure"])


def logs_tail(name: str | None = None, n: int = 2000) -> Result:
    return run(["docker", "logs", f"--tail={n}", _proxy(name)], timeout=20)


def compose(args: list[str], timeout: int = 1800, image: str | None = None) -> Result:
    """Run `docker compose` for the ACTIVE instance's project. Sets COMPOSE_PROJECT_NAME
    + the SS_* container-name vars + SS_INSTANCE_DIR (per-instance mount paths) + HOST_UID,
    and injects this instance's Claude token / firstmate repo."""
    paths, cfg = _active()
    idir = str(paths.instance_dir)
    env = {
        **os.environ,
        "HOST_UID": str(os.getuid()),
        "COMPOSE_PROJECT_NAME": cfg.project,
        "SS_INSTANCE_DIR": idir,
        "SS_AGENT_CONTAINER": cfg.agent_container,
        "SS_PROXY_CONTAINER": cfg.proxy_container,
        "SS_BROKER_CONTAINER": cfg.broker_container,
        "SS_ADB_CONTAINER": cfg.adb_container,
    }
    if image:
        env["SHIPSHAPE_AGENT_IMAGE"] = image
    # Push this instance's persistent Claude token into the container env so `claude`
    # auto-authenticates on start (compose interpolates ${CLAUDE_CODE_OAUTH_TOKEN:-}).
    try:
        tok = (paths.auth / "claude-token").read_text().strip()
        if tok:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    except OSError:
        pass
    # Build the agent image from this instance's configured firstmate source repo.
    try:
        sj = json.loads((paths.state / "settings.json").read_text())
        if sj.get("firstmate_repo"):
            env["FIRSTMATE_REPO"] = sj["firstmate_repo"]
    except (OSError, json.JSONDecodeError):
        pass
    cmd = ["docker", "compose", "-f", f"{paths.root}/docker-compose.yml", *args]
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(paths.root), env=env
        )
        return Result(p.returncode == 0, (p.stdout + p.stderr).strip())
    except FileNotFoundError:
        return Result(False, "docker not found")
    except subprocess.TimeoutExpired:
        return Result(False, f"timed out after {timeout}s")
    except OSError as e:
        return Result(False, str(e))


def open_terminal(
    container: str | None = None, inner: str | None = None, user: str | None = None
) -> Result:
    """Open a container shell (or `bash -lc <inner>`) in a NEW terminal window so the
    operator can keep the control plane visible. `user` maps to `docker exec -u` (pass
    "0" for a root shell). $SHIPSHAPE_TERMINAL overrides the auto-detected emulator;
    falls back to a message with the manual command."""
    import shutil as _sh

    container = _agent(container)
    exec_cmd = ["docker", "exec", "-it"]
    if user is not None:
        exec_cmd += ["-u", user]
    exec_cmd += [container, "/bin/bash"]
    if inner:
        exec_cmd += ["-lc", inner]
    custom = os.environ.get("SHIPSHAPE_TERMINAL")
    candidates = ([(custom, "--")] if custom else []) + [
        ("gnome-terminal", "--"), ("konsole", "-e"), ("xfce4-terminal", "-x"),
        ("xterm", "-e"), ("x-terminal-emulator", "-e"),
    ]
    for term, sep in candidates:
        exe = term.split()[0] if term else ""
        if not exe or not _sh.which(exe):
            continue
        argv = [*term.split(), "--", *exec_cmd] if sep == "--" else [*term.split(), sep, " ".join(exec_cmd)]
        try:
            subprocess.Popen(argv, start_new_session=True)
            return Result(True, f"opened {container} in a new {exe} window")
        except OSError as e:
            return Result(False, str(e))
    return Result(False, f"no terminal emulator found — run: {' '.join(exec_cmd)}")


def open_shell(container: str | None = None, user: str | None = None) -> Result:
    return open_terminal(container, user=user)


def logs_popen(name: str | None = None, tail: str = "0") -> subprocess.Popen:
    """Stream this instance's proxy log (for the live denied-domain harvester)."""
    return subprocess.Popen(
        ["docker", "logs", f"--tail={tail}", "--follow", _proxy(name)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
