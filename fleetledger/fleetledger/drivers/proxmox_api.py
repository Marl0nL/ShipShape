"""A small Proxmox VE API client: token auth and a pinned TLS certificate.

Proxmox hosts usually have self-signed certificates, so instead of a CA chain
the client pins the SHA-256 fingerprint of the API's certificate (from
ships.toml). The fingerprint is checked as soon as the TLS connection is up,
before the request (and with it the token) is sent.

The token secret is read from its file for each client, never logged, and
never put in an exception or repr.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import secrets
import ssl
import stat
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from fleetledger.errors import ConfigError, FleetError
from fleetledger.paths import expand


class ProxmoxError(FleetError):
    def __init__(self, message: str, *, status: int | None = None, hint: str | None = None):
        super().__init__(message, hint=hint)
        self.status = status


class TLSPinMismatch(ProxmoxError):
    pass


def cert_fingerprint(der: bytes) -> str:
    """SHA-256 of a DER certificate as colon-separated upper-case hex."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int | None, fingerprint: str, timeout: float):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # replaced by the fingerprint pin below
        super().__init__(host, port, timeout=timeout, context=context)
        self._fingerprint = fingerprint.upper()

    def connect(self) -> None:
        super().connect()
        der = self.sock.getpeercert(binary_form=True) or b""
        if not hmac.compare_digest(cert_fingerprint(der), self._fingerprint):
            self.close()
            raise TLSPinMismatch(
                f"The Proxmox API at {self.host} presented a certificate that does not match "
                "drivers.proxmox.tls_fingerprint; not sending the token.",
                hint="If the host's certificate was renewed, check the new fingerprint on the "
                "host and update ships.toml.",
            )


def read_token(path_value: str) -> str:
    path = expand(path_value)
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise ConfigError(f"Proxmox token file {path} does not exist.") from exc
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError(f"Proxmox token file {path} is readable by others; chmod 600 it.")
    secret = path.read_text(encoding="utf-8").strip()
    if not secret or "\n" in secret:
        raise ConfigError(f"Proxmox token file {path} must hold only the token secret.")
    return secret


class ProxmoxAPI:
    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        timeout: float = 30,
        connection_factory: Callable[..., http.client.HTTPSConnection] | None = None,
    ) -> None:
        url = urllib.parse.urlsplit(config["api_url"])
        self.host = url.hostname or ""
        self.port = url.port or 8006
        self.base = url.path.rstrip("/")  # /api2/json
        self.token_id = config["token_id"]
        self._token_file = config["token_file"]
        self.fingerprint = config["tls_fingerprint"]
        self.timeout = timeout
        self._factory = connection_factory or _PinnedConnection

    def __repr__(self) -> str:
        return f"ProxmoxAPI({self.host}:{self.port}, token_id={self.token_id})"

    def _connection(self) -> http.client.HTTPSConnection:
        return self._factory(self.host, self.port, self.fingerprint, self.timeout)

    def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        """Call the API; return its "data" member."""
        url = self.base + path
        headers = {
            "Authorization": f"PVEAPIToken={self.token_id}={read_token(self._token_file)}",
            "Accept": "application/json",
        }
        clean = {k: _value(v) for k, v in (params or {}).items() if v is not None}
        if body is None and clean:
            if method in ("GET", "DELETE"):
                url += "?" + urllib.parse.urlencode(clean)
            else:
                body = urllib.parse.urlencode(clean).encode("ascii")
                content_type = "application/x-www-form-urlencoded"
        if content_type:
            headers["Content-Type"] = content_type
        conn = self._connection()
        try:
            conn.request(method, url, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, ProxmoxError):
                raise
            raise ProxmoxError(f"Cannot reach the Proxmox API at {self.host}: {exc}") from exc
        finally:
            conn.close()
        try:
            doc = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            doc = {}
        if response.status >= 400:
            detail = doc.get("errors") if isinstance(doc, dict) else None
            extra = f": {json.dumps(detail, sort_keys=True)}" if detail else ""
            raise ProxmoxError(
                f"Proxmox {method} {path} failed: {response.status} {response.reason}{extra}",
                status=response.status,
            )
        return doc.get("data") if isinstance(doc, dict) else None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params)

    def post(self, path: str, **params: Any) -> Any:
        return self.request("POST", path, params)

    def put(self, path: str, **params: Any) -> Any:
        return self.request("PUT", path, params)

    def delete(self, path: str, **params: Any) -> Any:
        return self.request("DELETE", path, params)

    def upload(self, node: str, storage: str, file_name: str, data: bytes, content: str) -> Any:
        """Upload a file to a storage (multipart), with a checksum Proxmox verifies."""
        boundary = "fleetledger-" + secrets.token_hex(12)
        fields = {
            "content": content,
            "checksum": hashlib.sha256(data).hexdigest(),
            "checksum-algorithm": "sha256",
        }
        parts = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="filename"; '
            f'filename="{file_name}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
        )
        body = b"".join(parts) + data + f"\r\n--{boundary}--\r\n".encode()
        return self.request(
            "POST",
            f"/nodes/{node}/storage/{storage}/upload",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def wait_task(
        self,
        node: str,
        upid: str,
        *,
        timeout: float = 600,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Wait for a Proxmox task (UPID) to finish; raise unless it ended OK."""
        deadline = clock() + timeout
        quoted = urllib.parse.quote(upid, safe="")
        while True:
            task = self.get(f"/nodes/{node}/tasks/{quoted}/status")
            if task.get("status") == "stopped":
                if task.get("exitstatus") != "OK":
                    raise ProxmoxError(f"Proxmox task failed: {task.get('exitstatus')}")
                return
            if clock() > deadline:
                raise ProxmoxError(f"Proxmox task did not finish within {timeout:.0f}s.")
            sleep(1)


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)

