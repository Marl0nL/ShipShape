"""shipshape-setup (the installer at the checkout's root) and what it reads from FleetLedger,
`ship-fleet machine-setup` (fleetledger/machinesetup.py).

FleetLedger runs in-process as if on its own VM (test_refresher's `fl`): a real, unprivileged
sshd on 127.0.0.1 with the test's own authorized_keys. Its ship-fleet is a stand-in that
answers with what the real commands made beforehand (the setup data, the package, a machine's
bundle, trust's output), so the installer meets real ssh, real host keys and real forced
lines. Each computer is a sandbox (test_refresher.Machine): its own HOME, Herdr folder,
TMPDIR, and a PATH of the tools the installer needs plus fake hc, systemctl, herdr-app and
hostname. Nothing touches this computer's real ~/.ssh, Herdr, systemd or sshd.
"""

from __future__ import annotations

import getpass
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest
from fl_harness import keygen
from fleetledger.repo import Repo
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import env  # noqa: F401  (a fixture)
from test_refresher import SCRIPT, Machine, fl, serve  # noqa: F401  (fl is a fixture)

from fleetledger import authkeys, machinesetup, schemas

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "shipshape-setup"
USER = getpass.getuser()
MORE_TOOLS = ("bash", "dirname", "ln", "tar", "gzip", "cmp", "ssh-keyscan", "awk", "pwd",
              "basename", "sort")

pytestmark = [
    pytest.mark.skipif(shutil.which("ssh-keyscan") is None or shutil.which("bash") is None,
                       reason="needs OpenSSH's ssh-keyscan and bash"),
    pytest.mark.local_sshd,
]

# FleetLedger's ship-fleet, as the VM's shell or a forced line runs it: answers from files.
FAKE = """#!/bin/sh
d={d}
echo "$*" >>"$d/asked"
case "$1" in
  machine-bundle)
    if [ -f "$d/refuse" ]; then cat "$d/refuse" >&2; exit 1; fi
    cat "$d/bundle.json" ;;
  machine-setup)
    case "$2" in
      --json)
        if [ -f "$d/trusted" ]; then cat "$d/setup-after.json"; else cat "$d/setup.json"; fi ;;
      --package) cat "$d/package.tgz" ;;
      *) exit 2 ;;
    esac ;;
  trust) touch "$d/trusted"; cat "$d/trust.out" ;;
  *) echo "ship-fleet: argument command: invalid choice: '$1'" >&2; exit 2 ;;
esac
"""

RECORD = """#!/bin/sh
echo "$*" >>"{s}/{name}.log"
"""

# herdr-app: fleet.open works only while a Fleet tab is open (one tab.new opened).
HERDR_APP = """#!/bin/sh
echo "$*" >>"{s}/herdr-app.log"
case "$2" in
  fleet.open) [ -f "{s}/fleet-tab" ] ;;
  tab.new) touch "{s}/fleet-tab" ;;
esac
"""


class Computer(Machine):
    """A sandbox computer with Herdr on it (its folder, and a herdr-app that records)."""

    def __init__(self, root: Path, port: int, *, hostname="desktop", herdr=True):
        super().__init__(root, port)
        for tool in MORE_TOOLS:
            if not (self.bin / tool).exists():
                (self.bin / tool).symlink_to(shutil.which(tool))
        self._script("hostname", f"#!/bin/sh\necho {hostname}\n")
        self.herdr = self.home / ".config" / "herdr-app"
        self.tmp = root / "tmp"
        self.tmp.mkdir()
        if herdr:
            self.herdr.mkdir(parents=True)
            self._script("herdr-app", HERDR_APP.format(s=self.state))
        # ssh reads the real user's ~/.ssh whatever HOME says: as you use it (no -F), it is
        # pointed at this sandbox's config, known_hosts and key, and at no agent. A name it
        # must not look up (the seed's default name, a placeholder) fails as an unknown host
        # would.
        shim = (self.bin / "ssh").read_text().replace("fleetledger | *.invalid)",
                                                      "fleetledger | *.invalid | *.local)")
        mine = (f'case " $* " in *" -F "*) ;; *)\n'
                f'  h={self.home}/.ssh; c=$h/config; [ -f "$c" ] || c=/dev/null\n'
                '  set -- -F "$c" -o UserKnownHostsFile="$h/known_hosts" '
                '-o IdentityFile="$h/id_ed25519" -o IdentityAgent=none "$@" ;;\nesac\n')
        shim = shim.replace("exec ", mine + "exec ", 1)
        self._script("ssh", shim)

    def env(self) -> dict[str, str]:
        return {**super().env(), "TMPDIR": str(self.tmp)}

    def run(self, argv, timeout=120) -> subprocess.CompletedProcess:
        # A new session: no terminal to ask on, so every answer comes from an option.
        return subprocess.run(argv, env=self.env(), capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL, cwd=self.home,
                              start_new_session=True)

    def setup(self, *args, installer=INSTALLER) -> subprocess.CompletedProcess:
        return self.run(["sh", str(installer), *args])

    @property
    def settings(self) -> Path:
        return self.herdr / "mods" / "fleet" / "settings.json"


