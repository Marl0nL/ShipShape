"""The SSH certificate authority: signing, revocation, judging Ships, trust bundles,
and the ship-fleet ca / trust / untrust commands.

Certificates are made by the real ssh-keygen with throwaway CA keys; Ships are
fakes (FakeShip in test_cli.py), so nothing here needs a network.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
from fl_harness import keygen
from fleetledger.inventory import Ship
from test_cli import RUNNING, env, result  # noqa: F401  (env is a fixture)

from fleetledger import ca, schemas

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def listing(cert_line: str, tmp: Path) -> str:
    f = tmp / "c-cert.pub"
    f.write_text(cert_line + "\n")
    return subprocess.run(["ssh-keygen", "-L", "-f", str(f)], capture_output=True, text=True,
                          env={**os.environ, "TZ": "UTC"}, check=True).stdout


@pytest.fixture
def cakeys(tmp_path):
    folder = tmp_path / "ca"
    folder.mkdir()
    return ca.CAConfig(host_key=keygen(folder / "host_ca", "shipshape-host-ca"),
                       user_key=keygen(folder / "user_ca", "shipshape-user-ca"))


@pytest.fixture
def ship_key(tmp_path):
    k = keygen(tmp_path / "ship01", "ship-01")
    pub = (tmp_path / "ship01.pub").read_text().strip()
    return Ship(name="ship-01", id="7c9e6679-7425-40de-944b-e07fc1f90ae7", driver="manual",
                names=("ship-01.local", "ship-01.example.net"), host_key=pub), k


NO_AGENT = ca.Signer(agent_keys=lambda: set())


# --- signing ----------------------------------------------------------------------
def test_host_certificate_certifies_the_pinned_key_for_every_name(cakeys, ship_key, tmp_path):
    ship, _ = ship_key
    cert = ca.sign_host(NO_AGENT, cakeys, ship)
    text = listing(cert, tmp_path)
    assert "host certificate" in text
    for name in ("ship-01", "ship-01.local", "ship-01.example.net"):
        assert f"\n                {name}\n" in text or text.rstrip().endswith(name)
    # The certified key is exactly the pinned one.
    assert cert.split()[0] == ca.CERT_TYPE
    fp = subprocess.run(["ssh-keygen", "-lf", str(tmp_path / "c-cert.pub")], capture_output=True,
                        text=True).stdout.split()[1]
    assert fp == ca.fingerprint(ship.host_key)
    assert ca.fingerprint(cakeys.host_public) in text


def test_user_certificate_principals_validity_and_options(cakeys, tmp_path):
    keygen(tmp_path / "laptop", "laptop")
    m = ca.Machine("laptop", (tmp_path / "laptop.pub").read_text().strip(), "2026-01-01T00:00:00Z",
                   ("tag:work", "ship:ship-09"))
    now = ca.utc_now()
    cert, serial, until = ca.sign_user(NO_AGENT, cakeys, m, hours=24, now=now)
    text = listing(cert, tmp_path)
    assert "user certificate" in text
    assert "agent:tag:work" in text and "agent:ship:ship-09" in text and "agent:all" not in text
    assert f"Serial: {serial}" in text
    assert until == now + timedelta(hours=24)
    assert ca.stamp(until)[:-1].replace("Z", "") in text.replace("\n", " ")
    # Only a terminal: no port, agent or X11 forwarding, no ~/.ssh/rc.
    extensions = text.split("Extensions:")[1].split()
    assert extensions == ["permit-pty"]


def test_revoked_machine_is_never_certified(cakeys, tmp_path):
    keygen(tmp_path / "old", "old")
    m = ca.Machine("old", (tmp_path / "old.pub").read_text().strip(), "2026-01-01T00:00:00Z",
                   ("all",), revoked="2026-01-02T00:00:00Z")
    with pytest.raises(ca.FleetError, match="never certified again"):
        ca.sign_user(NO_AGENT, cakeys, m)


def test_signs_through_ssh_agent_without_the_private_key(cakeys, ship_key):
    ship, _ = ship_key
    seen = []

    def runner(argv, **kw):
        seen.append(argv)
        # Pretend to be ssh-keygen -U: write a certificate next to the subject.
        subject = Path(argv[-1])
        real = [a for a in argv if a != "-U"]
        real[real.index("-s") + 1] = str(cakeys.host_key)
        subprocess.run(real, check=True, capture_output=True)
        assert subject.with_name("subject-cert.pub").exists()
        return subprocess.CompletedProcess(argv, 0, "", "")

    signer = ca.Signer(runner=runner, agent_keys=lambda: {cakeys.host_public.split()[1]})
    ca.sign_host(signer, cakeys, ship)
    argv = seen[0]
    assert "-U" in argv
    assert str(cakeys.host_key) not in argv  # only the public half is named
    assert argv[argv.index("-s") + 1].endswith("ca.pub")


def test_refuses_odd_principals_and_keys(cakeys):
    with pytest.raises(ca.FleetError, match="principal"):
        NO_AGENT.sign(cakeys.user_key, cakeys.user_public, cakeys.user_public, host=False,
                      key_id="x", principals=["a,b"], valid_from=ca.utc_now(),
                      valid_to=ca.utc_now(), serial=1)
    with pytest.raises(ca.FleetError, match="ed25519"):
        NO_AGENT.sign(cakeys.user_key, cakeys.user_public, "ssh-rsa AAAAB3Nza x", host=False,
                      key_id="x", principals=["agent:all"], valid_from=ca.utc_now(),
                      valid_to=ca.utc_now(), serial=1)


# --- revocation list --------------------------------------------------------------
def _with_ca(store, cakeys, machines=()):
    with store.edit() as ed:
        ed.set_ca({"host_key": str(cakeys.host_key), "user_key": str(cakeys.user_key)})
        for name, entry in machines:
            ed.set_machine(name, entry)
    return store.load()


def test_krl_lists_untrusted_machines_and_is_cached(store, cakeys, tmp_path):
    keygen(tmp_path / "a", "a")
    keygen(tmp_path / "b", "b")
    a = (tmp_path / "a.pub").read_text().strip()
    b = (tmp_path / "b.pub").read_text().strip()
    inv = _with_ca(store, cakeys, [
        ("a", {"public_key": a, "added": "2026-01-01T00:00:00Z", "scope": ["all"],
               "revoked": "2026-01-02T00:00:00Z"}),
        ("b", {"public_key": b, "added": "2026-01-01T00:00:00Z", "scope": ["all"]}),
    ])
    krl = ca.build_krl(inv)
    assert ca.krl_revokes(krl, a) and not ca.krl_revokes(krl, b)
    assert ca.build_krl(inv) == krl  # same bytes while nothing changes
    assert ca.cached_krl(inv) == krl
    with store.edit() as ed:
        ed.set_machine_field("b", "revoked", "2026-01-03T00:00:00Z")
    inv = store.load()
    assert ca.cached_krl(inv) is None  # stale: the list changed
    assert ca.krl_revokes(ca.build_krl(inv), b)


# --- judging a Ship ------------------------------------------------------------------
def _status_with(cert=None, user_ca=None, krl_hash=None):
    doc = copy.deepcopy(RUNNING)
    doc["ssh"] = {"host_certificate": cert, "user_ca": user_ca or [],
                  "agent_principals": ["agent:all"], "revoked_keys_sha256": krl_hash}
    return doc


def test_judge(cakeys, ship_key):
    import hashlib

    ship, _ = ship_key
    krl = b"krl"
    good = {"serial": 1, "key_id": "x", "principals": ca.host_principals(ship),
            "valid_after": None, "valid_before": ca.stamp(ca.utc_now() + timedelta(days=80)),
            "ca_fingerprint": ca.fingerprint(cakeys.host_public)}
    ok_ca = [ca.fingerprint(cakeys.user_public)]
    ok = ca.judge(cakeys, ship, _status_with(good, ok_ca, hashlib.sha256(krl).hexdigest()), krl)
    assert ok.supported and not ok.needs_cert and not ok.trust_behind
    assert ca.judge(cakeys, ship, _status_with(None, ok_ca), krl).reason == "no host certificate"
    other = dict(good, ca_fingerprint="SHA256:" + "A" * 43)
    assert ca.judge(cakeys, ship, _status_with(other), krl).reason == "signed by another CA"
    renamed = dict(good, principals=["ship-01"])
    assert ca.judge(cakeys, ship, _status_with(renamed), krl).reason == "names changed"
    soon = dict(good, valid_before=ca.stamp(ca.utc_now() + timedelta(days=10)))
    assert ca.judge(cakeys, ship, _status_with(soon), krl).reason == "expires soon"
    assert ca.judge(cakeys, ship, _status_with(good, ok_ca, "0" * 64), krl).trust_behind
    assert ca.judge(cakeys, ship, _status_with(good, [], None), krl).trust_behind
    old = copy.deepcopy(RUNNING)
    old.pop("ssh")
    assert not ca.judge(cakeys, ship, old, krl).supported


# --- trust bundles ---------------------------------------------------------------------
def test_known_hosts_and_config(cakeys, ship_key):
    ship, _ = ship_key
    cfg = ca.CAConfig(cakeys.host_key, cakeys.user_key,
                      revoked_host_keys=("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOld old",))
    kh = ca.known_hosts_text(cfg, [ship])
    lines = [ln for ln in kh.splitlines() if not ln.startswith("#")]
    assert lines[0] == ("@cert-authority ship-01,ship-01.example.net,ship-01.local "
                        + cfg.host_public)
    assert lines[1] == "@revoked * ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOld"
    conf = ca.ssh_config_text("laptop", [ship], "~/.ssh/shipshape/id_ed25519")
    block = conf.split("Host ship-01\n")[1]
    for line in ("HostName ship-01.local", "User agent", "ForwardAgent no",
                 "StrictHostKeyChecking yes", "IdentitiesOnly yes",
                 "CertificateFile ~/.ssh/shipshape/laptop-cert.pub",
                 "HostKeyAlgorithms ssh-ed25519-cert-v01@openssh.com"):
        assert f"  {line}\n" in block


@pytest.fixture
def fake_herdr(tmp_path):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    log = tmp_path / "herdr.log"
    script = bindir / "herdr"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {log}\n'
        'if [ "$1 $2" = "machine list" ]; then echo \'[{"label": "ship-02"}]\'; fi\n'
    )
    script.chmod(0o755)
    return bindir, log


def test_install_script_installs_and_is_idempotent(cakeys, ship_key, tmp_path, fake_herdr):
    ship, _ = ship_key
    ship2 = Ship(name="ship-02", id="2f1b8a54-3c6e-4d1f-9a2b-5e8c7d6f4a31", driver="manual",
                 names=("ship-02.local",), host_key=ship.host_key)
    bundle = ca.write_bundle(tmp_path / "bundle", ca=cakeys, machine_name="laptop",
                             cert="ssh-ed25519-cert-v01@openssh.com AAAA laptop",
                             ships=[ship, ship2], identity="~/.ssh/shipshape/id_ed25519",
                             valid_before=ca.utc_now())
    for f in bundle.folder.iterdir():
        assert stat.S_IMODE(f.stat().st_mode) == 0o600
    home = tmp_path / "home"
    (home / ".ssh" / "shipshape").mkdir(parents=True)
    (home / ".ssh" / "config").write_text("Host other\n  User me\n")
    (home / ".ssh" / "shipshape" / "id_ed25519").write_text("(private key stand-in)")
    bindir, log = fake_herdr
    shell_env = {"HOME": str(home), "PATH": f"{bindir}:/usr/bin:/bin"}
    for _ in range(2):
        proc = subprocess.run(["sh", "install.sh"], cwd=bundle.folder, env=shell_env,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
    config = (home / ".ssh" / "config").read_text().splitlines()
    assert config[0] == f"Include {home}/.ssh/shipshape/config"
    assert config.count(config[0]) == 1 and "Host other" in config
    dest = home / ".ssh" / "shipshape"
    for name in ("config", "known_hosts", "laptop-cert.pub"):
        assert stat.S_IMODE((dest / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    calls = log.read_text().splitlines()
    adds = [c for c in calls if c.startswith("machine add")]
    # ship-02 is already a saved machine; ship-01 is added (once per run).
    assert adds == ["machine add ship-01 --label ship-01 --remote-session default"] * 2
    log.unlink()
    proc = subprocess.run(["sh", "install.sh", "--no-herdr"], cwd=bundle.folder,
                          env=shell_env, capture_output=True, text=True)
    assert proc.returncode == 0 and not log.exists()


def test_install_script_needs_the_private_key(cakeys, ship_key, tmp_path):
    ship, _ = ship_key
    bundle = ca.write_bundle(tmp_path / "b", ca=cakeys, machine_name="laptop", cert="x",
                             ships=[ship], identity="~/.ssh/shipshape/id_ed25519",
                             valid_before=ca.utc_now())
    home = tmp_path / "home"
    home.mkdir()
    proc = subprocess.run(["sh", "install.sh", "--no-herdr"], cwd=bundle.folder,
                          env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                          capture_output=True, text=True)
    assert proc.returncode == 1 and "Missing" in proc.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="needs shellcheck")
def test_install_script_passes_shellcheck(tmp_path):
    script = tmp_path / "install.sh"
    script.write_text(ca.install_script("laptop", ["ship-01", "ship-02"], "~/.ssh/x"))
    proc = subprocess.run(["shellcheck", "-s", "sh", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout


# --- the commands -----------------------------------------------------------------------
@pytest.fixture
def caenv(env, cakeys, tmp_path, monkeypatch):  # noqa: F811
    env.app.signer = ca.Signer(agent_keys=lambda: set())
    _with_ca(env.store, cakeys)
    env.trusted = []
    env.probes = []
    original = env.ship.__call__

    def ship_side(ship, command):
        if command[:4] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "ssh-trust"]:
            return 0, json.dumps(result("ssh-trust", changed=["/etc/ssh/x"])), ""
        if command[:4] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "version"]:
            v = result("version", data={"tooling": "0.1.0", "commit": "0123456789ab",
                                        "contracts": {"ship-status": 1, "ship-toml": 1,
                                                      "ship-ctl-result": 1, "bundle": 1,
                                                      "seed": 1, "runtime": 1,
                                                      "ssh-trust": 1}})
            return 0, json.dumps(v), ""
        return original(ship, command)

    class Side:
        calls = env.ship.calls
        status_override = env.ship.status_override

        def __call__(self, ship, command):
            return ship_side(ship, command)

    from test_cli import ShipSSH

    class RecordingSSH(ShipSSH):
        def run(self, ship, command, *, input=None, timeout=60):
            if command[3:4] == ["ssh-trust"]:
                env.trusted.append((ship.name, json.loads(input)))
            return super().run(ship, command, input=input, timeout=timeout)

        def probe_certificate(self, ship, known_hosts, timeout=30):
            env.probes.append((ship.name, known_hosts.read_text()))
            return None

    side = Side()
    env.app.ssh_factory = lambda inv: RecordingSSH(inv, side)
    env.cakeys = cakeys
    return env


def _no_ssh_section(e):
    doc = copy.deepcopy(RUNNING)
    doc["ssh"] = {"host_certificate": None, "user_ca": [], "agent_principals": [],
                  "revoked_keys_sha256": None}
    for name in ("ship-01", "ship-02"):
        e.ship.status_override[name] = doc


def test_ca_sign_certifies_pinned_keys_and_checks_with_ca_only(caenv):
    _no_ssh_section(caenv)
    code, out, err = caenv.run("ca", "sign")
    assert code == 0, err
    inv = caenv.store.load()
    sent = dict(caenv.trusted)
    assert set(sent) == {"ship-01", "ship-02"}
    doc = sent["ship-02"]
    assert doc["schema"] == "shipshape.ssh-trust/1"
    assert doc["user_ca_keys"] == [caenv.cakeys.user_public]
    assert doc["revoked_keys"]
    # The certificate is for the inventory's pinned key, for every name.
    cert_file = Path(caenv.cakeys.host_key).parent / "probe-cert.pub"
    cert_file.write_text(doc["host_certificate"] + "\n")
    text = subprocess.run(["ssh-keygen", "-L", "-f", str(cert_file)], capture_output=True,
                          text=True).stdout
    for name in ("ship-02", "ship-02.local", "ship-02.example.net"):
        assert name in text
    fp = subprocess.run(["ssh-keygen", "-lf", str(cert_file)], capture_output=True,
                        text=True).stdout.split()[1]
    assert fp == ca.fingerprint(inv.ship("ship-02").host_key)
    # Then a connection that trusts only the CA (never the pinned key) is tried.
    probes = dict(caenv.probes)
    assert probes["ship-02"].startswith("@cert-authority ship-02,ship-02.local,ship-02.example.net")
    assert inv.ship("ship-02").host_key.split()[1] not in probes["ship-02"]
    assert "presents the certificate" in out


def test_ca_sign_skips_up_to_date_and_old_ships(caenv):
    old = copy.deepcopy(RUNNING)
    old.pop("ssh")
    caenv.ship.status_override["ship-01"] = old
    code, out, err = caenv.run("ca", "sign", "ship-01")
    assert code == 1 and "predates certificates" in err
    assert caenv.trusted == []


def test_ca_sign_reports_a_failed_ca_only_check(caenv):
    _no_ssh_section(caenv)
    from test_cli import ShipSSH  # noqa: F401

    factory = caenv.app.ssh_factory

    def failing(inv):
        s = factory(inv)
        s.probe_certificate = lambda ship, kh, timeout=30: "Host key verification failed."
        return s

    caenv.app.ssh_factory = failing
    code, _, err = caenv.run("ca", "sign", "ship-01")
    assert code == 1 and "CA-only check failed" in err


def test_trust_untrust_cycle(caenv, tmp_path):
    keygen(tmp_path / "laptop", "laptop")
    pub = tmp_path / "laptop.pub"
    out_dir = tmp_path / "bundle"
    code, out, err = caenv.run("trust", "laptop", "--key", str(pub), "--scope", "tag:work",
                               "--hours", "12", "--out", str(out_dir))
    assert code == 0, err
    inv = caenv.store.load()
    m = ca.machine(inv, "laptop")
    assert m.scope == ("tag:work",) and m.cert_hours == 12 and m.last_serial
    files = {p.name for p in out_dir.iterdir()}
    assert files == {"laptop-cert.pub", "known_hosts", "config", "install.sh", "bundle.json"}
    # Only work Ships are in the bundle.
    assert "Host ship-01" in (out_dir / "config").read_text()
    assert "Host ship-02" not in (out_dir / "config").read_text()
    cert_text = subprocess.run(["ssh-keygen", "-L", "-f", str(out_dir / "laptop-cert.pub")],
                               capture_output=True, text=True).stdout
    assert "agent:tag:work" in cert_text
    # Renewal keeps the key; a different key under the same name is refused.
    code, out, _ = caenv.run("trust", "laptop", "--print-cert")
    assert code == 0 and out.startswith("ssh-ed25519-cert-v01@openssh.com ")
    keygen(tmp_path / "other", "other")
    code, _, err = caenv.run("trust", "laptop", "--key", str(tmp_path / "other.pub"))
    assert code == 1 and "different key" in err
    # Untrust: revoked in the inventory and pushed to every Ship in a KRL.
    _no_ssh_section(caenv)
    code, out, err = caenv.run("untrust", "laptop")
    assert code == 0, err
    assert ca.machine(caenv.store.load(), "laptop").revoked
    krl = __import__("base64").b64decode(dict(caenv.trusted)["ship-02"]["revoked_keys"])
    assert ca.krl_revokes(krl, pub.read_text())
    code, _, err = caenv.run("trust", "laptop")
    assert code == 1 and "stays untrusted" in err


def test_ca_show_prints_and_gives_json_from_the_same_rows(caenv, tmp_path):
    keygen(tmp_path / "laptop", "laptop")
    caenv.run("trust", "laptop", "--key", str(tmp_path / "laptop.pub"), "--scope", "tag:work",
              "--out", str(tmp_path / "bundle"))
    code, out, err = caenv.run("ca", "show")
    assert code == 0, err
    assert f"Host CA {ca.fingerprint(caenv.cakeys.host_public)}" in out
    assert "ship-01" in out and "laptop" in out and "tag:work" in out
    code, out, _ = caenv.run("ca", "show", "--json")
    doc = json.loads(out)
    assert set(doc) == {"host_ca", "user_ca", "ships", "machines"}
    assert [r["ship"] for r in doc["ships"]] == ["ship-01", "ship-02"]
    assert doc["machines"][0]["machine"] == "laptop"


def test_ca_show_says_a_ship_not_seen_yet(caenv):
    from fleetledger import status

    status.write_cache("ship-01", copy.deepcopy(RUNNING))
    status.cache_path("ship-02").unlink(missing_ok=True)
    code, out, err = caenv.run("ca", "show")
    assert code == 0, err
    line = next(x for x in out.splitlines() if x.strip().startswith("ship-02"))
    assert "not seen yet" in line
    code, out, _ = caenv.run("ca", "show", "--json")
    assert [r["seen"] for r in json.loads(out)["ships"]] == [True, False]


def test_a_narrowing_whose_revocation_does_not_reach_every_ship_fails(caenv, tmp_path):
    keygen(tmp_path / "lap", "lap")
    caenv.run("trust", "lap", "--key", str(tmp_path / "lap.pub"), "--out", str(tmp_path / "b"))
    caenv.ship.down.add("ship-02")
    code, out, err = caenv.run("trust", "lap", "--scope", "ship:ship-01", "--out",
                               str(tmp_path / "b"))
    assert code == 1  # the certificate is issued, but older ones still work on ship-02
    assert "NOT YET REVOKED on ship-02" in out + err and "Bundle:" in out
    assert ca.machine(caenv.store.load(), "lap").scope == ("ship:ship-01",)


def _pushed_krl(caenv, ship="ship-02"):
    return __import__("base64").b64decode(dict(caenv.trusted)[ship]["revoked_keys"])


def test_narrowing_a_scope_revokes_the_wider_certificates(caenv, tmp_path):
    keygen(tmp_path / "laptop", "laptop")
    pub = tmp_path / "laptop.pub"
    code, _, err = caenv.run("trust", "laptop", "--key", str(pub), "--scope", "all",
                             "--out", str(tmp_path / "b1"))
    assert code == 0, err
    wide = (tmp_path / "b1" / "laptop-cert.pub").read_text()
    code, renewed, _ = caenv.run("trust", "laptop", "--print-cert")  # same scope: no push
    assert code == 0 and caenv.trusted == []
    _no_ssh_section(caenv)
    code, out, err = caenv.run("trust", "laptop", "--scope", "tag:work",
                               "--out", str(tmp_path / "b2"))
    assert code == 0, err
    assert "2 certificate(s)" in out
    narrow = (tmp_path / "b2" / "laptop-cert.pub").read_text()
    assert {name for name, _ in caenv.trusted} == {"ship-01", "ship-02"}
    krl = _pushed_krl(caenv)
    assert ca.krl_revokes(krl, wide) and ca.krl_revokes(krl, renewed)
    assert not ca.krl_revokes(krl, narrow)
    assert not ca.krl_revokes(krl, pub.read_text())  # the machine itself stays trusted
    inv = caenv.store.load()
    assert ca.krl_revokes(ca.build_krl(inv), wide)
    assert schemas.errors("ships.toml", inv.doc) == []
    # Widening again lets the old certificates back in (they ask for no more than all).
    caenv.trusted.clear()
    code, _, err = caenv.run("trust", "laptop", "--scope", "all", "--print-cert")
    assert code == 0, err
    assert not ca.krl_revokes(_pushed_krl(caenv), wide)


def test_narrowing_revokes_a_certificate_issued_before_they_were_recorded(caenv, tmp_path):
    keygen(tmp_path / "laptop", "laptop")
    pub = tmp_path / "laptop.pub"
    code, _, err = caenv.run("trust", "laptop", "--key", str(pub), "--scope", "all",
                             "--out", str(tmp_path / "b1"))
    assert code == 0, err
    wide = (tmp_path / "b1" / "laptop-cert.pub").read_text()
    with caenv.store.edit() as ed:
        ed.set_machine_field("laptop", "issued", None)  # as written by an older ship-fleet
    _no_ssh_section(caenv)
    code, out, err = caenv.run("trust", "laptop", "--scope", "ship:ship-01", "--print-cert")
    assert code == 0, err
    assert ca.krl_revokes(_pushed_krl(caenv), wide)
    assert out.count("\n") == 1 and out.startswith(ca.CERT_TYPE)  # only the certificate
    assert "revocation list" in err


def test_lapsed_certificates_are_forgotten(cakeys):
    m = ca.Machine("laptop", "ssh-ed25519 AAAA laptop", "2026-01-01T00:00:00Z", ("tag:work",),
                   issued=(
                       {"serial": 1, "valid_before": "2026-01-01T00:00:00Z",
                        "principals": ["agent:tag:work"], "ca": "SHA256:x"},
                       {"serial": 2, "valid_before": "2026-01-01T00:00:00Z",
                        "principals": ["agent:all"], "ca": "SHA256:x"},
                   ))
    now = ca.parse_stamp("2026-02-01T00:00:00Z")
    record = ca.issued_record(m, cakeys, 3, now + timedelta(hours=1), now=now)
    # Lapsed and within scope: dropped. Lapsed but still in the revocation list: kept until
    # a scope change pushes a new list (dropping it here would change the list unpushed).
    assert [r["serial"] for r in record] == [2, 3]
    assert ca.unlapsed(record, now=now) == [record[1]]


def test_trust_refuses_private_keys_ca_keys_and_bad_scopes(caenv, tmp_path):
    keygen(tmp_path / "laptop", "laptop")
    code, _, err = caenv.run("trust", "laptop", "--key", str(tmp_path / "laptop"))
    assert code == 1 and "never a private key" in err
    code, _, err = caenv.run("trust", "laptop", "--key", str(caenv.cakeys.user_public_path))
    assert code == 1 and "CA key" in err
    code, _, err = caenv.run("trust", "laptop", "--key", str(tmp_path / "laptop.pub"),
                             "--scope", "everything")
    assert code == 1 and "Unknown scope" in err
    assert ca.machine(caenv.store.load(), "laptop") is None
    code, _, err = caenv.run("trust", "laptop", "--key", str(tmp_path / "laptop.pub"),
                             "--print-cert")
    assert code == 0, err
    code, _, err = caenv.run("trust", "desk", "--key", str(tmp_path / "laptop.pub"))
    assert code == 1 and "already trusted under another name" in err


def test_status_flags_missing_certificate(caenv):
    _no_ssh_section(caenv)
    code, out, _ = caenv.run("status", "ship-01", "--json")
    codes = [a["code"] for a in json.loads(out)["attention"]]
    assert "host-cert-missing" in codes and "ssh-trust-behind" in codes


def test_adopt_replace_revokes_the_old_host_key(caenv, tmp_path):
    keygen(tmp_path / "new", "new")
    key = " ".join((tmp_path / "new.pub").read_text().split()[:2])
    caenv.app.keyscan = lambda host: key
    old = caenv.store.load().ship("ship-01").host_key_type_and_blob
    code, _, err = caenv.run("adopt", "ship-01", "--replace", "--fingerprint",
                             ca.fingerprint(key))
    assert code == 0, err
    assert caenv.store.load().doc["ca"]["revoked_host_keys"] == [old]


def test_ca_init_makes_two_keys(env, tmp_path):  # noqa: F811
    host, user = tmp_path / "k" / "host_ca", tmp_path / "k" / "user_ca"
    code, out, err = env.run("ca", "init", "--host-key", str(host), "--user-key", str(user),
                             "--no-passphrase")
    assert code == 0, err
    doc = env.store.load().doc["ca"]
    assert doc == {"host_key": str(host), "user_key": str(user)}
    assert (host.with_suffix(".pub")).read_text().split()[2] == "shipshape-host-ca"
    assert ca.fingerprint((user.parent / "user_ca.pub").read_text()) in out
    assert env.run("ca", "init")[0] == 1  # already set up


# --- dialling, and this computer ------------------------------------------------------


def test_a_bundle_dials_the_first_name_that_resolves(cakeys, ship_key):
    ship, _ = ship_key
    assert ca.dial_name(ship, lambda n: n == "ship-01.example.net") == "ship-01.example.net"
    assert ca.dial_name(ship, lambda n: False) == ship.names[0]  # nothing resolves: as before
    conf = ca.ssh_config_text("laptop", [ship], "~/.ssh/x",
                              {"ship-01": "ship-01.example.net"})
    assert "  HostName ship-01.example.net\n" in conf


def test_trust_writes_the_name_that_resolves_here(caenv, tmp_path):
    keygen(tmp_path / "laptop", "laptop")
    caenv.app.resolves = lambda host: host == "ship-02.example.net"
    code, _, err = caenv.run("trust", "laptop", "--key", str(tmp_path / "laptop.pub"),
                             "--out", str(tmp_path / "b"))
    assert code == 0, err
    config = (tmp_path / "b" / "config").read_text()
    # ship-02.local does not resolve here; its other name does. ship-01 has only the one.
    assert "Host ship-02\n  HostName ship-02.example.net\n" in config
    assert "Host ship-01\n  HostName ship-01.local\n" in config


@pytest.fixture
def here(caenv, tmp_path, monkeypatch):
    """A scratch home for this computer's SSH config; herdr is never run."""
    home = tmp_path / "userhome"
    (home / ".ssh").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    caenv.app.herdr = False
    caenv.home_dir = home
    return caenv


