"""The keys that may log in to FleetLedger's computer (loginkeys.py): `ship-fleet keys ls`,
`name` and `revoke`, and the audit lines that name the key a command ran under.

The file is always the test's own (conftest: no_real_authorized_keys), and so is the
session: conftest clears SSH_USER_AUTH, SSH_CONNECTION and SSH_CLIENT, which tests set
themselves. The last part runs a real, unprivileged sshd on 127.0.0.1 with
ExposeAuthInfo, as the appliance's seed sets it, and logs in with the keys listed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fl_harness import keygen, start_sshd
from fleetledger.concierge import audit
from test_authkeys import SSHD, ak, client, on_vm, seed, trust
from test_ca import caenv, cakeys  # noqa: F401  (fixtures)
from test_cli import env  # noqa: F401  (a fixture)

from fleetledger import authkeys, cli, loginkeys, schemas

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

ROOT = Path(__file__).resolve().parents[2]
ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI"
A = ED + "AECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8g"  # three made-up ed25519 keys
B = ED + "HNlbGZzZWxmc2VsZnNlbGZzZWxmc2VsZnNlbGZzZWxm"
C = ED + "HBvb3Bvb3Bvb3Bvb3Bvb3Bvb3Bvb3Bvb3Bvb3Bvb3Bv"
FILE = (f"# my keys, from the seed\n{A} you@desktop\n"
        f'from="192.0.2.0/24",no-pty {B}  old-laptop\r\n'
        f'command="/usr/bin/true",restrict {C} backup-host\n')


@pytest.fixture
def vm(caenv, tmp_path, monkeypatch):  # noqa: F811
    """FleetLedger's own VM ([fleet] self names this computer), with a CA."""
    caenv.tmp = tmp_path
    on_vm(caenv, monkeypatch)
    return caenv


def fp(key_line: str) -> str:
    from fleetledger import sshkeys

    return sshkeys.fingerprint(key_line)


def keys_json(e) -> list[dict]:
    code, out, err = e.run("keys", "ls", "--json")
    assert code == 0, err
    doc = json.loads(out)
    for entry in doc:
        assert schemas.errors("fleetledger-key", entry) == [], entry
    return doc


def lines() -> list[bytes]:
    return ak().read_bytes().split(b"\n")


def logged(*actions: str) -> list[dict]:
    return [x for x in audit.read() if x["action"] in actions]


# --- ls ------------------------------------------------------------------------------------------


def test_ls_lists_every_line_with_a_key_as_the_file_has_it(vm):
    seed(FILE)
    (ak().parent / "authorized_keys2").write_text("ecdsa-sha2-nistp256 AAAA!!!! x\n")
    blob, _ = trust(vm)  # a trusted machine's bundle-only line, in FleetLedger's block
    doc = keys_json(vm)
    assert [(k["file"], k["line"], k["name"], k["access"]) for k in doc] == [
        ("authorized_keys", 2, "you@desktop", "shell"),
        ("authorized_keys", 3, "old-laptop", "shell"),
        ("authorized_keys", 4, "backup-host", "command"),
        ("authorized_keys", 6, "laptop", "bundle"),
        ("authorized_keys2", 1, "x", "shell")]
    # Fingerprints as ssh-keygen prints them for each line's key (fed one by one: older
    # ssh-keygens do not read every authorized_keys line).
    for k, x in zip(doc[:4], [A, B, C, f"ssh-ed25519 {blob}"], strict=True):
        one = vm.tmp / "one.pub"
        one.write_text(x + "\n")
        said = subprocess.run(["ssh-keygen", "-lf", str(one)], capture_output=True,
                               text=True, check=True).stdout.split()
        assert k["fingerprint"] == said[1]
    assert doc[0]["type"] == "ssh-ed25519" and doc[1]["options"] == 'from="192.0.2.0/24",no-pty'
    assert doc[2]["command"] == "/usr/bin/true" and doc[3]["machine"] == "laptop"
    assert doc[3]["options"].startswith('restrict,command="')
    assert [k["changeable"] for k in doc] == [True, True, True, False, False]
    assert {k["this_session"] for k in doc} == {None}  # no session here says which
    assert doc[4]["fingerprint"] is None  # not a key sshd can decode
    assert blob not in json.dumps(doc)  # never the key itself
    code, out, _ = vm.run("keys", "ls")
    assert code == 0 and "you@desktop" in out and "only the command /usr/bin/true" in out
    assert "bundle only, machine laptop (ship-fleet untrust laptop)" in out
    assert "not FleetLedger's own VM" not in out


