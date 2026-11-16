"""The trusted machines' bundle keys in FleetLedger's own authorized_keys (authkeys.py):
`ship-fleet trust` adds a machine's line, `untrust` removes it, `trust --sync-keys`
rewrites the block, `doctor` checks it, `ca show` says each machine's state.

The file is always the test's own (conftest: no_real_authorized_keys). The last part
runs a real, unprivileged sshd on 127.0.0.1 against it, with a fake ship-fleet as the
forced command, and checks what a key can and cannot do through its line.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import socket
import stat
import subprocess
import threading
from pathlib import Path

import pytest
from fl_harness import keygen, start_sshd
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import env  # noqa: F401  (a fixture)

from fleetledger import appliance, authkeys, ca, handover, schemas

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

VM = "fleetledger"
OWNER_LINES = ('# my keys, from the seed\n'
               'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8g '
               'you@desktop\n'
               'from="192.0.2.0/24",no-pty ssh-ed25519 '
               'AAAAC3NzaC1lZDI1NTE5AAAAIHNlbGZzZWxmc2VsZnNlbGZzZWxmc2VsZnNlbGZzZWxm  spaced\r\n')


def on_vm(e, monkeypatch, *, here: bool = True) -> None:
    """[fleet] self names this computer (or, here=False, another one)."""
    key = keygen(e.tmp / "vm-host", "vm")
    pub = key.with_name("vm-host.pub").read_text().split()
    appliance.record_self(e.store, {"name": VM, "names": [f"{VM}.local"],
                                    "host_key": f"{pub[0]} {pub[1]}"})
    monkeypatch.setattr(socket, "gethostname", lambda: VM if here else "desktop")


@pytest.fixture
def vm(caenv, tmp_path, monkeypatch):  # noqa: F811
    caenv.tmp = tmp_path
    on_vm(caenv, monkeypatch)
    return caenv


def ak() -> Path:
    return authkeys.path()


def seed(text: str = OWNER_LINES, mode: int = 0o600) -> bytes:
    ak().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ak().write_bytes(text.encode())
    ak().chmod(mode)
    return text.encode()


def block() -> list[str]:
    lines = ak().read_text().split("\n")
    if authkeys.BEGIN not in lines:
        return []
    return lines[lines.index(authkeys.BEGIN) + 1:lines.index(authkeys.END)]


def outside() -> bytes:
    """The file with FleetLedger's block (markers and all) taken out."""
    lines = ak().read_bytes().split(b"\n")
    if authkeys.BEGIN.encode() not in lines:
        return ak().read_bytes()
    b, e = lines.index(authkeys.BEGIN.encode()), lines.index(authkeys.END.encode())
    return b"\n".join(lines[:b] + lines[e + 1:])


def trust(e, name="laptop", *more, key=None):
    key = key or keygen(e.tmp / name, name).with_name(f"{name}.pub")
    code, out, err = e.run("trust", name, "--key", str(key), *more)
    assert code == 0, out + err
    return key.read_text().split()[1], out


def doctor_item(e):
    from fleetledger import firstrun

    found = [i for i in firstrun.everything(e.app, offline=True) if i.name == "bundle keys"]
    return found[0] if found else None


def line(name: str, blob: str) -> str:
    return (f'restrict,command="{authkeys.SHIP_FLEET} machine-bundle {name}" '
            f"ssh-ed25519 {blob} {name}")


# --- trust and untrust --------------------------------------------------------------------
def test_trust_adds_one_forced_line_and_untrust_removes_it(vm):
    before = seed()
    blob, out = trust(vm)
    assert block() == [line("laptop", blob)]
    assert outside() == before  # your lines, byte for byte (the \r\n line too)
    assert "Bundle key added to" in out and "for laptop" in out
    assert f"From laptop, ssh -i ~/.ssh/shipshape/id_ed25519 {getpass.getuser()}@<this VM> " \
           "now returns its bundle as JSON" in out
    assert out.index("now returns its bundle") < out.index("copy the folder over")
    code, out, err = vm.run("untrust", "laptop", "--yes")
    assert "Bundle key line(s) removed from" in out and "laptop" in out
    assert ak().read_bytes() == before  # the whole block goes with its last line
    assert doctor_item(vm).state == "ok"