def test_this_computer_is_trusted_with_your_key_and_installed_here(here):
    from fleetledger import trustcmd

    code, out, err = here.run("trust", "--this-computer")
    assert code == 0, err
    inv = here.store.load()
    marker = trustcmd.this_computer()
    m = ca.machine(inv, marker["machine"])
    you = Path(inv.agent_key_paths[0])
    assert m.scope == ("all",) and ca._blob(m.public_key) == ca._blob(you.read_text())
    assert marker["identity"] == str(you.with_suffix(""))  # its private half, here
    ssh_dir = here.home_dir / ".ssh"
    assert (ssh_dir / "config").read_text().splitlines()[0] == (
        f"Include {ssh_dir}/shipshape/config")
    config = (ssh_dir / "shipshape" / "config").read_text()
    assert "Host ship-01\n" in config and "Host ship-02\n" in config
    assert f"IdentityFile {you.with_suffix('')}" in config
    assert "can attach: ssh ship-01, herdr --remote ship-01" in out
    assert trustcmd.attaches_here(inv, inv.ship("ship-01"))
    # Again: the same machine entry, renewed, never a second one.
    assert here.run("trust", "--this-computer")[0] == 0
    assert [x.name for x in ca.machines(here.store.load())] == [m.name]


def test_after_enrolling_this_computers_config_is_refreshed_only_if_trusted(here):
    from fleetledger import trustcmd

    said = []
    assert trustcmd.trust_this_computer(here.app, here.store, said.append,
                                        create=False) is False
    assert said == [] and not (here.home_dir / ".ssh" / "shipshape").exists()
    here.run("trust", "--this-computer")
    serial = ca.machine(here.store.load(), trustcmd.this_computer()["machine"]).last_serial
    trustcmd.after_enrol(here.app, here.store, "ship-01", said.append)
    assert ca.machine(here.store.load(),
                      trustcmd.this_computer()["machine"]).last_serial != serial  # renewed