def fingerprint(pub: Path) -> str:
    return subprocess.run(["ssh-keygen", "-lf", str(pub)], capture_output=True, text=True,
                          check=True).stdout.split()[1]


@pytest.fixture
def vm(fl):  # noqa: F811
    """fl, with a ship-fleet that answers the installer too, and FleetLedger's checkout the
    real one (its herdr/ and shipshape-setup are what the package holds)."""
    (fl.d / "ship-fleet").write_text(FAKE.format(d=fl.d))
    fl.app.repo_factory = lambda: Repo(REPO)
    fl.dest = f"{USER}@127.0.0.1"
    fl.fp = fingerprint(fl.d / "hostkey.pub")
    return fl


def publish(e, *, after: bool = False) -> dict:
    """What machine-setup says now, for the stand-in (after: once trust has run)."""
    code, out, err = e.run("machine-setup", "--json")
    assert code == 0, err
    doc = json.loads(out)
    assert schemas.errors("machine-setup", doc) == []
    (e.d / ("setup-after.json" if after else "setup.json")).write_text(out)
    pkg = machinesetup.package(REPO)
    (e.d / "package.tgz").write_bytes(pkg)
    assert doc["package"] == {"sha256": hashlib.sha256(pkg).hexdigest(), "bytes": len(pkg)}
    return doc


def shell_line(e, pub: Path, who: str) -> str:
    """Your own line, as the seed writes one: a shell (whose PATH finds the stand-in)."""
    t, b = pub.read_text().split()[:2]
    shell = f'command="env PATH={e.d}:/usr/bin:/bin sh -c \\"$SSH_ORIGINAL_COMMAND\\""'
    return f"{shell} {t} {b} {who}\n"


def add_line(line: str) -> None:
    path = authkeys.path()
    path.write_text(line + path.read_text())


def computer(e, tmp_path, name, **kw) -> Computer:
    return Computer(tmp_path / name, e.port, **kw)


@pytest.fixture
def desktop(vm, tmp_path):
    """Your main machine: its own key (~/.ssh/id_ed25519) has a shell on the VM, and
    FleetLedger trusts it under that key, as desktop."""
    m = computer(vm, tmp_path, "desktop")
    m.key = m.home / ".ssh" / "id_ed25519"
    m.key.parent.mkdir(parents=True)
    keygen(m.key, "you@desktop")
    add_line(shell_line(vm, m.key.with_name("id_ed25519.pub"), "you@desktop"))
    code, out, err = vm.run("trust", "desktop", "--key", str(m.key) + ".pub")
    assert code == 0, out + err
    publish(vm)
    serve(vm, "desktop")
    return m


def asked(e) -> list[str]:
    p = e.d / "asked"
    return p.read_text().splitlines() if p.exists() else []


def herdr_files(m: Computer) -> dict[str, bytes]:
    return {str(p.relative_to(m.herdr)): p.read_bytes()
            for p in sorted(m.herdr.rglob("*")) if p.is_file()}


