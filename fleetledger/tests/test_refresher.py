"""The refresher a trusted machine runs (fleetledger/refresher/refresh.sh), and the line
`ship-fleet trust` prints to install it there.

FleetLedger runs in-process (caenv) as if on its own VM: `trust` writes the machine's forced
line into the test's own authorized_keys, and a real, unprivileged sshd on 127.0.0.1 serves
it. The forced command is a stand-in for ship-fleet that answers with the bundle the real
`ship-fleet machine-bundle` made (or a hostile one a test wrote), or refuses.

Each machine is a sandbox: its own HOME, and a PATH of its own holding only the tools the
refresher needs, an `ssh` that adds the sshd's port and reaches nothing but 127.0.0.1, and
fake hc, systemctl, launchctl (and uname, for macOS). Nothing touches this computer's real
~/.ssh, systemd, launchd or hc.
"""

from __future__ import annotations

import base64
import copy
import getpass
import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest
from fl_harness import keygen, start_sshd
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import RUNNING_CORE, env  # noqa: F401  (env is a fixture)

from fleetledger import appliance, authkeys, refresher, schemas, status

SSHD = os.environ.get("FLEETLEDGER_TEST_SSHD") or shutil.which("sshd") or "/usr/sbin/sshd"
SCRIPT = Path(refresher.__file__).with_name(refresher.SCRIPT)
TOOLS = ("sh", "sed", "tr", "head", "cut", "grep", "mkdir", "mv", "rm", "cat", "date",
         "mktemp", "chmod", "wc", "cp", "id", "sleep", "readlink", "base64", "ssh-keygen", "env",
         "ls", "touch", "tail", "find", "sha256sum")
REAL_SSH = shutil.which("ssh") or "/usr/bin/ssh"

pytestmark = [
    pytest.mark.skipif(shutil.which("ssh-keygen") is None or shutil.which("ssh") is None,
                       reason="needs OpenSSH"),
    pytest.mark.local_sshd,
]

# The forced command: what FleetLedger's ship-fleet would answer, from files the test leaves.
FAKE_FLEETLEDGER = """#!/bin/sh
d={d}
echo "$*" >>"$d/asked"
if [ -f "$d/refuse" ]; then cat "$d/refuse" >&2; exit 1; fi
cat "$d/bundle.json"
"""

# ssh as the sandbox sees it: the real one, to the test's sshd only. A name (the VM's bare
# name) is never looked up: it fails as an unknown host would.
SSH_SHIM = """#!/bin/sh
for a in "$@"; do
  case $a in fleetledger | *.invalid)
    echo "ssh: Could not resolve hostname $a (a test)" >&2; exit 255 ;;
  esac
done
exec {ssh} -p {port} -o HostKeyAlias=127.0.0.1 "$@"
"""

FAKE_HC = """#!/bin/sh
# hc's machines, one name a line, in {s}/hc.
echo "$*" >>"{s}/hc.log"
touch "{s}/hc"
case "$1 $2" in
  "machine ls")
    printf '{{"machines": ['; sep=''
    while read -r n; do
      printf '%s{{"name": "%s", "target": "%s"}}' "$sep" "$n" "$n"; sep=','
    done <"{s}/hc"
    printf ']}}\\n' ;;
  "machine add")
    grep -qx "$3" "{s}/hc" && {{ echo "already added" >&2; exit 1; }}
    echo "$3" >>"{s}/hc" ;;
  "machine rm")
    grep -qx "$3" "{s}/hc" || {{ echo "no such machine" >&2; exit 1; }}
    grep -vx "$3" "{s}/hc" >"{s}/hc.new"; mv "{s}/hc.new" "{s}/hc" ;;
  *) exit 2 ;;
esac
"""

RECORDER = """#!/bin/sh
echo "$*" >>"{s}/{name}.log"
"""