def test_ls_says_the_key_this_session_logged_in_with(vm, tmp_path, monkeypatch):
    seed(FILE)
    info = tmp_path / "auth-info"
    info.write_text(f"publickey {B}\n")
    info.chmod(0o600)
    monkeypatch.setenv("SSH_USER_AUTH", str(info))
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.10 50000 192.0.2.1 22")
    assert [k["this_session"] for k in keys_json(vm)] == [False, True, False]
    assert "shell (this session)" in vm.run("keys", "ls")[1]


def test_ls_reads_only_and_works_off_the_vm_and_while_locked(caenv, tmp_path,  # noqa: F811
                                                              monkeypatch):
    caenv.tmp = tmp_path
    on_vm(caenv, monkeypatch, here=False)  # a desktop: your own login's keys
    before = seed(FILE)
    monkeypatch.setattr("fleetledger.vault.state", lambda: "locked")
    doc = keys_json(caenv)
    assert len(doc) == 3 and not any(k["changeable"] for k in doc)
    code, out, _ = caenv.run("keys", "ls")
    assert code == 0 and "not FleetLedger's own VM" in out
    for argv in (["name", "you@desktop", "me"], ["revoke", "old-laptop", "--yes"]):
        code, _, err = caenv.run("keys", *argv)
        assert code == 1 and "vault is locked" in err
    monkeypatch.setattr("fleetledger.vault.state", lambda: "off")
    for argv in (["name", "you@desktop", "me"], ["revoke", "old-laptop", "--yes"]):
        code, _, err = caenv.run("keys", *argv)
        assert code == 1 and "not FleetLedger's own VM" in err and "your own login's" in err
    assert ak().read_bytes() == before


def test_ls_with_no_file_lists_nothing(vm):
    assert keys_json(vm) == []
    assert "No key may log in" in vm.run("keys", "ls")[1]
    assert not ak().exists()


# --- name ----------------------------------------------------------------------------------------


def test_name_changes_one_comment_and_nothing_else(vm):
    before = seed(FILE, mode=0o640)
    code, out, err = vm.run("keys", "name", "old-laptop", "laptop-2024")
    assert code == 0, err
    after = lines()
    old = before.split(b"\n")
    assert len(after) == len(old)
    assert [i for i, (x, y) in enumerate(zip(old, after, strict=True)) if x != y] == [2]
    # The options and key as they were, one space, the name, and its \r kept.
    assert after[2] == f'from="192.0.2.0/24",no-pty {B} laptop-2024\r'.encode()
    assert (ak().stat().st_mode & 0o777) == 0o640
    k = next(k for k in keys_json(vm) if k["name"] == "laptop-2024")
    assert k["fingerprint"] == fp(B) and k["options"] == 'from="192.0.2.0/24",no-pty'
    # By fingerprint too, and a line with no comment gets one.
    seed(f"{A}\n{B} b\n")
    assert vm.run("keys", "name", fp(A), "desktop")[0] == 0
    assert ak().read_text() == f"{A} desktop\n{B} b\n"
    assert logged("key-name")[-1]["service"] == fp(A)
    # The same name again changes nothing.
    stamp = ak().stat().st_mtime_ns
    code, out, _ = vm.run("keys", "name", "desktop", "desktop")
    assert code == 0 and "already named" in out and ak().stat().st_mtime_ns == stamp


@pytest.mark.parametrize("name", [
    "two words", 'quote"d', "new\nline", "cr\r", "tab\there", 'command="sh"', "a,b",
    ".hidden", "", "x" * 65, "nul\0", "SHA256:abc", "émoji", "semi;colon"])
def test_a_name_that_could_be_read_as_anything_else_is_refused(vm, name):
    before = seed(FILE)
    code, _, err = vm.run("keys", "name", "you@desktop", name)
    assert code == 1
    assert ("not a name FleetLedger writes" if name else "Say which key") in err
    assert ak().read_bytes() == before


@pytest.mark.parametrize("comment", [
    'x" command="/bin/sh', "a\rb", 'environment="LD_PRELOAD=/x"', "tail,restrict",
    "  spaced out  ", "nul\0after", "SHA256:" + "A" * 43])
