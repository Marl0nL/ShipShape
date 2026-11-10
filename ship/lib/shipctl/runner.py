"""Run commands, as root or as the agent user, with hard timeouts.

As root on a Ship, agent commands run through `runuser -u agent` with a clean
environment: the agent's HOME, its PATH (~/.local/bin first), its npm prefix and
the catalogue's environment file. In sandbox mode (tests) they run as the
current user with HOME pointed at the agent home inside the sandbox.
"""

from __future__ import annotations

import contextlib
import os
import pwd
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .homefs import HomeFS, UnsafePath
from .paths import AGENT, Paths


@dataclass
class Result:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


TIMEOUT_RC = 124
NOT_FOUND_RC = 127


def read_env_file(home: Path, rel: str) -> dict[str, str]:
    """Parse the KEY='value' lines ship-ctl writes (see tools.write_env_file).

    The file is in the agent's home, so it is read without following symlinks.
    """
    env: dict[str, str] = {}
    try:
        raw = HomeFS(home).read(rel)
        lines = raw.decode("utf-8").splitlines() if raw else []
    except (OSError, UnsafePath, UnicodeDecodeError):
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


class Runner:
    def __init__(self, paths: Paths):
        self.paths = paths

    # --- the agent user ----------------------------------------------------
    def agent_ids(self) -> tuple[int, int] | None:
        """(uid, gid) of the agent user, or None in sandbox mode or before it exists."""
        if self.paths.sandbox:
            return None
        try:
            pw = pwd.getpwnam(AGENT)
        except KeyError:
            return None
        return pw.pw_uid, pw.pw_gid

    def agent_env(self) -> dict[str, str]:
        home = self.paths.agent_path(self.paths.agent_home)
        if self.paths.sandbox:
            env = dict(os.environ)
            env["HOME"] = str(self.paths.agent_home)
            base_path = os.environ.get("PATH", "/usr/bin:/bin")
            env["PATH"] = f"{self.paths.agent_home}/.local/bin:{base_path}"
        else:
            ids = self.agent_ids()
            env = {
                "HOME": home,
                "USER": AGENT,
                "LOGNAME": AGENT,
                "SHELL": "/bin/bash",
                "LANG": "C.UTF-8",
                "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            }
            if ids:
                env["XDG_RUNTIME_DIR"] = f"/run/user/{ids[0]}"
        env["npm_config_prefix"] = f"{env['HOME']}/.local"
        env.update(
            read_env_file(self.paths.agent_home, self.paths.home_rel(self.paths.agent_env_file))
        )
        return env

    # --- running -------------------------------------------------------------
    def run(
        self,
        argv: list[str],
        *,
        timeout: float = 60,
        agent: bool = False,
        input: str | None = None,
        discard: bool = False,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        limit: int | None = None,
    ) -> Result:
        """Run argv. `discard` sends stdout and stderr to /dev/null (for token probes).
        `limit` keeps at most that many bytes of stdout, reading and dropping the rest,
        and sends stderr to /dev/null (for sign-in checks, signincheck.py)."""
        if agent:
            full_env = self.agent_env()
            full_env.update(env or {})
            cwd = cwd or self.paths.agent_home
            if not self.paths.sandbox:
                pairs = [f"{k}={v}" for k, v in full_env.items()]
                argv = ["runuser", "-u", AGENT, "--", "env", "-i", *pairs, *argv]
                run_env = None
            else:
                run_env = full_env
        else:
            run_env = None
            if env:
                run_env = dict(os.environ)
                run_env.update(env)
        if cwd is not None and not Path(cwd).is_dir():
            cwd = None
        out_target = subprocess.DEVNULL if discard else subprocess.PIPE
        capped = limit is not None and not discard
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                stdout=out_target,
                stderr=subprocess.DEVNULL if capped else out_target,
                env=run_env,
                cwd=cwd,
                text=not capped,
                start_new_session=True,
            )
        except FileNotFoundError:
            return Result(NOT_FOUND_RC, "", f"{argv[0]}: not found")
        except PermissionError:
            return Result(126, "", f"{argv[0]}: permission denied")
        if capped:
            return _capped(proc, argv[0], timeout, limit, input)
        try:
            out, err = proc.communicate(input=input, timeout=timeout)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return Result(TIMEOUT_RC, "", f"{argv[0]}: timed out after {timeout:g}s")
        return Result(proc.returncode, out or "", err or "")


def _capped(proc: subprocess.Popen, name: str, timeout: float, limit: int,
            input: str | None) -> Result:
    """Wait for proc, keeping the first `limit` bytes of its stdout."""
    kept = bytearray()
    fd = proc.stdout.fileno()

    def read() -> None:
        # os.read returns what is there now: nothing printed waits in a buffer.
        with contextlib.suppress(OSError):
            while chunk := os.read(fd, 65536):
                if len(kept) < limit:
                    kept.extend(chunk[: limit - len(kept)])

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    if input is not None:
        with contextlib.suppress(OSError):
            proc.stdin.write(input.encode("utf-8"))
            proc.stdin.close()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        reader.join(1)
        return Result(TIMEOUT_RC, "", f"{name}: timed out after {timeout:g}s")
    # A helper the command left behind (holding stdout open, say) ends with it.
    reader.join(0.5)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    reader.join(1)
    return Result(proc.returncode, bytes(kept).decode("utf-8", "replace"), "")