def test_the_line_is_restrict_and_one_command_and_nothing_else(vm):
    seed("")
    blob, _ = trust(vm)
    (got,) = block()
    options, keytype, key, comment = got.split(" machine-bundle laptop\" ")[0], *got.rsplit(
        " ", 3)[1:]
    assert options == f'restrict,command="{authkeys.SHIP_FLEET}'
    assert (keytype, key, comment) == ("ssh-ed25519", blob, "laptop")
    assert authkeys.SHIP_FLEET == "/home/fleetledger/.local/bin/ship-fleet"  # what the wrapper runs


def test_a_machine_that_never_asked_starts_its_idle_clock_when_its_line_goes_in(vm):
    seed("")
    trust(vm)
    allowed = vm.store.load().doc["machine"]["laptop"]["refresh_allowed"]
    assert ca.parse_stamp(allowed) is not None
    with vm.store.edit() as ed:  # one that has asked keeps its own clock
        ed.set_machine_field("laptop", "refresh_allowed", None)
        ed.set_machine_field("laptop", "last_refreshed", "2026-01-01T00:00:00Z")
    ak().write_text("")
    assert vm.run("trust", "--sync-keys")[0] == 0
    doc = vm.store.load().doc["machine"]["laptop"]
    assert block() and "refresh_allowed" not in doc and doc["last_refreshed"] == \
        "2026-01-01T00:00:00Z"
    # Never asked, but allowed again long ago: its clock starts at its line all the same.
    with vm.store.edit() as ed:
        ed.set_machine_field("laptop", "last_refreshed", None)
        ed.set_machine_field("laptop", "refresh_allowed", "2026-01-02T00:00:00Z")
    ak().write_text("")
    vm.run("trust", "--sync-keys")
    assert vm.store.load().doc["machine"]["laptop"]["refresh_allowed"] > "2026-01-02T00:00:00Z"
    # Refused for being idle: a new line does not let it in again.
    with vm.store.edit() as ed:
        ed.set_machine_field("laptop", "refresh_allowed", "2026-01-02T00:00:00Z")
        ed.set_machine_field("laptop", "idle_refused", "2026-03-01T00:00:00Z")
    ak().write_text("")
    vm.run("trust", "--sync-keys")
    assert vm.store.load().doc["machine"]["laptop"]["refresh_allowed"] == "2026-01-02T00:00:00Z"


def test_a_rename_rekey_or_scope_change_keeps_the_line_right(vm):
    seed("")
    blob, _ = trust(vm)
    assert vm.run("trust", "laptop", "--scope", "tag:work")[0] == 0
    assert block() == [line("laptop", blob)]  # the line names no scope
    other = keygen(vm.tmp / "other", "other").with_name("other.pub")
    code, out, err = vm.run("trust", "laptop", "--key", str(other))
    assert code != 0 and "trusted with a different key" in err  # re-keying is a new name
    assert vm.run("untrust", "laptop", "--yes")[0] == 0
    new_blob, _ = trust(vm, "laptop-2", key=other)  # what a "rename" is
    assert block() == [line("laptop-2", new_blob)]


def test_lines_outside_the_block_and_the_files_mode_are_never_changed(vm):
    text = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8g x"
    seed(text, mode=0o640)  # no newline at its end
    blob, _ = trust(vm)
    assert ak().read_text().startswith(text + "\n" + authkeys.BEGIN + "\n")
    assert stat.S_IMODE(ak().stat().st_mode) == 0o640
    # Below the block too: a line you add after it stays where it is.
    ak().write_text(ak().read_text() + "# mine, after\n")
    trust(vm, "phone")
    assert ak().read_text().endswith(authkeys.END + "\n# mine, after\n")
    assert len(block()) == 2
    assert not list(ak().parent.glob(".authorized_keys.*"))  # no temporary file left


