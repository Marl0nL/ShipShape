"""Why a Ship refused to reboot (fleetledger.inhibitors).

PROBE is the script FleetLedger runs on the Ship as fleet. Here it runs for real
with this Python, against a made-up /proc and stand-ins for busctl and loginctl
on PATH, with the data logind and loginctl give on a Ship (systemd 259).
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
from fleetledger.errors import FleetError
from fleetledger.ssh import Result

from fleetledger import inhibitors

PTS0 = 136 << 8  # tty_nr of /dev/pts/0
PTS300 = (137 << 8) | 44  # /dev/pts/300
NOBODY = 4_000_000  # a uid with no name here


def manager(uid: int) -> str:
    """The cgroup of a user's systemd service manager (its per-user unit)."""
    return f"/user.slice/user-{uid}.slice/user@{uid}.service"


def _proc(root, pid, comm, cgroup, uid=0, tty_nr=0, cmdline=""):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "comm").write_text(comm + "\n")
    (d / "cgroup").write_text(f"0::{cgroup}\n")
    (d / "stat").write_text(f"{pid} ({comm}) S 1 {pid} {pid} {tty_nr} -1 4194560 0 0\n")
    (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (d / "cmdline").write_text(cmdline.replace(" ", "\0"))


@pytest.fixture
def ship_side(tmp_path):
    """A Ship's /proc and logind, as the probe sees them."""
    proc = tmp_path / "proc"
    proc.mkdir()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    inhibitors_list = [
        ["shutdown:sleep", "APT", "APT is installing or removing packages", "block", 0, 4242],
        ["shutdown", "APT", "APT is installing or removing packages", "block", 0, 812],
        ["shutdown", "APT", "APT is installing or removing packages", "block", 0, 5000],
        ["shutdown", "Herdr", "Save terminal workspace layout", "delay", NOBODY, 900],
        ["sleep", "ModemManager", "ModemManager needs to reset devices", "delay", 0, 950],
        ["shutdown", "Weak", "root may pass this one", "block-weak", 0, 960],
        ["sleep", "Sleepy", "only blocks suspend", "block", 0, 970],
        ["shutdown", "Tool", "a lock taken in a user service", "block", 0, 6000],
    ]
    (tmp_path / "inhibitors.json").write_text(json.dumps(
        {"type": "a(ssssuu)", "data": [inhibitors_list]}))
    (bin_ / "busctl").write_text(f"#!/bin/sh\ncat {tmp_path / 'inhibitors.json'}\n")
    sessions = {"7": "Name=fleet\nService=sshd\nTTY=\nState=active",
                "9": "Name=sam\nService=sshd\nTTY=\nState=closing"}
    (bin_ / "loginctl").write_text(
        "#!/bin/sh\ncase \"$2\" in\n"
        + "".join(f"  {k}) printf '%s\\n' '{v}' ;;\n" for k, v in sessions.items())
        + "  *) exit 1 ;;\nesac\n")
    for script in bin_.iterdir():
        script.chmod(0o755)
    _proc(proc, 4242, "apt", "/user.slice/user-1000.slice/session-7.scope", tty_nr=PTS0)
    _proc(proc, 812, "unattended-upgr", "/system.slice/unattended-upgrades.service",
          cmdline="/usr/bin/python3 /usr/bin/unattended-upgrade")
    # 5000 has exited; what is left of its apt, in a logged-out session:
    _proc(proc, 5001, "apt-get", "/user.slice/user-1001.slice/session-9.scope",
          tty_nr=PTS300)
    _proc(proc, 5002, "sleep", "/user.slice/user-1001.slice/session-9.scope")
    _proc(proc, 1306, "unattended-upgr", "/system.slice/unattended-upgrades.service",
          cmdline="/usr/bin/python3 /usr/share/unattended-upgrades/unattended-upgrade-shutdown"
                  " --wait-for-signal")
    _proc(proc, 900, "herdr", manager(1001) + "/app.slice/herdr.service", uid=NOBODY)
    _proc(proc, 6000, "systemd-inhibit", manager(0) + "/app.slice/tool.service")
    return proc, bin_


def run_probe(proc, bin_):
    return subprocess.run([sys.executable, "-I", "-", str(proc)], input=inhibitors.PROBE,
                          capture_output=True, text=True, env={"PATH": f"{bin_}:/usr/bin:/bin"},
                          timeout=60)


def test_the_probe_finds_every_shutdown_block_lock_and_where_its_holder_runs(ship_side):
    done = run_probe(*ship_side)
    assert done.returncode == 0, done.stderr
    found = {b.pid: b for b in inhibitors.parse(done.stdout)}
    # Delay, block-weak and sleep-only locks never refuse a root reboot.
    assert sorted(found) == [812, 4242, 5000, 6000]

    in_session = found[4242].process
    assert (in_session.comm, in_session.user, in_session.tty) == ("apt", "root", "pts/0")
    assert in_session.session == {"id": "7", "user": "fleet", "service": "sshd", "tty": None,
                                  "state": "active"}
    assert in_session.where == "in fleet's SSH login session on pts/0"

    service = found[812].process
    assert (service.unit, service.unit_user, service.session) == \
        ("unattended-upgrades.service", None, None)
    assert found[6000].process.unit == "tool.service"
    assert found[6000].process.unit_user == "root"

    gone = found[5000]
    assert gone.process is None
    # APT's own helper is left (the shutdown helper and the hook's sleep are not APT's).
    assert [(p.pid, p.comm, p.tty) for p in gone.still_running] == [(5001, "apt-get",
                                                                     "pts/300")]
    assert gone.still_running[0].session["state"] == "closing"


def test_the_probe_says_so_when_logind_cannot_be_asked(ship_side):
    proc, bin_ = ship_side
    (bin_ / "busctl").write_text("#!/bin/sh\nexit 1\n")
    assert run_probe(proc, bin_).returncode == 3


def test_an_unreadable_probe_answer_is_an_error():
    for junk in ("", "not json", '{"blockers": [{"who": "x"}]}', "[]"):
        with pytest.raises(FleetError):
            inhibitors.parse(junk)


class _SSH:
    def __init__(self, code, out):
        self.code, self.out, self.calls = code, out, []

    def run(self, ship, command, *, input=None, timeout=60):
        self.calls.append((command, input))
        return Result(self.code, self.out.encode(), "", "ship-01.local")


def test_probe_runs_the_script_on_stdin_as_fleet_and_gives_none_when_it_cannot_tell():
    ssh = _SSH(0, '{"blockers": []}')
    assert inhibitors.probe(ssh, object()) == []
    assert ssh.calls == [(["python3", "-I", "-"], inhibitors.PROBE.encode())]  # no sudo
    assert inhibitors.probe(_SSH(3, ""), object()) is None
    assert inhibitors.probe(_SSH(0, "garbage"), object()) is None


def _blocker(**process):
    base = {"pid": 4242, "comm": "apt", "user": "root", "tty": "pts/0", "session": None,
            "unit": None, "unit_user": None}
    return inhibitors.Blocker("APT", "APT is installing or removing packages", "block",
                              "root", 4242, inhibitors.Process(**{**base, **process}))


def test_a_lock_in_an_open_session_names_the_session_and_says_close_it_or_force():
    session = {"id": "7", "user": "fleet", "service": "sshd", "tty": None, "state": "active"}
    summary, steps = inhibitors.describe("ship-01", [_blocker(session=session)], "denied")
    assert summary == ('reboot refused: a login session is blocking it: apt (PID 4242, as '
                       "root) in fleet's SSH login session on pts/0 holds a shutdown lock: "
                       '"APT is installing or removing packages".')
    assert steps[0].startswith("Let it finish, or close fleet's session on the Ship")
    assert steps[1].startswith("To reboot anyway: ship-fleet reboot ship-01 --force")
    assert "sudo dpkg --configure -a" in steps[2]


def test_a_logged_out_session_with_apt_still_running_says_let_it_finish():
    session = {"id": "9", "user": "sam", "service": "sshd", "tty": None, "state": "closing"}
    left = inhibitors.Process(5001, "apt-get", "root", "pts/3", session)
    gone = inhibitors.Blocker("APT", "APT is installing or removing packages", "block",
                              "root", 5000, None, (left,))
    summary, steps = inhibitors.describe("ship-01", [gone], "denied")
    assert "a shutdown lock that APT took (PID 5000, which has exited) is still held" in summary
    assert ("still running: apt-get (PID 5001, as root) in sam's SSH login session on pts/3 "
            "(logged out, but its programs still run)") in summary
    assert steps[0] == ("That session is logged out already: let what is left of it finish, "
                        "then reboot again.")


def test_a_lock_held_by_a_service_says_wait_for_it():
    summary, steps = inhibitors.describe(
        "ship-01", [_blocker(comm="unattended-upgr", tty=None,
                             unit="unattended-upgrades.service")], "denied")
    assert summary == ("reboot refused: unattended-upgr (PID 4242, as root) in "
                       'unattended-upgrades.service holds a shutdown lock: "APT is installing '
                       'or removing packages".')
    assert steps[0] == "Wait for it to finish, then reboot again."


def test_a_lock_not_taken_by_apt_has_no_dpkg_step():
    other = inhibitors.Blocker("Backup", "Copying the disk", "block", "root", 77,
                               inhibitors.Process(77, "backup", "root", None, None,
                                                  "backup.service"))
    summary, steps = inhibitors.describe("ship-01", [other], "denied")
    assert 'backup (PID 77, as root) in backup.service holds a shutdown lock: "Copying' in summary
    assert not any("dpkg" in s for s in steps)


def test_refused_carries_the_steps_as_its_hint_and_its_data():
    exc = inhibitors.RebootRefused("ship-01", "denied", [_blocker()])
    assert exc.message.startswith("reboot refused: apt (PID 4242, as root) holds")
    assert exc.hint == " ".join(exc.steps)
    assert exc.data()["blockers"][0]["process"]["comm"] == "apt"
