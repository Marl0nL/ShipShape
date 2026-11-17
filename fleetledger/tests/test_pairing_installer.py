"""Pairing by code end to end: shipshape-setup on a computer with no credential asks a real,
unprivileged sshd on 127.0.0.1 through the well-known pairing key's forced line; the forced
command is the real `ship-fleet machine-pair` (in a child process, on this test's FleetLedger
folders); the approval is made in-process, as you would on a trusted device.

The computers are test_setup_installer's sandboxes (their own HOME, Herdr folder, TMPDIR and
PATH); nothing touches this computer's real ~/.ssh, Herdr, systemd, sshd or FleetLedger.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from fl_harness import keygen
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import env  # noqa: F401  (a fixture)
from test_refresher import fl, serve  # noqa: F401  (fl is a fixture)
from test_setup_installer import FAKE, INSTALLER, REPO, asked, computer, publish, vm  # noqa: F401

from fleetledger import authkeys, ca, pairing

pytestmark = [
    pytest.mark.skipif(shutil.which("ssh-keyscan") is None or shutil.which("bash") is None,
                       reason="needs OpenSSH's ssh-keyscan and bash"),
    pytest.mark.local_sshd,
]

# The real machine-pair, run as sshd runs the forced line: on this test's FleetLedger folders,
# as on its own VM (named fleetledger), with TTL from $d/ttl when a test sets one.
BOOT = """
import os, socket, sys
from datetime import timedelta
socket.gethostname = lambda: "fleetledger"
from fleetledger import cli, pairing
ttl = os.path.join({d!r}, "ttl")
if os.path.exists(ttl):
    pairing.TTL = timedelta(seconds=int(open(ttl).read()))