def test_trust_names_must_be_plain_and_a_hand_edited_one_gets_no_line(vm):
    seed("")
    key = keygen(vm.tmp / "evil", "evil").with_name("evil.pub")
    code, out, err = vm.run("trust", "evil\n", "--key", str(key))
    assert code != 0 and "a-z, 0-9 and -" in err
    with vm.store.edit() as ed:  # ships.toml's own check lets a trailing newline through
        ed.set_machine("evil\n", {"public_key": key.read_text().strip(),
                                  "added": "2026-01-01T00:00:00Z", "scope": ["all"]})
    authkeys.sync(vm.store)
    assert "evil" not in ak().read_text()
    assert "none for evil" in doctor_item(vm).detail


@pytest.mark.parametrize("tail", [' x"', ' ,command="sh"', ' restrict\r'])
def test_only_the_keys_own_bytes_reach_the_line_whatever_its_comment(vm, tail):
    seed("")
    blob, _ = trust(vm)
    with vm.store.edit() as ed:  # by hand, past trust's own check
        ed.set_machine_field("laptop", "public_key", f"ssh-ed25519 {blob}{tail}")
    authkeys.sync(vm.store)
    assert block() == [line("laptop", blob)]


def test_a_key_that_is_not_plain_ed25519_gets_no_line_and_loses_its_old_one(vm):
    key = "ssh-ed25519 AAAAB3NzaC1yc2EAAAADAQAB"  # an rsa key's bytes, named ssh-ed25519
    seed("")
    blob, _ = trust(vm)
    with vm.store.edit() as ed:  # by hand
        ed.set_machine_field("laptop", "public_key", key.format(blob=blob))
    authkeys.sync(vm.store)
    assert block() == []


def test_this_computer_gets_no_line(vm):
    seed("")
    trust(vm)
    (Path(os.environ["FLEETLEDGER_CONFIG_DIR"]) / "this-computer.json").write_text(
        json.dumps({"machine": "laptop", "identity": "~/.ssh/id_ed25519"}))
    authkeys.sync(vm.store)
    assert block() == []


# --- your own keys come first ----------------------------------------------------------------
def test_a_key_with_its_own_line_gets_no_restricted_line_and_ca_show_says_so(vm):
    key = keygen(vm.tmp / "mine", "mine").with_name("mine.pub")
    before = seed(key.read_text())
    _, out = trust(vm, "desktop", key=key)
    assert ak().read_bytes() == before
    assert "desktop's key has a line of its own in" in out and "adds no bundle-only line" in out
    assert "now returns its bundle" not in out
    code, out, _ = vm.run("ca", "show")
    assert "bundle key: its own line (one of your keys)" in out
    code, out, _ = vm.run("ca", "show", "--json")
    doc = json.loads(out)
    assert schemas.errors("fleetledger-ca", doc) == []
    assert [m.get("bundle_key") for m in doc["machines"]] == ["own-line"]
    assert "desktop use their own line" in doctor_item(vm).detail


def test_a_key_in_authorized_keys2_counts_as_its_own_line(vm):
    key = keygen(vm.tmp / "mine", "mine").with_name("mine.pub")
    seed("")
    (ak().parent / "authorized_keys2").write_text(key.read_text())
    trust(vm, "desktop", key=key)
    assert block() == []


def test_your_own_line_added_later_wins_and_the_restricted_one_goes(vm):
    seed("")
    blob, _ = trust(vm)
    # You give that key a shell of your own, below the block (as ssh-copy-id would).
    ak().write_text(ak().read_text() + f"ssh-ed25519 {blob} laptop-shell\n")
    item = doctor_item(vm)
    assert item.state == "fail" and "no longer trusted here: laptop" in item.detail
    assert item.next == authkeys.SYNC
    assert vm.run("trust", "--sync-keys")[0] == 0
    assert ak().read_text() == f"ssh-ed25519 {blob} laptop-shell\n"


