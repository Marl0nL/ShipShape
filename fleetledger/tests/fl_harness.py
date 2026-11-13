"""FleetLedger test helpers: import them from here, not from conftest.

A module named `conftest` is ambiguous when pytest is given files from both test
folders (ship/tests has one too), so helpers live in this uniquely named module and
conftest.py only defines the fixtures. ship/tests does the same with ship_harness.py.
"""

from __future__ import annotations

import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts"


def keygen(path: Path, comment: str) -> Path:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_sshd(sshd: str, folder: Path, config: Callable[[int], str],
               attempts: int = 5) -> tuple[subprocess.Popen, int]:
    """Start an unprivileged sshd on a free localhost port, its config file
    `folder/config` written by config(port); returns (process, port).

    The port is free when picked but not held, so something else (another test
    worker) can take it before sshd binds: sshd then exits, and this tries again with
    a new port. Ready means sshd's own "Server listening" line, not a connection,
    which could reach whatever took the port."""
    log = folder / "sshd.log"
    for _ in range(attempts):
        port = _free_port()
        (folder / "config").write_text(config(port))
        with log.open("wb") as err:
            proc = subprocess.Popen([sshd, "-D", "-e", "-f", str(folder / "config")], stderr=err)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if f"Server listening on 127.0.0.1 port {port}" in log.read_text(errors="replace"):
                return proc, port
            if proc.poll() is not None:
                break  # it could not bind (the port was taken): try another
            time.sleep(0.05)
        proc.kill()
        proc.wait(timeout=5)
    raise RuntimeError(
        f"sshd never listened after {attempts} ports:\n{log.read_text(errors='replace')}")


def short_folder() -> Path:
    """A new private folder under /tmp, short enough for Unix socket paths (about 108
    bytes), whatever TMPDIR is. The short_dir fixture removes it again."""
    return Path(tempfile.mkdtemp(prefix="fl-", dir="/tmp"))


def _answers(sock: Path) -> bool:
    """Something listens on the socket: it exists a moment before ssh-agent listens."""
    with socket.socket(socket.AF_UNIX) as s:
        try:
            s.connect(str(sock))
        except (FileNotFoundError, ConnectionRefusedError):
            return False
    return True


class SSHAgents:
    """Private ssh-agents run as child processes (ssh-agent -D), so they can always be
    stopped: nothing outlives the test."""

    def __init__(self):
        self.procs: list[subprocess.Popen] = []

    def start(self, sock: Path) -> Path:
        proc = subprocess.Popen(["ssh-agent", "-D", "-a", str(sock)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.procs.append(proc)
        deadline = time.monotonic() + 10
        while not _answers(sock):
            if proc.poll() is not None:
                raise RuntimeError(f"ssh-agent did not start: {proc.stderr.read()!r}")
            if time.monotonic() > deadline:
                raise RuntimeError("ssh-agent did not answer on its socket in 10 s")
            time.sleep(0.01)
        return sock

    def stop_all(self) -> None:
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            proc.stderr.close()
        self.procs.clear()


class NoKeyring:
    """What tests get by default: no usable keyring, so nothing ever reaches a real one."""

    def get_password(self, service, item):
        raise RuntimeError("no keyring in tests")

    set_password = get_password


INVENTORY = """\
# My fleet (a comment that must survive edits)
schema = 1

[fleet]
admin_key = "{keys}/fleet_admin"
agent_keys = ["{keys}/you.pub"]

[defaults]
driver = "manual"
tags = ["work"]
claude = {{ remote_control = "on", default_mode = "auto" }}

[tag.work]
claude = "work@example.com"
gcloud = "work@example.com"
github = "example-work"

[tag.personal]
claude = "you@example.com"
github = "example-user"

[profile_options.orchestrator]
repo_url = "https://example.com/orchestrator.git"

[ship.ship-01]
id = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
tags = ["work"]
driver = "manual"
names = ["ship-01.local"]
host_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGVob3N0a2V5ZXhhbXBsZWhvc3RrZXlleGFt"
profiles = ["orchestrator"]
enrolled = "2026-01-01T00:00:00Z"

[ship.ship-02]
id = "2f1b8a54-3c6e-4d1f-9a2b-5e8c7d6f4a31"
tags = ["personal"]
driver = "manual"
names = ["ship-02.local", "ship-02.example.net"]
host_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGVob3N0a2V5c2hpcDAyZXhhbXBsZWhvc3Rr"
claude = {{ default_mode = "bypassPermissions", allow_bypass = true }}
enrolled = "2026-01-01T00:00:00Z"
"""


class FakeSSH:
    """Stands in for fleetledger.ssh.SSH: answers each remote command from a handler.

    handler(ship, command, input) returns (returncode, stdout, stderr) or raises.
    """

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def run(self, ship, command, *, input=None, timeout=60):
        from fleetledger.ssh import Result

        self.calls.append((ship.name, list(command), input))
        code, out, err = self.handler(ship, list(command), input)
        if isinstance(out, str):
            out = out.encode()
        return Result(code, out, err, ship.names[0])


def no_ship_answers(inventory):
    """An ssh_factory for tests that start the real poller (server.serve) but are not
    about Ships: every Ship is unreachable, and no real ssh runs (fl_netguard.py)."""
    from fleetledger.ssh import Unreachable

    def handler(ship, command, input):
        raise Unreachable(f"{ship.name} did not answer (a test: no Ship answers).")

    return FakeSSH(handler)


# --- the web UI ------------------------------------------------------------------------------


def wait_for(predicate, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError("timed out")


def finished(web, count=1):
    """The newest web job, once at least count jobs have run and all of them are done."""
    wait_for(lambda: len(web.app_ui.jobs.recent()) >= count
             and all(j.done for j in web.app_ui.jobs.recent()))
    return web.app_ui.jobs.recent()[0]


def last_job(web):
    """The newest web job, once it is done."""
    wait_for(lambda: web.app_ui.jobs.recent() and web.app_ui.jobs.recent()[0].done)
    return web.app_ui.jobs.recent()[0]


class Clock:
    """A clock a test moves by hand: clock.now += seconds. Starts at start, or at
    the real time."""

    def __init__(self, start: float | None = None):
        self.now = time.time() if start is None else start

    def __call__(self):
        return self.now


def fake_tskey(kind: str, rest: str) -> str:
    """A made-up Tailscale key ("tskey-<kind>-<rest>"), built at runtime so no literal in
    the source looks like a real key to secret scanners (scripts/hygiene.py refuses one)."""
    return "-".join(("tsk" + "ey", kind, rest))