def test_this_computer_without_your_private_key_says_so(here):
    from fleetledger import trustcmd

    inv = here.store.load()
    inv.agent_key_paths[0].with_suffix("").unlink()
    code, out, _ = here.run("trust", "--this-computer")
    assert code == 1 and "private half is not here" in out
    assert trustcmd.this_computer() is None


def test_joining_the_overlay_re_signs_the_host_certificate_with_its_overlay_name(caenv):
    from test_cli import with_overlay

    with_overlay(caenv)
    code, out, err = caenv.run("overlay", "join", "ship-01")
    assert code == 0, err
    sent = dict(caenv.trusted)["ship-01"]
    cert_file = Path(caenv.cakeys.host_key).parent / "joined-cert.pub"
    cert_file.write_text(sent["host_certificate"] + "\n")
    text = subprocess.run(["ssh-keygen", "-L", "-f", str(cert_file)], capture_output=True,
                          text=True).stdout
    assert "ship-01.example-tailnet.ts.net" in text and "ship-01.local" in text
    assert "re-signed with its overlay name" in out


def test_this_computers_config_dials_the_name_that_resolves_here(here):
    here.app.resolves = lambda host: host == "ship-02.example.net"
    assert here.run("trust", "--this-computer")[0] == 0
    config = (here.home_dir / ".ssh" / "shipshape" / "config").read_text()
    assert "Host ship-02\n  HostName ship-02.example.net\n" in config


