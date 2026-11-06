"""Keep the web UI to the person at this computer (docs/web-ui.md, "Security").

Following Jupyter's model:

- The server binds 127.0.0.1 only (server.py); there is no option to change it.
- Every request must name this server in its Host header (127.0.0.1:PORT or
  localhost:PORT), so a page on another site cannot reach it through DNS
  rebinding. One naming this computer must not carry a proxy's headers
  (PROXY_HEADERS): Tailscale Serve keeps the client's Host, so a request through
  it could otherwise pass as a browser on this computer.
- One more origin only when you turn it on (web/remote.py): https://NAME, this
  computer's own name, which Tailscale Serve terminates and sends to the same
  loopback port. A request naming it must have come through Serve from a
  signed-in person's device (X-Forwarded-Proto https and a Tailscale-User-Login
  header, which Serve sets and strips from what clients send; Funnel and tagged
  devices get none), and that login must be one you allowed (ship-fleet ui remote
  --login), before anything else. Its Origin must be exactly https://NAME, its
  cookie is `__Host-` and Secure, and a session made on one origin never works
  on the other.
- On the remote origin a browser signs in by pairing once (devices.py): a
  one-time link makes a named device, kept hashed in a 0600 file so a restart
  does not sign it out, tied to the login that paired it, revocable, with idle
  and absolute lifetimes. Its danger actions (devices.FRESH_ONLY) need a pairing
  younger than devices.FRESH.
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

from fleetledger.errors import FleetError
from fleetledger.web import devices as devices_mod
from fleetledger.web.devices import Device, Devices
from fleetledger.web.remote import Remote

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
LOCAL = "local"  # Session.where: a browser on this computer (else the remote origin)
# Host-only, Secure and Path=/ by the browser's own rules; only on the HTTPS origin.
REMOTE_COOKIE = "__Host-fleetledger"
# Headers a proxy adds (Tailscale Serve sets the first three on every request it passes
# on, and the identity ones for a person's device). Never sent by a browser itself.
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded",
                 "tailscale-user-login", "tailscale-user-name", "tailscale-user-profile-pic",
                 "tailscale-app-capabilities", "tailscale-funnel-request",
                 "tailscale-headers-info")

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
    where: str = LOCAL  # the origin it signed in on: LOCAL, or the remote origin
    device: Device | None = None  # a paired browser on the remote origin


@dataclass
class Gate:
    """Who may use this server: the one-time link tokens and the sessions they made."""

    port: int
    remote: Remote | None = None  # the one HTTPS origin Tailscale Serve gives it, if on
    devices: Devices | None = None  # the remote origin's paired browsers (none: no sign-in)
    # () -> the logins allowed now (remote.Allowed, which follows the file); else the
    # ones remote was made with.
    allowed: Callable[[], tuple[str, ...]] | None = None
    clock: Callable[[], float] = time.monotonic
    token_ttl: float = TOKEN_TTL
    session_idle: float = SESSION_IDLE
    _tokens: dict[bytes, float] = field(default_factory=dict)  # sha256 -> expiry
    _sessions: dict[bytes, Session] = field(default_factory=dict)  # sha256(id) -> session
    # new_token() also runs on the terminal's "Enter for a new link" thread.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def local_hosts(self) -> frozenset[str]:
        return frozenset({f"127.0.0.1:{self.port}", f"localhost:{self.port}"})

    @property
    def cookie_name(self) -> str:
        # Cookies ignore ports, so another local server's cookie must not collide.
        return f"fleetledger-{self.port}"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def where(self, host: str | None) -> str | None:
        """Which origin a request's Host header names: LOCAL, the remote origin, or
        None (refused). The remote name is matched exactly, never as a pattern."""
        if host is None:
            return None
        host = host.lower()
        if host in self.local_hosts:
            return LOCAL
        if self.remote is not None and host in (self.remote.host, self.remote.hostport):
            return self.remote.origin
        return None

    def origins_for(self, where: str) -> frozenset[str]:
        if where == LOCAL:
            return frozenset(f"http://{host}" for host in self.local_hosts)
        return frozenset({where}) if self.remote and where == self.remote.origin else frozenset()

    def cookie_name_for(self, where: str) -> str:
        return self.cookie_name if where == LOCAL else REMOTE_COOKIE

    @staticmethod
    def proxied(headers: dict[str, str]) -> bool:
        """Did a proxy (Tailscale Serve, say) pass this request on? A browser on this
        computer, or through an SSH tunnel, never sends these headers. Serve keeps the
        client's Host, so a request through it can name 127.0.0.1: these headers are how
        it is still told apart, and refused on this computer's address."""
        return any(name in headers for name in PROXY_HEADERS)

    @staticmethod
    def through_serve(headers: dict[str, str]) -> bool:
        """Did a request on the remote origin come through Tailscale Serve, over HTTPS,
        from a signed-in person's device? Serve sets both headers and strips the
        identity ones from what clients send; it sets none for Funnel or tagged
        devices. (Another program on this computer could forge them, so this is
        never the only check: a session is still needed.)"""
        login = headers.get("tailscale-user-login", "")
        return (headers.get("x-forwarded-proto") == "https"
                and bool(login.strip()) and not login.startswith("\0"))

    def login_of(self, headers: dict[str, str]) -> str | None:
        """The request's Tailscale login, if it is one you allowed; else None."""
        login = headers.get("tailscale-user-login", "").strip().lower()
        if self.remote is None or not login:
            return None
        allowed = self.allowed() if self.allowed is not None else self.remote.logins
        return login if login in allowed else None

    # --- link tokens --------------------------------------------------------
    def new_token(self) -> str:
        """A fresh one-time token; only its hash is kept."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            now = self.clock()
            self._tokens = {d: exp for d, exp in self._tokens.items() if exp > now}
            self._tokens[_digest(token)] = now + self.token_ttl
        return token

    def link(self, token: str, where: str = LOCAL) -> str:
        base = self.base_url if where == LOCAL else where
        return f"{base}{LOGIN_PATH}?token={token}"

    def _take_token(self, token: str) -> bool | None:
        """Use up one of this run's link tokens: True if valid, False if expired, None if
        it is not one of them. One use only, valid or expired."""
        digest = _digest(token)
        expiry = None
        with self._lock:
            for known, exp in self._tokens.items():
                if hmac.compare_digest(known, digest):
                    expiry = exp
            if expiry is None:
                return None
            del self._tokens[digest]
        return expiry > self.clock()

    def redeem(self, token: str | None, where: str = LOCAL, *, login: str | None = None,
               user_agent: str | None = None, replaces: str | None = None) -> Session | None:
        """Swap a valid, unused, unexpired token for a new session. On the remote origin
        that pairs a device (login: the allowed Tailscale login opening it; replaces:
        the session id this browser held before, if any)."""
        if not token or len(token) > 200:
            return None
        if where == LOCAL:
            if not self._take_token(token):
                return None
            session = Session(secrets.token_urlsafe(32), secrets.token_urlsafe(32),
                              self.clock(), where=where)
            self._sessions[_digest(session.id)] = session
            return session
        if self.devices is None or self.remote is None or not login:
            return None  # nobody signs in remotely without a device store and a login
        mine = self._take_token(token)  # the terminal's link works here too
        if mine is None:
            pairing = self.devices.take_pairing(token)
            if pairing is None:
                return None
            name, how, machine = pairing.name, pairing.how, pairing.machine
        elif mine:
            name, how, machine = None, devices_mod.FROM_TERMINAL, None
        else:
            return None
        # The device this browser held before, if the same login paired it (another
        # login's device is neither replaced nor lends its name).
        old = self.devices.get(replaces, login) if replaces else None
        session_id = secrets.token_urlsafe(32)
        device = self.devices.add(
            session_id, name=name or (old.name if old else devices_mod.guess_name(user_agent)),
            login=login, origin=self.remote.origin, how=how,
            replaces=old.key if old else None, machine=machine)
        from fleetledger.concierge import audit

        audit.record("-", "ui-pair", "web", "again" if old else "ok", account=login)
        return Session(session_id, devices_mod.csrf_for(session_id), self.clock(),
                       where=where, device=device)

    # --- sessions -------------------------------------------------------------
    def session(self, cookie_value: str | None, where: str = LOCAL,
                login: str | None = None) -> Session | None:
        if not cookie_value or len(cookie_value) > 200:
            return None
        if where != LOCAL:
            return self._device_session(cookie_value, where, login)
        session = self._sessions.get(_digest(cookie_value))
        if session is None or session.where != where:  # never across origins
            return None
        now = self.clock()
        if now - session.seen > self.session_idle:
            del self._sessions[_digest(cookie_value)]
            return None
        session.seen = now
        return session

    def _device_session(self, cookie_value: str, where: str,
                        login: str | None) -> Session | None:
        """A paired device's session: on the origin it was paired on, from the login that
        paired it, not lapsed and not revoked (the file is re-read when it changes)."""
        if self.devices is None or self.remote is None or where != self.remote.origin:
            return None
        device = self.devices.get(cookie_value, login or "")
        if device is None or device.origin != where:
            return None
        return Session(cookie_value, devices_mod.csrf_for(cookie_value), self.clock(),
                       where=where, device=device)

    def cookie_header(self, session: Session) -> str:
        if session.where == LOCAL:
            return f"{self.cookie_name}={session.id}; HttpOnly; SameSite=Strict; Path=/"
        # Kept when the browser closes; the device itself lapses on the server's terms.
        return (f"{REMOTE_COOKIE}={session.id}; Secure; HttpOnly; SameSite=Strict; Path=/; "
                f"Max-Age={devices_mod.ABSOLUTE}")

    def fresh_enough(self, session: Session, path: str) -> bool:
        """May this session make this change? A danger action (devices.FRESH_ONLY) from a
        remote device needs one paired within devices.FRESH; this computer's are never
        held to it."""
        if session.device is None or not devices_mod.needs_fresh(path):
            return True
        return self.devices is not None and session.device.fresh(self.devices.wall())

    # --- request checks -------------------------------------------------------
    def host_ok(self, host: str | None) -> bool:
        return self.where(host) is not None

    def origin_ok(self, origin: str | None, where: str = LOCAL) -> bool:
        """An Origin naming the same origin the request's Host does, exactly."""
        return origin is not None and origin.lower() in self.origins_for(where)

    @staticmethod
    def csrf_ok(session: Session, presented: str | None) -> bool:
        return bool(presented) and hmac.compare_digest(
            session.csrf.encode(), presented.encode("utf-8", "replace")
        )