def test_a_hostile_comment_becomes_the_name_and_gains_nothing(vm, comment):
    before = seed(f"{A} {comment}\n{B} other\n")
    other = before.split(b"\n")[1]
    code, out, err = vm.run("keys", "name", fp(A), "plain")
    assert code == 0, err
    got = lines()
    assert got[0] == f"{A} plain".encode() and got[1] == other and got[2:] == [b""]
    parsed = authkeys.parse(got[0].decode())
    assert parsed.options == "" and parsed.comment == "plain"
    assert [k["access"] for k in keys_json(vm)] == ["shell", "shell"]


def test_name_refuses_what_is_not_its_to_name(vm):
    seed(FILE)
    trust(vm)
    (ak().parent / "authorized_keys2").write_text(f"{C} k2\n")
    for selector, said in [("laptop", "bundle-only line FleetLedger keeps for laptop"),
                           ("k2", "authorized_keys2"),
                           ("nobody", "No key here is named"),
                           ("SHA256:" + "A" * 43, "No key here")]:
        before = ak().read_bytes()
        code, _, err = vm.run("keys", "name", selector, "fresh")
        assert code == 1 and said in err, err
        assert ak().read_bytes() == before
    code, _, err = vm.run("keys", "name", "you@desktop", "backup-host")
    assert code == 1 and "already named backup-host" in err
    seed(f"{A} twin\n{B} twin\n")
    code, _, err = vm.run("keys", "name", "twin", "one")
    assert code == 1 and "2 lines match" in err and fp(A) in err


def test_name_refuses_a_file_with_broken_markers(vm):
    before = seed(FILE + authkeys.BEGIN + "\n")
    code, _, err = vm.run("keys", "name", "you@desktop", "me")
    assert code == 1 and "marker" in err and ak().read_bytes() == before


# --- revoke --------------------------------------------------------------------------------------


def test_revoke_removes_exactly_one_line(vm):
    before = seed(FILE, mode=0o600)
    code, out, err = vm.run("keys", "revoke", "old-laptop", "--yes")
    assert code == 0, err
    old = before.split(b"\n")
    assert lines() == old[:2] + old[3:]
    assert "can no longer log in" in out
    assert (ak().stat().st_mode & 0o777) == 0o600
    assert logged("key-revoke")[-1]["service"] == fp(B)
    code, _, err = vm.run("keys", "revoke", fp(C), "--yes")
    assert code == 0, err
    assert lines() == [old[0], old[1], old[4]]


def test_revoke_asks_for_the_name_in_a_terminal(vm):
    before = seed(FILE)
    code, _, err = vm.run("keys", "revoke", "old-laptop")
    assert code == 1 and "Nothing revoked" in err and "--yes" in err
    vm.app.stdin_isatty = lambda: True
    vm.app.confirm = lambda q: "yes"
    code, _, err = vm.run("keys", "revoke", "old-laptop")
    assert code == 1 and err.strip().endswith("Nothing revoked.")
    assert ak().read_bytes() == before
    asked = []
    vm.app.confirm = lambda q: asked.append(q) or "old-laptop"
    assert vm.run("keys", "revoke", "old-laptop")[0] == 0
    assert "Type old-laptop to revoke it" in asked[0] and fp(B) in asked[0]
    assert b"old-laptop" not in ak().read_bytes()


def test_revoke_refuses_the_last_key_with_a_shell(vm):
    # A line whose key sshd cannot decode lets nobody in: it is not another shell.
    before = seed(f'{A} you@desktop\ncommand="/usr/bin/true" {C} only-a-command\n'
                  "ssh-ed25519 AAAA!!!! broken\n")
    code, _, err = vm.run("keys", "revoke", "you@desktop", "--yes")
    assert code == 1 and "last key here that surely gets a shell" in err
    assert "--allow-lockout" in err
    assert ak().read_bytes() == before
    assert vm.run("keys", "revoke", "only-a-command", "--yes")[0] == 0  # not a shell
    code, _, err = vm.run("keys", "revoke", "you@desktop", "--yes", "--allow-lockout")
    assert code == 0, err
    assert ak().read_text() == "ssh-ed25519 AAAA!!!! broken\n"


@pytest.mark.parametrize("other", [
    "ssh-ed25519 AAAA old",  # not a key ssh-keygen accepts
    f"ssh-ed25519 {B.split()[1][:40]} old",  # a truncated paste
    f"ssh-rsa {B.split()[1]} old",  # its type and its key disagree
    f'expiry-time="20200101" {B} old',  # expired
    f'from="192.0.2.1" {B} old',  # only from somewhere else
    f"bogus-option {B} old",  # sshd refuses the line
    f'command="/bin/true" {B} job\n{B} old',  # sshd uses the first line: no shell
    f"{authkeys.BEGIN}\n{B} old\n{authkeys.END}",  # in FleetLedger's block
])
def test_only_a_line_that_surely_gives_a_shell_counts_as_another(vm, other):
    before = seed(f"{A} me\n{other}\n")
    code, _, err = vm.run("keys", "revoke", "me", "--yes")
    assert code == 1 and "last key here that surely gets a shell" in err
    assert ak().read_bytes() == before