def _managed_you(here, tmp_path):
    from fleetledger import keys

    you = tmp_path / "managed" / "you_managed"
    keys.make(you, "you", "file")
    with here.store.edit() as ed:
        ed.doc["fleet"]["agent_keys"] = [f"{you}.pub"]
        keys.record(ed, "file", [str(you)])
    return you


def test_a_key_fleetledger_made_is_not_promised_to_plain_ssh_without_an_agent(here, tmp_path,
                                                                             monkeypatch):
    from fleetledger import keys

    _managed_you(here, tmp_path)
    monkeypatch.setattr(keys, "USERS_AGENT", None)  # no ssh-agent of yours here
    code, out, _ = here.run("trust", "--this-computer")
    assert code == 1 and "Plain ssh cannot open your key here" in out
    assert "can attach" not in out


def test_a_key_fleetledger_made_is_loaded_into_your_agent_for_plain_ssh(here, tmp_path,
                                                                       monkeypatch):
    import signal

    from fleetledger import keys

    you = _managed_you(here, tmp_path)
    sock = tmp_path / "agent.sock"
    out = subprocess.run(["ssh-agent", "-a", str(sock)], capture_output=True, text=True,
                         check=True).stdout
    pid = int(out.split("SSH_AGENT_PID=")[1].split(";")[0])
    monkeypatch.setenv("SSH_AUTH_SOCK", str(sock))
    monkeypatch.setattr(keys, "USERS_AGENT", str(sock))
    try:
        code, out, _ = here.run("trust", "--this-computer")
        held = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True).stdout
    finally:
        os.kill(pid, signal.SIGTERM)
    assert code == 0 and "can attach" in out and "from your ssh-agent" in out
    assert keys.fingerprint(you) in held