# --- doctor, and a file changed by hand -----------------------------------------------------
def test_doctor_checks_the_block_matches_exactly(vm):
    seed("")
    blob, _ = trust(vm)
    assert doctor_item(vm).state == "ok"
    text = ak().read_text()
    ak().write_text(text.replace("restrict,", ""))  # a line's options edited by hand
    item = doctor_item(vm)
    assert item.state == "fail" and "changed by hand: laptop" in item.detail
    assert item.next == "ship-fleet trust --sync-keys"
    code, out, err = vm.run("doctor", "--offline")
    assert "bundle keys" in out and "ship-fleet trust --sync-keys" in out
    assert ak().read_text() == text.replace("restrict,", "")  # doctor wrote nothing
    assert vm.run("trust", "--sync-keys")[0] == 0
    assert ak().read_text() == text
    ak().write_text(text.replace(line("laptop", blob) + "\n", ""))  # a missing line
    assert doctor_item(vm).state == "missing"
    vm.run("trust", "--sync-keys")
    ak().write_text(text + "")
    with vm.store.edit() as ed:  # a stale one: untrusted behind FleetLedger's back
        ed.set_machine_field("laptop", "revoked", "2026-01-01T00:00:00Z")
    assert "no longer trusted here: laptop" in doctor_item(vm).detail
    ak().chmod(0o666)
    vm.run("trust", "--sync-keys")
    item = doctor_item(vm)
    assert item.state == "fail" and "mode 0666" in item.detail and item.next.startswith("chmod")
    assert stat.S_IMODE(ak().stat().st_mode) == 0o666  # sync keeps your mode


def test_a_duplicated_line_is_one_again_after_sync(vm):
    seed("")
    blob, _ = trust(vm)
    ak().write_text(ak().read_text().replace(authkeys.END, line("laptop", blob) + "\n"
                                             + authkeys.END))
    assert doctor_item(vm).state == "fail"
    vm.run("trust", "--sync-keys")
    assert block() == [line("laptop", blob)]


def test_a_line_in_the_block_that_is_no_machines_is_never_removed(vm):
    seed("")
    trust(vm)
    mine = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8g me"
    ak().write_text(ak().read_text().replace(authkeys.END, mine + "\n" + authkeys.END))
    before = ak().read_bytes()
    item = doctor_item(vm)
    assert item.state == "fail" and "line(s) 3 in FleetLedger's block" in item.detail
    code, out, err = vm.run("trust", "--sync-keys")
    assert code != 0 and "will not remove them" in err
    code, out, err = vm.run("untrust", "laptop", "--yes")
    assert code == 1 and "bundle key may still be in" in err  # said; revoked all the same
    assert ak().read_bytes() == before


@pytest.mark.parametrize("markers", [
    "begin", "end", "two", "reversed"])
def test_broken_markers_stop_every_write(vm, markers):
    b, e = authkeys.BEGIN, authkeys.END
    text = {"begin": f"{b}\nx\n", "end": f"x\n{e}\n", "two": f"{b}\n{e}\n{b}\n{e}\n",
            "reversed": f"{e}\n{b}\n"}[markers]
    before = seed(text)
    key = keygen(vm.tmp / "laptop", "laptop").with_name("laptop.pub")
    code, out, err = vm.run("trust", "laptop", "--key", str(key))
    assert code == 1 and "start and" in err and "marker" in err  # trusted, no line
    assert ak().read_bytes() == before
    assert doctor_item(vm).state == "fail"


def test_a_symlinked_file_is_left_alone(vm):
    seed("")
    real = ak().with_name("elsewhere")
    real.write_text("")
    ak().unlink()
    ak().symlink_to(real)
    key = keygen(vm.tmp / "laptop", "laptop").with_name("laptop.pub")
    assert vm.run("trust", "laptop", "--key", str(key))[0] == 1
    assert ak().is_symlink() and real.read_text() == ""


