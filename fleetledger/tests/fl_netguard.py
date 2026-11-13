"""Tests touch no network: no DNS lookup of a name, no connection to another machine,
no real ssh to anything but this computer's loopback.

FleetLedger's tests use placeholder Ship names (ship-01.local, ship-31.example.net).
A real lookup of one can hang for seconds where mDNS or DNS does not answer, and a
real ssh to one would open a connection wherever such a name does answer. conftest.py
turns the guard on for every test (`no_network`), with two opt-ins for the tests that
use the network on purpose:

- `@pytest.mark.real_resolver`: names may be looked up for real (the resolver's own
  tests). Without it, cli._resolves is the fake below.
- `@pytest.mark.local_sshd`: real ssh to a loopback address (127.0.0.1, ::1,
  localhost), as the tests that run a private sshd do. Without it a test runs no real
  ssh at all, local or not.

"Real ssh" is the system's OpenSSH (ssh, scp, sftp, ssh-keyscan); a test's own
stand-in script of that name, in a temp folder or first on PATH, may run. Nothing
opts in to ssh or connections to another machine. A test that tries anything the
guard refuses fails, naming what it tried: the attempt raises NetworkRefused where
it happens, and since a background thread (the web UI's poller, a job) may swallow
that, the guard also fails the test at teardown with everything it refused.

The checks run in an audit hook (sys.addaudithook), which sees every lookup, connect
and process start in this process, plus a wrapper around pexpect's spawn (its child
execs after a fork, out of the hook's sight). The hook cannot be removed once added,
so it is added once, on import, and does nothing while no guard is active.
"""

from __future__ import annotations

import functools
import ipaddress
import os
import shutil
import socket
import sys
import threading
from dataclasses import dataclass, field

LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}

# ssh's options that take an argument (ssh(1)); the rest are flags.
SSH_ARG_OPTS = set("BbcDEeFIiJLlmOopPQRSWw")
# Modes in which ssh opens no connection: print its config (-G), query (-Q), version
# (-V), or a command to an existing master over its local socket (-O).
SSH_NO_CONNECT = set("GQVO")
SSH_KEYSCAN_ARG_OPTS = set("fOpTt")
SSH_PROGRAMS = {"ssh", "scp", "sftp", "ssh-keyscan"}


class NetworkRefused(RuntimeError):
    """A test tried to reach the network (fl_netguard.py)."""


@dataclass
class Guard:
    real_resolver: bool = False
    local_sshd: bool = False
    refused: list[str] = field(default_factory=list)

    def refuse(self, what: str) -> None:
        with _lock:
            self.refused.append(what)
        raise NetworkRefused(f"test reached for the network: {what}. {HELP}")


HELP = ("Tests touch no network: inject a fake (FakeSSH, ssh_factory=, resolves=) or "
        "see fleetledger/tests/fl_netguard.py for the opt-in markers.")

_lock = threading.Lock()
_active: Guard | None = None


def start(*, real_resolver: bool = False, local_sshd: bool = False) -> Guard:
    global _active
    _active = Guard(real_resolver=real_resolver, local_sshd=local_sshd)
    return _active


def stop() -> Guard | None:
    global _active
    guard, _active = _active, None
    return guard


# --- what counts as local ---------------------------------------------------------------


def _address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def is_loopback(host: str) -> bool:
    host = host.strip().rstrip(".").lower()
    if host in LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    addr = _address(host)
    return addr is not None and (addr.is_loopback or addr.is_unspecified)


def needs_lookup(host: object) -> bool:
    """Would getaddrinfo(host) ask a resolver? An address, an empty host (a wildcard
    bind) or a loopback name does not."""
    if host is None:
        return False
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host)
    return bool(host) and _address(host) is None and not is_loopback(host)


# --- ssh argv -------------------------------------------------------------------------


def _bare_host(dest: str) -> str:
    if dest.startswith("ssh://"):
        dest = dest[len("ssh://"):].split("/", 1)[0]
        dest = dest.rsplit("@", 1)[-1]
        if dest.startswith("["):
            return dest[1:].split("]", 1)[0]
        return dest.rsplit(":", 1)[0] if dest.count(":") == 1 else dest
    return dest.rsplit("@", 1)[-1]


