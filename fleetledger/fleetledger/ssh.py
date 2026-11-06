"""Reach Ships with the system's OpenSSH (contracts/ship-runtime.md).

- Your SSH config and agent are respected; FleetLedger only adds options.
- Host keys come from the inventory, never from the network: FleetLedger writes
  its own known_hosts file from ships.toml and checks every connection against
  it with HostKeyAlias=<ship name>, whichever of the Ship's names it dials.
  A different key is refused outright; there is no trust-on-first-use.
- The fleet admin key logs in as fleet only; agent forwarding, X11 and port
  forwarding are always off.
- Connections are shared (ControlMaster), so polling many Ships is cheap.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shlex
import subprocess
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from fleetledger import paths
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory, Ship

SHIP_CTL = "/usr/local/bin/ship-ctl"
SEND_CHUNK = 256 * 1024

HOST_KEY_ERRORS = (
    "host key verification failed",
    "remote host identification has changed",
    "host key for",  # "...has changed and you have requested strict checking"
    "no ed25519 host key is known",
)
CONNECT_ERRORS = (
    "could not resolve hostname",
    "name or service not known",
    "temporary failure in name resolution",
    "connection refused",
    "connection timed out",
    "operation timed out",
    "no route to host",
    "network is unreachable",
    "connection closed by",
    "connection reset by",
)


class HostKeyMismatch(FleetError):
    """The Ship answered with a host key other than the pinned one."""

    exit_code = 4


class Unreachable(FleetError):
    """None of the Ship's names could be reached."""

    exit_code = 3


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: bytes
    stderr: str
    host: str  # the name that answered

    @property
    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")


Runner = Callable[..., subprocess.CompletedProcess]


def known_hosts_text(inventory: Inventory) -> str:
    lines = ["# Written by FleetLedger from ships.toml; edits are overwritten."]
    for ship in inventory.ships():
        lines.append(f"{ship.name} {ship.host_key_type_and_blob}")
    return "\n".join(lines) + "\n"


def remote_command(argv: Sequence[str]) -> str:
    """One string for ssh's remote side; the fleet user's shell splits it back."""
    return shlex.join(argv)


def as_agent(command: str) -> list[str]:
    """Run a shell command as agent in a login shell (contracts/ship-runtime.md)."""
    return ["sudo", "-n", "-iu", "agent", "--", "bash", "-c", command]


def ship_ctl(*args: str) -> list[str]:
    return ["sudo", "-n", SHIP_CTL, *args]


