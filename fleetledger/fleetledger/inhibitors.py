"""Why a Ship refused to reboot: the shutdown locks (logind inhibitors) on it.

logind refuses `systemctl reboot` while a program holds a *block* lock on
shutdown, root included, unless the caller overrides it (systemd 257 and later;
the Ship image has 259). On a Ship that program is APT: apt, apt-get and
unattended-upgrade hold one while they install or remove packages, and their own
helper processes keep it until the last of them ends. Run from an interactive
login, an apt that is still going holds every reboot off until it ends or that
session is closed. herdr's lock is a *delay* lock: it never blocks; logind waits
up to InhibitDelayMaxSec (30 s on a Ship) for herdr to save its state.

`probe` runs PROBE on the Ship as fleet, without sudo (logind lists its locks to
any user, and /proc and loginctl tell which login session a process is in), so
it works on every Ship whatever its tooling version. `describe` turns what it
found into the words FleetLedger shows, with the next step.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any

from fleetledger.errors import FleetError
from fleetledger.inventory import Ship

# Run as `python3 -I -` with this on stdin; an optional argument is the proc root
# (tests). Prints {"blockers": [...]}: every block lock on shutdown (logind lets
# root past a block-weak one) with the process that took it and where it runs. If
# that process has exited, the APT processes still running, since APT's helpers
# keep its lock (only root could read which process holds the lock itself).
PROBE = r'''
import json, os, pwd, re, subprocess, sys

PROC = sys.argv[1] if len(sys.argv) > 1 else "/proc"
APT = {"apt", "apt-get", "aptitude", "unattended-upgr", "dpkg"}


def run(*argv):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def read(pid, name):
    try:
        with open(f"{PROC}/{pid}/{name}", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def user(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return str(uid)


def tty(stat):
    try:
        nr = int(stat.rsplit(")", 1)[1].split()[4])
    except (AttributeError, IndexError, ValueError):
        return None
    major, minor = (nr >> 8) & 0xFFF, (nr & 0xFF) | ((nr >> 12) & 0xFFF00)
    if 136 <= major <= 143:
        return f"pts/{(major - 136) * 256 + minor}"
    if major == 4:
        return f"tty{minor}" if minor < 64 else f"ttyS{minor - 64}"
    return None


def process(pid):
    """What runs as pid and where (login session or unit); None once it has exited."""
    comm = read(pid, "comm")
    if comm is None:
        return None
    status = read(pid, "status") or ""
    uid = re.search(r"^Uid:\s+([0-9]+)", status, re.M)
    lines = (read(pid, "cgroup") or "").strip().splitlines()
    path = lines[-1].split(":", 2)[-1] if lines else ""
    # unattended-upgrade-shutdown waits for every shutdown (a delay lock): never a holder.
    helper = "unattended-upgrade-shutdown" in (read(pid, "cmdline") or "")
    info = {"pid": pid, "comm": comm.strip(), "user": user(int(uid.group(1))) if uid else None,
            "tty": tty(read(pid, "stat")), "session": None, "unit": None, "unit_user": None,
            "shutdown_helper": helper}
    session = re.search(r"/session-([^/]+)\.scope$", path)
    unit = re.search(r"/([^/]+\.service)$", path)
    owner = re.search(r"/user@([0-9]+)\.service/", path)
    if session:
        props = run("loginctl", "show-session", session.group(1), "--property=Name",
                    "--property=Service", "--property=TTY", "--property=State")
        s = dict(line.split("=", 1) for line in (props or "").splitlines() if "=" in line)
        info["session"] = {"id": session.group(1), "user": s.get("Name") or None,
                           "service": s.get("Service") or None, "tty": s.get("TTY") or None,
                           "state": s.get("State") or None}
    elif unit:
        info["unit"] = unit.group(1)
        info["unit_user"] = user(int(owner.group(1))) if owner else None
    return info


out = run("busctl", "--json=short", "call", "org.freedesktop.login1",
          "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "ListInhibitors")
if out is None:
    sys.exit(3)
rows = json.loads(out)["data"][0]
taken = {row[5] for row in rows}  # processes that took a lock: each listed for its own
found = []
for what, who, why, mode, uid, pid in rows:
    if mode != "block" or "shutdown" not in what.split(":"):
        continue
    item = {"who": who, "why": why, "mode": mode, "user": user(uid), "pid": pid,
            "process": process(pid), "still_running": []}
    if item["process"] is None and who == "APT":
        pids = sorted(int(p) for p in os.listdir(PROC) if p.isdigit() and int(p) not in taken)
        item["still_running"] = [i for i in map(process, pids) if i and i["comm"] in APT
                                 and not i["shutdown_helper"]]
    found.append(item)
print(json.dumps({"blockers": found}))
'''

# Locks taken by APT: cut off, they can leave a package half configured.
APT_NAMES = frozenset({"apt", "apt-get", "aptitude", "unattended-upgr", "dpkg"})


@dataclass(frozen=True)
class Process:
    """A process on a Ship, and where it runs."""

    pid: int
    comm: str
    user: str | None = None  # the user it runs as
    tty: str | None = None  # its controlling terminal (pts/0)
    session: dict[str, Any] | None = None  # the login session it runs in
    unit: str | None = None  # else the service it runs in
    unit_user: str | None = None  # the user whose service manager runs that unit

    @classmethod
    def of(cls, doc: dict[str, Any] | None) -> Process | None:
        if doc is None:
            return None
        return cls(**{k: doc.get(k) for k in cls.__dataclass_fields__})

    @property
    def where(self) -> str:
        """For a sentence: "in fleet's SSH login session on pts/0"."""
        if self.session:
            user = self.session.get("user") or "someone"
            tty = self.session.get("tty") or self.tty
            how = "SSH login session" if self.session.get("service") == "sshd" else "login session"
            closing = (" (logged out, but its programs still run)"
                       if self.session.get("state") == "closing" else "")
            return f"in {user}'s {how}" + (f" on {tty}" if tty else "") + closing
        if self.unit:
            return f"in {self.unit_user}'s {self.unit}" if self.unit_user else f"in {self.unit}"
        return ""

    def said(self) -> str:
        who = f"{self.comm} (PID {self.pid}" + (f", as {self.user})" if self.user else ")")
        return f"{who} {self.where}".rstrip()


@dataclass(frozen=True)
class Blocker:
    """One shutdown block lock on a Ship, and the process that took it."""

    who: str  # the name it gave logind ("APT")
    why: str  # its reason ("APT is installing or removing packages")
    mode: str
    user: str  # the user that took it
    pid: int
    process: Process | None = None  # None once that process has exited
    still_running: tuple[Process, ...] = field(default=())  # then: APT's processes left

    @property
    def is_apt(self) -> bool:
        names = {p.comm for p in (self.process, *self.still_running) if p}
        return self.who == "APT" or bool(names & APT_NAMES)

    @property
    def sessions(self) -> list[dict[str, Any]]:
        """The login sessions its process (or what is left of APT) runs in."""
        return [p.session for p in (self.process, *self.still_running) if p and p.session]

    def said(self) -> str:
        if self.process is not None:
            return f"{self.process.said()} holds a shutdown lock: \"{self.why}\""
        text = (f"a shutdown lock that {self.who} took (PID {self.pid}, which has exited) is "
                f"still held: \"{self.why}\"")
        if self.still_running:
            text += "; still running: " + ", ".join(p.said() for p in self.still_running)
        return text

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def parse(stdout: bytes | str) -> list[Blocker]:
    """The probe's output; FleetError if it is not what PROBE prints."""
    try:
        doc = json.loads(stdout)
        return [Blocker(who=item["who"], why=item["why"], mode=item["mode"],
                        user=item["user"], pid=item["pid"],
                        process=Process.of(item.get("process")),
                        still_running=tuple(Process.of(p) for p in item.get("still_running")
                                            or ()))
                for item in doc["blockers"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise FleetError(f"unreadable answer about its shutdown locks ({exc})") from None


def probe(ssh: Any, ship: Ship) -> list[Blocker] | None:
    """The shutdown block locks on the Ship now; None if it could not tell."""
    try:
        result = ssh.run(ship, ["python3", "-I", "-"], input=PROBE.encode(), timeout=60)
    except FleetError:
        return None
    if result.returncode != 0:
        return None
    try:
        return parse(result.stdout)
    except FleetError:
        return None


def force_step(ship: str) -> str:
    return (f"To reboot anyway: ship-fleet reboot {ship} --force (in the web UI, Force "
            "reboot). That overrides the lock: what holds it is cut off and open sessions "
            "are disconnected; herdr still saves every agent first.")


def describe(ship: str, blockers: list[Blocker] | None, said: str) -> tuple[str, list[str]]:
    """(summary, next steps) for a reboot logind refused; said is its own message."""
    if not blockers:
        return (f"reboot refused: {said}. Something on the Ship holds a shutdown lock "
                "(systemd-inhibit --list on the Ship shows it), most often apt running "
                "in an open login session.",
                ["Let it finish or close that session, then reboot again.", force_step(ship)])
    sessions = [s for b in blockers for s in b.sessions]
    head = "reboot refused: a login session is blocking it: " if sessions else \
        "reboot refused: "
    summary = head + "; ".join(b.said() for b in blockers) + "."
    steps = []
    open_ = sorted({s.get("user") or "someone" for s in sessions if s.get("state") != "closing"})
    if open_:
        steps.append(f"Let it finish, or close {' and '.join(open_)}'s session on the Ship "
                     "(log out of it), then reboot again.")
    if any(s.get("state") == "closing" for s in sessions):
        steps.append("That session is logged out already: let what is left of it finish, "
                     "then reboot again.")
    if any(not b.sessions for b in blockers):
        steps.append("Wait for it to finish, then reboot again.")
    steps.append(force_step(ship))
    if any(b.is_apt for b in blockers):
        steps.append("If APT is cut off mid-way, run sudo dpkg --configure -a on the Ship "
                     "afterwards.")
    return summary, steps


class RebootRefused(FleetError):
    """logind refused the reboot because something holds a shutdown block lock."""

    def __init__(self, ship: str, said: str, blockers: list[Blocker] | None) -> None:
        summary, steps = describe(ship, blockers, said)
        super().__init__(summary, hint=" ".join(steps))
        self.ship = ship
        self.said = said
        self.blockers = blockers
        self.steps = steps

    def data(self) -> dict[str, Any]:
        return {"said": self.said,
                "blockers": None if self.blockers is None
                else [b.as_dict() for b in self.blockers]}
