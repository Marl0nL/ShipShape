"""The concierge's audit log: who, what, when, and the outcome. Never a secret.

One JSON object per line in ~/.local/share/fleetledger/audit.log (mode 0600):
time, Ship, action, service, outcome and the account now signed in. No codes,
links, tokens or terminal output are ever written here.
"""

from __future__ import annotations

import json
import os

from fleetledger import paths
from fleetledger.inventory import now_timestamp


def record(
    ship: str, action: str, service: str, outcome: str, account: str | None = None
) -> dict:
    entry = {
        "at": now_timestamp(),
        "ship": ship,
        "action": action,
        "service": service,
        "outcome": outcome,
        "account": account,
    }
    path = paths.audit_log_path()
    paths.private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    os.chmod(path, 0o600)
    return entry


def read() -> list[dict]:
    try:
        lines = paths.audit_log_path().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in lines if line.strip()]
