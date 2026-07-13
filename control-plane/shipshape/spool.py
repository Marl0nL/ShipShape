"""File-spool IPC between the in-container broker and the host control plane.

The broker (untrusted, on the agent network) can only drop request files here and
read back responses — it holds no credentials and no host access. The host watcher
reads requests and writes responses. All writes are atomic (write-tmp + rename), so
a reader never sees a half-written file.

  spool/req/<id>.json   {id, kind, payload, ts}      kind: domain | command | refresh
  spool/resp/<id>.json  {id, status, message, ts, data}
                        status: pending | ok | denied | error
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def req_path(spool: Path, rid: str) -> Path:
    return spool / "req" / f"{rid}.json"


def resp_path(spool: Path, rid: str) -> Path:
    return spool / "resp" / f"{rid}.json"


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def write_request(spool: Path, rid: str, kind: str, payload: dict, ts: float) -> None:
    _atomic_write(req_path(spool, rid), {"id": rid, "kind": kind, "payload": payload, "ts": ts})


def write_response(
    spool: Path, rid: str, status: str, message: str, ts: float, data: dict | None = None
) -> None:
    _atomic_write(
        resp_path(spool, rid),
        {"id": rid, "status": status, "message": message, "ts": ts, "data": data or {}},
    )


def unprocessed_requests(spool: Path) -> list[dict]:
    """Requests that have no response yet (i.e. not yet ingested by the watcher).

    The watcher writes at least a 'pending' response as soon as it ingests a
    request, so anything without a response file here is genuinely new.
    """
    out: list[dict] = []
    reqdir = spool / "req"
    if not reqdir.is_dir():
        return out
    for f in sorted(reqdir.glob("*.json")):
        if resp_path(spool, f.stem).exists():
            continue
        r = read_json(f)
        if r:
            out.append(r)
    return out
