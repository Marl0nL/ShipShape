"""In-container broker — the ONLY thing on the agent network that reaches the
control plane, and it is deliberately powerless: it holds no credentials and no
host access. It can only drop request files into the shared spool and read back
responses that the host watcher writes.

Endpoints (agent -> broker):
  POST /request-domain   {domain, reason}
  POST /request-command  {command, reason}
  POST /refresh-auth     {passphrase}
  GET  /status/<id>
  GET  /health

Each POST enqueues a request and briefly waits for a fast (automatic) resolution;
if none arrives it returns 202 with status "pending" (the operator resolves it
out-of-band, and the agent can re-poll /status/<id>).
"""

import json
import os
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SPOOL = Path(os.environ.get("SHIPSHAPE_SPOOL", "/spool"))
PORT = int(os.environ.get("SHIPSHAPE_BROKER_PORT", "8099"))
POLL_SECONDS = float(os.environ.get("SHIPSHAPE_BROKER_POLL", "8"))

ROUTES = {"/request-domain": "domain", "/request-command": "command", "/refresh-auth": "refresh"}


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def _read(path: Path):
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_POST(self):
        kind = ROUTES.get(self.path)
        if not kind:
            return self._send(404, {"error": "not found"})
        rid = uuid.uuid4().hex
        _atomic_write(
            SPOOL / "req" / f"{rid}.json",
            {"id": rid, "kind": kind, "payload": self._read_body(), "ts": time.time()},
        )
        deadline = time.time() + POLL_SECONDS
        while time.time() < deadline:
            r = _read(SPOOL / "resp" / f"{rid}.json")
            if r and r.get("status") != "pending":
                return self._send(200, r)
            time.sleep(0.3)
        r = _read(SPOOL / "resp" / f"{rid}.json") or {"id": rid, "status": "pending", "message": "queued"}
        return self._send(202, r)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path.startswith("/status/"):
            rid = self.path.rsplit("/", 1)[-1]
            r = _read(SPOOL / "resp" / f"{rid}.json")
            return self._send(200, r or {"id": rid, "status": "unknown", "message": "no such request"})
        return self._send(404, {"error": "not found"})

    def log_message(self, *args):  # silence default stderr logging
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
