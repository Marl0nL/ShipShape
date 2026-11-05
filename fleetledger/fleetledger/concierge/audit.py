"""The audit log: who, what, when, and the outcome. Never a secret.

One JSON object per line in ~/.local/share/fleetledger/audit.log (mode 0600):
time, Ship, action, service, outcome and the account now signed in. No codes,
links, tokens, paths or terminal output are ever written here.

Actions: sign-ins and sign-outs (login, logout; service: the provider), and
changes to Ships, from the command functions so the CLI and the web UI both
record them: create and destroy (service: the driver), forget, snapshot-rollback
and snapshot-delete (service: the snapshot's name), re-pin (service: host-key),
and untrust (Ship "-", service: the machine's name). Outcome: ok, failed, or
steps (the manual driver: the person does it by hand).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager

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


@contextmanager
def recorded(ship: str, action: str, service: str) -> Iterator[dict]:
    """Record a change to a Ship once it is done: ok, or failed if it raised. The
    caller may set result["outcome"] (steps: the manual driver's instructions)."""
    result = {"outcome": "ok"}
    try:
        yield result
    except BaseException:
        record(ship, action, service, "failed")
        raise
    record(ship, action, service, result["outcome"])