def test_a_symlinked_file_on_a_desktop_stops_nothing(caenv, tmp_path, monkeypatch):  # noqa: F811
    caenv.tmp = tmp_path
    on_vm(caenv, monkeypatch, here=False)
    seed("")
    real = ak().with_name("elsewhere")
    real.write_text("ssh-ed25519 AAAA mine\n")
    ak().unlink()
    ak().symlink_to(real)
    trust(caenv)
    assert doctor_item(caenv) is None
    assert caenv.run("untrust", "laptop", "--yes")[0] == 0
    assert ak().is_symlink() and real.read_text() == "ssh-ed25519 AAAA mine\n"


def test_on_a_desktop_your_own_keys_line_is_not_spoken_of(caenv, tmp_path,  # noqa: F811
                                                         monkeypatch):
    caenv.tmp = tmp_path
    on_vm(caenv, monkeypatch, here=False)
    key = keygen(tmp_path / "mine", "mine").with_name("mine.pub")
    seed(key.read_text())
    _, out = trust(caenv, "laptop", key=key)
    assert "line of its own" not in out
    code, out, _ = caenv.run("trust", "--sync-keys")
    assert code == 0 and "line of its own" not in out


def test_an_unreadable_file_is_said_not_a_crash(vm):
    seed("")
    ak().chmod(0)
    if os.access(ak(), os.R_OK):
        pytest.skip("running as root")
    key = keygen(vm.tmp / "laptop", "laptop").with_name("laptop.pub")
    code, out, err = vm.run("trust", "laptop", "--key", str(key))
    assert code == 1 and "Cannot read" in err and "Certified laptop" in out
    item = doctor_item(vm)
    assert item.state == "fail" and "Cannot read" in item.detail
    assert vm.run("ca", "show")[0] == 0
    ak().chmod(0o600)


def test_an_interrupted_write_leaves_the_file_as_it_was(vm, monkeypatch):
    before = seed()
    real = os.replace

    def cut(a, b):
        raise KeyboardInterrupt

    key = keygen(vm.tmp / "laptop", "laptop").with_name("laptop.pub")
    with vm.store.edit() as ed:
        ed.set_machine("laptop", {"public_key": key.read_text().strip(),
                                  "added": "2026-01-01T00:00:00Z", "scope": ["all"]})
    monkeypatch.setattr(os, "replace", cut)
    with pytest.raises(KeyboardInterrupt):
        authkeys.sync(vm.store)
    monkeypatch.setattr(os, "replace", real)
    assert ak().read_bytes() == before
    assert not list(ak().parent.glob(".authorized_keys.*"))
    assert "refresh_allowed" not in vm.store.load().doc["machine"]["laptop"]


def test_a_change_made_while_it_writes_is_kept(vm, monkeypatch):
    seed("")
    real = authkeys._write
    calls = []

    def meanwhile(f, text):
        if not calls:  # you append a key between FleetLedger's read and its rename
            calls.append(1)
            with ak().open("a") as fh:
                fh.write(OWNER_LINES.splitlines()[1] + "\n")
        return real(f, text)

    monkeypatch.setattr(authkeys, "_write", meanwhile)
    blob, _ = trust(vm)
    assert ak().read_text().startswith(OWNER_LINES.splitlines()[1] + "\n")
    assert block() == [line("laptop", blob)]


def test_concurrent_trusts_and_untrusts_end_in_step(vm):
    seed(OWNER_LINES)
    names = [f"m{i}" for i in range(6)]
    for n in names:
        trust(vm, n)
    errors = []

    def work(n):
        try:
            if n in names[:3]:
                with vm.store.edit() as ed:
                    ed.set_machine_field(n, "revoked", "2026-01-01T00:00:00Z")
            authkeys.sync(vm.store)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [x.rsplit(" ", 1)[1] for x in block()] == names[3:]
    assert outside() == OWNER_LINES.encode()


