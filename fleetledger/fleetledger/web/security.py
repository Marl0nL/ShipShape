"""Keep the web UI to the person at this computer (docs/web-ui.md, "Security").

Following Jupyter's model:

- The server binds 127.0.0.1 only (server.py); there is no option to change it.
- Every request must name this server in its Host header (127.0.0.1:PORT or
  localhost:PORT), so a page on another site cannot reach it through DNS
  rebinding.
- You get in with a one-time link that `ship-fleet ui` prints (and opens). The
  token in it is swapped for a session cookie (HttpOnly, SameSite=Strict) and
  dropped from the address bar. Only a hash of the token is kept, and it works
  once, within TOKEN_TTL seconds.
- Every change (any method but GET and HEAD) needs the session's CSRF token, in
  the X-CSRF-Token header or a `csrf` form field, and an Origin header naming
  this server exactly. Bodies are capped at MAX_BODY bytes and must be plain
  form posts.
- One exception to "plain form posts": a file for a Ship's inbox is posted to
  UPLOAD_PATH as the raw bytes of one file (application/octet-stream), with the
  CSRF token in the X-CSRF-Token header (never the body), the same Origin check,
  and a Content-Length of at most MAX_UPLOAD; the body is streamed to the handler
  and cut off at the declared length, never buffered here.
- The live terminal's WebSocket needs the session cookie and an Origin naming
  this server; the socket handler then asks for the CSRF token as its first
  message.
- Every response carries a strict Content-Security-Policy (scripts and styles
  from this server only, no inline code, no framing) and is never cached.

Nothing here is logged: not tokens, cookies, form bodies nor paths with queries.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs

TOKEN_TTL = 15 * 60
SESSION_IDLE = 12 * 3600
MAX_BODY = 64 * 1024
# A file for a Ship's inbox (web/inboxweb.py): the inbox's own per-file limit.
UPLOAD_PATH = re.compile(r"^/ships/[a-z0-9][a-z0-9-]{0,30}/inbox/upload$")
MAX_UPLOAD = 256 * 1024 * 1024
SAFE_METHODS = ("GET", "HEAD")
# Paths reachable without a session: the token link and the static files.
OPEN_PREFIXES = ("/static/",)
LOGIN_PATH = "/login"

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


@dataclass
class Session:
    id: str
    csrf: str
    seen: float


@dataclass
class Gate:
    """Who may use this server: the one-time link tokens and the sessions they made."""

    port: int
    clock: Callable[[], float] = time.monotonic
    token_ttl: float = TOKEN_TTL
    session_idle: float = SESSION_IDLE
    _tokens: dict[bytes, float] = field(default_factory=dict)  # sha256 -> expiry
    _sessions: dict[bytes, Session] = field(default_factory=dict)  # sha256(id) -> session
    # new_token() also runs on the terminal's "Enter for a new link" thread.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    @property
    def origins(self) -> frozenset[str]:
        return frozenset(f"http://{host}" for host in self.hosts)

    @property
    def cookie_name(self) -> str:
        # Cookies ignore ports, so another local server's cookie must not collide.
        return f"fleetledger-{self.port}"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # --- link tokens --------------------------------------------------------
    def new_token(self) -> str:
        """A fresh one-time token; only its hash is kept."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            self._tokens = {d: exp for d, exp in self._tokens.items() if exp > now}
            self._tokens[_digest(token)] = now + self.token_ttl
        return token

    def link(self, token: str) -> str:
        return f"{self.base_url}{LOGIN_PATH}?token={token}"

    def redeem(self, token: str | None) -> Session | None:
        """Swap a valid, unused, unexpired token for a new session."""
        if not token or len(token) > 200:
            return None
        digest = _digest(token)
        expiry = None
        with self._lock:
            for known, exp in self._tokens.items():
                if hmac.compare_digest(known, digest):
                    expiry = exp
            if expiry is None:
                return None
            del self._tokens[digest]  # one use only, valid or expired
        if expiry <= self.clock():
            return None
        session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32), self.clock())
        self._sessions[_digest(session.id)] = session
        return session

    # --- sessions -------------------------------------------------------------
    def session(self, cookie_value: str | None) -> Session | None:
        if not cookie_value or len(cookie_value) > 200:
            return None
        session = self._sessions.get(_digest(cookie_value))
        if session is None:
            return None
        now = self.clock()
        if now - session.seen > self.session_idle:
            del self._sessions[_digest(cookie_value)]
            return None
        session.seen = now
        return session

    def cookie_header(self, session: Session) -> str:
        return (
            f"{self.cookie_name}={session.id}; HttpOnly; SameSite=Strict; Path=/"
        )

    # --- request checks -------------------------------------------------------
    def host_ok(self, host: str | None) -> bool:
        return host is not None and host.lower() in self.hosts

    def origin_ok(self, origin: str | None) -> bool:
        return origin is not None and origin.lower() in self.origins

    @staticmethod
    def csrf_ok(session: Session, presented: str | None) -> bool:
        return bool(presented) and hmac.compare_digest(
            session.csrf.encode(), presented.encode("utf-8", "replace")
        )