# --- your main machine ---------------------------------------------------------------------------
def test_the_main_machine_is_set_up_in_one_command(vm, desktop):
    m = desktop
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    # FleetLedger's host key, pinned on first contact (the fingerprint confirmed it).
    assert f"its host key is ED25519 {vm.fp}" in out
    known = (m.home / ".ssh" / "known_hosts").read_text().splitlines()
    assert known == [f"127.0.0.1 {vm.host_key}"]
    # Which machine it is, from its key: nothing typed, nothing trusted anew.
    assert "This computer is desktop on FleetLedger." in out
    assert not any(a.startswith("trust") for a in asked(vm))
    # The refresher: the one FleetLedger serves, fetched through the machine's own key.
    assert (m.d / "refresh").read_bytes() == SCRIPT.read_bytes()
    assert m.conf()["name"] == "desktop" and m.conf()["key"] == str(m.key)
    assert "machine-bundle desktop" in asked(vm)
    assert m.state_doc()["last_error"] is None
    assert m.log("systemctl")[-1] == "--user enable --now shipshape-refresh.timer"
    # The Desk and the Fleet panel, from FleetLedger's package.
    for mod in ("desk", "desk-agents", "desk-spaces", "fleet"):
        assert (m.herdr / "mods" / mod / "mod.json").is_file(), mod
    assert (m.herdr / "layouts" / "desk.json").is_file()
    assert (m.herdr / "mods" / "fleet" / "model.js").read_bytes() == (
        REPO / "herdr" / "mods" / "fleet" / "model.js").read_bytes()
    assert (m.home / ".local" / "bin" / "desk").is_symlink()
    # The panel's settings, from what worked: the destination, and the key ssh used (a
    # file, not an agent's).
    assert json.loads(m.settings.read_text()) == {"ssh": vm.dest,
                                                 "identity_file": "~/.ssh/id_ed25519"}
    # The Fleet tab, opened; the installer itself, kept for updates.
    assert m.log("herdr-app") == ["do fleet.open", 'do tab.new {"name":"Fleet","mod":"fleet"}']
    assert "Opened the Fleet tab in Herdr." in out
    assert (m.home / ".local" / "bin" / "shipshape-setup").read_bytes() == INSTALLER.read_bytes()
    # The refresher's own hint is not said: the installer did that part.
    assert "run shipshape-setup --herdr-client" not in out
    assert not list(m.tmp.iterdir())  # nothing left behind


def test_a_second_run_updates_and_asks_nothing(vm, desktop):
    m = desktop
    first = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert first.returncode == 0, first.stdout + first.stderr
    before = herdr_files(m)
    refresh = (m.d / "refresh").read_bytes()
    known = (m.home / ".ssh" / "known_hosts").read_text()
    # The installed copy, with no option at all: it remembers where FleetLedger is.
    proc = m.run([str(m.home / ".local" / "bin" / "shipshape-setup")])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "set up before as a Herdr client: updating it" in proc.stdout
    assert "The refresher here is current; refreshing." in proc.stdout
    assert "First contact" not in proc.stdout
    assert herdr_files(m) == before
    assert (m.d / "refresh").read_bytes() == refresh
    assert (m.home / ".ssh" / "known_hosts").read_text() == known
    assert "settings.json: yours, unchanged" in proc.stdout
    assert m.log("herdr-app")[-1] == "do fleet.open"  # the tab it opened, not another
    assert sum(x.startswith("do tab.new") for x in m.log("herdr-app")) == 1


def test_settings_you_set_by_hand_are_kept(vm, desktop):
    m = desktop
    m.settings.parent.mkdir(parents=True)
    m.settings.write_text('{"ssh": "fleetledger@fleetledger.example.invalid",\n'
                          ' "identity_file": null}\n')
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp,
                   "--no-open")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # Your ssh is for somewhere else: the key it logged in with here does not go beside it.
    assert json.loads(m.settings.read_text()) == {
        "ssh": "fleetledger@fleetledger.example.invalid", "identity_file": None}
    assert m.log("herdr-app") == []  # --no-open
    # A file with more in it than the two settings is left alone.
    odd = '{"ssh": null, "identity_file": "~/.ssh/mine", "theme": "dark"}\n'
    m.settings.write_text(odd)
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--no-open")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert m.settings.read_text() == odd and "left as it is" in proc.stdout
    # Your identity_file stays when the ssh value is filled in.
    m.settings.write_text('{"identity_file": "~/.ssh/mine"}')
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--no-open")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(m.settings.read_text()) == {"ssh": vm.dest,
                                                 "identity_file": "~/.ssh/mine"}