def test_a_certificate_authority_line_is_listed_by_the_schema_and_guarded_as_a_shell(vm):
    before = seed(f'cert-authority,command="/bin/true" {A} ca-forced\ncert-authority {B} ca\n'
                  f'command="/bin/true" {C} job\n')
    doc = keys_json(vm)  # schema-checked
    assert [(k["access"], k["command"]) for k in doc] == [
        ("cert-authority", None), ("cert-authority", None), ("command", "/bin/true")]
    code, _, err = vm.run("keys", "revoke", "ca", "--yes")
    assert code == 1 and "surely gets a shell" in err and ak().read_bytes() == before


def test_a_key_only_in_authorized_keys2_does_not_count_as_another_shell(vm):
    before = seed(f"{A} me\n")
    (ak().parent / "authorized_keys2").write_text(f"{B} old\n")
    code, _, err = vm.run("keys", "revoke", "me", "--yes")
    assert code == 1 and "surely gets a shell" in err and ak().read_bytes() == before


def test_revoking_the_last_line_keeps_the_newline_of_the_one_before(vm):
    seed(f"{A} one\n{B} two\n{C} three")
    assert vm.run("keys", "revoke", "three", "--yes")[0] == 0
    assert ak().read_text() == f"{A} one\n{B} two\n"


def test_revoke_refuses_the_key_this_session_logged_in_with(vm, tmp_path, monkeypatch):
    before = seed(FILE)
    info = tmp_path / "auth-info"
    info.write_text(f"publickey {A}\n")
    info.chmod(0o600)
    monkeypatch.setenv("SSH_CONNECTION", "192.0.2.10 50000 192.0.2.1 22")
    monkeypatch.setenv("SSH_USER_AUTH", str(info))
    code, _, err = vm.run("keys", "revoke", "you@desktop", "--yes")
    assert code == 1 and "the key this session logged in with" in err
    assert ak().read_bytes() == before
    assert vm.run("keys", "revoke", "old-laptop", "--yes")[0] == 0  # another key: fine
    # sshd does not say (no ExposeAuthInfo): over ssh, any key may be this session's.
    monkeypatch.delenv("SSH_USER_AUTH")
    before = ak().read_bytes()
    code, _, err = vm.run("keys", "revoke", "backup-host", "--yes")
    assert code == 1 and "cannot tell which key" in err and ak().read_bytes() == before
    assert vm.run("keys", "revoke", "backup-host", "--yes", "--allow-lockout")[0] == 0
    # Not over ssh at all (a console, sudo -iu): nothing to cut off but the last shell.
    monkeypatch.delenv("SSH_CONNECTION")
    seed(FILE)
    assert vm.run("keys", "revoke", "old-laptop", "--yes")[0] == 0


@pytest.mark.parametrize("info", [
    "publickey ssh-ed25519-cert-v01@openssh.com AAAA\n",  # a certificate: which line?
    None,  # a symlink
    "x" * (loginkeys.MAX_AUTH_INFO + 10),
])
def test_an_auth_info_file_it_cannot_trust_says_nothing(tmp_path, monkeypatch, info):
    seed(FILE)
    path = tmp_path / "auth-info"
    if info is None:
        target = tmp_path / "real"
        target.write_text(f"publickey {A}\n")
        path.symlink_to(target)
    else:
        path.write_text(info)
    monkeypatch.setenv("SSH_USER_AUTH", str(path))
    assert loginkeys.session().keys is None and loginkeys.session().over_ssh
    assert loginkeys.actor() == {}


def test_revoke_refuses_what_is_not_its_to_remove(vm):
    seed(FILE)
    trust(vm)
    (ak().parent / "authorized_keys2").write_text(f"{C} k2\n")
    before = ak().read_bytes()
    code, _, err = vm.run("keys", "revoke", "laptop", "--yes")
    assert code == 1 and "ship-fleet untrust laptop" in err
    code, _, err = vm.run("keys", "revoke", "k2", "--yes")
    assert code == 1 and "authorized_keys2" in err
    # backup-host's key also has a line in authorized_keys2: one line gone, it still logs in.
    code, _, err = vm.run("keys", "revoke", "backup-host", "--yes")
    assert code == 1 and "has 2 lines" in err
    assert ak().read_bytes() == before