def content_security_policy(port: int) -> str:
    return "; ".join(
        [
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self'",
            f"connect-src 'self' ws://127.0.0.1:{port} ws://localhost:{port}",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
        ]
    )


def security_headers(port: int) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-security-policy", content_security_policy(port).encode()),
        (b"x-frame-options", b"DENY"),
        (b"x-content-type-options", b"nosniff"),
        # Not no-referrer: with it, browsers send `Origin: null` on form posts,
        # and the Origin check would refuse every change. same-origin still
        # keeps the address (and any token) from ever leaving this server.
        (b"referrer-policy", b"same-origin"),
        (b"cache-control", b"no-store"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"cross-origin-resource-policy", b"same-origin"),
    ]


def _headers(scope: Scope) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in scope.get("headers", []):
        name = key.decode("latin-1").lower()
        if name in out:  # a repeated Host, Origin or Cookie is never trusted
            out[name] = "\0duplicate"
            continue
        out[name] = value.decode("latin-1")
    return out


def _cookie(headers: dict[str, str], name: str) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    try:
        jar.load(raw)
    except Exception:  # noqa: BLE001 - a malformed cookie header is just no session
        return None
    morsel = jar.get(name)
    return morsel.value if morsel else None


class GateMiddleware:
    """Applies the Gate to every HTTP request and WebSocket handshake."""

    def __init__(self, app: ASGIApp, gate: Gate) -> None:
        self.app = app
        self.gate = gate
        self.extra_headers = security_headers(gate.port)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        headers = _headers(scope)
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send, headers)
            return
        if scope["type"] != "http":
            return

        async def send_secured(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [
                    (k, v) for k, v in self.extra_headers if k not in names
                ]
            await send(message)

        if not self.gate.host_ok(headers.get("host")):
            await _plain(send_secured, 400, "Bad Host header.")
            return
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if path.startswith(OPEN_PREFIXES) and method in SAFE_METHODS:
            await self.app(scope, receive, send_secured)
            return
        if path == LOGIN_PATH and method == "GET":  # not HEAD: a link preview must not use it
            await self._login(scope, send_secured)
            return
        session = self.gate.session(_cookie(headers, self.gate.cookie_name))
        if session is None:
            await _plain(
                send_secured,
                401,
                "Not signed in to this FleetLedger. Open the link `ship-fleet ui` printed "
                "(press Enter in its terminal for a new one).",
            )
            return
        scope.setdefault("state", {})["session"] = session
        if method in SAFE_METHODS:
            await self.app(scope, receive, send_secured)
            return

        # A change: same origin, plain form (or empty) body, CSRF token.
        if not self.gate.origin_ok(headers.get("origin")):
            await _plain(send_secured, 403, "Cross-origin request refused.")
            return
        if method == "POST" and UPLOAD_PATH.match(path):
            await self._upload(scope, receive, send_secured, headers, session)
            return
        kind = headers.get("content-type", "").split(";")[0].strip().lower()
        if kind not in ("", "application/x-www-form-urlencoded"):
            await _plain(send_secured, 415, "Only form posts are accepted.")
            return
        declared = headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY):
            await _plain(send_secured, 413, "Request too large.")
            return
        body = b""
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body += message.get("body", b"")
            more = message.get("more_body", False)
            if len(body) > MAX_BODY:
                await _plain(send_secured, 413, "Request too large.")
                return
        presented = headers.get("x-csrf-token")
        if presented is None:
            try:
                fields = parse_qs(body.decode("utf-8"), max_num_fields=100)
            except (UnicodeDecodeError, ValueError):
                fields = {}
            values = fields.get("csrf", [])
            presented = values[0] if len(values) == 1 else None
        if not self.gate.csrf_ok(session, presented):
            await _plain(send_secured, 403, "Missing or wrong CSRF token; reload the page.")
            return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send_secured)

    async def _upload(self, scope: Scope, receive: Receive, send: Send,
                      headers: dict[str, str], session: Session) -> None:
        """One file's raw bytes (the inbox's drop zone). Checked before a byte is
        read: the CSRF header, the type and the declared size. The handler then
        reads the body as a stream, which ends at the declared length."""
        kind = headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/octet-stream":
            await _plain(send, 415, "A file upload is sent as application/octet-stream.")
            return
        if not self.gate.csrf_ok(session, headers.get("x-csrf-token")):
            await _plain(send, 403, "Missing or wrong CSRF token; reload the page.")
            return
        declared = headers.get("content-length")
        if declared is None or not declared.isdigit():
            await _plain(send, 411, "A file upload needs its length.")
            return
        if int(declared) > MAX_UPLOAD:
            await _plain(send, 413, f"Too large: the inbox takes files of up to "
                         f"{MAX_UPLOAD // (1024 * 1024)} MB each.")
            return
        left = int(declared)

        async def capped() -> Message:
            nonlocal left
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if len(body) > left:  # more than declared: the server should not allow it
                    return {"type": "http.disconnect"}
                left -= len(body)
            return message

        scope.setdefault("state", {})["upload_length"] = int(declared)
        await self.app(scope, capped, send)

    async def _login(self, scope: Scope, send: Send) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        tokens = query.get("token", [])
        session = self.gate.redeem(tokens[0] if len(tokens) == 1 else None)
        if session is None:
            await _plain(
                send,
                403,
                "That link is used, expired or wrong. Press Enter in the terminal running "
                "`ship-fleet ui` for a new one.",
            )
            return
        # A same-origin refresh (not a redirect), so the SameSite=Strict cookie
        # is sent on the next request even when the link came from elsewhere,
        # and the token leaves the address bar.
        page = (
            b'<!doctype html><meta charset="utf-8">'
            b'<meta http-equiv="refresh" content="0;url=/">'
            b"<title>FleetLedger</title><p>Signed in. <a href=\"/\">Continue</a>.</p>"
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/html; charset=utf-8"),
                    (b"content-length", str(len(page)).encode()),
                    (b"set-cookie", self.gate.cookie_header(session).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": page})

    async def _websocket(
        self, scope: Scope, receive: Receive, send: Send, headers: dict[str, str]
    ) -> None:
        session = self.gate.session(_cookie(headers, self.gate.cookie_name))
        if (
            not self.gate.host_ok(headers.get("host"))
            or not self.gate.origin_ok(headers.get("origin"))
            or session is None
        ):
            message = await receive()  # websocket.connect
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008})
            return
        scope.setdefault("state", {})["session"] = session
        await self.app(scope, receive, send)


async def _plain(send: Send, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
