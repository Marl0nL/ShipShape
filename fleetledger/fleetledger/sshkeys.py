"""SSH public keys: their base64 blob and their SHA256 fingerprints.

The one home for reading a key line, asking ssh-keygen for a key file's
fingerprint and listing the fingerprints ssh-agent holds (ssh-add -l).
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
from collections.abc import Callable
from typing import Any

from fleetledger.errors import FleetError

Runner = Callable[..., subprocess.CompletedProcess]


def _run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False, **kw)


def blob(key_line: str) -> str:
    """The base64 key of a public key line ("ssh-ed25519 AAAA... comment"); "" if it
    has none. Two lines with the same blob are the same key, whatever the comment."""
    parts = key_line.split()
    return parts[1] if len(parts) > 1 else ""


def _sha256(raw: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def fingerprint(key_line: str) -> str:
    """OpenSSH SHA256 fingerprint of a public key line (as ssh-keygen -l prints it).
    Lenient, for keys FleetLedger already holds: no blob hashes as an empty one."""
    return _sha256(base64.b64decode(blob(key_line)))


def checked_fingerprint(key_line: str) -> str:
    """fingerprint() of a key line from outside (a key scan, a seed); FleetError
    when it is not an OpenSSH public key line."""
    try:
        raw = base64.b64decode(key_line.split()[1], validate=True)
    except (IndexError, ValueError) as exc:
        raise FleetError("Not an OpenSSH public key line.") from exc
    return _sha256(raw)


def keygen_fingerprint(public_path: str, run: Runner = _run) -> str | None:
    """ssh-keygen -lf's fingerprint of a public key file; None if that fails."""
    try:
        result = run(["ssh-keygen", "-lf", public_path])
    except (OSError, subprocess.SubprocessError):
        return None
    parts = (result.stdout or "").split()
    return parts[1] if result.returncode == 0 and len(parts) > 1 else None


def agent_fingerprints(run: Runner = _run) -> list[str] | None:
    """The fingerprints ssh-agent holds (ssh-add -l); None if there is no ssh-add or
    it cannot reach an agent."""
    try:
        result = run(["ssh-add", "-l"])
    except (OSError, subprocess.SubprocessError):  # no ssh-add, or the agent hangs
        return None
    if result.returncode == 2:  # cannot connect to the agent
        return None
    return [line.split()[1] for line in (result.stdout or "").splitlines()
            if len(line.split()) > 1]
