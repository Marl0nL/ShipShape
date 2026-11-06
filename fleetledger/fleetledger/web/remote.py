"""The web UI from your other devices, through Tailscale Serve
(docs/runbooks/remote-ui.md).

FleetLedger still binds 127.0.0.1 only. Tailscale Serve, on this computer,
terminates HTTPS for this computer's MagicDNS name and proxies to that port; the
gate (security.py) then accepts exactly one more origin, https://<that name>, and
only for requests that came through Serve from a signed-in person's device.

- Off unless you turn it on: `ship-fleet ui remote https://NAME --port N
  --login you@example.com`. The setting is a fact about this computer, kept
  beside ships.toml (like this-computer.json) and never part of the fleet.
- Only the Tailscale logins you list may use it: a request from anyone else is
  refused before FleetLedger's own sign-in, and with none listed the remote
  address is not served at all. Each browser then still pairs once with a
  one-time link (devices.py).
- Every start of `ship-fleet ui` checks, through the local `tailscale` command,
  that Tailscale is running here, that NAME is this computer's own name (with an
  HTTPS certificate on offer), that Serve sends NAME (and nothing else at it, on
  any port) to this port, and that Funnel is off for it. Anything else and the UI refuses
  to start; `ship-fleet ui --local` starts it for this computer only.
- A file that is not exactly what this module writes is refused too, never
  guessed at.
"""

from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fleetledger import paths
from fleetledger.errors import ConfigError, FleetError

