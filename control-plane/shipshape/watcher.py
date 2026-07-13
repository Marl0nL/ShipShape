"""Host-side spool watcher: ingest broker requests into the operator's queues.

Runs inside the TUI process (or `shipshape watch`). It ingests new requests and
resolves the ones that are automatic (OTP-gated auth refresh). Domain and command
requests are parked in the operator's queues for an explicit approve/decline —
this function never runs a command or approves a domain on its own.
"""

from __future__ import annotations

import re
import time

from . import creds, otp, spool
from .config import Config, Paths
from .state import CommandStore, PendingStore


def process_once(paths: Paths, cfg: Config) -> int:
    """Ingest all currently-unprocessed requests. Returns how many were handled."""
    handled = 0
    for req in spool.unprocessed_requests(paths.spool):
        rid = req.get("id", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", rid):
            continue  # id is the trusted filename stem; ignore anything malformed
        kind = req.get("kind", "")
        payload = req.get("payload", {}) or {}
        now = time.time()

        if kind == "domain":
            host = (payload.get("domain") or "").strip()
            if host:
                PendingStore(paths.pending).record(
                    host, "REQUEST", reason=payload.get("reason"), rid=rid
                )
                spool.write_response(paths.spool, rid, "pending", f"queued domain {host}", now)
            else:
                spool.write_response(paths.spool, rid, "error", "no domain given", now)

        elif kind == "command":
            command = (payload.get("command") or "").strip()
            if command:
                CommandStore(paths.commands).add(rid, command, payload.get("reason", ""))
                spool.write_response(paths.spool, rid, "pending", "queued for operator approval", now)
            else:
                spool.write_response(paths.spool, rid, "error", "no command given", now)

        elif kind == "refresh":
            ok, msg = otp.validate_and_consume(paths.state, payload.get("passphrase", ""))
            if not ok:
                spool.write_response(paths.spool, rid, "denied", msg, now)
            else:
                res = creds.refresh_gcp(paths, cfg)
                spool.write_response(
                    paths.spool, rid, "ok" if res.ok else "error", res.message, now
                )

        else:
            spool.write_response(paths.spool, rid, "error", f"unknown kind '{kind}'", now)

        handled += 1
    return handled