def test_revoking_a_trusted_machines_own_line_says_its_bundle_line_comes_back(vm):
    seed(FILE)
    key = keygen(vm.tmp / "desk", "desk").with_name("desk.pub")
    own = key.read_text().split()
    ak().write_text(ak().read_text() + f"{own[0]} {own[1]} desk-own\n")
    trust(vm, "desk", key=key)  # its key has a line of its own: no bundle line
    code, out, err = vm.run("keys", "revoke", "desk-own", "--yes")
    assert code == 0, err
    assert "Its machine desk is still trusted" in out and "ship-fleet untrust desk" in out


def test_a_change_made_while_it_asked_is_never_overwritten(vm):
    seed(FILE)
    vm.app.stdin_isatty = lambda: True

    def meanwhile(q):  # someone renames that very line while you are asked
        ak().write_text(ak().read_text().replace("old-laptop", "renamed"))
        return "old-laptop"

    vm.app.confirm = meanwhile
    code, _, err = vm.run("keys", "revoke", "old-laptop")
    assert code == 1 and "changed while FleetLedger worked" in err
    assert b"renamed" in ak().read_bytes() and len(lines()) == 5


def test_an_interrupted_revoke_leaves_the_file_as_it_was(vm, monkeypatch):
    before = seed(FILE)

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", boom)
    code, _, err = vm.run("keys", "revoke", "old-laptop", "--yes")
    assert code == 1 and "No space left" in err
    assert ak().read_bytes() == before
    assert [p.name for p in ak().parent.iterdir()] == ["authorized_keys"]  # no temp file


# --- the actor in the audit log -----------------------------------------------------------------


def test_audit_lines_name_the_key_sshd_says(vm, tmp_path, monkeypatch):
    seed(FILE)
    info = tmp_path / "auth-info"
    info.write_text(f"publickey {B}\n")
    info.chmod(0o600)
    monkeypatch.setenv("SSH_USER_AUTH", str(info))
    assert vm.run("keys", "name", "you@desktop", "desktop")[0] == 0
    entry = logged("key-name")[-1]
    assert entry["actor"] == "old-laptop" and entry["actor_key"] == fp(B)
    # A key with no line here: its fingerprint only. No file: nobody named.
    info.write_text("publickey ssh-ed25519 " + "QUJD" * 10 + "\n")
    assert vm.run("keys", "name", "desktop", "desk")[0] == 0
    entry = logged("key-name")[-1]
    assert "actor" not in entry and entry["actor_key"].startswith("SHA256:")
    monkeypatch.delenv("SSH_USER_AUTH")
    assert vm.run("keys", "name", "desk", "desktop")[0] == 0
    assert not {"actor", "actor_key"} & set(logged("key-name")[-1])


def test_the_web_ui_names_no_actor_whatever_session_started_it(vm, tmp_path, monkeypatch):
    from fleetledger.web import jobs

    seed(FILE)
    info = tmp_path / "auth-info"
    info.write_text(f"publickey {A}\n")
    info.chmod(0o600)
    monkeypatch.setenv("SSH_USER_AUTH", str(info))

    class Sink:
        def write(self, text):
            pass

    assert jobs.BrowserApp(vm.app, Sink()).actor is None
    code = cli.main(["keys", "name", "old-laptop", "laptop-2"], jobs.BrowserApp(vm.app, Sink()))
    assert code == 0
    assert not {"actor", "actor_key"} & set(logged("key-name")[-1])
    audit.record("-", "outside-a-command", "-", "ok")  # the web UI's own lines
    assert "actor" not in logged("outside-a-command")[-1]


# --- against a real sshd -------------------------------------------------------------------------


