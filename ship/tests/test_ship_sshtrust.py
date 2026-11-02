"""ship-ctl ssh-trust and the status `ssh` section (contracts/ship-ctl.md).

Uses the real ssh-keygen to make a throwaway CA, host key and certificates; sshd
is a fake (ship/tests/fakes/fake.py) that can be told to refuse.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from ship_harness import assert_valid, ship  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from shipctl.sshtrust import parse_cert_listing  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

DROPIN = "/etc/ssh/sshd_config.d/20-shipshape-ca.conf"


def keygen(path: Path, comment: str) -> Path:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)], check=True
    )
    return path


def sign(
    ca: Path,
    key_pub: Path,
    *,
    host: bool,
    principals: str,
    validity: str = "-5m:+90d",
    serial: int = 7,
) -> str:
    args = [
        "ssh-keygen",
        "-q",
        "-s",
        str(ca),
        "-I",
        "test-cert",
        "-n",
        principals,
        "-V",
        validity,
        "-z",
        str(serial),
    ]
    if host:
        args.append("-h")
    subprocess.run([*args, str(key_pub)], check=True)
    cert = key_pub.with_name(key_pub.name.removesuffix(".pub") + "-cert.pub")
    return cert.read_text().strip()


def krl(path: Path, *pubs: Path) -> str:
    spec = path.with_suffix(".spec")
    spec.write_text("".join(f"key: {p.read_text().strip()}\n" for p in pubs))
    subprocess.run(["ssh-keygen", "-q", "-k", "-f", str(path), str(spec)], check=True)
    return base64.b64encode(path.read_bytes()).decode()


@pytest.fixture
def keys(ship, tmp_path):  # noqa: F811
    k = tmp_path / "keys"
    k.mkdir()
    ca = keygen(k / "host-ca", "host-ca")
    user_ca = keygen(k / "user-ca", "user-ca")
    host = keygen(k / "host", "ship-01")
    other = keygen(k / "other", "other")
    fleet = keygen(k / "fleet", "fleet-admin")
    laptop = keygen(k / "laptop", "laptop")
    ssh_dir = ship.p("/etc/ssh")
    ssh_dir.mkdir(parents=True)
    shutil.copy(k / "host.pub", ssh_dir / "ssh_host_ed25519_key.pub")
    (ssh_dir / "sshd_config").write_text("Include /etc/ssh/sshd_config.d/*.conf\n")
    fleet_ssh = ship.p("/home/fleet/.ssh")
    fleet_ssh.mkdir(parents=True)
    (fleet_ssh / "authorized_keys").write_text("# admin\nrestrict " + (k / "fleet.pub").read_text())
    return {
        "dir": k,
        "ca": ca,
        "user_ca": user_ca,
        "host": host,
        "other": other,
        "fleet": fleet,
        "laptop": laptop,
    }


def trust(ship, doc: dict):  # noqa: F811
    doc = {"schema": "shipshape.ssh-trust/1", **doc}
    return ship.ctl("ssh-trust", "--file", "-", "--json", input=json.dumps(doc))


def host_cert(keys, **kw) -> str:
    kw.setdefault("principals", "ship-01,ship-01.local")
    return sign(keys["ca"], keys["dir"] / "host.pub", host=True, **kw)


def snapshot(ship) -> dict:  # noqa: F811
    etc = ship.p("/etc/ssh")
    return {str(p.relative_to(etc)): p.read_bytes() for p in sorted(etc.rglob("*")) if p.is_file()}


def test_installs_certificate_ca_and_krl(ship, keys):  # noqa: F811
    cert = host_cert(keys)
    user_ca = (keys["dir"] / "user-ca.pub").read_text().strip()
    rc, doc, out = trust(
        ship,
        {
            "host_certificate": cert,
            "user_ca_keys": [user_ca],
            "revoked_keys": krl(keys["dir"] / "krl", keys["dir"] / "laptop.pub"),
        },
    )
    assert rc == 0, out
    assert_valid("ship-ctl-result", doc)
    assert doc["command"] == "ssh-trust" and doc["ok"]
    assert ship.p("/etc/ssh/ssh_host_ed25519_key-cert.pub").read_text().strip() == cert
    assert ship.p("/etc/ssh/shipshape/user-ca.pub").read_text().strip() == user_ca
    dropin = ship.p(DROPIN).read_text()
    for line in (
        "HostCertificate /etc/ssh/ssh_host_ed25519_key-cert.pub",
        "TrustedUserCAKeys /etc/ssh/shipshape/user-ca.pub",
        "AuthorizedPrincipalsFile /etc/ssh/shipshape/principals/%u",
        "RevokedKeys /etc/ssh/shipshape/revoked-keys",
    ):
        assert line in dropin.splitlines()
    # Certificates log in as agent only, with principals from ship.toml.
    principals = ship.p("/etc/ssh/shipshape/principals/agent").read_text().split()
    assert principals == ["agent:all", "agent:tag:work", "agent:ship:ship-01"]
    assert not ship.p("/etc/ssh/shipshape/principals/fleet").exists()
    data = doc["data"]
    assert data["host_certificate"]["principals"] == ["ship-01", "ship-01.local"]
    assert data["host_certificate"]["serial"] == 7
    assert data["user_ca"] and data["user_ca"][0].startswith("SHA256:")
    assert len(data["revoked_keys_sha256"]) == 64
    assert ["sshd", "-t", "-f", str(ship.p("/etc/ssh/sshd_config"))] in ship.calls("sshd")
    assert ["systemctl", "try-reload-or-restart", "ssh.service"] in ship.calls("systemctl")
    # The status document reports the same, and validates.
    rc, status, out = ship.ctl("status", "--json")
    assert rc == 0, out
    assert_valid("ship-status", status)
    assert status["ssh"] == data


def test_second_install_changes_nothing(ship, keys):  # noqa: F811
    cert = host_cert(keys)
    assert trust(ship, {"host_certificate": cert})[0] == 0
    calls = len(ship.calls("systemctl"))
    rc, doc, _ = trust(ship, {"host_certificate": cert})
    assert rc == 0 and doc["changed"] == []
    assert len(ship.calls("systemctl")) == calls  # no reload


@pytest.mark.parametrize(
    ("make", "why"),
    [
        (
            lambda k: sign(k["ca"], k["dir"] / "other.pub", host=True, principals="ship-01"),
            "different key",
        ),
        (
            lambda k: sign(k["ca"], k["dir"] / "host.pub", host=False, principals="ship-01"),
            "user certificate",
        ),
        (
            lambda k: sign(k["ca"], k["dir"] / "host.pub", host=True, principals="ship-02"),
            "does not list",
        ),
        (
            lambda k: sign(
                k["ca"],
                k["dir"] / "host.pub",
                host=True,
                principals="ship-01",
                validity="20200101:20200201",
            ),
            "expired",
        ),
        (
            lambda k: sign(
                k["ca"],
                k["dir"] / "host.pub",
                host=True,
                principals="ship-01",
                validity="+30d:+60d",
            ),
            "not valid yet",
        ),
    ],
    ids=["other-key", "user-cert", "wrong-name", "expired", "future"],
)
def test_refuses_bad_host_certificates(ship, keys, make, why):  # noqa: F811
    before = snapshot(ship)
    rc, doc, out = trust(ship, {"host_certificate": make(keys)})
    assert rc == 1, out
    assert doc["error"]["code"] == "invalid-input"
    assert why in doc["error"]["message"]
    assert snapshot(ship) == before
    assert ship.calls("systemctl") == []


def test_refuses_a_krl_that_locks_fleet_out(ship, keys):  # noqa: F811
    before = snapshot(ship)
    rc, doc, _ = trust(ship, {"revoked_keys": krl(keys["dir"] / "krl", keys["dir"] / "fleet.pub")})
    assert rc == 1 and doc["error"]["code"] == "invalid-input"
    assert "fleet" in doc["error"]["message"]
    assert snapshot(ship) == before


def test_refuses_a_krl_ssh_keygen_cannot_read(ship, keys):  # noqa: F811
    rc, doc, _ = trust(ship, {"revoked_keys": base64.b64encode(b"not a krl").decode()})
    assert rc == 1 and "revocation list" in doc["error"]["message"]
    assert not ship.p("/etc/ssh/shipshape/revoked-keys").exists()


def test_refuses_extra_lines_and_private_keys(ship, keys):  # noqa: F811
    user_ca = (keys["dir"] / "user-ca.pub").read_text().strip()
    rc, doc, _ = trust(ship, {"user_ca_keys": [user_ca + "\n"]})
    assert rc == 1 and doc["error"]["code"] == "invalid-input"
    private = (keys["dir"] / "user-ca").read_text()
    rc, doc, out = trust(ship, {"host_certificate": private})
    assert rc == 1 and doc["error"]["code"] == "invalid-input"
    assert "PRIVATE" not in out
    assert not ship.p("/etc/ssh/shipshape/user-ca.pub").exists()


def test_rolls_back_when_sshd_refuses(ship, keys):  # noqa: F811
    assert trust(ship, {"host_certificate": host_cert(keys, serial=1)})[0] == 0
    before = snapshot(ship)
    ship.set(sshd_refuses="/etc/ssh/sshd_config.d/20-shipshape-ca.conf line 3: Bad option")
    user_ca = (keys["dir"] / "user-ca.pub").read_text().strip()
    rc, doc, _ = trust(
        ship, {"host_certificate": host_cert(keys, serial=2), "user_ca_keys": [user_ca]}
    )
    assert rc == 1 and doc["error"]["code"] == "invalid-input"
    assert "Bad option" in doc["error"]["message"]
    assert snapshot(ship) == before


def test_empty_user_ca_list_turns_certificate_logins_off(ship, keys):  # noqa: F811
    user_ca = (keys["dir"] / "user-ca.pub").read_text().strip()
    assert trust(ship, {"user_ca_keys": [user_ca]})[0] == 0
    assert "TrustedUserCAKeys" in ship.p(DROPIN).read_text()
    rc, doc, _ = trust(ship, {"user_ca_keys": []})
    assert rc == 0
    assert not ship.p("/etc/ssh/shipshape/user-ca.pub").exists()
    text = ship.p(DROPIN).read_text()
    assert "TrustedUserCAKeys" not in text and "AuthorizedPrincipalsFile" not in text
    assert doc["data"]["user_ca"] == []


def test_tag_change_updates_principals(ship, keys):  # noqa: F811
    rc, _, out = ship.ctl("set", "ship.tags=personal,lab", "--json")
    assert rc == 0, out
    principals = ship.p("/etc/ssh/shipshape/principals/agent").read_text().split()
    assert principals == ["agent:all", "agent:tag:personal", "agent:tag:lab", "agent:ship:ship-01"]


def test_apply_removes_a_fleet_principals_file(ship, keys):  # noqa: F811
    fleet = ship.p("/etc/ssh/shipshape/principals/fleet")
    fleet.parent.mkdir(parents=True)
    fleet.write_text("agent:all\n")
    assert ship.ctl("apply", "--json")[0] == 0
    assert not fleet.exists()


def test_status_flags_an_expiring_certificate(ship, keys):  # noqa: F811
    assert trust(ship, {"host_certificate": host_cert(keys, validity="-1d:+3d")})[0] == 0
    rc, status, _ = ship.ctl("status", "--json")
    codes = [a["code"] for a in status["attention"]]
    assert "host-cert-expiring" in codes
    assert_valid("ship-status", status)


def test_status_without_certificates(ship):  # noqa: F811
    rc, status, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert status["ssh"]["host_certificate"] is None
    assert status["ssh"]["user_ca"] == [] and status["ssh"]["revoked_keys_sha256"] is None


def test_parse_listing_variants():
    text = (
        "cert.pub:\n"
        "        Type: ssh-ed25519-cert-v01@openssh.com host certificate\n"
        "        Public key: ED25519-CERT SHA256:" + "A" * 43 + "\n"
        "        Signing CA: ED25519 SHA256:" + "B" * 43 + " (using ssh-ed25519)\n"
        '        Key ID: "ship-01 host"\n'
        "        Serial: 9\n"
        "        Valid: forever\n"
        "        Principals: \n"
        "                ship-01\n"
        "                ship-01.local\n"
        "        Critical Options: (none)\n"
        "        Extensions: (none)\n"
    )
    info = parse_cert_listing(text)
    assert info["type"] == "host" and info["serial"] == 9
    assert info["valid_after"] is None and info["valid_before"] is None
    assert info["principals"] == ["ship-01", "ship-01.local"]
    after = parse_cert_listing(text.replace("Valid: forever", "Valid: after 2026-01-01T00:00:00"))
    assert after["valid_after"] == "2026-01-01T00:00:00Z" and after["valid_before"] is None
    assert parse_cert_listing("not a certificate") is None