# --- a computer FleetLedger does not trust yet --------------------------------------------------
def test_with_a_shell_it_has_fleetledger_trust_it_once_you_say_yes(vm, tmp_path):
    m = computer(vm, tmp_path, "workdesk", hostname="Work-Desk.example.invalid")
    shell = m.home / ".ssh" / "id_ed25519"
    shell.parent.mkdir(parents=True)
    keygen(shell, "you@workdesk")
    add_line(shell_line(vm, shell.with_name("id_ed25519.pub"), "you@workdesk"))
    bundle_key = m.home / ".ssh" / "shipshape" / "id_ed25519"
    bundle_key.parent.mkdir(parents=True)
    keygen(bundle_key, "shipshape-work-desk")
    publish(vm)  # before: FleetLedger does not know it
    # What the trust it asks for makes, done for real here (the stand-in only records it).
    code, out, err = vm.run("trust", "work-desk", "--key", str(bundle_key) + ".pub")
    assert code == 0, out + err
    (vm.d / "trust.out").write_text(out)
    publish(vm, after=True)
    serve(vm, "work-desk")
    # No terminal and no --yes: it shows what it would do, and does nothing.
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 1
    assert "as work-desk, for all" in proc.stdout and "add --yes" in proc.stderr
    assert not any(a.startswith("trust") for a in asked(vm))
    assert not (m.d / "refresh").exists()
    # With --yes: trusted as shown, then set up.
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--yes")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    blob = bundle_key.with_name("id_ed25519.pub").read_text().split()[1]
    assert f"trust work-desk --key ssh-ed25519 {blob} --scope all --logins both" in asked(vm)
    assert "FleetLedger trusts this computer as work-desk." in proc.stdout
    assert "sh -c '" not in proc.stdout  # trust's paste line is not shown: nothing to paste
    assert m.conf()["name"] == "work-desk" and m.conf()["key"] == str(bundle_key)
    assert "machine-bundle work-desk" in asked(vm)
    # The panel uses your shell key, never the bundle-only one.
    assert json.loads(m.settings.read_text()) == {"ssh": vm.dest,
                                                 "identity_file": "~/.ssh/id_ed25519"}


def test_without_credentials_it_says_the_one_step_and_installs_nothing(vm, tmp_path):
    publish(vm)
    m = computer(vm, tmp_path, "laptop", hostname="laptop")
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 1
    key = m.d / "id_ed25519"
    blob = key.with_name("id_ed25519.pub").read_text().split()[1]
    err = proc.stderr
    assert "cannot trust itself on FleetLedger: no key here logs in" in err
    assert "Machines > Trust a machine, with the name laptop" in err
    assert (f"ssh {vm.dest} ship-fleet trust laptop --key 'ssh-ed25519 {blob}' --scope all "
            "--logins both") in err
    assert "paste the one line it prints" in err
    # Its key, for that step; nothing installed.
    assert sorted(p.name for p in m.d.iterdir()) == ["id_ed25519", "id_ed25519.pub"]
    assert not m.herdr.joinpath("mods").exists()
    assert asked(vm) == []  # nothing reached ship-fleet
    assert m.log("herdr-app") == [] and m.log("systemctl") == []


def test_a_bundle_only_machine_finishes_from_the_pasted_line_and_a_checkout(vm, tmp_path):
    from test_refresher import install

    m = computer(vm, tmp_path, "laptop", hostname="laptop")
    m.key = m.home / ".ssh" / "shipshape" / "id_ed25519"
    m.key.parent.mkdir(parents=True)
    keygen(m.key, "laptop")
    publish(vm)
    proc = install(vm, m)  # the line ship-fleet trust prints, pasted
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "for the Desk and the Fleet panel, run shipshape-setup --herdr-client" in proc.stdout
    # The one command left, from a checkout: no shell, so the mods come from the checkout and
    # the panel has nothing to ssh with (it shows fleet.json's Ships).
    proc = m.setup("--herdr-client")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # The host key the pasted line pinned: no question, no fingerprint to give.
    assert "the one this computer's refresher pinned" in proc.stdout
    assert (m.herdr / "mods" / "fleet" / "mod.json").is_file()
    assert not m.settings.exists() and "fleet.json" in proc.stdout
    assert not any(a.startswith("machine-setup") for a in asked(vm))
    # Once the Fleet panel is there, the pasted line has nothing more to say.
    proc = install(vm, m)
    assert proc.returncode == 0 and "shipshape-setup --herdr-client" not in proc.stdout