REMOTE_FILE = "remote-ui.json"
# The ports Tailscale Serve offers HTTPS on.
SERVE_PORTS = (443, 8443, 10000)
LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# A Tailscale login as Serve passes it on: you@example.com, or example-user@github.
LOGIN = re.compile(r"^[a-z0-9._%+'-]{1,64}@[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                   r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
MAX_LOGINS = 20
RUNBOOK = "docs/runbooks/remote-ui.md"

Run = Callable[[list[str]], str]  # tailscale arguments -> its stdout


@dataclass(frozen=True)
class Remote:
    """The one HTTPS origin Tailscale Serve gives this UI, and the loopback port behind it."""

    origin: str  # https://NAME, or https://NAME:8443
    port: int  # 127.0.0.1:port, where Serve sends it
    logins: tuple[str, ...] = ()  # the Tailscale logins that may use it (lower case)

    @property
    def hostname(self) -> str:
        return urlsplit(self.origin).hostname or ""

    @property
    def https_port(self) -> int:
        return urlsplit(self.origin).port or 443

    @property
    def host(self) -> str:
        """The Host header a browser sends for it (no port when it is 443)."""
        return self.origin[len("https://"):]

    @property
    def hostport(self) -> str:
        """How Tailscale Serve's config names it."""
        return f"{self.hostname}:{self.https_port}"

    @property
    def websocket(self) -> str:
        return f"wss://{self.host}"


def parse_origin(text: str) -> str:
    """https://NAME[:PORT], normalised; anything else is refused with why."""
    example = "https://fleetledger.example-tailnet.ts.net"
    raw = (text or "").strip()
    try:
        url = urlsplit(raw)
        port = url.port
    except ValueError as exc:
        raise FleetError(f"Not an address: {raw!r}.", hint=f"For example {example}.") from exc
    if url.scheme != "https":
        raise FleetError("The remote address must be https://: Tailscale Serve gives it "
                         "HTTPS, and a browser keeps the copy buttons and the cookie's "
                         "Secure flag only on HTTPS.", hint=f"For example {example}.")
    if url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        raise FleetError("The remote address is just https:// and a name: no path, query "
                         "or user.", hint=f"For example {example}.")
    host = url.hostname or ""
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    labels = host.split(".")
    if (is_ip or len(labels) < 3 or not all(LABEL.match(label) for label in labels)
            or labels[-1] == "localhost"):
        raise FleetError(f"{host or raw!r} is not a MagicDNS name. Serve's certificate is for "
                         "this computer's full Tailscale name.", hint=f"For example {example} "
                         "(tailscale status shows this computer's name).")
    if port is not None and port not in SERVE_PORTS:
        raise FleetError(f"Tailscale Serve offers HTTPS on ports "
                         f"{', '.join(map(str, SERVE_PORTS))} only, not {port}.")
    return f"https://{host}" + (f":{port}" if port not in (None, 443) else "")


def parse_login(text: str) -> str:
    login = (text or "").strip().lower()
    if not LOGIN.match(login) or len(login) > 254:
        raise FleetError(f"{text!r} is not a Tailscale login.",
                         hint="As Tailscale shows it (admin console, Users): you@example.com, "
                              "or example-user@github.")
    return login


def parse_logins(texts: list[str]) -> tuple[str, ...]:
    logins = tuple(dict.fromkeys(parse_login(t) for t in texts))
    if len(logins) > MAX_LOGINS:
        raise FleetError(f"At most {MAX_LOGINS} logins.")
    return logins


def _path():
    return paths.config_dir() / REMOTE_FILE


def load() -> Remote | None:
    """The saved setting, or None when it is off. A file FleetLedger did not write
    as it is now is refused, so nothing half-read turns the remote UI on."""
    try:
        text = _path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ConfigError(f"Cannot read {paths.tilde(_path())}: {exc.strerror}.") from exc
    broken = ConfigError(f"{paths.tilde(_path())} is not a remote web UI setting FleetLedger "
                         "wrote.", hint="Turn it on again (ship-fleet ui remote https://NAME "
                         "--port N), or off (ship-fleet ui remote --off); ship-fleet ui "
                         "--local starts the UI for this computer only meanwhile.")
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise broken from exc
    # Without "logins": written before the allow-list existed, so nobody may sign in.
    if not isinstance(doc, dict) or set(doc) not in ({"origin", "port"},
                                                     {"origin", "port", "logins"}):
        raise broken
    origin, port, logins = doc["origin"], doc["port"], doc.get("logins", [])
    if not isinstance(origin, str) or not _port_ok(port) or not isinstance(logins, list):
        raise broken
    try:
        if (parse_origin(origin) != origin
                or not all(isinstance(x, str) for x in logins)
                or list(parse_logins(logins)) != logins):
            raise broken
    except FleetError as exc:
        raise broken from exc
    return Remote(origin=origin, port=port, logins=tuple(logins))


def _port_ok(port: Any) -> bool:
    return isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535


def save(remote: Remote) -> None:
    folder = paths.config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    doc = {"origin": remote.origin, "port": remote.port, "logins": list(remote.logins)}
    paths.write_private(_path(), (json.dumps(doc, indent=2) + "\n").encode("utf-8"))


def clear() -> bool:
    try:
        _path().unlink()
    except FileNotFoundError:
        return False
    return True


# --- the Tailscale Serve check -------------------------------------------------------


def run_tailscale(args: list[str]) -> str:
    """The local `tailscale` command's output; it only reads, never changes anything."""
    try:
        proc = subprocess.run(["tailscale", *args], capture_output=True, text=True,
                              timeout=15, check=False)
    except FileNotFoundError as exc:
        raise FleetError("Tailscale is not installed here (no tailscale command), so no "
                         "other device can reach this UI through it.",
                         hint="ship-fleet ui --local starts it for this computer only.") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FleetError(f"`tailscale {' '.join(args)}` did not answer.",
                         hint="ship-fleet ui --local starts the UI for this computer "
                              "only.") from exc
    if proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise FleetError(f"`tailscale {' '.join(args)}` failed"
                         + (f": {lines[-1]}" if lines else "."),
                         hint="ship-fleet ui --local starts the UI for this computer only.")
    return proc.stdout


def _json(run: Run, args: list[str]) -> dict:
    text = run(args)
    try:
        doc = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise FleetError(f"`tailscale {' '.join(args)}` did not print JSON.") from exc
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise FleetError(f"`tailscale {' '.join(args)}` printed something unexpected.")
    return doc


def _dict(config: dict, key: str) -> dict:
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def _host_of(hostport: str) -> str:
    return str(hostport).rsplit(":", 1)[0].rstrip(".").lower()


def serve_command(remote: Remote) -> str:
    https = "" if remote.https_port == 443 else f" --https={remote.https_port}"
    return f"tailscale serve --bg{https} 127.0.0.1:{remote.port}"


def check_serve(remote: Remote, run: Run) -> None:
    """Refuse (FleetError) unless Tailscale Serve, here, sends exactly remote.origin to
    127.0.0.1:remote.port, on the tailnet only."""
    local = "ship-fleet ui --local starts the UI for this computer only."
    status = _json(run, ["status", "--json"])
    state = status.get("BackendState")
    if state != "Running":
        raise FleetError(f"Tailscale is not running here (it says {state or 'nothing'}), so "
                         f"{remote.origin} cannot reach this UI.",
                         hint=f"Start it (tailscale up), or: {local}")
    me = str((status.get("Self") or {}).get("DNSName") or "").rstrip(".").lower()
    if me != remote.hostname:
        raise FleetError(f"{remote.hostname} is not this computer's Tailscale name"
                         + (f" (here it is {me})" if me else "") + ".",
                         hint="Use this computer's own name: ship-fleet ui remote "
                              f"https://{me or 'NAME'} --port {remote.port}.")
    if remote.hostname not in (status.get("CertDomains") or []):
        raise FleetError(f"Tailscale has no HTTPS certificate for {remote.hostname}.",
                         hint="Turn on HTTPS certificates for your tailnet (admin console, "
                              "DNS), then try again.")
    serve = _json(run, ["serve", "status", "--json"])
    configs = [serve, *[c for c in (serve.get("Foreground") or {}).values()
                        if isinstance(c, dict)]]
    found = [c for c in configs if remote.hostport in (c.get("Web") or {})]
    fix = f"Run: {serve_command(remote)}"
    if not found:
        raise FleetError(f"Tailscale Serve is not serving {remote.origin} here.", hint=fix)
    # Funnel on any port of the name, in any config (Tailscale funnels a port if the
    # background config or any foreground one allows it).
    funnels = sorted({key for c in configs for key, on in (_dict(c, "AllowFunnel")).items()
                      if on and _host_of(key) == remote.hostname})
    if funnels:
        raise FleetError(f"Funnel is on for {', '.join(funnels)}: that puts it on the public "
                         "internet. FleetLedger is never served through Funnel.",
                         hint="Clear it (tailscale serve reset), then serve it on your "
                              f"tailnet only: {serve_command(remote)}")
    # Nothing else on any other port of the name either: a browser sends the name's
    # cookies to every port of it, FleetLedger's session cookie included.
    others = sorted({key for c in configs for key in _dict(c, "Web")
                     if _host_of(key) == remote.hostname and key != remote.hostport}
                    | {f"{remote.hostname}:{port}" for c in configs
                       for port, tcp in _dict(c, "TCP").items()
                       if str(port) != str(remote.https_port) and isinstance(tcp, dict)
                       and str(tcp.get("TerminateTLS") or "").lower() == remote.hostname})
    if others:
        # A key Tailscale wrote is NAME:PORT; one without a port (hand-edited, or a
        # format to come) is still refused, never a crash.
        ports = ", ".join(k.rsplit(":", 1)[1] if ":" in k else "(none given)" for k in others)
        raise FleetError(f"Tailscale Serve also serves {remote.hostname} on port {ports}. "
                         "FleetLedger must have the name to itself: your browser sends its "
                         "session cookie to every port of the name.",
                         hint="Stop that one (tailscale serve reset clears everything), then: "
                              f"{serve_command(remote)}")
    for config in found:
        handlers = (config["Web"][remote.hostport] or {}).get("Handlers") or {}
        if set(handlers) != {"/"}:
            others = sorted(p for p in handlers if p != "/")
            raise FleetError(f"Tailscale Serve sends {remote.origin}"
                             + (f" paths {', '.join(others)}" if others else "")
                             + " somewhere besides this UI. FleetLedger must have the name "
                               "to itself: anything else served there is the same site to "
                               "your browser.",
                             hint="Clear it (tailscale serve reset), then: "
                                  f"{serve_command(remote)}")
        proxy = str((handlers["/"] or {}).get("Proxy") or "").rstrip("/")
        if proxy not in (f"http://127.0.0.1:{remote.port}", f"http://localhost:{remote.port}"):
            raise FleetError(f"Tailscale Serve sends {remote.origin} to "
                             f"{proxy or 'something other than a local port'}, not to this "
                             f"UI on 127.0.0.1:{remote.port}.", hint=fix)


def runner(app: Any) -> Run:
    return getattr(app, "tailscale", None) or run_tailscale


def for_ui(app: Any, *, port: int, local: bool) -> Remote | None:
    """The remote origin `ship-fleet ui` should also accept, after the Serve check;
    None when it is off (or --local). Refuses rather than start half-way."""
    if local:
        return None
    remote = load()
    if remote is None:
        return None
    if not remote.logins:
        raise FleetError(f"Nobody may use {remote.origin} yet: say whose Tailscale login may.",
                         hint="ship-fleet ui remote --login you@example.com (or ship-fleet ui "
                              "--local for this computer only).")
    if port and port != remote.port:
        raise FleetError(f"The remote web UI is set up on port {remote.port} (Tailscale Serve "
                         f"sends {remote.origin} there), not {port}.",
                         hint=f"Leave out --port, or run ship-fleet ui --local --port {port} "
                              "for this computer only.")
    check_serve(remote, runner(app))
    return remote


# --- ship-fleet ui remote --------------------------------------------------------------


def cmd_remote(args, app) -> int:
    from fleetledger.concierge import audit

    if args.off:
        if args.origin:
            raise FleetError("Give an address to turn it on, or --off, not both.")
        before = load_quietly()
        if clear():
            audit.record("-", "ui-remote", "web", "off")
            app.say("Remote web UI is off: the next ship-fleet ui is for this computer only, "
                    "and one running now refuses every request on the remote address.")
            if before is not None:
                app.say(f"Tailscale Serve still forwards to the port; stop that with: "
                        f"tailscale serve --https={before.https_port} off (or tailscale serve "
                        "reset, which clears every Serve setting)")
        else:
            app.say("Remote web UI was already off.")
        return 0
    logins = parse_logins(args.login) if args.login else ()
    if not args.origin:
        if not logins:
            return _show(app)
        current = load()
        if current is None:
            raise FleetError("The remote web UI is off; turn it on with its address: "
                             "ship-fleet ui remote https://NAME --port N --login LOGIN.")
        save(Remote(current.origin, current.port, logins))
        audit.record("-", "ui-remote", "web", "logins")
        app.say(f"Only {', '.join(logins)} may now use {current.origin}, in a running "
                "ship-fleet ui too. Devices paired by another login no longer get in.")
        return 0
    origin = parse_origin(args.origin)
    if args.port is None:
        raise FleetError("Give the loopback port Tailscale Serve sends it to: --port N "
                         "(for example --port 8765).")
    if not _port_ok(args.port):
        raise FleetError("--port must be between 1024 and 65535.")
    if not logins:
        raise FleetError("Say whose Tailscale login may use it: --login you@example.com "
                         "(repeat it for more than one).")
    remote = Remote(origin=origin, port=args.port, logins=logins)
    check_serve(remote, runner(app))
    save(remote)
    audit.record("-", "ui-remote", "web", "on")
    app.say(f"Remote web UI is on: {remote.origin}, through Tailscale Serve to "
            f"127.0.0.1:{remote.port}, for {', '.join(logins)}. Run ship-fleet ui (it now "
            f"uses port {remote.port}); each start checks Serve again. Pair each device "
            "once: ship-fleet ui link --name laptop.")
    app.say(f"Before using it, read the risks in {RUNBOOK}: anyone with a session there "
            "controls the whole fleet.")
    return 0


class Allowed:
    """The logins that may use a running UI's remote address, as remote-ui.json says
    now: re-read whenever the file changes, so taking a login off, or turning the
    remote UI off, takes effect at the next request. A missing or broken file, or one
    naming another address or port than this run serves, allows nobody."""

    def __init__(self, remote: Remote) -> None:
        self.remote = remote
        self._stamp: tuple[int, int, int] | None = None
        self._logins: tuple[str, ...] = ()

    def __call__(self) -> tuple[str, ...]:
        try:
            st = _path().stat()
        except OSError:
            return ()
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size)
        if stamp != self._stamp:
            now = load_quietly()
            same = now is not None and (now.origin, now.port) == (self.remote.origin,
                                                                   self.remote.port)
            self._logins = now.logins if same else ()
            self._stamp = stamp
        return self._logins


def load_quietly() -> Remote | None:
    try:
        return load()
    except FleetError:
        return None


def _show(app) -> int:
    remote = load()
    if remote is None:
        app.say("Remote web UI: off (this computer only). To use it from your other devices "
                "through Tailscale Serve: ship-fleet ui remote https://NAME --port N "
                f"(see {RUNBOOK}).")
        return 0
    app.say(f"Remote web UI: on: {remote.origin}, through Tailscale Serve to "
            f"127.0.0.1:{remote.port}.")
    app.say("May use it: " + (", ".join(remote.logins) if remote.logins else
                              "nobody yet (ship-fleet ui remote --login you@example.com)"))
    try:
        check_serve(remote, runner(app))
    except FleetError as exc:
        app.say(f"Serve check: NOT OK: {exc.message}")
        if exc.hint:
            app.say(f"  {exc.hint}")
        return 1
    app.say("Serve check: ok (this computer's name, served to this port only, Funnel off).")
    return 0
