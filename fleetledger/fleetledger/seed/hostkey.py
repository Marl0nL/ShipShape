"""A Ship's SSH host key, made by FleetLedger before the Ship exists.

The private half goes only into the seed (contracts/seed.md) and is never
written anywhere else by FleetLedger: ssh-keygen writes it into a private
temporary folder that is removed straight away.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from fleetledger import sshkeys
from fleetledger.errors import FleetError


@dataclass(frozen=True)
class HostKey:
    public: str  # "ssh-ed25519 AAAA... <comment>"
    private: str = field(repr=False)  # OpenSSH PEM; never logged or kept

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public)


fingerprint = sshkeys.checked_fingerprint  # 'SHA256:...'; FleetError on a bad line


def generate(comment: str) -> HostKey:
    """A new ed25519 host key, via the system's ssh-keygen."""
    with tempfile.TemporaryDirectory(prefix="fleetledger-hostkey-") as tmp:
        key = Path(tmp) / "key"
        try:
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key)],
                check=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise FleetError("ssh-keygen not found; install OpenSSH.") from exc
        except subprocess.CalledProcessError as exc:
            raise FleetError("ssh-keygen failed to make a host key.") from exc
        private = key.read_text(encoding="ascii")
        public = key.with_suffix(".pub").read_text(encoding="ascii").strip()
    return HostKey(public=public, private=private)