class Machine:
    """A sandbox computer the refresher runs on."""

    def __init__(self, root: Path, port: int, *, awk: str = "awk", hc: bool = True,
                 systemd: bool = True, os_name: str | None = None):
        self.root = root
        self.home = root / "home"
        self.bin = root / "bin"
        self.state = root / "state"
        for d in (self.home, self.bin, self.state):
            d.mkdir(parents=True)
        for tool in TOOLS:
            (self.bin / tool).symlink_to(shutil.which(tool))
        self.set_awk(awk)
        self._script("ssh", SSH_SHIM.format(ssh=REAL_SSH, port=port))
        if hc:
            self._script("hc", FAKE_HC.format(s=self.state))
        if systemd:
            self._script("systemctl", RECORDER.format(s=self.state, name="systemctl"))
        self._script("launchctl", RECORDER.format(s=self.state, name="launchctl"))
        self._script("uname", f"#!/bin/sh\necho {os_name or 'Linux'}\n")
        self.d = self.home / ".ssh" / "shipshape"

    def set_awk(self, awk: str) -> None:
        words = awk.split()
        target = self.bin / "awk"
        target.unlink(missing_ok=True)
        if len(words) == 1:
            target.symlink_to(shutil.which(words[0]))
        else:
            rest = " ".join(words[1:])
            self._script("awk", f'#!/bin/sh\nexec {shutil.which(words[0])} {rest} "$@"\n')

    def _script(self, name: str, text: str) -> None:
        p = self.bin / name
        p.write_text(text)
        p.chmod(0o755)

    def env(self) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": str(self.bin), "LANG": "C"}

    def run(self, argv, timeout=120) -> subprocess.CompletedProcess:
        return subprocess.run(argv, env=self.env(), capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, cwd=self.home)

    def refresh(self, *args) -> subprocess.CompletedProcess:
        return self.run([str(self.d / "refresh"), *args])

    def files(self) -> dict[str, bytes]:
        """Every file under HOME, by path, with its bytes."""
        return {str(p.relative_to(self.home)): p.read_bytes()
                for p in sorted(self.home.rglob("*")) if p.is_file() and not p.is_symlink()}

    def hc(self) -> list[str]:
        p = self.state / "hc"
        return p.read_text().split() if p.exists() else []

    def log(self, name: str) -> list[str]:
        p = self.state / f"{name}.log"
        return p.read_text().splitlines() if p.exists() else []

    def state_doc(self) -> dict:
        return json.loads((self.d / "state.json").read_text())

    def conf(self) -> dict[str, str]:
        return dict(x.split("=", 1) for x in (self.d / "machine.conf").read_text().splitlines()
                    if x and not x.startswith("#"))


@pytest.fixture
def fl(caenv, tmp_path, short_dir, monkeypatch):  # noqa: F811
    """FleetLedger on its own VM (named fleetledger, reached at 127.0.0.1), served by a real
    sshd whose host key is the one FleetLedger recorded for itself."""
    if not os.access(SSHD, os.X_OK):
        pytest.skip("no sshd binary (set FLEETLEDGER_TEST_SSHD)")
    e = caenv
    e.tmp = tmp_path
    d = short_dir
    keygen(d / "hostkey", "host")
    pub = (d / "hostkey.pub").read_text().split()
    e.host_key = f"{pub[0]} {pub[1]}"
    appliance.record_self(e.store, {"name": "fleetledger", "names": ["127.0.0.1"],
                                    "host_key": e.host_key})
    monkeypatch.setattr(socket, "gethostname", lambda: "fleetledger")
    fake = d / "ship-fleet"
    fake.write_text(FAKE_FLEETLEDGER.format(d=d))
    fake.chmod(0o755)
    monkeypatch.setattr(authkeys, "SHIP_FLEET", str(fake))
    authkeys.path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    authkeys.path().write_text("")
    authkeys.path().chmod(0o600)
    proc, port = start_sshd(SSHD, d, lambda port: (
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d / 'hostkey'}\n"
        f"AuthorizedKeysFile {authkeys.path()}\nUsePAM no\nStrictModes no\nPidFile none\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"))
    e.port, e.d = port, d
    # Every Ship runs herdr-core with a host certificate (its last status says so).
    for name in ("ship-01", "ship-02"):
        status.write_cache(name, RUNNING_CORE)
    yield e
    proc.terminate()
    proc.wait(timeout=5)


def serve(e, name="laptop") -> dict:
    """The bundle the real ship-fleet machine-bundle makes, served by the forced command."""
    code, out, err = e.run("machine-bundle", name)
    assert code == 0, err
    (e.d / "bundle.json").write_text(out)
    return json.loads(out)


def serve_doc(e, doc) -> None:
    (e.d / "bundle.json").write_text(doc if isinstance(doc, str) else json.dumps(doc, indent=2))


def machine(e, tmp_path, name="laptop", *, key_at=".ssh/shipshape/id_ed25519", **kw) -> Machine:
    m = Machine(tmp_path / name, e.port, **kw)
    key = m.home / key_at
    key.parent.mkdir(parents=True, exist_ok=True)
    keygen(key, name)
    m.key = key
    return m


