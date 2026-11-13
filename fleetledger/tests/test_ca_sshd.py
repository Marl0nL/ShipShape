"""Certificates against a real OpenSSH server (skipped where there is no sshd).

An unprivileged sshd on localhost is set up the way `ship-ctl ssh-trust` sets up
a Ship (contracts/ship-ctl.md: HostCertificate, TrustedUserCAKeys,
AuthorizedPrincipalsFile, RevokedKeys), with the principals file for the user
running the test standing in for agent's. Then, with keys and certificates from
FleetLedger's CA code:

- FleetLedger's CA-only check accepts the Ship's host certificate, and a
  known_hosts naming another CA is refused;
- a trusted machine's certificate logs in; one for another tag, a lapsed one,
  a revoked one, or a plain key do not.

Run them where sshd exists (a Ship, CI with openssh-server):
    FLEETLEDGER_TEST_SSHD=/usr/sbin/sshd python -m pytest fleetledger/tests/test_ca_sshd.py
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fl_harness import keygen, start_sshd
from fleetledger.ssh import SSH

from fleetledger import ca

SSHD = os.environ.get("FLEETLEDGER_TEST_SSHD") or shutil.which("sshd") or "/usr/sbin/sshd"
NO_AGENT = ca.Signer(agent_keys=lambda: set())
# Every test here runs real ssh against a private sshd on 127.0.0.1 (fl_netguard.py).
pytestmark = pytest.mark.local_sshd


@pytest.fixture
def setup(store, home, tmp_path, monkeypatch, short_dir):
    if not os.access(SSHD, os.X_OK):
        pytest.skip("no sshd binary (set FLEETLEDGER_TEST_SSHD)")
    monkeypatch.setenv("FLEETLEDGER_RUNTIME_DIR", str(short_dir))
    d = tmp_path / "sshd"
    d.mkdir()
    cakeys = ca.CAConfig(keygen(d / "host_ca", "host-ca"), keygen(d / "user_ca", "user-ca"))
    keygen(d / "hostkey", "ship-01")
    host_pub = (d / "hostkey.pub").read_text().strip()
    with store.edit() as ed:
        ed.set_field("ship-01", "names", ["127.0.0.1"])
        ed.set_field("ship-01", "host_key", host_pub)
        ed.set_ca({"host_key": str(cakeys.host_key), "user_key": str(cakeys.user_key)})
    inv = store.load()
    ship = inv.ship("ship-01")
    (d / "hostkey-cert.pub").write_text(ca.sign_host(NO_AGENT, cakeys, ship) + "\n")
    (d / "user-ca.pub").write_text(cakeys.user_public + "\n")
    principals = d / "principals"
    principals.mkdir()
    (principals / getpass.getuser()).write_text("agent:all\nagent:tag:work\nagent:ship:ship-01\n")
    keygen(d / "revoked", "revoked")
    spec = d / "spec"
    spec.write_text(f"key: {(d / 'revoked.pub').read_text().strip()}\n")
    subprocess.run(["ssh-keygen", "-q", "-k", "-f", str(d / "krl"), str(spec)], check=True)
    shutil.copy(home / "keys" / "fleet_admin.pub", d / "authorized_keys")
    proc, port = start_sshd(SSHD, d, lambda port: (
        f"Port {port}\nListenAddress 127.0.0.1\nHostKey {d / 'hostkey'}\n"
        f"HostCertificate {d / 'hostkey-cert.pub'}\n"
        f"TrustedUserCAKeys {d / 'user-ca.pub'}\n"
        f"AuthorizedPrincipalsFile {principals}/%u\n"
        f"RevokedKeys {d / 'krl'}\n"
        f"AuthorizedKeysFile {d / 'authorized_keys'}\nUsePAM no\nStrictModes no\n"
        "PidFile none\nPasswordAuthentication no\nKbdInteractiveAuthentication no\n"
    ))
    yield {"dir": d, "port": port, "ca": cakeys, "inv": inv, "ship": ship}
    proc.terminate()
    proc.wait(timeout=5)


def _login(s, key: Path, cert: str | None) -> subprocess.CompletedProcess:
    d = s["dir"]
    kh = d / "client_known_hosts"
    kh.write_text(ca.known_hosts_text(s["ca"], [s["ship"]]))
    argv = ["ssh", "-F", "/dev/null", "-p", str(s["port"]), "-i", str(key),
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none", "-o", "BatchMode=yes",
            "-o", f"UserKnownHostsFile={kh}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "HostKeyAlgorithms=ssh-ed25519-cert-v01@openssh.com",
            "-l", getpass.getuser()]
    if cert is not None:
        cert_file = key.with_name(key.name + "-cert.pub")
        cert_file.write_text(cert + "\n")
        argv += ["-o", f"CertificateFile={cert_file}"]
    return subprocess.run([*argv, "127.0.0.1", "echo", "in"], capture_output=True, text=True,
                          timeout=30)


def _machine(s, name: str, scope: tuple[str, ...]) -> tuple[Path, ca.Machine]:
    key = keygen(s["dir"] / name, name)
    pub = (s["dir"] / f"{name}.pub").read_text().strip()
    return key, ca.Machine(name, pub, "2026-01-01T00:00:00Z", scope)


def test_fleetledger_checks_the_host_certificate_with_ca_only(setup):
    s = setup
    ssh = SSH(s["inv"], fleet_user=getpass.getuser(), extra_options=["-p", str(s["port"])])
    good = s["dir"] / "kh-good"
    good.write_text(f"@cert-authority ship-01,127.0.0.1 {s['ca'].host_public}\n")
    assert ssh.probe_certificate(s["ship"], good) is None
    wrong = s["dir"] / "kh-wrong"
    wrong.write_text(f"@cert-authority ship-01,127.0.0.1 {s['ca'].user_public}\n")
    assert ssh.probe_certificate(s["ship"], wrong) is not None


def test_a_trusted_machine_logs_in_within_its_scope(setup):
    s = setup
    key, m = _machine(s, "laptop", ("tag:work",))
    cert, _, _ = ca.sign_user(NO_AGENT, s["ca"], m, hours=1)
    proc = _login(s, key, cert)
    assert proc.returncode == 0 and proc.stdout.strip() == "in", proc.stderr


def test_certificates_outside_scope_lapsed_or_revoked_are_refused(setup):
    s = setup
    key, m = _machine(s, "other", ("tag:personal",))
    cert, _, _ = ca.sign_user(NO_AGENT, s["ca"], m, hours=1)
    assert _login(s, key, cert).returncode == 255  # wrong tag
    key, m = _machine(s, "lapsed", ("all",))
    past = ca.utc_now() - timedelta(hours=3)
    cert, _, _ = ca.sign_user(NO_AGENT, s["ca"], m, hours=1, now=past)
    assert _login(s, key, cert).returncode == 255  # expired an hour ago
    revoked = s["dir"] / "revoked"
    m = ca.Machine("revoked", (s["dir"] / "revoked.pub").read_text().strip(),
                   "2026-01-01T00:00:00Z", ("all",))
    cert, _, _ = ca.sign_user(NO_AGENT, s["ca"], m, hours=1)
    assert _login(s, revoked, cert).returncode == 255  # in the KRL
    key, _ = _machine(s, "plain", ("all",))
    assert _login(s, key, None).returncode == 255  # no certificate, not authorized


def test_no_principals_file_means_no_certificate_logins(setup):
    """On a Ship only agent has a principals file, so certificates never log in as fleet."""
    s = setup
    key, m = _machine(s, "laptop", ("all",))
    cert, _, _ = ca.sign_user(NO_AGENT, s["ca"], m, hours=1)
    (s["dir"] / "principals" / getpass.getuser()).unlink()
    assert _login(s, key, cert).returncode == 255


def _principals(s, text):
    (s["dir"] / "principals" / getpass.getuser()).write_text(text)


def test_an_agent_certificate_never_opens_the_owner_login(setup):
    """The test user stands in for the owner: its principals file as a Ship writes the
    owner's (owner:*). An agent-only certificate is refused; an owner one logs in."""
    s = setup
    _principals(s, "owner:all\nowner:tag:work\nowner:ship:ship-01\n")
    key, m = _machine(s, "shared", ("all",))
    agent_only = ca.Machine(m.name, m.public_key, m.added, m.scope, logins=("agent",))
    cert, _, _ = ca.sign_user(NO_AGENT, s["ca"], agent_only, hours=1)
    assert _login(s, key, cert).returncode == 255
    owner = ca.Machine(m.name, m.public_key, m.added, m.scope, logins=("owner",))
    cert, _, _ = ca.sign_user(NO_AGENT, s["ca"], owner, hours=1)
    proc = _login(s, key, cert)
    assert proc.returncode == 0 and proc.stdout.strip() == "in", proc.stderr


