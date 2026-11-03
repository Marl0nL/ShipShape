"""A small Proxmox VE API client: token auth and a pinned TLS certificate.

Proxmox hosts usually have self-signed certificates, so instead of a CA chain
the client pins the SHA-256 fingerprint of the API's certificate (from
ships.toml). The fingerprint is checked as soon as the TLS connection is up,
before the request (and with it the token) is sent.

The token secret is read from its store (a 0600 file, or the OS keyring) for
each request, never logged, and never put in an exception or repr.
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
from pathlib import Path
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
                "host, then pin it: ship-fleet setup --redo hypervisor.",
            )


def server_fingerprint(host: str, port: int, timeout: float = 10) -> str:
    """The SHA-256 fingerprint of the certificate a server presents, to show the
    user before pinning it. Nothing is sent over the connection."""
    import socket

    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # we only read the certificate, to show it
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host  # IPv6
    try:
        with socket.create_connection((bare, port), timeout=timeout) as raw, \
                context.wrap_socket(raw, server_hostname=bare) as tls:
            der = tls.getpeercert(binary_form=True) or b""
    except (OSError, ssl.SSLError) as exc:
        raise ProxmoxError(f"Cannot reach {host}:{port}: {exc}") from exc
    if not der:
        raise ProxmoxError(f"{host}:{port} presented no certificate.")
    return cert_fingerprint(der)


def read_token(path_value: str) -> str:
    path = expand(path_value)
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise ConfigError(f"Proxmox token file {path} does not exist.") from exc
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError(f"Proxmox token file {path} is readable by others; chmod 600 it.")
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:  # a folder, unreadable, not text
        raise ConfigError(f"Cannot read the Proxmox token file {path} "
                          f"({type(exc).__name__}).") from None
    if not secret or "\n" in secret:
        raise ConfigError(f"Proxmox token file {path} must hold only the token secret.")
    return secret


TOKEN_SWITCH_HINT = ("To store it again, or keep it in a 0600 file instead: "
                     "ship-fleet setup --redo hypervisor.")


def token_secret(config: Mapping[str, Any]) -> str:
    """The token secret, from the store ships.toml names (token_store). A keyring
    error is never answered by reading a file instead."""
    if config.get("token_store", "file") == "keyring":
        from fleetledger.overlay import secret_store

        return secret_store.read_keyring(config["keyring_item"], "Proxmox token",
                                         TOKEN_SWITCH_HINT)
    if not config.get("token_file"):
        raise ConfigError("drivers.proxmox has neither token_file nor token_store = keyring.")
    return read_token(config["token_file"])


def token_where(config: Mapping[str, Any]) -> str:
    if config.get("token_store", "file") == "keyring":
        return f"the keyring (item {config.get('keyring_item')})"
    return f"{config.get('token_file')} (0600)"


class ProxmoxAPI:
    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        timeout: float = 30,
        connection_factory: Callable[..., http.client.HTTPSConnection] | None = None,
        secret: Callable[[], str] | None = None,
    ) -> None:
        url = urllib.parse.urlsplit(config["api_url"])
        self.host = url.hostname or ""
        self.port = url.port or 8006
        self.base = url.path.rstrip("/")  # /api2/json
        self.token_id = config["token_id"]
        # Read for each request, from where ships.toml says (or, during setup, from
        # memory before anything is stored).
        stored = dict(config)
        self._secret = secret or (lambda: token_secret(stored))
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
            "Authorization": f"PVEAPIToken={self.token_id}={self._secret()}",
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
            try:
                conn.request(method, url, body=body, headers=headers)
            except (OSError, http.client.HTTPException) as exc:
                if isinstance(exc, ProxmoxError):
                    raise
                raise ProxmoxError(f"Cannot reach the Proxmox API at {self.host}: {exc}") from exc
            return self._answer(conn, method, path)
        finally:
            conn.close()

    def _answer(self, conn: http.client.HTTPSConnection, method: str, path: str) -> Any:
        """Read the response on conn; return its "data" member or raise."""
        try:
            response = conn.getresponse()
            raw = response.read()
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, ProxmoxError):
                raise
            raise ProxmoxError(f"Cannot reach the Proxmox API at {self.host}: {exc}") from exc
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
        # Order matters: Proxmox rejects a checksum that arrives before its
        # algorithm (400), and the file must come last.
        fields = {
            "content": content,
            "checksum-algorithm": "sha256",
            "checksum": hashlib.sha256(data).hexdigest(),
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

    def upload_file(
        self,
        node: str,
        storage: str,
        file_name: str,
        path: Path,
        content: str,
        *,
        sha256: str,
        chunk_size: int = 1 << 20,
        timeout: float = 1800,
    ) -> Any:
        """Upload a large file from disk without holding it in memory.

        The file is checked against sha256 first (so a changed file is never sent),
        and Proxmox checks the same sum once it has the file.
        """
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as fh:
            while block := fh.read(chunk_size):
                digest.update(block)
                size += len(block)
        if not hmac.compare_digest(digest.hexdigest(), sha256):
            raise ProxmoxError(f"{path} does not match its recorded sha256; not uploading it.")
        boundary = "fleetledger-" + secrets.token_hex(12)
        head = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
            for name, value in (("content", content), ("checksum-algorithm", "sha256"),
                                ("checksum", sha256))
        ) + (
            f'--{boundary}\r\nContent-Disposition: form-data; name="filename"; '
            f'filename="{file_name}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        api_path = f"/nodes/{node}/storage/{storage}/upload"
        conn = self._factory(self.host, self.port, self.fingerprint, max(self.timeout, timeout))
        try:
            try:
                conn.putrequest("POST", self.base + api_path)
                conn.putheader(
                    "Authorization", f"PVEAPIToken={self.token_id}={self._secret()}"
                )
                conn.putheader("Accept", "application/json")
                conn.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
                conn.putheader("Content-Length", str(len(head) + size + len(tail)))
                conn.endheaders()  # connects, and checks the TLS pin, before anything is sent
                conn.send(head)
                with path.open("rb") as fh:
                    while block := fh.read(chunk_size):
                        conn.send(block)
                conn.send(tail)
            except (OSError, http.client.HTTPException) as exc:
                if isinstance(exc, ProxmoxError):
                    raise
                raise ProxmoxError(f"Uploading to the Proxmox API at {self.host} failed: {exc}") \
                    from exc
            return self._answer(conn, "POST", api_path)
        finally:
            conn.close()

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