def paste_line(e, m: Machine, name="laptop", *trust_args) -> str:
    code, out, err = e.run("trust", name, "--key", str(m.key.with_name(m.key.name + ".pub")),
                           *trust_args)
    assert code == 0, out + err
    lines = [x for x in out.splitlines() if x.startswith("sh -c '")]
    assert len(lines) == 1, out
    return lines[0]


def install(e, m: Machine, name="laptop", *extra) -> subprocess.CompletedProcess:
    line = paste_line(e, m, name)
    serve(e, name)
    return m.run(["sh", "-c", " ".join([line, *extra])])


@pytest.fixture
def laptop(fl, tmp_path):
    m = machine(fl, tmp_path)
    proc = install(fl, m)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return m


# --- the first install ----------------------------------------------------------------------------
@pytest.mark.parametrize("key_at", [".ssh/shipshape/id_ed25519", ".ssh/id_ed25519",
                                    ".ssh/work/laptop_key"])
def test_the_line_trust_prints_installs_the_refresher_and_records_name_and_key(
        fl, tmp_path, key_at):
    m = machine(fl, tmp_path, key_at=key_at)
    proc = install(fl, m)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Refreshed laptop: 2 Ship(s)" in proc.stdout
    # The refresher, executable, the very script FleetLedger serves.
    refresh = m.d / "refresh"
    assert refresh.read_bytes() == SCRIPT.read_bytes()
    assert refresh.stat().st_mode & 0o777 == 0o700
    # What nobody types again: the name it is trusted under and where its key is.
    assert m.conf() == {"name": "laptop", "key": str(m.key), "user": getpass.getuser(),
                        "hosts": "127.0.0.1 fleetledger", "host_key": fl.host_key,
                        "host_names": "overlay", "hc": "on", "hc_path": str(m.bin / "hc")}
    # Its certificate, for its key, and the SSH config from the refresher's template.
    cert = m.d / "laptop-cert.pub"
    fp = subprocess.run(["ssh-keygen", "-lf", str(m.key) + ".pub"], capture_output=True,
                        text=True).stdout.split()[1]
    listing = subprocess.run(["ssh-keygen", "-L", "-f", str(cert)], capture_output=True,
                             text=True).stdout
    assert f"Public key: ED25519-CERT {fp}" in listing
    config = (m.d / "config").read_text()
    for ship in ("ship-01", "ship-02"):
        assert f"\nHost {ship}\n" in config
    ident = "~/" + key_at
    assert f"  IdentityFile {ident}\n" in config
    assert "  CertificateFile ~/.ssh/shipshape/laptop-cert.pub\n" in config
    assert "@cert-authority " in (m.d / "known_hosts").read_text()
    assert (m.home / ".ssh" / "config").read_text().splitlines()[0] == f"Include {m.d}/config"
    # fleet.json: the bundle as checked, without the refresher; state.json for the panel.
    fleet = json.loads((m.d / "fleet.json").read_text())
    assert "refresher" not in fleet
    assert schemas.errors("machine-bundle", fleet) == []
    assert [s["name"] for s in fleet["ships"]] == ["ship-01", "ship-02"]
    st = m.state_doc()
    assert schemas.errors("refresh-state", st) == []
    assert st["last_error"] is None and st["ships"] == 2 and st["machine"] == "laptop"
    assert st["certificate_valid_before"] == fleet["certificate"]["valid_before"]
    # herdr-core Ships went to hc, and the refresher keeps a list of them.
    assert m.hc() == ["ship-01", "ship-02"]
    assert (m.d / "hc-added").read_text().split() == ["ship-01", "ship-02"]
    # The timer: written and started (fake systemctl), hourly and at login.
    assert m.log("systemctl") == ["--user daemon-reload",
                                  "--user enable --now shipshape-refresh.timer"]
    # Nothing left of the first fetch; every file private.
    assert not list(m.d.glob(".first-*")) and not list(m.d.glob(".stage*"))
    for p in m.d.iterdir():
        if p.name.startswith("id_ed25519"):
            continue  # your key, made by you
        assert p.stat().st_mode & 0o077 == 0, p
    # FleetLedger heard two asks (the line's fetch, the first refresh), by this name alone.
    assert set((fl.d / "asked").read_text().splitlines()) == {"machine-bundle laptop"}


def test_no_public_key_file_for_the_trusted_key_says_so(fl, tmp_path):
    m = machine(fl, tmp_path)
    line = paste_line(fl, m)
    m.key.with_name(m.key.name + ".pub").unlink()
    proc = m.run(["sh", "-c", line])
    assert proc.returncode != 0
    assert "No .pub file under ~/.ssh holds the key trusted as laptop." in proc.stderr
    assert not (m.d / "refresh").exists()