def test_a_key_you_can_open_is_preferred_over_one_fleetledger_made(here, tmp_path):
    from fleetledger import trustcmd

    managed = _managed_you(here, tmp_path)
    inv = here.store.load()
    mine = Path(inv.doc["fleet"]["admin_key"]).parent / "you.pub"  # the fixture's own key
    with here.store.edit() as ed:
        ed.doc["fleet"]["agent_keys"] = [f"{managed}.pub", str(mine)]
    assert trustcmd._your_private_key(here.store.load()) == mine.with_suffix("")


def test_this_computers_certificate_is_renewed_in_its_last_day(here):
    from fleetledger import trustcmd

    here.run("trust", "--this-computer")
    name = trustcmd.this_computer()["machine"]
    serial = ca.machine(here.store.load(), name).last_serial
    assert not trustcmd.renew_this_computer_if_due(here.app, here.store)  # 7 days left
    assert ca.machine(here.store.load(), name).last_serial == serial
    with here.store.edit() as ed:
        ed.set_machine_field(name, "last_valid_before",
                             ca.stamp(ca.utc_now() + timedelta(hours=5)))
    assert trustcmd.renew_this_computer_if_due(here.app, here.store)
    assert ca.machine(here.store.load(), name).last_serial != serial


def test_a_linked_ssh_config_is_written_through_its_link(here, tmp_path):
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    (dotfiles / "ssh_config").write_text("Host other\n  User me\n")
    config = here.home_dir / ".ssh" / "config"
    config.symlink_to(dotfiles / "ssh_config")
    assert here.run("trust", "--this-computer")[0] == 0
    assert config.is_symlink()
    lines = (dotfiles / "ssh_config").read_text().splitlines()
    assert lines[0].startswith("Include ") and "Host other" in lines