def test_narrowing_a_pre_upgrade_machine_revokes_its_certificate_on_a_real_sshd(setup, store):
    """X2: before the upgrade an agent:* certificate also opened the owner login (the
    owner's principals file was agent's, as here). After the migration and --logins
    agent, the KRL FleetLedger builds refuses that certificate."""
    from fleetledger import trustcmd

    s = setup
    key, m = _machine(s, "shared", ("all",))
    before = ca.Machine(m.name, m.public_key, m.added, m.scope, logins=("agent",))
    cert, serial, until = ca.sign_user(NO_AGENT, s["ca"], before, hours=24)
    assert _login(s, key, cert).returncode == 0  # opens the (pre-#33) owner login
    with store.edit() as ed:  # as the previous FleetLedger recorded it: no logins
        ed.set_machine("shared", {
            "public_key": m.public_key, "added": m.added, "scope": ["all"],
            "last_serial": serial, "last_valid_before": ca.stamp(until),
            "issued": [{"serial": serial, "valid_before": ca.stamp(until),
                        "principals": ["agent:all"],
                        "ca": ca.fingerprint(s["ca"].user_public)}]})
    trustcmd.migrate_logins(store, lambda line: None)
    with store.edit() as ed:
        ed.set_machine_field("shared", "logins", ["agent"])
    (s["dir"] / "krl").write_bytes(ca.build_krl(store.load()))
    assert _login(s, key, cert).returncode == 255  # revoked