def test_the_line_runs_only_the_refresher_it_names(fl, tmp_path):
    m = machine(fl, tmp_path)
    line = paste_line(fl, m)
    pwned = m.root / "pwned"
    doc = serve(fl)
    doc["refresher"] = base64.b64encode(f"#!/bin/sh\ntouch {pwned}\n".encode()).decode()
    serve_doc(fl, doc)
    proc = m.run(["sh", "-c", line])
    assert proc.returncode != 0 and "is not the one this line names" in proc.stderr
    assert not pwned.exists() and not (m.d / "refresh").exists()


def test_a_ship_named_like_a_host_of_your_own_is_left_out(fl, laptop):
    cfg = laptop.home / ".ssh" / "config"
    cfg.write_text(cfg.read_text() + "Host nas SHIP-02\n  HostName nas.example.invalid\n")
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert "Left out: ship-02" in proc.stderr
    assert "Host ship-02" not in (laptop.d / "config").read_text()
    assert laptop.state_doc()["warning"].startswith("Left out: ship-02")
    assert laptop.hc() == ["ship-01"]  # the one it added for ship-02 went too


def test_no_timer_flag_leaves_no_timer(fl, tmp_path):
    m = machine(fl, tmp_path)
    proc = install(fl, m, "laptop", "--no-timer")
    assert proc.returncode == 0, proc.stderr
    assert "No timer (--no-timer)" in proc.stdout
    assert m.log("systemctl") == []
    assert not (m.home / ".config").exists()


def test_the_line_needs_a_fleetledger_that_serves_the_refresher(fl, tmp_path):
    m = machine(fl, tmp_path)
    line = paste_line(fl, m)
    doc = serve(fl)
    del doc["refresher"]
    serve_doc(fl, doc)
    proc = m.run(["sh", "-c", line])
    assert proc.returncode != 0 and "FleetLedger sent no refresher" in proc.stderr
    assert not (m.d / "refresh").exists()


# --- the timer, as text -------------------------------------------------------------------------
def test_the_systemd_units_and_their_removal(laptop):
    units = laptop.home / ".config" / "systemd" / "user"
    service = (units / "shipshape-refresh.service").read_text()
    timer = (units / "shipshape-refresh.timer").read_text()
    assert "Type=oneshot\n" in service and "ExecStart=%h/.ssh/shipshape/refresh\n" in service
    for line in ("OnStartupSec=1min", "OnCalendar=hourly", "Persistent=true",
                 "WantedBy=timers.target"):
        assert f"{line}\n" in timer
    proc = laptop.refresh("remove-timer")
    assert proc.returncode == 0 and "Removed the systemd user timer" in proc.stdout
    assert not (units / "shipshape-refresh.timer").exists()
    assert not (units / "shipshape-refresh.service").exists()
    assert "--user disable --now shipshape-refresh.timer" in laptop.log("systemctl")


def test_without_systemd_it_says_to_run_it_another_way(fl, tmp_path):
    m = machine(fl, tmp_path, systemd=False)
    proc = install(fl, m)
    assert proc.returncode == 0, proc.stderr
    assert "No timer: this computer has no systemd" in proc.stdout


def test_the_launchd_agent_on_macos_and_its_removal(fl, tmp_path):
    m = machine(fl, tmp_path, os_name="Darwin")
    proc = install(fl, m)
    assert proc.returncode == 0, proc.stderr
    plist = m.home / "Library" / "LaunchAgents" / "local.shipshape.refresh.plist"
    text = plist.read_text()
    assert f"<string>{m.home}/.ssh/shipshape/refresh</string>" in text
    assert "<key>RunAtLoad</key>\n  <true/>" in text
    assert "<key>StartInterval</key>\n  <integer>3600</integer>" in text
    uid = os.getuid()
    assert m.log("launchctl") == [f"bootout gui/{uid}/local.shipshape.refresh",
                                  f"bootstrap gui/{uid} {plist}"]
    assert m.log("systemctl") == []
    proc = m.refresh("remove-timer")
    assert proc.returncode == 0 and not plist.exists()
    assert m.log("launchctl")[-1] == f"bootout gui/{uid}/local.shipshape.refresh"