def test_an_overlay_join_refreshes_this_computers_ssh_config(here):
    from test_cli import with_overlay

    here.run("trust", "--this-computer")
    config = here.home_dir / ".ssh" / "shipshape" / "config"
    assert "Host ship-01\n  HostName ship-01.local\n" in config.read_text()
    with_overlay(here)
    assert here.run("overlay", "join", "ship-01")[0] == 0
    assert "Host ship-01\n  HostName ship-01.example-tailnet.ts.net\n" in config.read_text()


# --- which logins a machine may use (agent:*, owner:*) -----------------------------------


def _cert_text(path):
    return subprocess.run(["ssh-keygen", "-L", "-f", str(path)], capture_output=True,
                          text=True).stdout


def test_a_new_machine_logs_in_as_both_by_default_or_as_chosen(caenv, tmp_path):
    for name, logins in (("laptop", None), ("shared", "agent"), ("admin-box", "owner")):
        keygen(tmp_path / name, name)
        argv = ["trust", name, "--key", str(tmp_path / f"{name}.pub"),
                "--out", str(tmp_path / f"b-{name}")]
        code, out, err = caenv.run(*argv, *(["--logins", logins] if logins else []))
        assert code == 0, err
    both = _cert_text(tmp_path / "b-laptop" / "laptop-cert.pub")
    assert "agent:all" in both and "owner:all" in both
    agent_only = _cert_text(tmp_path / "b-shared" / "shared-cert.pub")
    assert "agent:all" in agent_only and "owner:" not in agent_only
    owner_only = _cert_text(tmp_path / "b-admin-box" / "admin-box-cert.pub")
    assert "owner:all" in owner_only and "agent:" not in owner_only
    # An owner-only machine's SSH config logs in as you, and herdr is not set up.
    config = (tmp_path / "b-admin-box" / "config").read_text()
    assert "  User human\n" in config and "  User agent\n" not in config
    assert "    for ship in ; do\n" in (tmp_path / "b-admin-box" / "install.sh").read_text()
    assert ca.machine(caenv.store.load(), "shared").logins == ("agent",)
    assert caenv.run("trust", "x", "--key", str(tmp_path / "laptop.pub"), "--logins",
                     "root")[0] == 1