# --- what it never trusts ------------------------------------------------------------------------
def test_first_contact_needs_the_host_key_confirmed(vm, desktop):
    m = desktop
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--yes")
    assert proc.returncode == 1
    assert f"--host-key-fingerprint {vm.fp}" in proc.stderr
    assert not (m.home / ".ssh" / "known_hosts").exists() and asked(vm) == []
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest,
                   "--host-key-fingerprint", "SHA256:" + "A" * 43)
    assert proc.returncode == 1 and "not the fingerprint you gave" in proc.stderr
    assert not (m.home / ".ssh" / "known_hosts").exists() and asked(vm) == []


def test_a_host_key_that_differs_from_known_hosts_is_refused(vm, desktop, tmp_path):
    m = desktop
    keygen(tmp_path / "other", "other")
    other = " ".join((tmp_path / "other.pub").read_text().split()[:2])
    (m.home / ".ssh" / "known_hosts").write_text(f"127.0.0.1 {other}\n")
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest)
    assert proc.returncode == 1 and "not the one ~/.ssh/known_hosts has" in proc.stderr
    assert asked(vm) == [] and not (m.d / "refresh").exists()


def test_a_vm_that_is_not_the_one_fleetledger_records_is_refused(vm, desktop, tmp_path):
    m = desktop
    doc = json.loads((vm.d / "setup.json").read_text())
    keygen(tmp_path / "other", "other")
    doc["fleetledger"]["host_key"] = " ".join((tmp_path / "other.pub").read_text().split()[:2])
    (vm.d / "setup.json").write_text(json.dumps(doc, indent=2))
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 1 and "is not the VM FleetLedger records" in proc.stderr
    assert not (m.d / "refresh").exists() and not m.herdr.joinpath("mods").exists()


@pytest.mark.parametrize("what", ["refresher", "package"])
def test_a_refresher_or_package_it_was_not_named_is_not_installed(vm, desktop, what):
    m = desktop
    if what == "refresher":
        doc = json.loads((vm.d / "setup.json").read_text())
        doc["refresher_sha256"] = "0" * 64
        (vm.d / "setup.json").write_text(json.dumps(doc, indent=2))
    else:
        raw = (vm.d / "package.tgz").read_bytes()
        (vm.d / "package.tgz").write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 1, proc.stdout
    assert "not the one its setup data names: not installed" in proc.stderr
    assert not (m.d / "refresh").exists()
    assert not m.herdr.joinpath("mods").exists()


@pytest.mark.parametrize("change", [
    lambda d: d["machines"][0].update(name="desk top"),
    lambda d: d["fleetledger"].update(user="fleet;ledger"),
    lambda d: d["fleetledger"].update(hosts=["127.0.0.1 -oProxyCommand=x"]),
    lambda d: d.update(refresher_sha256="$(id)"),
    lambda d: d["fleetledger"].update(hosts=["a\\\"b"]),
])
def test_a_value_out_of_place_is_refused(vm, desktop, change):
    m = desktop
    doc = json.loads((vm.d / "setup.json").read_text())
    change(doc)
    (vm.d / "setup.json").write_text(json.dumps(doc, indent=2))
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 1, proc.stdout
    assert "out of place" in proc.stderr or "not a document" in proc.stderr
    assert not (m.d / "refresh").exists()


# --- the other roles ----------------------------------------------------------------------------
def test_the_roles_not_built_yet_say_so(vm, tmp_path):
    m = computer(vm, tmp_path, "box", herdr=False)
    proc = m.setup("--fleetledger-remote", "--ship-host")
    assert proc.returncode == 1
    assert "not available as one step yet" in proc.stderr
    assert "ship-fleet appliance seed" in proc.stderr
    assert "a host for Ships: not available yet" in proc.stderr
    proc = m.setup()
    assert proc.returncode == 2 and "--herdr-client, --fleetledger-here" in proc.stderr
    # A downloaded copy, with no checkout beside it (one beside it would run its install).
    copy = m.root / "dl" / "shipshape-setup"
    copy.parent.mkdir()
    shutil.copy(INSTALLER, copy)
    proc = m.setup("--fleetledger-here", installer=copy)
    assert proc.returncode == 1 and "installs from a ShipShape checkout" in proc.stderr