# --- refreshing ---------------------------------------------------------------------------------
def test_a_new_ship_appears_and_a_destroyed_one_goes(fl, laptop):
    with fl.store.edit() as ed:
        ed.add_ship("ship-03", {"id": "3c0f3a5e-0000-4000-8000-000000000003", "driver": "manual",
                                "tags": ["work"], "names": ["ship-03.local"],
                                "host_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGVo"
                                            "b3N0a2V5c2hpcDAzZXhhbXBsZWhvc3Rr"})
    status.write_cache("ship-03", RUNNING_CORE)
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert "\nHost ship-03\n" in (laptop.d / "config").read_text()
    assert laptop.hc() == ["ship-01", "ship-02", "ship-03"]
    assert "hc added ship-03." in proc.stdout
    with fl.store.edit() as ed:
        ed.remove_ship("ship-03")
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert "Host ship-03" not in (laptop.d / "config").read_text()
    assert laptop.hc() == ["ship-01", "ship-02"]
    assert "machine rm ship-03" in laptop.log("hc")
    assert (laptop.d / "hc-added").read_text().split() == ["ship-01", "ship-02"]


def test_a_machine_it_did_not_add_survives(fl, tmp_path):
    m = machine(fl, tmp_path)
    (m.state / "hc").write_text("ship-01\nmy-box\n")  # added by you (or install.sh)
    proc = install(fl, m)
    assert proc.returncode == 0, proc.stderr
    assert m.hc() == ["ship-01", "my-box", "ship-02"]
    assert (m.d / "hc-added").read_text().split() == ["ship-02"]
    with fl.store.edit() as ed:
        ed.remove_ship("ship-01")
        ed.remove_ship("ship-02")
    serve(fl)
    assert m.refresh().returncode == 0
    assert m.hc() == ["ship-01", "my-box"]  # only its own ship-02 went
    assert not any(x.startswith("machine rm ship-01") or "my-box" in x for x in m.log("hc"))


def test_a_ship_that_leaves_herdr_core_leaves_hc(fl, laptop):
    herdr = copy.deepcopy(RUNNING_CORE)
    herdr["herdr"]["runtime"] = "herdr"
    status.write_cache("ship-02", herdr)
    serve(fl)
    assert laptop.refresh().returncode == 0
    assert laptop.hc() == ["ship-01"]
    assert "\nHost ship-02\n" in (laptop.d / "config").read_text()  # still a Ship to ssh to


def test_a_second_run_changes_nothing(fl, laptop):
    before = laptop.files()
    hc_calls = len(laptop.log("hc"))
    serve(fl)  # the same bundle (its "at" moves on)
    assert laptop.refresh().returncode == 0
    after = laptop.files()
    changed = {k for k in before if before[k] != after.get(k)} | (after.keys() - before.keys())
    assert changed <= {".ssh/shipshape/state.json", ".ssh/shipshape/fleet.json"}
    assert [x for x in laptop.log("hc")[hc_calls:] if not x.startswith("machine ls")] == []


def test_a_refresh_writes_nothing_outside_its_own_files(fl, laptop):
    (laptop.home / ".ssh" / "config").write_text(
        (laptop.home / ".ssh" / "config").read_text() + "Host mine\n  HostName example.invalid\n")
    before = laptop.files()
    with fl.store.edit() as ed:
        ed.remove_ship("ship-02")
    serve(fl)
    assert laptop.refresh().returncode == 0
    after = laptop.files()
    changed = {k for k in before if before[k] != after.get(k)} | (after.keys() - before.keys())
    own = {f".ssh/shipshape/{f}" for f in ("laptop-cert.pub", "known_hosts", "config",
                                           "fleet.json", "state.json", "hc-added")}
    assert changed and changed <= own


def test_an_old_lock_is_taken_over_even_when_its_pid_lives_again(fl, laptop):
    (laptop.d / ".lock").mkdir()
    (laptop.d / ".lock" / "pid").write_text(f"{os.getpid()}\n")  # alive: a reused PID
    old = laptop.d / ".lock"
    os.utime(old, (old.stat().st_atime - 3600, old.stat().st_mtime - 3600))
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert not (laptop.d / ".lock").exists()


def test_a_stale_lock_is_taken_over(fl, laptop):
    (laptop.d / ".lock").mkdir()
    (laptop.d / ".lock" / "pid").write_text("999999999\n")
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    assert not (laptop.d / ".lock").exists()


# --- what fails keeps the last good state -------------------------------------------------------
def assert_kept(m: Machine, before: dict, proc, says: str):
    assert proc.returncode == 1
    assert says in proc.stderr, proc.stderr
    assert len(proc.stderr.strip().splitlines()) == 1, proc.stderr
    after = m.files()
    changed = {k for k in before if before[k] != after.get(k)} | (after.keys() - before.keys())
    assert changed <= {".ssh/shipshape/state.json"}, changed
    st = m.state_doc()
    assert schemas.errors("refresh-state", st) == []
    assert st["last_error"] == proc.stderr.strip()
    assert st["last_success"] is not None and st["last_success"] <= st["last_attempt"]
    assert st["ships"] == 2