def test_narrowing_a_machine_to_the_agent_revokes_its_owner_certificate(caenv, tmp_path):
    keygen(tmp_path / "laptop", "laptop")
    caenv.run("trust", "laptop", "--key", str(tmp_path / "laptop.pub"),
              "--out", str(tmp_path / "b1"))
    both = (tmp_path / "b1" / "laptop-cert.pub").read_text()
    _no_ssh_section(caenv)
    code, out, err = caenv.run("trust", "laptop", "--logins", "agent",
                               "--out", str(tmp_path / "b2"))
    assert code == 0, err
    assert "may now log in as the agent's login" in out
    narrow = (tmp_path / "b2" / "laptop-cert.pub").read_text()
    krl = _pushed_krl(caenv)
    assert ca.krl_revokes(krl, both) and not ca.krl_revokes(krl, narrow)
    assert "owner:" not in _cert_text(tmp_path / "b2" / "laptop-cert.pub")


def test_machines_trusted_before_keep_both_and_are_told_once(caenv, tmp_path):
    keygen(tmp_path / "old", "old")
    with caenv.store.edit() as ed:  # as ships.toml had it before logins existed
        ed.set_machine("old", {"public_key": (tmp_path / "old.pub").read_text().strip(),
                               "added": "2026-01-01T00:00:00Z", "scope": ["all"]})
    code, out, err = caenv.run("trust", "old", "--print-cert")
    assert code == 0, err
    assert "Machines trusted before keep what they had, both: old" in err  # stdout: cert
    assert ca.machine(caenv.store.load(), "old").logins == ("agent", "owner")
    cert = tmp_path / "old-cert.pub"
    cert.write_text(out)
    assert "owner:all" in _cert_text(cert) and "agent:all" in _cert_text(cert)
    code, out, err = caenv.run("trust", "old", "--print-cert")
    assert "trusted before" not in out + err  # once