@pytest.fixture
def sshd(vm, short_dir):
    """An sshd that tells sessions which key let them in (ExposeAuthInfo, as the
    appliance's seed sets it), with two keys of yours in the test's authorized_keys."""
    if not os.access(SSHD, os.X_OK):
        pytest.skip("no sshd binary (set FLEETLEDGER_TEST_SSHD)")
    d = vm.tmp / "sshd"
    d.mkdir()
    keygen(d / "hostkey", "host")
    one, two = keygen(d / "one", "one"), keygen(d / "two", "two")
    seed("".join(p.with_name(p.name + ".pub").read_text() for p in (one, two))
         .replace(" one\n", " desktop\n").replace(" two\n", " laptop\n"))
    proc, port = start_sshd(SSHD, d, lambda port: (
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d / 'hostkey'}\n"
        f"AuthorizedKeysFile {ak()}\nUsePAM no\nStrictModes no\nPidFile none\n"
        "PasswordAuthentication no\nKbdInteractiveAuthentication no\nExposeAuthInfo yes\n"))
    vm.port, vm.one, vm.two = port, one, two
    yield vm
    proc.terminate()
    proc.wait(timeout=5)


def ssh(s, key: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-p", str(s.port), *client(key), "127.0.0.1", *argv],
                          capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)


def session_of(s, key: Path, tmp: Path) -> dict:
    """Log in with key; what the session's environment says, its auth-info file kept."""
    keep = tmp / f"info-{key.name}"
    proc = ssh(s, key, f'cp "$SSH_USER_AUTH" {keep} && echo "$SSH_CONNECTION"')
    assert proc.returncode == 0, proc.stderr
    return {"SSH_USER_AUTH": str(keep), "SSH_CONNECTION": proc.stdout.strip()}


@pytest.mark.local_sshd
def test_each_key_is_named_in_the_audit_as_sshd_says(sshd, monkeypatch):
    for key, name in ((sshd.one, "desktop"), (sshd.two, "laptop")):
        # Live, in the session itself: the same reading, with its own SSH_USER_AUTH.
        live = ssh(sshd, key, f"HOME={authkeys.HOME} PYTHONPATH={ROOT / 'fleetledger'} "
                   f"{sys.executable} -c 'import json; from fleetledger import loginkeys; "
                   "print(json.dumps(loginkeys.actor()))'")
        assert live.returncode == 0, live.stderr
        want = {"actor": name, "actor_key": fp(key.with_name(key.name + ".pub").read_text())}
        assert json.loads(live.stdout) == want
        for k, v in session_of(sshd, key, sshd.tmp).items():
            monkeypatch.setenv(k, v)
        assert sshd.run("keys", "name", name, name + "-x")[0] == 0
        entry = logged("key-name")[-1]
        assert entry["actor"] == name and entry["actor_key"] == want["actor_key"]
        assert sshd.run("keys", "name", name + "-x", name)[0] == 0


@pytest.mark.local_sshd
def test_a_revoked_key_can_no_longer_log_in_and_the_other_still_can(sshd, monkeypatch):
    for k, v in session_of(sshd, sshd.one, sshd.tmp).items():
        monkeypatch.setenv(k, v)
    # This session's own key: refused unless you say so.
    code, _, err = sshd.run("keys", "revoke", "desktop", "--yes")
    assert code == 1 and "the key this session logged in with" in err
    code, _, err = sshd.run("keys", "revoke", "laptop", "--yes")
    assert code == 0, err
    gone = ssh(sshd, sshd.two, "echo in")
    assert gone.returncode == 255 and "Permission denied" in gone.stderr
    mine = ssh(sshd, sshd.one, "echo shell-$((40+2))")
    assert mine.stdout.strip() == "shell-42", mine.stderr
    entry = logged("key-revoke")[-1]
    assert entry["actor"] == "desktop" and entry["service"] == fp(
        sshd.two.with_name("two.pub").read_text())
    # The last key with a shell, and this session's: both refusals, then the flag.
    code, _, err = sshd.run("keys", "revoke", "desktop", "--yes")
    assert code == 1 and "last key here that surely gets a shell" in err
    assert ssh(sshd, sshd.one, "true").returncode == 0
    assert sshd.run("keys", "revoke", "desktop", "--yes", "--allow-lockout")[0] == 0
    assert ssh(sshd, sshd.one, "true").returncode == 255


@pytest.mark.local_sshd
def test_a_renamed_key_still_logs_in_with_what_its_line_allowed(sshd):
    pub = sshd.one.with_name("one.pub").read_text().split()
    seed(f'no-pty,command="echo forced" {pub[0]} {pub[1]} desktop\n'
         + sshd.two.with_name("two.pub").read_text())
    assert sshd.run("keys", "name", "desktop", 'main')[0] == 0
    assert ssh(sshd, sshd.one, "echo", "shell").stdout.strip() == "forced"
    assert ak().read_text().startswith(f'no-pty,command="echo forced" {pub[0]} {pub[1]} main\n')
