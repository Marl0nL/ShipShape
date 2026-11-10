"""sshkeys: one home for key blobs and fingerprints, and the old names that use it."""

import subprocess

import pytest
from fleetledger.errors import FleetError
from fleetledger.seed import hostkey

from fleetledger import ca, firstrun, keys, sshkeys


@pytest.fixture
def key(tmp_path):
    path = tmp_path / "id"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "someone@example.com",
                    "-f", str(path)], check=True, capture_output=True)
    return path


def test_every_fingerprint_is_ssh_keygens(key):
    line = key.with_suffix(".pub").read_text().strip()
    listed = subprocess.run(["ssh-keygen", "-lf", str(key.with_suffix(".pub"))],
                            capture_output=True, text=True, check=True).stdout.split()[1]
    assert ca.fingerprint(line) == hostkey.fingerprint(line) == listed
    assert sshkeys.fingerprint(line) == sshkeys.checked_fingerprint(line) == listed
    assert keys.fingerprint(key) == firstrun.fingerprint(str(key.with_suffix(".pub"))) == listed
    assert sshkeys.keygen_fingerprint(str(key.with_suffix(".pub"))) == listed


def test_blob_ignores_the_comment(key):
    line = key.with_suffix(".pub").read_text().strip()
    kind, b64 = line.split()[:2]
    assert sshkeys.blob(line) == sshkeys.blob(f"{kind} {b64} other@example.com") == b64
    assert ca._blob(line) == b64
    assert sshkeys.blob("ssh-ed25519") == ""


def test_a_bad_line_is_lenient_in_ca_and_refused_from_outside():
    empty = sshkeys.fingerprint("")
    assert ca.fingerprint("ssh-ed25519") == empty  # no blob: an empty one, as before
    for bad in ("", "ssh-ed25519", "ssh-ed25519 not*base64"):
        with pytest.raises(FleetError, match="Not an OpenSSH public key line"):
            hostkey.fingerprint(bad)


def done(code, out=""):
    return subprocess.CompletedProcess([], code, out, "")


def test_agent_fingerprints_parse_ssh_add():
    listing = ("256 SHA256:aaa someone@example.com (ED25519)\n"
               "256 SHA256:bbb (ED25519-CERT)\n")
    assert sshkeys.agent_fingerprints(lambda argv: done(0, listing)) == ["SHA256:aaa",
                                                                         "SHA256:bbb"]
    assert sshkeys.agent_fingerprints(lambda argv: done(2)) is None  # no agent

    def missing(argv):
        raise FileNotFoundError(argv[0])

    assert sshkeys.agent_fingerprints(missing) is None
    assert sshkeys.keygen_fingerprint("x.pub", missing) is None
    assert sshkeys.keygen_fingerprint("x.pub", lambda argv: done(1, "")) is None


def test_firstrun_asks_no_agent_without_a_socket(monkeypatch):
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    assert firstrun.agent_fingerprints(lambda argv: pytest.fail("ran ssh-add")) is None