# --- where FleetLedger adds nothing ----------------------------------------------------------
def test_on_a_desktop_trust_writes_nothing_and_untrust_still_removes(caenv, tmp_path,  # noqa: F811
                                                                    monkeypatch):
    caenv.tmp = tmp_path
    on_vm(caenv, monkeypatch, here=False)
    _, out = trust(caenv)
    assert not authkeys.ssh_dir().exists() and "Bundle key" not in out
    assert doctor_item(caenv) is None
    assert "bundle_key" not in caenv.run("ca", "show", "--json")[1]
    # A block left from before (this computer was the VM once): it only shrinks.
    blob = (tmp_path / "laptop.pub").read_text().split()[1]
    seed(f"{authkeys.BEGIN}\n{line('laptop', blob)}\n{authkeys.END}\n")
    trust(caenv, "phone")
    assert block() == [line("laptop", blob)]
    code, out, _ = caenv.run("trust", "--sync-keys")
    assert code == 0 and "adds no bundle keys here" in out
    assert caenv.run("untrust", "laptop", "--yes")[0] == 0
    assert ak().read_text() == ""


@pytest.mark.parametrize("role", ["standby", "retired"])
def test_a_copy_that_does_not_act_wants_no_lines(vm, role):
    seed("")
    trust(vm)
    handover.write(vm.store, handover.Marker(role, handover.now(), "elsewhere", 1))
    r = authkeys.review(vm.store, vm.store.load())
    assert not r.adds and r.wanted == {} and r.stale == ["laptop"]
    item = doctor_item(vm)
    assert item.state == "fail" and "Edit" in item.next and "by hand" in item.next
    authkeys.sync(vm.store)
    assert block() == []


def test_sync_keys_takes_nothing_else(vm):
    for argv in (["laptop"], ["--scope", "all"], ["--allow-again"], ["--hostnames", "overlay"]):
        code, out, err = vm.run("trust", "--sync-keys", *argv)
        assert code != 0 and "--sync-keys takes no machine" in err, argv


def test_sync_writes_only_when_something_changed(vm):
    from fleetledger.concierge import audit

    seed("")
    trust(vm)
    written = ak().stat().st_mtime_ns, ak().stat().st_ino
    code, out, _ = vm.run("trust", "--sync-keys")
    assert code == 0 and "already matches the trusted machines (1 bundle key(s))" in out
    assert (ak().stat().st_mtime_ns, ak().stat().st_ino) == written
    assert [a["service"] for a in audit.read() if a["action"] == "authorized-keys"] == ["laptop"]


def test_key_of_reads_lines_as_sshd_does():
    assert authkeys.key_of("ssh-ed25519 AAAA c") == "AAAA"
    assert authkeys.key_of('command="a \\" ssh-ed25519 BBBB",restrict ssh-ed25519 AAAA') == \
        "AAAA"
    assert authkeys.key_of('from="a b" ssh-rsa CCCC') == "CCCC"
    assert authkeys.key_of("# ssh-ed25519 AAAA") is None
    assert authkeys.key_of("  ") is None and authkeys.key_of("garbage") is None


def test_an_unreadable_authorized_keys2_adds_nothing(vm):
    seed("")
    other = ak().parent / "authorized_keys2"
    other.write_text("")
    other.chmod(0)
    if os.access(other, os.R_OK):
        pytest.skip("running as root")
    key = keygen(vm.tmp / "laptop", "laptop").with_name("laptop.pub")
    assert vm.run("trust", "laptop", "--key", str(key))[0] == 1
    assert ak().read_text() == ""
    other.chmod(0o600)


def test_the_wrappers_ship_fleet_is_what_lines_run():
    assert f"exec {authkeys.SHIP_FLEET} \"$@\"" in appliance.WRAPPER


def test_a_path_with_anything_odd_in_it_makes_no_line(monkeypatch):
    blob = "AAAAC3NzaC1lZDI1NTE5AAAAIAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8g"
    monkeypatch.setattr(authkeys, "SHIP_FLEET", '/bin/ship-fleet" ,x')
    assert authkeys.line_for("laptop", f"ssh-ed25519 {blob}") is None