def content_security_policy(port: int, remote: Remote | None = None) -> str:
    sockets = f"ws://127.0.0.1:{port} ws://localhost:{port}"
    if remote is not None:  # the live terminal from an HTTPS page is wss:
        sockets += f" {remote.websocket}"
    return "; ".join(
        [
            "default-src 'none'",
            "script-src 'self'",
            "style-src 'self'",
            "img-src 'self'",
            f"connect-src 'self' {sockets}",
            "form-action 'self'",
            "frame-ancestors 'none'",
            "base-uri 'none'",
        ]
    )


def security_headers(port: int, remote: Remote | None = None) -> list[tuple[bytes, bytes]]:
    return [
        (b"content-security-policy", content_security_policy(port, remote).encode()),
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
        self.extra_headers = security_headers(gate.port, gate.remote)

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

        where = self.gate.where(headers.get("host"))
        if where is None:
            await _plain(send_secured, 400, "Bad Host header.")
            return
        if where == LOCAL and self.gate.proxied(headers):
            await _plain(send_secured, 400, PROXIED)
            return
        login = None
        if where != LOCAL:
            if not self.gate.through_serve(headers):
                await _plain(send_secured, 403, NOT_THROUGH_SERVE)
                return
            login = self.gate.login_of(headers)
            if login is None:  # before anything else, static files included
                await _plain(send_secured, 403, NOT_ALLOWED)
                return
        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if path.startswith(OPEN_PREFIXES) and method in SAFE_METHODS:
            await self.app(scope, receive, send_secured)
            return
        cookie = _cookie(headers, self.gate.cookie_name_for(where))
        if path == LOGIN_PATH and method == "GET":  # not HEAD: a link preview must not use it
            await self._login(scope, send_secured, where, login=login,
                              user_agent=headers.get("user-agent"), replaces=cookie)
            return
        session = self.gate.session(cookie, where, login)
        if session is None:
            await _plain(send_secured, 401, NOT_SIGNED_IN if where == LOCAL
                         else NOT_PAIRED)
            return
        scope.setdefault("state", {})["session"] = session
        if method in SAFE_METHODS:
            await self.app(scope, receive, send_secured)
            return

        # A change: same origin, plain form (or empty) body, CSRF token.
        if not self.gate.origin_ok(headers.get("origin"), where):
            await _plain(send_secured, 403, "Cross-origin request refused.")
            return
        if session.device is not None and not self.gate.fresh_enough(session, path):
            await _plain(send_secured, 403, NOT_FRESH)
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

    async def _login(self, scope: Scope, send: Send, where: str, *, login: str | None,
                     user_agent: str | None, replaces: str | None) -> None:
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        tokens = query.get("token", [])
        try:
            session = self.gate.redeem(tokens[0] if len(tokens) == 1 else None, where,
                                       login=login, user_agent=user_agent, replaces=replaces)
        except FleetError as exc:  # too many devices paired: say so
            await _plain(send, 403, exc.message)
            return
        if session is None:
            await _plain(
                send,
                403,
                "That link is used, expired or wrong. Press Enter in the terminal running "
                "`ship-fleet ui` for a new one." if where == LOCAL else
                "That link is used, expired or wrong. Get a new one: `ship-fleet ui link` on "
                "the FleetLedger computer (over ssh is fine).",
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
        where = self.gate.where(headers.get("host"))
        remote_ok = where == LOCAL or (where is not None and self.gate.through_serve(headers)
                                       and self.gate.login_of(headers) is not None)
        session = remote_ok and self.gate.session(
            _cookie(headers, self.gate.cookie_name_for(where)), where,
            None if where == LOCAL else self.gate.login_of(headers))
        if (
            where is None
            or (where == LOCAL and self.gate.proxied(headers))
            or not remote_ok
            or not self.gate.origin_ok(headers.get("origin"), where)
            or not session
        ):
            message = await receive()  # websocket.connect
            if message["type"] == "websocket.connect":
                await send({"type": "websocket.close", "code": 1008})
            return
        scope.setdefault("state", {})["session"] = session
        await self.app(scope, receive, send)


NOT_SIGNED_IN = ("Not signed in to this FleetLedger. Open a link `ship-fleet ui` printed "
                 "(press Enter in its terminal for a new one).")
PROXIED = ("This computer's address (127.0.0.1 or localhost) takes no request a proxy passed "
           "on. From your other devices, open the remote address FleetLedger printed.")
NOT_PAIRED = ("This device is not signed in to this FleetLedger. Pair it once: run "
              "`ship-fleet ui link` on the FleetLedger computer (over ssh is fine) and open "
              "the link here, or use Settings, Devices, Pair a device on a browser that is "
              "signed in.")
NOT_FRESH = (f"This device was paired more than {devices_mod.FRESH // 3600} hours ago, and "
             "this action (destroy, "
             "rollback, trusting machines, setup's keys, pairing another device ...) needs a "
             "fresh pairing. Pair this browser again: run `ship-fleet ui link` on the "
             "FleetLedger computer (over ssh is fine) and open the link here.")
NOT_ALLOWED = ("This Tailscale login may not use this FleetLedger (or its remote address is "
               "off). Its owner allows logins with `ship-fleet ui remote --login`.")
NOT_THROUGH_SERVE = ("This address works only through Tailscale Serve, from a device signed in "
                     "to your tailnet as a person (not a tagged device, and never through "
                     "Funnel).")


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