class SSH:
    def __init__(
        self,
        inventory: Inventory,
        *,
        ssh_bin: str = "ssh",
        runner: Runner = subprocess.run,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        connect_timeout: int = 10,
        fleet_user: str = "fleet",
        extra_options: Sequence[str] = (),
    ) -> None:
        self.inventory = inventory
        self.fleet_user = fleet_user
        self.extra_options = list(extra_options)
        self.ssh_bin = ssh_bin
        self.runner = runner
        self.popen = popen  # send(): a file streamed to a command's stdin
        self.connect_timeout = connect_timeout
        self._synced = False

    # --- known hosts ------------------------------------------------------
    def sync_known_hosts(self) -> Path:
        """Make FleetLedger's known_hosts match the inventory (mode 0600)."""
        path = self.inventory.known_hosts
        if not self._synced:
            text = known_hosts_text(self.inventory)
            try:
                current = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                current = None
            if current != text:
                paths.private_dir(path.parent)
                paths.write_private(path, text.encode("utf-8"))
            self._synced = True
        return path

    # --- command lines ----------------------------------------------------
    def options(self, ship: Ship, *, user: str | None = None, batch: bool = True) -> list[str]:
        user = user or self.fleet_user
        known_hosts = self.sync_known_hosts()
        # The socket name includes the pinned key, so after a re-pin no command
        # can ride on a connection that was checked against the old key.
        pin = hashlib.sha256(ship.host_key_type_and_blob.encode()).hexdigest()[:8]
        control = paths.private_dir(paths.runtime_dir()) / f"cm-{pin}-%C"
        opts = {
            "StrictHostKeyChecking": "yes",
            "UserKnownHostsFile": str(known_hosts),
            "GlobalKnownHostsFile": "/dev/null",
            "HostKeyAlias": ship.name,
            "HostKeyAlgorithms": "ssh-ed25519",
            "CheckHostIP": "no",
            "UpdateHostKeys": "no",
            "ForwardAgent": "no",
            "ForwardX11": "no",
            "ClearAllForwardings": "yes",
            "PermitLocalCommand": "no",
            "ConnectTimeout": str(self.connect_timeout),
            "ServerAliveInterval": "15",
        }
        # Unix socket paths are limited to about 108 bytes; %C expands to 40.
        if len(str(control)) - 2 + 40 < 100:
            opts.update(ControlMaster="auto", ControlPath=str(control), ControlPersist="60")
        else:
            opts.update(ControlMaster="no", ControlPath="none")
        if batch:
            opts["BatchMode"] = "yes"
        argv: list[str] = []
        for key, value in opts.items():
            argv += ["-o", f"{key}={value}"]
        if user == self.fleet_user:
            argv += ["-o", "IdentitiesOnly=yes", "-i", str(self.inventory.admin_key)]
        argv += [*self.extra_options, "-l", user]
        return argv

    def argv(
        self,
        ship: Ship,
        host: str,
        command: Sequence[str] | None = None,
        *,
        user: str | None = None,
        tty: bool = False,
        batch: bool = True,
    ) -> list[str]:
        argv = [self.ssh_bin, *self.options(ship, user=user, batch=batch)]
        if tty:
            argv.append("-tt")
        argv += ["--", host]
        if command:
            argv.append(remote_command(command))
        return argv

    # --- running ----------------------------------------------------------
    def run(
        self,
        ship: Ship,
        command: Sequence[str],
        *,
        input: bytes | None = None,
        timeout: float = 60,
    ) -> Result:
        """Run a command on the Ship as fleet, trying each of its names in order."""
        failures = []
        for host in ship.names:
            try:
                proc = self.runner(
                    self.argv(ship, host, command),
                    input=input if input is not None else b"",
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                failures.append(f"{host}: timed out after {timeout:.0f}s")
                continue
            except FileNotFoundError as exc:
                raise FleetError(f"{self.ssh_bin} not found; install OpenSSH.") from exc
            stderr = proc.stderr.decode("utf-8", errors="replace")
            if proc.returncode == 255:
                lowered = stderr.lower()
                if any(marker in lowered for marker in HOST_KEY_ERRORS):
                    raise HostKeyMismatch(
                        f"{ship.name} ({host}) did not present its pinned host key; refusing "
                        "to connect. Something else may be answering at that name.",
                        hint="If you rebuilt this Ship from a new seed, re-pin it with "
                        "`ship-fleet adopt`.",
                    )
                if "permission denied" in lowered:
                    raise FleetError(
                        f"{ship.name} ({host}) refused FleetLedger's admin key.",
                        hint="Is the key loaded in ssh-agent, and was this Ship seeded with it?",
                    )
                if any(marker in lowered for marker in CONNECT_ERRORS) or not proc.stdout:
                    failures.append(f"{host}: {_last_line(stderr) or 'connection failed'}")
                    continue
            return Result(proc.returncode, proc.stdout, stderr, host)
        raise Unreachable(f"Cannot reach {ship.name}: " + "; ".join(failures))

    def send(
        self,
        ship: Ship,
        command: Sequence[str],
        source: BinaryIO,
        *,
        on_progress: Callable[[int], None] | None = None,
        timeout: float = 600,
    ) -> Result:
        """Run a command on the Ship as fleet with source's bytes streamed to its stdin
        (a file for the inbox), never all in memory; on_progress(bytes sent so far).

        Each of the Ship's names is tried in order until one answers (ship-ctl keeps
        nothing of a cut-short file, so sending again is safe). A command that stops
        reading (it refused the file) ends the stream; its own answer is returned.
        """
        failures = []
        start = source.tell()
        for host in ship.names:
            source.seek(start)
            try:
                proc = self.popen(self.argv(ship, host, command), stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except FileNotFoundError as exc:
                raise FleetError(f"{self.ssh_bin} not found; install OpenSSH.") from exc
            out: list[bytes] = []
            err: list[bytes] = []
            readers = [threading.Thread(target=lambda s=s, b=b: b.append(s.read()), daemon=True)
                       for s, b in ((proc.stdout, out), (proc.stderr, err))]
            for reader in readers:
                reader.start()
            timer = threading.Timer(timeout, proc.kill)
            timer.start()
            sent = 0
            try:
                while chunk := source.read(SEND_CHUNK):
                    try:
                        proc.stdin.write(chunk)
                    except (BrokenPipeError, ValueError):
                        break  # it stopped reading: its answer says why
                    sent += len(chunk)
                    if on_progress:
                        on_progress(sent)
                with contextlib.suppress(BrokenPipeError, ValueError):
                    proc.stdin.close()
                code = proc.wait()
                for reader in readers:
                    reader.join()
            finally:
                timed_out = not timer.is_alive()
                timer.cancel()
                if proc.poll() is None:
                    proc.kill()
            stdout = b"".join(out)
            stderr = b"".join(err).decode("utf-8", errors="replace")
            if timed_out:
                raise FleetError(f"{ship.name}: sending timed out after {timeout:.0f}s.")
            if code == 255 and not stdout:
                lowered = stderr.lower()
                if any(marker in lowered for marker in HOST_KEY_ERRORS):
                    raise HostKeyMismatch(
                        f"{ship.name} ({host}) did not present its pinned host key; refusing "
                        "to connect.", hint="If you rebuilt this Ship from a new seed, re-pin "
                        "it with `ship-fleet adopt`.")
                if "permission denied" in lowered:
                    raise FleetError(f"{ship.name} ({host}) refused FleetLedger's admin key.")
                # ship-ctl keeps nothing of a cut-short file, so the next name can
                # simply be sent the whole file again.
                failures.append(f"{host}: {_last_line(stderr) or 'connection failed'}")
                continue
            return Result(code, stdout, stderr, host)
        raise Unreachable(f"Cannot reach {ship.name}: " + "; ".join(failures))

    def probe_certificate(self, ship: Ship, known_hosts: Path, timeout: float = 30) -> str | None:
        """Log in as fleet trusting only a CA line (known_hosts), never the pinned key.

        Proves a Ship presents a valid host certificate for its name. A fresh
        connection (no sharing), so an open pinned connection cannot answer for
        it. Each of the Ship's names is tried in order, as for any command; a
        host-key failure stops at once. Returns None on success, else why it failed.
        """
        overrides = {
            "UserKnownHostsFile": str(known_hosts),
            "HostKeyAlgorithms": "ssh-ed25519-cert-v01@openssh.com",
            "ControlMaster": "no",
            "ControlPath": "none",
        }
        dropped = {*overrides, "ControlPersist"}
        base = self.options(ship)
        prefix = [self.ssh_bin]
        i = 0
        while i < len(base):
            if base[i] == "-o" and base[i + 1].split("=", 1)[0] in dropped:
                i += 2
                continue
            prefix.append(base[i])
            i += 1
        for key, value in overrides.items():
            prefix += ["-o", f"{key}={value}"]
        failures = []
        for host in ship.names:
            try:
                proc = self.runner([*prefix, "--", host, "true"], input=b"",
                                   capture_output=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                failures.append(f"{host}: timed out")
                continue
            if proc.returncode == 0:
                return None
            why = _last_line(proc.stderr.decode("utf-8", errors="replace")) or "failed"
            if any(marker in why.lower() for marker in HOST_KEY_ERRORS):
                return f"{host}: {why}"
            failures.append(f"{host}: {why}")
        return "; ".join(failures) or "no names to try"

    def forget(self, ship: Ship) -> None:
        """Close shared connections to the Ship (after a reboot, say), so the next
        command makes a fresh connection instead of riding a dead one."""
        for host in ship.names:
            argv = [self.ssh_bin, *self.options(ship), "-O", "exit", "--", host]
            try:
                self.runner(argv, input=b"", capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def exec_interactive(
        self, ship: Ship, *, user: str, command: Sequence[str] = (), host: str | None = None
    ) -> None:
        """Replace this process with an interactive ssh session (ship-fleet ssh)."""
        argv = self.argv(ship, host or ship.names[0], command or None, user=user, tty=True,
                         batch=False)
        os.execvp(argv[0], argv)


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""