# --- against a real sshd --------------------------------------------------------------------
SSHD = os.environ.get("FLEETLEDGER_TEST_SSHD") or shutil.which("sshd") or "/usr/sbin/sshd"
FAKE = """#!/bin/sh
# What the forced command was given, and what the session had.
if [ -t 0 ] || [ -t 1 ]; then tty=yes; else tty=no; fi
printf 'FAKE argv=%s|tty=%s|agent=%s\\n' "$*" "$tty" "${SSH_AUTH_SOCK:+yes}"
"""


@pytest.fixture
def sshd(vm, short_dir, monkeypatch):
    if not os.access(SSHD, os.X_OK):
        pytest.skip("no sshd binary (set FLEETLEDGER_TEST_SSHD)")
    fake = short_dir / "ship-fleet"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    monkeypatch.setattr(authkeys, "SHIP_FLEET", str(fake))
    d = vm.tmp / "sshd"
    d.mkdir()
    keygen(d / "hostkey", "host")
    owner = keygen(d / "owner", "owner")
    seed(owner.with_name("owner.pub").read_text())
    proc, port = start_sshd(SSHD, d, lambda port: (
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d / 'hostkey'}\n"
        f"AuthorizedKeysFile {ak()}\nUsePAM no\nStrictModes no\nPidFile none\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\n"
        "AllowTcpForwarding yes\nAllowAgentForwarding yes\nPermitTTY yes\n"
        f"Subsystem sftp internal-sftp\n"))
    vm.port, vm.owner, vm.short = port, owner, short_dir
    yield vm
    proc.terminate()
    proc.wait(timeout=5)