def test_herdr_missing_installs_the_refresher_and_says_so(vm, desktop, tmp_path):
    m = computer(vm, tmp_path, "server", herdr=False)
    m.key = m.home / ".ssh" / "id_ed25519"
    m.key.parent.mkdir(parents=True)
    shutil.copy(desktop.key, m.key)
    shutil.copy(str(desktop.key) + ".pub", str(m.key) + ".pub")
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Herdr is not installed here" in proc.stdout
    assert (m.d / "refresh").is_file() and not m.herdr.exists()


# --- ship-fleet machine-setup ---------------------------------------------------------------------
def test_machine_setup_lists_trusted_machines_and_how_their_key_gets_in(vm, tmp_path):
    keygen(tmp_path / "laptop", "laptop")
    keygen(tmp_path / "mine", "mine")
    add_line(shell_line(vm, tmp_path / "mine.pub", "you"))
    for name, key in (("laptop", "laptop"), ("desktop", "mine"), ("gone", "laptop2")):
        if name == "gone":
            keygen(tmp_path / key, key)
        assert vm.run("trust", name, "--key", str(tmp_path / f"{key}.pub"))[0] == 0
    assert vm.run("untrust", "gone", "--yes")[0] == 0
    doc = publish(vm)
    by = {m["name"]: m for m in doc["machines"]}
    assert by.keys() == {"laptop", "desktop"}  # an untrusted one is left out
    assert by["laptop"]["access"] == "bundle" and by["desktop"]["access"] == "shell"
    assert by["laptop"]["key"] == " ".join((tmp_path / "laptop.pub").read_text().split()[:2])
    assert doc["fleetledger"] == {"name": "fleetledger", "user": USER,
                                  "hosts": ["127.0.0.1", "fleetledger"],
                                  "host_key": vm.host_key}
    assert doc["refresher_sha256"] == hashlib.sha256(SCRIPT.read_bytes()).hexdigest()


def test_the_package_is_the_installer_and_herdr_less_its_tests_the_same_each_time():
    one, two = machinesetup.package(REPO), machinesetup.package(REPO)
    assert one == two
    with tarfile.open(fileobj=io.BytesIO(one)) as tar:
        names = tar.getnames()
        assert all(m.isfile() and m.uid == 0 and m.mtime == 0 for m in tar.getmembers())
        link = tar.extractfile("herdr/mods/desk/fleet-model.js").read()
    assert "shipshape-setup" in names and "herdr/install-desk.sh" in names
    assert "herdr/mods/fleet/main.js" in names and "herdr/layouts/desk.json" in names
    assert not any(n.startswith("herdr/tests") or "__pycache__" in n for n in names)
    assert link == (REPO / "herdr" / "mods" / "fleet" / "model.js").read_bytes()


def test_machine_setup_prints_the_package_and_the_installer(vm):
    vm.app.out_bytes = io.BytesIO()
    code, _, err = vm.run("machine-setup", "--package")
    assert code == 0, err
    assert vm.app.out_bytes.getvalue() == machinesetup.package(REPO)
    code, out, _ = vm.run("machine-setup", "--installer")
    assert code == 0 and out == INSTALLER.read_text()
    code, _, err = vm.run("machine-setup")
    assert code == 1 and "--json, --package or --installer" in err


def test_machine_setup_answers_only_on_its_own_vm(vm, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "desktop")
    code, out, err = vm.run("machine-setup", "--json")
    assert code == 1 and out == "" and "FleetLedger's own VM" in err


def test_the_installer_is_executable_posix_shell_at_the_root():
    assert INSTALLER.read_bytes().startswith(b"#!/bin/sh\n")
    assert INSTALLER.stat().st_mode & 0o111