def _as_before_the_upgrade(store, name):
    """ships.toml as the previous FleetLedger left it: no logins, agent:* issued only."""
    with store.edit() as ed:
        entry = ed.doc["machine"][name]
        del entry["logins"]
        for i in entry["issued"]:
            i["principals"] = [p for p in i["principals"] if p.startswith("agent:")]


def test_narrowing_a_machine_trusted_before_the_upgrade_revokes_its_certificates(caenv,
                                                                                tmp_path):
    """X2: its agent:* certificates opened your login on the Ships of their day."""
    keygen(tmp_path / "shared", "shared")
    caenv.run("trust", "shared", "--key", str(tmp_path / "shared.pub"),
              "--out", str(tmp_path / "b1"))
    old = (tmp_path / "b1" / "shared-cert.pub").read_text()
    old_serial = ca.machine(caenv.store.load(), "shared").last_serial
    _as_before_the_upgrade(caenv.store, "shared")
    _no_ssh_section(caenv)
    code, out, err = caenv.run("trust", "shared", "--logins", "agent",
                               "--out", str(tmp_path / "b2"))
    assert code == 0, err
    assert "update every Ship" in out  # the migration says what --logins agent needs
    assert "1 certificate(s) issued for more" in out
    assert ca.krl_revokes(_pushed_krl(caenv), old)
    inv = caenv.store.load()
    assert old_serial in [s for v in ca.revoked_serials(inv).values() for s in v]
    assert not ca.krl_revokes(_pushed_krl(caenv), (tmp_path / "b2" / "shared-cert.pub")
                              .read_text())


def test_hand_ordered_logins_are_read_in_order(caenv, tmp_path):
    keygen(tmp_path / "m", "m")
    caenv.run("trust", "m", "--key", str(tmp_path / "m.pub"), "--logins", "owner,agent",
              "--out", str(tmp_path / "b"))
    with caenv.store.edit() as ed:
        ed.doc["machine"]["m"]["logins"] = ["owner", "agent"]
    assert ca.machine(caenv.store.load(), "m").logins == ("agent", "owner")
    code, out, err = caenv.run("trust", "m", "--out", str(tmp_path / "b2"))
    assert code == 0, err
    assert ca.parse_logins("owner,agent") == ["agent", "owner"]


def test_a_revocation_that_did_not_reach_a_ship_is_said_plainly(caenv, tmp_path,
                                                               monkeypatch):
    from fleetledger import trustcmd

    keygen(tmp_path / "lap", "lap")
    caenv.run("trust", "lap", "--key", str(tmp_path / "lap.pub"), "--out", str(tmp_path / "b"))

    def unreachable(app, inv, ship, **kw):
        raise ca.FleetError(f"{ship.name}: unreachable")

    monkeypatch.setattr(trustcmd, "certify", unreachable)
    said = []
    with pytest.raises(trustcmd.PushFailed) as exc:
        trustcmd.set_logins(caenv.app, caenv.store, "lap", ["agent"], said.append)
    assert exc.value.ships == ["ship-01", "ship-02"]
    assert any("NOT YET REVOKED on ship-01, ship-02" in line for line in said)
    assert ca.machine(caenv.store.load(), "lap").logins == ("agent",)  # recorded anyway


def test_an_owner_only_bundle_names_its_ships_without_herdr(caenv, tmp_path):
    keygen(tmp_path / "box", "box")
    caenv.run("trust", "box", "--key", str(tmp_path / "box.pub"), "--logins", "owner",
              "--out", str(tmp_path / "b"))
    script = (tmp_path / "b" / "install.sh").read_text()
    assert "    for ship in ; do\n" in script  # no herdr saved machines
    assert "Try: ssh ship-01 true   (your own login; herdr needs the agent's)" in script


def test_this_computer_attaches_only_while_it_may_log_in_as_the_agent(here):
    from fleetledger import trustcmd

    assert here.run("trust", "--this-computer")[0] == 0
    name = trustcmd.this_computer()["machine"]
    inv = here.store.load()
    assert trustcmd.attaches_here(inv, inv.ship("ship-01"))
    trustcmd.set_logins(here.app, here.store, name, ["owner"], lambda line: None)
    inv = here.store.load()
    assert not trustcmd.attaches_here(inv, inv.ship("ship-01"))  # no herdr --remote here
    config = (here.home_dir / ".ssh" / "shipshape" / "config").read_text()
    assert "  User human\n" in config  # set_logins rewrote this computer's bundle
    assert here.run("trust", "--this-computer", "--logins", "agent")[0] == 1