def ssh(s, key: Path, *argv: str, opts=(), timeout=30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-p", str(s.port), *opts, *client(key), "127.0.0.1", *argv],  # ssh keeps
        # the first value of an option: opts may replace client()'s
        capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def client(key: Path) -> list[str]:
    """Options ssh, scp and sftp all take (each names the port its own way)."""
    return ["-F", "/dev/null", "-i", str(key), "-o", "IdentitiesOnly=yes",
            "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR", "-o", f"User={getpass.getuser()}"]


def fake_said(proc) -> list[str]:
    return [x for x in proc.stdout.splitlines() if x.startswith("FAKE ")]


pytest_sshd = pytest.mark.local_sshd


@pytest_sshd
@pytest.mark.parametrize("command", [
    [], ["ship-fleet", "machine-bundle", "other"], ["machine-bundle", "other"],
    ["bash", "-c", "touch {p}"], ["touch {p}; echo"], ["laptop; touch {p}"],
    ["$(touch {p})"], ["`touch {p}`"], ["\"; touch {p}; \""], ["true\ntouch {p}"]])
def test_a_machines_key_runs_its_own_bundle_whatever_it_asks(sshd, command):
    trust(sshd)
    key = sshd.tmp / "laptop"
    pwned = sshd.tmp / "pwned"
    proc = ssh(sshd, key, *[c.format(p=pwned) for c in command])
    assert fake_said(proc) == ["FAKE argv=machine-bundle laptop|tty=no|agent="], proc.stderr
    assert proc.stdout.strip().splitlines() == fake_said(proc)  # nothing else ran
    assert not pwned.exists()


@pytest_sshd
def test_a_machines_key_gets_no_terminal_agent_or_forwarding(sshd):
    trust(sshd)
    key = sshd.tmp / "laptop"
    # A terminal: refused (the client gives up on a forced -tt; nothing gets one).
    tty = ssh(sshd, key, opts=["-tt"])
    assert "PTY allocation request failed" in tty.stderr and "tty=yes" not in tty.stdout
    mine = ssh(sshd, sshd.owner, "tty -s && echo tty=yes", opts=["-tt"])
    assert "tty=yes" in mine.stdout, mine.stderr  # your own line gets one: the test can tell
    agent = subprocess.Popen(["ssh-agent", "-D", "-a", str(sshd.short / "a.sock")],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            if (sshd.short / "a.sock").exists():
                break
            threading.Event().wait(0.02)
        fwd = {"opts": ["-A", "-o", f"IdentityAgent={sshd.short / 'a.sock'}"]}
        assert fake_said(ssh(sshd, key, **fwd)) == [
            "FAKE argv=machine-bundle laptop|tty=no|agent="]
        # The same request on your own line does forward it: the test can tell.
        mine = ssh(sshd, sshd.owner, "echo agent=${SSH_AUTH_SOCK:+yes}", **fwd)
        assert mine.stdout.strip() == "agent=yes", mine.stderr
    finally:
        agent.terminate()
        agent.wait(timeout=5)
    # Port forwarding: a direct channel (-W, as -L opens) and a remote one (-R).
    proc = ssh(sshd, key, opts=["-W", f"127.0.0.1:{sshd.port}"])
    assert proc.returncode != 0 and "SSH-2.0" not in proc.stdout
    proc = ssh(sshd, key, opts=["-N", "-o", "ExitOnForwardFailure=yes",
                                "-R", f"0:127.0.0.1:{sshd.port}"], timeout=30)
    assert proc.returncode != 0
    mine = ssh(sshd, sshd.owner, opts=["-W", f"127.0.0.1:{sshd.port}"], timeout=10)
    assert "SSH-2.0" in mine.stdout  # your own line may: the test can tell


@pytest_sshd
def test_scp_and_sftp_get_nothing_through_a_machines_key(sshd):
    trust(sshd)
    key = sshd.tmp / "laptop"
    src = sshd.tmp / "src"
    src.write_text("x")
    for mode in (["-O"], ["-s"]):
        dest = sshd.tmp / f"copied{mode[0]}"
        proc = subprocess.run(["scp", *mode, "-P", str(sshd.port), *client(key),
                               str(src), f"127.0.0.1:{dest}"],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode != 0 and not dest.exists(), proc.stderr
    batch = sshd.tmp / "batch"
    dest = sshd.tmp / "put"
    batch.write_text(f"put {src} {dest}\n")
    proc = subprocess.run(["sftp", "-b", str(batch), "-P", str(sshd.port),
                           *client(key), "127.0.0.1"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0 and not dest.exists()
    # Your own line gets sftp: the test can tell.
    proc = subprocess.run(["sftp", "-b", str(batch), "-P", str(sshd.port),
                           *client(sshd.owner), "127.0.0.1"],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and dest.read_text() == "x", proc.stderr


@pytest_sshd
def test_an_untrusted_machines_key_is_refused_and_yours_keeps_its_shell(sshd):
    trust(sshd)
    key = sshd.tmp / "laptop"
    assert sshd.run("untrust", "laptop", "--yes")[0] == 0
    proc = ssh(sshd, key, "echo", "in")
    assert proc.returncode == 255 and "Permission denied" in proc.stderr
    assert fake_said(proc) == []
    mine = ssh(sshd, sshd.owner, "echo shell-$((40+2))")
    assert mine.stdout.strip() == "shell-42", mine.stderr


@pytest_sshd
def test_a_machine_whose_key_is_yours_keeps_your_shell(sshd):
    _, out = trust(sshd, "desktop", key=sshd.owner.with_name("owner.pub"))
    assert "has a line of its own" in out
    mine = ssh(sshd, sshd.owner, "echo shell-$((40+2))")
    assert mine.stdout.strip() == "shell-42", mine.stderr


@pytest_sshd
def test_sshd_uses_the_first_matching_line_which_is_why_yours_stay_first(sshd):
    """What the rule above rests on: with a restricted line for your key before your
    own, sshd forces the command on you. FleetLedger never writes that."""
    pub = sshd.owner.with_name("owner.pub").read_text().split()
    own = ak().read_text()
    ak().write_text(line("desktop", pub[1]) + "\n" + own)
    proc = ssh(sshd, sshd.owner, "echo shell")
    assert fake_said(proc) == ["FAKE argv=machine-bundle desktop|tty=no|agent="]
    ak().write_text(own + line("desktop", pub[1]) + "\n")
    assert ssh(sshd, sshd.owner, "echo shell").stdout.strip() == "shell"