sys.exit(cli.main(["machine-pair"]))
"""

PAIR_CASE = """  machine-pair)
    exec env {env} {python} -c "$(cat "$d/boot.py")" ;;
  machine-bundle)
    i=0
    while [ ! -f "$d/bundle.json" ] && [ "$i" -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
    cat "$d/bundle.json" ;;
"""


@pytest.fixture
def pairvm(vm, monkeypatch):  # noqa: F811
    """test_setup_installer's vm, with pairing on and the real machine-pair as its forced
    command."""
    keep = {k: os.environ[k] for k in ("FLEETLEDGER_CONFIG_DIR", "FLEETLEDGER_DATA_DIR",
                                       "FLEETLEDGER_RUNTIME_DIR")}
    keep["PYTHONPATH"] = str(REPO / "fleetledger")
    keep["HOME"] = str(vm.tmp / "vm-home")
    (vm.tmp / "vm-home").mkdir()
    (vm.d / "boot.py").write_text(BOOT.format(d=str(vm.d)))
    words = " ".join(f"{k}={v}" for k, v in keep.items())
    fake = FAKE.format(d=vm.d).replace(
        "  machine-bundle)\n    if [ -f \"$d/refuse\" ]; then cat \"$d/refuse\" >&2; exit 1; fi\n"
        "    cat \"$d/bundle.json\" ;;\n",
        PAIR_CASE.format(env=words, python=sys.executable))
    assert "machine-pair)" in fake
    (vm.d / "ship-fleet").write_text(fake)
    publish(vm)
    code, out, err = vm.run("pair", "on")
    assert code == 0, out + err
    return vm


class Run:
    """The installer running in the background, its output read as it comes."""

    def __init__(self, m, *args):
        environ = {**m.env(), "SHIPSHAPE_SETUP_PAIR_WAIT": "1"}
        self.proc = subprocess.Popen(["sh", str(INSTALLER), *args], env=environ, cwd=m.home,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.out: list[str] = []
        self.err: list[str] = []
        self.threads = [threading.Thread(target=self._read, args=(f, into), daemon=True)
                        for f, into in ((self.proc.stdout, self.out), (self.proc.stderr, self.err))]
        for t in self.threads:
            t.start()

    @staticmethod
    def _read(f, into):
        for line in f:
            into.append(line)

    def shown(self, until: str, timeout: float = 60) -> str:
        deadline = time.monotonic() + timeout
        while until not in "".join(self.out):
            if self.proc.poll() is not None:
                time.sleep(0.2)
                if until in "".join(self.out):
                    break
                raise AssertionError(f"it stopped first:\n{''.join(self.out)}{''.join(self.err)}")
            if time.monotonic() > deadline:
                self.proc.kill()
                raise AssertionError(f"never said {until!r}:\n{''.join(self.out)}")
            time.sleep(0.05)
        return "".join(self.out)

    def finish(self, timeout: float = 120) -> tuple[int, str, str]:
        self.proc.wait(timeout=timeout)
        for t in self.threads:
            t.join(timeout=10)
        return self.proc.returncode, "".join(self.out), "".join(self.err)


def start(m, *args) -> Run:
    return Run(m, *args)


def shown(run: Run, until: str, timeout: float = 60) -> str:
    return run.shown(until, timeout)


def finish(run: Run, timeout: float = 120) -> tuple[int, str, str]:
    return run.finish(timeout)


def the_code(out: str) -> tuple[str, str]:
    code = re.search(r"Code: +([0-9]{3}) ([0-9]{3})", out)
    pid = re.search(r"ship-fleet pair approve (p[0-9a-f]{6})", out)
    assert code and pid, out
    return code.group(1) + code.group(2), pid.group(1)


def test_a_new_computer_is_paired_by_code_and_finishes_as_a_trusted_run(pairvm, tmp_path):
    e = pairvm
    m = computer(e, tmp_path, "laptop", hostname="Laptop.example.invalid")
    proc = start(m, "--herdr-client", "--fleetledger", e.dest, "--host-key-fingerprint", e.fp)
    out = shown(proc, "Waiting for it")
    code, pid = the_code(out)
    key = m.d / "id_ed25519"
    blob = key.with_name("id_ed25519.pub").read_text().split()[1]
    from fleetledger import sshkeys

    # Shown there: its own key's fingerprint and FleetLedger's host key, pinned first.
    assert f"This computer's key:    ED25519 {sshkeys.fingerprint('ssh-ed25519 ' + blob)}" in out
    assert f"FleetLedger's host key: ED25519 {e.fp}" in out
    assert f"its host key is ED25519 {e.fp}" in out.split("Code:")[0]
    # On FleetLedger: one request, its key, the name it gave itself; nothing trusted yet.
    (row,) = pairing.listing()
    assert row["id"] == pid and row["host"] == "laptop"
    assert row["fingerprint"] == sshkeys.fingerprint(f"ssh-ed25519 {blob}")
    assert row["source"] == "127.0.0.1"
    assert ca.machine(e.store.load(), "laptop") is None
    before = [x for _, x in authkeys.read().block]
    assert before == [authkeys.pairing_line()]
    # You approve it, typing its code.
    rc, said, err = e.run("pair", "approve", pid, "--code", code, "--yes")
    assert rc == 0, said + err
    serve(e, "laptop")
    rc, out, err = finish(proc)
    assert rc == 0, out + err
    assert "FleetLedger approved this computer as laptop." in out
    # Then as a trusted run: its refresher, through its own key; the Desk and the Fleet panel
    # from this checkout; no settings.json (it has no shell there).
    assert m.conf()["name"] == "laptop" and m.conf()["key"] == str(key)
    assert "machine-bundle laptop" in asked(e)
    assert (m.d / "refresh").is_file() and m.state_doc()["last_error"] is None
    assert (m.herdr / "mods" / "fleet" / "mod.json").is_file()
    assert not m.settings.exists()
    # authorized_keys: the one approved line, beside the pairing key's.
    assert [x for _, x in authkeys.read().block] == [
        authkeys.line_for("laptop", f"ssh-ed25519 {blob}"), authkeys.pairing_line()]
    # No key text was copied by hand: the only key on FleetLedger is the one it made.
    assert ca.machine(e.store.load(), "laptop").public_key == f"ssh-ed25519 {blob}"
    assert not list(m.tmp.iterdir())  # nothing left behind here (the pairing key included)


def test_a_refused_computer_says_so_and_installs_nothing(pairvm, tmp_path):
    e = pairvm
    m = computer(e, tmp_path, "laptop", hostname="laptop")
    proc = start(m, "--herdr-client", "--fleetledger", e.dest, "--host-key-fingerprint", e.fp)
    _code, pid = the_code(shown(proc, "Waiting for it"))
    assert e.run("pair", "refuse", pid)[0] == 0
    rc, out, err = finish(proc)
    assert rc == 1 and "refused this computer's pairing request" in err
    assert sorted(p.name for p in m.d.iterdir()) == ["id_ed25519", "id_ed25519.pub"]
    assert not m.herdr.joinpath("mods").exists()
    assert ca.machine(e.store.load(), "laptop") is None
    assert [x for _, x in authkeys.read().block] == [authkeys.pairing_line()]


def test_a_request_that_lapses_says_so(pairvm, tmp_path):
    e = pairvm
    (e.d / "ttl").write_text("3")
    m = computer(e, tmp_path, "laptop", hostname="laptop")
    proc = start(m, "--herdr-client", "--fleetledger", e.dest, "--host-key-fingerprint", e.fp)
    rc, out, err = finish(proc, timeout=60)
    assert rc == 1 and "lapsed" in err, out + err
    assert not (m.d / "refresh").exists()
    assert ca.machine(e.store.load(), "laptop") is None


def test_ctrl_c_withdraws_the_request(pairvm, tmp_path):
    e = pairvm
    m = computer(e, tmp_path, "laptop", hostname="laptop")
    proc = start(m, "--herdr-client", "--fleetledger", e.dest, "--host-key-fingerprint", e.fp)
    shown(proc, "Waiting for it")
    assert len(pairing.listing()) == 1
    os.killpg(proc.proc.pid, signal.SIGINT)
    finish(proc)
    assert pairing.listing() == []
    assert not list(m.tmp.iterdir())


def test_a_second_requester_is_not_approved_with_your_code(pairvm, tmp_path):
    e = pairvm
    mine = computer(e, tmp_path, "laptop", hostname="laptop")
    theirs = computer(e, tmp_path, "intruder", hostname="laptop")  # the same claimed name
    p1 = start(mine, "--herdr-client", "--fleetledger", e.dest, "--host-key-fingerprint", e.fp)
    code1, pid1 = the_code(shown(p1, "Waiting for it"))
    p2 = start(theirs, "--herdr-client", "--fleetledger", e.dest, "--host-key-fingerprint", e.fp)
    code2, pid2 = the_code(shown(p2, "Waiting for it"))
    assert code1 != code2 and pid1 != pid2
    # Your code, typed, approves your computer alone.
    rc, said, err = e.run("pair", "approve", "--code", code1, "--yes")
    assert rc == 0, said + err
    serve(e, "laptop")
    rc, out, err = finish(p1)
    assert rc == 0, out + err
    theirs_blob = (theirs.d / "id_ed25519.pub").read_text().split()[1]
    assert not any(theirs_blob in x for _, x in authkeys.read().block)
    rc2, out2, err2 = e.run("pair", "refuse", pid2)
    assert rc2 == 0
    rc, out, err = finish(p2)
    assert rc == 1 and "refused" in err
    assert not (theirs.d / "refresh").exists()


def test_with_pairing_off_it_says_the_step_by_hand(vm, tmp_path):  # noqa: F811
    publish(vm)
    m = computer(vm, tmp_path, "laptop", hostname="laptop")
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 1
    assert "Could not ask to be paired by code: FleetLedger takes no pairing request" in proc.stderr
    assert "Machines > Trust a machine, with the name laptop" in proc.stderr
    assert asked(vm) == []  # sshd refused the pairing key: nothing reached ship-fleet
    # --no-pairing: straight to the step by hand.
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--no-pairing")
    assert proc.returncode == 1 and "Could not ask" not in proc.stderr
    assert "Machines > Trust a machine" in proc.stderr


def _pairing_key(tmp: Path) -> Path:
    text = INSTALLER.read_text()
    body = re.search(r"^PAIR_KEY_BODY='([A-Za-z0-9+/=\n]+)'$", text, re.M).group(1)
    key = tmp / "pairing"
    key.write_text(f"-----BEGIN OPENSSH {'PRIVATE KEY'}-----\n{body}\n"
                   f"-----END OPENSSH {'PRIVATE KEY'}-----\n")
    key.chmod(0o600)
    return key


def _ssh(e, key: Path, *argv: str, opts=()) -> subprocess.CompletedProcess:
    import getpass

    return subprocess.run(
        ["ssh", "-p", str(e.port), *opts, "-F", "/dev/null", "-i", str(key),
         "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
         "-o", "LogLevel=ERROR", "-o", f"User={getpass.getuser()}", "127.0.0.1", *argv],
        capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)


@pytest.mark.parametrize("command", [
    [], ["sh", "-c", "touch {p}"], ["ship-fleet", "ls"], ["machine-bundle", "laptop"],
    ["touch {p}; echo"], ["$(touch {p})"], ["request x laptop; touch {p}"],
    ["`touch {p}`"], ["status 0000; touch {p}"]])
def test_the_pairing_key_can_only_ask(pairvm, command):
    e = pairvm
    key = _pairing_key(e.tmp)
    pwned = e.tmp / "pwned"
    proc = _ssh(e, key, *[c.format(p=pwned) for c in command])
    doc = json.loads(proc.stdout)
    assert doc == {"schema": pairing.SCHEMA, "state": "refused", "why": pairing.BAD}
    assert not pwned.exists()
    assert not pairing.requests_path().exists()
    # No terminal for it either.
    tty = _ssh(e, key, opts=["-tt"])
    assert "PTY allocation request failed" in tty.stderr


def test_a_waiting_computers_own_key_logs_in_to_nothing(pairvm, tmp_path):
    e = pairvm
    keygen(tmp_path / "laptop", "laptop")
    blob = (tmp_path / "laptop.pub").read_text().split()[1]
    proc = _ssh(e, _pairing_key(e.tmp), f"request {blob} laptop")
    assert json.loads(proc.stdout)["state"] == "pending"
    mine = _ssh(e, tmp_path / "laptop", "machine-bundle", "laptop")
    assert mine.returncode == 255 and "ermission denied" in mine.stderr
    assert "machine-bundle laptop" not in asked(e)