def test_a_newer_installer_from_fleetledger_does_the_rest_itself(vm, desktop, tmp_path):
    # FleetLedger's checkout has another shipshape-setup than the one run here.
    root = tmp_path / "vm-checkout"
    shutil.copytree(REPO / "herdr", root / "herdr", ignore=shutil.ignore_patterns("tests"),
                    symlinks=True)
    newer = INSTALLER.read_text().replace('say "FleetLedger: $DEST"',
                                          'say "FleetLedger (the newer installer): $DEST"')
    (root / "shipshape-setup").write_text(newer)
    pkg = machinesetup.package(root)
    (vm.d / "package.tgz").write_bytes(pkg)
    doc = json.loads((vm.d / "setup.json").read_text())
    doc["package"] = {"sha256": hashlib.sha256(pkg).hexdigest(), "bytes": len(pkg)}
    (vm.d / "setup.json").write_text(json.dumps(doc, indent=2))
    m = desktop
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "FleetLedger has another version of this installer; it does the rest." in proc.stdout
    assert "FleetLedger (the newer installer): " in proc.stdout
    assert (m.home / ".local" / "bin" / "shipshape-setup").read_text() == newer
    assert (m.d / "refresh").read_bytes() == SCRIPT.read_bytes()
    assert not list(m.tmp.iterdir())  # the handed-over copy went with it


def test_the_installer_piped_from_fleetledger_needs_no_checkout(vm, desktop):
    """ssh ... ship-fleet machine-setup --installer | sh -s -- ...: not a file, no checkout."""
    m = desktop
    line = (f"cat {INSTALLER} | sh -s -- --herdr-client --fleetledger {vm.dest} "
            f"--host-key-fingerprint {vm.fp} --no-open")
    proc = m.run(["sh", "-c", line])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (m.d / "refresh").read_bytes() == SCRIPT.read_bytes()
    assert (m.herdr / "mods" / "fleet" / "mod.json").is_file()
    assert (m.home / ".local" / "bin" / "shipshape-setup").read_bytes() == INSTALLER.read_bytes()
    assert "another version" not in proc.stdout  # what runs is what FleetLedger serves


def test_a_package_with_a_link_in_it_is_not_installed(vm, desktop, tmp_path):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        info = tarfile.TarInfo("herdr/mods/fleet/main.js")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(info)
    pkg = raw.getvalue()
    (vm.d / "package.tgz").write_bytes(pkg)
    doc = json.loads((vm.d / "setup.json").read_text())
    doc["package"] = {"sha256": hashlib.sha256(pkg).hexdigest(), "bytes": len(pkg)}
    (vm.d / "setup.json").write_text(json.dumps(doc, indent=2))
    m = desktop
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 1 and "other than plain files" in proc.stderr
    assert not m.herdr.joinpath("mods").exists()


def test_an_older_fleetledger_is_told_to_update_first(vm, desktop):
    (vm.d / "ship-fleet").write_text(
        "#!/bin/sh\necho 'usage: ship-fleet [-h] {setup,doctor,...} ...' >&2\n"
        "echo \"ship-fleet: error: argument command: invalid choice: '$1'\" >&2\nexit 2\n")
    m = desktop
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp)
    assert proc.returncode == 1
    assert "is older than this installer: update it there first" in proc.stderr
    assert "OpenSSH" not in proc.stderr


def test_refresher_options_on_a_later_run_are_applied(vm, desktop):
    m = desktop
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp,
                   "--no-open")
    assert proc.returncode == 0 and m.conf()["hc"] == "on"
    proc = m.setup("--herdr-client", "--no-hc", "--no-open")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert m.conf()["hc"] == "off" and "is current; refreshing" not in proc.stdout


def test_fleetledger_out_of_reach_still_updates_a_computer_set_up_before(vm, desktop):
    m = desktop
    proc = m.setup("--herdr-client", "--fleetledger", vm.dest, "--host-key-fingerprint", vm.fp,
                   "--no-open")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (m.home / ".ssh" / "known_hosts").unlink()
    # Nothing answers there (the sandbox's sshd is on 127.0.0.1 only).
    proc = m.setup("--herdr-client", "--fleetledger", f"{USER}@127.0.0.2", "--no-open",
                   installer=m.home / ".local" / "bin" / "shipshape-setup")  # no checkout
    assert proc.returncode == 0, proc.stdout + proc.stderr  # its refresh uses machine.conf
    assert "Could not reach FleetLedger" in proc.stderr
    assert "the Desk and the Fleet panel: kept as they are" in proc.stdout