def ssh_destinations(argv: list[str]) -> list[str] | None:
    """The hosts this ssh/scp/sftp/ssh-keyscan argv would connect to, or None when it
    opens no connection (ssh -G, -O ...)."""
    program = os.path.basename(argv[0])
    arg_opts = SSH_KEYSCAN_ARG_OPTS if program == "ssh-keyscan" else SSH_ARG_OPTS
    positional: list[str] = []
    jump: list[str] = []
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            positional += argv[i + 1:i + 2] if program == "ssh" else argv[i + 1:]
            break
        if arg.startswith("-") and len(arg) > 1:
            for j, flag in enumerate(arg[1:], start=1):
                if program == "ssh" and flag in SSH_NO_CONNECT:
                    return None
                if flag in arg_opts:
                    value = arg[j + 1:] or (argv[i + 1] if i + 1 < len(argv) else "")
                    if not arg[j + 1:]:
                        i += 1
                    if flag == "J":
                        jump += [_bare_host(h) for h in value.split(",")]
                    elif flag == "o" and value.lower().startswith("proxyjump"):
                        jump += [_bare_host(h) for h in value.split("=", 1)[-1].split(",")]
                    break
            i += 1
            continue
        positional.append(arg)
        if program == "ssh":  # the first one is the host; the rest, its command
            break
        i += 1
    if program == "ssh":
        hosts = [_bare_host(positional[0])] if positional else []
    elif program == "ssh-keyscan":
        hosts = [h for arg in positional for h in arg.split(",") if h]
    else:  # scp, sftp: [user@]host:path or sftp's [user@]host
        hosts = []
        for arg in positional:
            if program == "sftp" or (":" in arg and "/" not in arg.split(":", 1)[0]):
                hosts.append(_bare_host(arg.split(":", 1)[0] if program == "scp" else arg))
    return hosts + jump


SYSTEM_PATH = "/usr/local/bin:/usr/bin:/bin"


@functools.cache
def _system_binary(program: str) -> str | None:
    found = shutil.which(program, path=SYSTEM_PATH)
    return os.path.realpath(found) if found else None


def is_real(program: str, path: str | None = None) -> bool:
    """Is this the system's OpenSSH program, not a test's stand-in (a script named ssh
    in a temp folder, put first on PATH)? A program that is not there runs nothing."""
    found = program if os.sep in program else shutil.which(program, path=path)
    if not found:
        return False
    name = os.path.basename(found)
    return os.path.realpath(found) == _system_binary(name)


def check_spawn(argv: list[str], env: object = None) -> None:
    guard = _active
    if guard is None or not argv:
        return
    if os.path.basename(argv[0]) not in SSH_PROGRAMS:
        return
    path = None
    if isinstance(env, dict):
        path = env.get("PATH", env.get(b"PATH"))
        path = os.fsdecode(path) if path is not None else os.defpath
    if not is_real(argv[0], path):
        return
    hosts = ssh_destinations(argv)
    if not hosts:  # no connection: ssh -G, ssh -O exit, or no host at all
        return
    shown = " ".join([argv[0], "...", *hosts])
    if not all(is_loopback(h) for h in hosts):
        guard.refuse(f"a real {os.path.basename(argv[0])} to another machine ({shown})")
    if not guard.local_sshd:
        guard.refuse(f"a real {os.path.basename(argv[0])} without @pytest.mark.local_sshd "
                     f"({shown})")


# --- the audit hook -------------------------------------------------------------------


def _argv(args: object) -> list[str]:
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    try:
        return [os.fsdecode(a) for a in args]  # type: ignore[union-attr]
    except TypeError:
        return []


def _audit(event: str, args: tuple) -> None:
    guard = _active
    if guard is None:
        return
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex"):
        if not guard.real_resolver and needs_lookup(args[0]):
            guard.refuse(f"a DNS lookup of {args[0]!r} without @pytest.mark.real_resolver")
    elif event in ("socket.connect", "socket.sendto", "socket.sendmsg"):
        sock, address = args[0], args[-1]
        if getattr(sock, "family", None) in (socket.AF_INET, socket.AF_INET6) \
                and isinstance(address, tuple) and address:
            host = str(address[0])
            if not is_loopback(host):
                guard.refuse(f"a connection to {host} (another machine)")
    elif event == "subprocess.Popen":  # (executable, args, cwd, env)
        check_spawn(_argv(args[0] if args[0] is not None else args[1])[:1]
                    + _argv(args[1])[1:], args[3])
    elif event == "os.posix_spawn":  # (path, argv, env)
        check_spawn(_argv(args[0])[:1] + _argv(args[1])[1:], args[2])


def _install() -> None:
    sys.addaudithook(_audit)
    try:
        from pexpect import pty_spawn
    except ImportError:  # pragma: no cover - a declared dependency
        return
    real = pty_spawn.spawn._spawnpty

    def guarded(self, args, **kwargs):
        check_spawn(_argv(args), kwargs.get("env"))
        return real(self, args, **kwargs)

    pty_spawn.spawn._spawnpty = guarded


_install()


# --- the fake resolver ----------------------------------------------------------------


def fake_resolves(host: str, timeout: float = 0.0) -> bool:
    """cli._resolves in tests: an address or a loopback name resolves, no other name
    does (as on a machine where nobody answers for the placeholder names)."""
    return _address(host) is not None or is_loopback(host)