def test_a_refusal_keeps_the_last_good_state(fl, laptop):
    before = laptop.files()
    (fl.d / "refuse").write_text(
        "ship-fleet: laptop has not asked for its bundle for 30 days: ship-fleet trust laptop "
        "--allow-again lets it ask again.\x1b[31m\n")
    proc = laptop.refresh()
    assert_kept(laptop, before, proc, "FleetLedger refused this machine's bundle: ship-fleet: "
                "laptop has not asked for its bundle for 30 days")
    assert "\x1b" not in proc.stderr  # FleetLedger's words, printable characters only
    # And once it is allowed again, it works.
    (fl.d / "refuse").unlink()
    serve(fl)
    assert laptop.refresh().returncode == 0
    assert laptop.state_doc()["last_error"] is None


@pytest.mark.parametrize("code", ["vault-locked", "sign-failed"])
def test_a_certificate_that_was_due_and_not_signed_keeps_the_last_good_state(fl, laptop, code):
    before = laptop.files()
    doc = serve(fl)
    doc["ships"] = doc["ships"][:1]
    doc["certificate"]["not_renewed"] = {"code": code, "why": "a reason"}
    serve_doc(fl, doc)
    proc = laptop.refresh()
    says = "vault is locked" if code == "vault-locked" else "could not sign"
    assert_kept(laptop, before, proc, says)


def test_no_certificate_keeps_the_last_good_state(fl, laptop):
    before = laptop.files()
    doc = serve(fl)
    doc["certificate"].update(line=None, valid_before=None)
    serve_doc(fl, doc)
    assert_kept(laptop, before, laptop.refresh(), "FleetLedger has no certificate for laptop")


def test_an_untrusted_machine_gets_nothing_and_keeps_nothing_new(fl, laptop):
    before = laptop.files()
    code, out, err = fl.run("untrust", "laptop", "--yes")
    assert code == 0, err
    proc = laptop.refresh()
    assert_kept(laptop, before, proc, "Could not reach FleetLedger (ssh to")
    assert "Permission denied" in proc.stderr


def test_a_different_host_key_is_never_trusted(fl, laptop, tmp_path):
    other = keygen(tmp_path / "other-host", "other").with_name("other-host.pub").read_text()
    conf = (laptop.d / "machine.conf").read_text().replace(
        fl.host_key, " ".join(other.split()[:2]))
    (laptop.d / "machine.conf").write_text(conf)
    before = laptop.files()
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 1
    assert "Host key verification failed" in proc.stderr
    after = laptop.files()
    assert {k for k in before if before[k] != after.get(k)} <= {".ssh/shipshape/state.json"}


def test_settings_it_does_not_know_are_refused(fl, laptop):
    conf = laptop.d / "machine.conf"
    for bad in ("name=laptop;touch pwned", "key=relative/key", "hosts=a b;c",
                "user=root;x", "evil=1", "host_key=ssh-rsa AAAA", "hosts="):
        good = conf.read_text()
        key = bad.split("=")[0]
        lines = [x for x in good.splitlines() if not x.startswith(f"{key}=")]
        conf.write_text("\n".join(lines + [bad]) + "\n")
        proc = laptop.refresh()
        assert proc.returncode == 1 and "machine.conf" in proc.stderr, (bad, proc.stderr)
        conf.write_text(good)
    assert not (laptop.home / "pwned").exists()


# --- hostile bundles ----------------------------------------------------------------------------
INJECT = "x\n  ProxyCommand touch {pwned}"


def _set(doc, path, value):
    *head, last = path
    for k in head:
        doc = doc[k]
    doc[last] = value


