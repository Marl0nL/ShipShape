"""The Proxmox client against a real local HTTPS server with a self-signed cert."""

import hashlib
import json
import ssl
import subprocess
import threading
import urllib.parse
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fleetledger.drivers.proxmox_api import (
    ProxmoxAPI,
    ProxmoxError,
    TLSPinMismatch,
    cert_fingerprint,
    read_token,
)
from fleetledger.errors import ConfigError

SECRET = "00000000-test-secret-0000-000000000000"


@pytest.fixture
def server(tmp_path):
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
         "-nodes", "-days", "1",
         "-subj", "/CN=pve.example.com", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            seen.append((self.command, self.path, dict(self.headers), body))
            if self.path.startswith("/api2/json/missing"):
                payload, code = {"data": None, "errors": {"vmid": "no such VM"}}, 500
            else:
                payload, code = {"data": {"ok": True, "path": self.path}}, 200
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_DELETE = _reply

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    der = ssl.PEM_cert_to_DER_cert(cert.read_text())
    token = tmp_path / "token"
    token.write_text(SECRET + "\n")
    token.chmod(0o600)
    config = {
        "api_url": f"https://127.0.0.1:{httpd.server_address[1]}/api2/json",
        "token_id": "shipshape@pve!build",
        "token_file": str(token),
        "tls_fingerprint": cert_fingerprint(der),
    }
    yield config, seen
    httpd.shutdown()


def test_pinned_request_sends_token(server):
    config, seen = server
    api = ProxmoxAPI(config)
    assert api.get("/version")["path"] == "/api2/json/version"
    method, path, headers, _ = seen[-1]
    assert headers["Authorization"] == f"PVEAPIToken=shipshape@pve!build={SECRET}"
    assert SECRET not in repr(api)


def test_wrong_pin_sends_nothing(server):
    config, seen = server
    api = ProxmoxAPI(dict(config, tls_fingerprint=":".join(["AB"] * 32)))
    with pytest.raises(TLSPinMismatch) as exc:
        api.get("/version")
    assert seen == []
    assert SECRET not in str(exc.value)


def test_parameters_and_errors(server):
    config, seen = server
    api = ProxmoxAPI(config)
    api.get("/cluster/nextid", vmid=9100)
    assert seen[-1][1] == "/api2/json/cluster/nextid?vmid=9100"
    api.post("/nodes/pve/qemu/9000/clone", newid=9101, full=True, pool="shipshape")
    form = urllib.parse.parse_qs(seen[-1][3].decode())
    assert form == {"newid": ["9101"], "full": ["1"], "pool": ["shipshape"]}
    api.delete("/nodes/pve/qemu/9101", purge=True)
    assert seen[-1][:2] == ("DELETE", "/api2/json/nodes/pve/qemu/9101?purge=1")
    with pytest.raises(ProxmoxError) as exc:
        api.get("/missing")
    assert exc.value.status == 500 and "no such VM" in exc.value.message
    assert SECRET not in exc.value.message


def test_upload_is_multipart_with_checksum(server):
    config, seen = server
    ProxmoxAPI(config).upload("pve", "shipshape-seeds", "ship-01-0a1b2c3d.iso", b"ISO!", "iso")
    method, path, headers, body = seen[-1]
    assert path == "/api2/json/nodes/pve/storage/shipshape-seeds/upload"
    message = BytesParser(policy=HTTP).parsebytes(
        f"Content-Type: {headers['Content-Type']}\r\n\r\n".encode() + body
    )
    parts = {p.get_param("name", header="content-disposition"): p for p in message.iter_parts()}
    # Proxmox needs the algorithm before the checksum, and the file last.
    assert list(parts) == ["content", "checksum-algorithm", "checksum", "filename"]
    assert parts["content"].get_content().strip() == "iso"
    assert parts["checksum-algorithm"].get_content().strip() == "sha256"
    assert parts["checksum"].get_content().strip() == hashlib.sha256(b"ISO!").hexdigest()
    assert parts["filename"].get_filename() == "ship-01-0a1b2c3d.iso"
    assert parts["filename"].get_payload(decode=True) == b"ISO!"


def test_token_file_rules(tmp_path):
    token = tmp_path / "t"
    token.write_text("secret\n")
    token.chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        read_token(str(token))
    token.chmod(0o600)
    assert read_token(str(token)) == "secret"
    token.write_text("a\nb\n")
    with pytest.raises(ConfigError, match="only the token"):
        read_token(str(token))
    with pytest.raises(ConfigError, match="does not exist"):
        read_token(str(tmp_path / "nope"))


def test_wait_task():
    class API(ProxmoxAPI):
        def __init__(self, answers):
            self.answers = list(answers)
            self.paths = []

        def get(self, path, **params):
            self.paths.append(path)
            return self.answers.pop(0)

    api = API([{"status": "running"}, {"status": "stopped", "exitstatus": "OK"}])
    api.wait_task("pve", "UPID:pve:1:2:3:qmclone:9101:x@pve!t:", sleep=lambda s: None)
    quoted = "UPID%3Apve%3A1%3A2%3A3%3Aqmclone%3A9101%3Ax%40pve%21t%3A"
    assert api.paths[0] == f"/nodes/pve/tasks/{quoted}/status"
    failing = API([{"status": "stopped", "exitstatus": "clone failed"}])
    with pytest.raises(ProxmoxError, match="clone failed"):
        failing.wait_task("pve", "UPID:x", sleep=lambda s: None)
    ticks = iter(range(0, 10000, 100))
    slow = API([{"status": "running"}] * 50)
    with pytest.raises(ProxmoxError, match="did not finish"):
        slow.wait_task(
            "pve", "UPID:x", timeout=250, sleep=lambda s: None, clock=lambda: next(ticks)
        )