HOSTILE = {
    "ship name with a newline": (("ships", 0, "name"), "ship-01\nProxyCommand touch PWNED"),
    "ship name with a glob": (("ships", 0, "name"), "*"),
    "ship named like FleetLedger": (("ships", 0, "name"), "fleetledger"),
    "host name with a space": (("ships", 0, "host_names", "lan"), "a ProxyCommand=touch PWNED"),
    "host name with %": (("ships", 0, "host_names", "lan"), "%h.example.invalid"),
    "overlay name with a quote": (("ships", 0, "host_names", "overlay"), 'a"b'),
    "tag with a space": (("ships", 0, "tags"), ["work x"]),
    "runtime not known": (("ships", 0, "runtime"), "shell"),
    "user with a semicolon": (("ssh", "user"), "agent;touch PWNED"),
    "certificate not a certificate": (("certificate", "line"),
                                      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA== x"),
    "certificate with a newline": (("certificate", "line"),
                                   "ssh-ed25519-cert-v01@openssh.com AAAA\nProxyCommand x"),
    "CA line for every host": (("known_hosts",), ["@cert-authority * ssh-ed25519 "
                                                  "AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGU="]),
    "CA line naming FleetLedger": (("known_hosts",), ["@cert-authority 127.0.0.1 ssh-ed25519 "
                                                      "AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGU="]),
    "CA line naming FleetLedger in capitals": (("known_hosts",), [
        "@cert-authority FLEETLEDGER,ship-01 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGU="]),
    "a Ship's host name is FleetLedger's": (("ships", 0, "host_names", "lan"), "127.0.0.1"),
    "a Ship named localhost": (("ships", 0, "name"), "localhost"),
    "two CA lines": (("known_hosts",),
                     ["@cert-authority a ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA==",
                      "@cert-authority b ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA=="]),
    "a script beside the data": (("install_sh",), "#!/bin/sh\ntouch PWNED\n"),
    "another machine's bundle": (("machine",), "desktop"),
    "another schema": (("schema",), "shipshape.machine-bundle/2"),
    "a ship field it does not know": (("ships", 0, "proxy_command"), "touch PWNED"),
    "valid_before not a time": (("certificate", "valid_before"), "$(touch PWNED)"),
    "idle days not a number": (("idle_limit", "days"), "30"),
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_a_hostile_value_is_refused_and_nothing_changes(fl, laptop, case):
    pwned = laptop.root / "pwned"
    path, value = HOSTILE[case]
    doc = serve(fl)
    value = json.loads(json.dumps(value).replace("PWNED", str(pwned)))
    _set(doc, path, value)
    serve_doc(fl, doc)
    before = laptop.files()
    proc = laptop.refresh()
    assert proc.returncode == 1, proc.stdout
    assert ("not a document this refresher takes" in proc.stderr
            or "has a value out of place" in proc.stderr), proc.stderr
    after = laptop.files()
    assert {k for k in before if before[k] != after.get(k)} <= {".ssh/shipshape/state.json"}
    assert not pwned.exists()


RAW_HOSTILE = {
    "duplicate key": lambda t: t.replace('"machine": "laptop"',
                                         '"machine": "laptop", "machine": "laptop"', 1),
    "trailing text": lambda t: t + "\ntouch pwned\n",
    "two documents": lambda t: t + t,
    "not JSON": lambda t: "#!/bin/sh\ntouch pwned\n",
    "a byte that is not ASCII": lambda t: t.replace('"laptop"', '"laptöp"', 1),
    "a NUL": lambda t: t.replace('"at"', '"a\x00t"', 1),
    "deep nesting": lambda t: t[:-2] + ', "x": ' + "[" * 50 + "]" * 50 + "}\n",
    "an upper-case key": lambda t: t.replace('"machine"', '"Machine"', 1),
    "a control character in a string": lambda t: t.replace('"laptop"', '"lap\\ttop"', 1),
    "empty": lambda t: "",
}


@pytest.mark.parametrize("case", sorted(RAW_HOSTILE))
def test_a_malformed_answer_is_refused_and_nothing_changes(fl, laptop, case):
    serve(fl)
    text = (fl.d / "bundle.json").read_text()
    (fl.d / "bundle.json").write_bytes(RAW_HOSTILE[case](text).encode("utf-8"))
    before = laptop.files()
    proc = laptop.refresh()
    assert proc.returncode == 1, proc.stdout
    after = laptop.files()
    assert {k for k in before if before[k] != after.get(k)} <= {".ssh/shipshape/state.json"}
    assert not (laptop.home / "pwned").exists()


def test_an_answer_over_1_mib_is_refused(fl, laptop):
    doc = serve(fl)
    doc["refresher"] = "A" * (1024 * 1024 + 8)
    serve_doc(fl, doc)
    before = laptop.files()
    proc = laptop.refresh()
    assert proc.returncode == 1 and "1 MiB" in proc.stderr, proc.stderr
    after = laptop.files()
    assert {k for k in before if before[k] != after.get(k)} <= {".ssh/shipshape/state.json"}


def test_a_refresh_never_runs_or_installs_the_refresher_it_is_sent(fl, laptop):
    pwned = laptop.root / "pwned"
    evil = f"#!/bin/sh\ntouch {pwned}\n".encode()
    doc = serve(fl)
    doc["refresher"] = base64.b64encode(evil).decode()
    serve_doc(fl, doc)
    mine = (laptop.d / "refresh").read_bytes()
    assert laptop.refresh().returncode == 0
    assert (laptop.d / "refresh").read_bytes() == mine == SCRIPT.read_bytes()
    assert not pwned.exists()
    assert "refresher" not in json.loads((laptop.d / "fleet.json").read_text())


def test_a_certificate_for_another_key_is_refused(fl, laptop, tmp_path):
    other = keygen(tmp_path / "desk", "desk").with_name("desk.pub")
    code, out, err = fl.run("trust", "desk", "--key", str(other))
    assert code == 0, err
    desk = serve(fl, "desk")
    doc = serve(fl)
    doc["certificate"]["line"] = desk["certificate"]["line"]
    serve_doc(fl, doc)
    before = laptop.files()
    proc = laptop.refresh()
    assert proc.returncode == 1 and "for another key" in proc.stderr, proc.stderr
    assert {k for k, v in before.items() if laptop.files().get(k) != v} <= {
        ".ssh/shipshape/state.json"}


# --- other awks ---------------------------------------------------------------------------------
AWKS = [a for a in ("mawk", "gawk --posix", "busybox awk", "original-awk")
        if shutil.which(a.split()[0])]


@pytest.mark.parametrize("awk", AWKS)
def test_other_awks_read_and_refuse_the_same(fl, laptop, awk):
    laptop.set_awk(awk)
    serve(fl)
    proc = laptop.refresh()
    assert proc.returncode == 0, proc.stderr
    doc = serve(fl)
    doc["ships"][0]["name"] = "ship-01\nProxyCommand x"
    serve_doc(fl, doc)
    proc = laptop.refresh()
    assert proc.returncode == 1 and "value out of place" in proc.stderr, proc.stderr
    text = (fl.d / "bundle.json").read_text()
    serve_doc(fl, text.replace('"machine"', '"machine": 1, "machine"'))
    assert laptop.refresh().returncode == 1


# --- the main machine: your own key, which has a shell --------------------------------------------
def test_your_own_key_asks_by_name_through_its_shell_with_the_same_line(fl, tmp_path):
    m = machine(fl, tmp_path, "desktop")
    pub = m.key.with_name(m.key.name + ".pub").read_text().split()
    # Your own line, as the seed writes one: a shell (here, one whose PATH finds the stand-in).
    shell = f'command="env PATH={fl.d}:/usr/bin:/bin sh -c \\"$SSH_ORIGINAL_COMMAND\\""'
    authkeys.path().write_text(f"{shell} {pub[0]} {pub[1]} you@desktop\n")
    line = paste_line(fl, m, "desktop")
    assert "# >>>" not in authkeys.path().read_text()  # no restricted line for your key
    serve(fl, "desktop")
    proc = m.run(["sh", "-c", line])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert m.conf()["name"] == "desktop"
    assert set((fl.d / "asked").read_text().splitlines()) == {"machine-bundle desktop"}
    assert m.state_doc()["machine"] == "desktop"


# --- the line itself ----------------------------------------------------------------------------
def test_the_line_carries_only_checked_values():
    good = dict(name="laptop", key_blob="AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGU=",
                user="fleetledger", hosts=["fleetledger.local", "192.0.2.10"],
                host_key="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGV4YW1wbGU=")
    line = refresher.first_install_line(**good)
    assert line.startswith("sh -c '") and line.endswith("' sh") and line.count("'") == 2
    for k, v in [("name", "lap'top"), ("name", "a b"), ("user", "x;y"), ("hosts", ["a b"]),
                 ("hosts", ["a'b"]), ("hosts", []), ("key_blob", "AAAA'"),
                 ("host_key", "ssh-rsa AAAAB3"), ("host_key", "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5'")]:
        assert refresher.first_install_line(**{**good, k: v}) is None, (k, v)


def test_trust_prints_no_line_off_its_own_vm(fl, tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "desktop")
    m = machine(fl, tmp_path)
    code, out, err = fl.run("trust", "laptop", "--key", str(m.key) + ".pub")
    assert code == 0, err
    assert "sh -c '" not in out and "copy the folder over" in out


def test_the_script_is_executable_shell_shipped_with_fleetledger():
    assert SCRIPT.read_bytes().startswith(b"#!/bin/sh\n")
    assert base64.b64decode(refresher.script_b64()) == SCRIPT.read_bytes()
    mode = SCRIPT.stat().st_mode
    assert stat.S_ISREG(mode)
