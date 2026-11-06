"""The web UI from your other devices, through Tailscale Serve (web/remote.py and the gate).

Tailscale is simulated: FakeTailscale answers `tailscale status --json` and
`tailscale serve status --json`, and requests on the remote name carry the headers
Serve adds (the browser's Host kept, X-Forwarded-Proto, Tailscale-User-Login).
Nothing here reaches a real tailnet.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pytest
from fleetledger.concierge import audit
from fleetledger.errors import ConfigError, FleetError
from fleetledger.web import app as webapp
from fleetledger.web import remote, security, server
from fleetledger.web.devices import Devices
from fleetledger.web.fleet import Fleet
from fleetledger.web.remote import Remote
from fleetledger.web.security import Gate
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_cli import FakeShip, ShipSSH
from test_web import BASE, ORIGIN, PORT

from fleetledger import cli

NAME = "fleetledger.example-tailnet.ts.net"
REMOTE = f"https://{NAME}"
LOGIN = "you@example.com"
# What Tailscale Serve adds to a request from one of your (untagged) devices.
SERVE = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": NAME,
         "X-Forwarded-For": "192.0.2.7", "Tailscale-User-Login": LOGIN,
         "Tailscale-User-Name": "Example User"}
REMOTE_ORIGIN = {"Origin": REMOTE}
ON = Remote(REMOTE, PORT, (LOGIN,))  # the setting, with one login allowed


class FakeTailscale:
    """The local tailscale command's read-only answers: this computer is NAME, and
    Serve sends https://NAME to 127.0.0.1:PORT."""

    def __init__(self, name=NAME, port=PORT, https=443):
        self.status = {"BackendState": "Running", "Self": {"DNSName": name + "."},
                       "CertDomains": [name]}
        self.serve = {"TCP": {str(https): {"HTTPS": True}},
                      "Web": {f"{name}:{https}": {
                          "Handlers": {"/": {"Proxy": f"http://127.0.0.1:{port}"}}}}}
        self.calls = []

    @property
    def handlers(self):
        return self.serve["Web"][f"{NAME}:443"]["Handlers"]

    def __call__(self, args):
        self.calls.append(args)
        if args == ["status", "--json"]:
            return json.dumps(self.status)
        if args == ["serve", "status", "--json"]:
            return json.dumps(self.serve)
        raise AssertionError(f"unexpected tailscale call {args}")


@pytest.fixture
def web(store, fake_repo):
    app = cli.App(out=io.StringIO(), err=io.StringIO(), store_factory=lambda: store,
                  repo_factory=lambda: fake_repo,
                  ssh_factory=lambda inv: ShipSSH(inv, FakeShip()),
                  stdin_isatty=lambda: False, sleep=lambda s: None)
    gate = Gate(PORT, remote=ON, devices=Devices())
    fleet = Fleet(app, interval=30)
    asgi = webapp.create_app(app, gate, fleet=fleet)
    fleet.poll()

    class Env:
        pass

    env = Env()
    env.gate, env.asgi = gate, asgi
    env.local = TestClient(asgi, base_url=BASE, follow_redirects=False)
    env.remote = TestClient(asgi, base_url=REMOTE, headers=SERVE, follow_redirects=False)

    def sign_in(client):
        response = client.get(f"/login?token={gate.new_token()}")
        assert response.status_code == 200, response.text
        page = client.get("/")
        assert page.status_code == 200, page.text
        return re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    env.sign_in = sign_in
    return env


# --- off by default --------------------------------------------------------------------


def test_off_by_default_the_remote_name_is_refused(store, fake_repo):
    """Today's behaviour, unchanged: without the setting, only 127.0.0.1 and localhost."""
    gate = Gate(PORT)
    asgi = webapp.create_app(cli.App(out=io.StringIO(), store_factory=lambda: store,
                                     repo_factory=lambda: fake_repo), gate)
    client = TestClient(asgi, base_url=REMOTE, headers=SERVE, follow_redirects=False)
    assert client.get(f"/login?token={gate.new_token()}").status_code == 400
    assert client.get("/static/app.css").status_code == 400
    csp = TestClient(asgi, base_url=BASE).get("/static/app.css").headers[
        "content-security-policy"]
    assert "wss:" not in csp


def test_the_ui_reads_no_remote_setting_when_none_is_saved(home):
    assert remote.load() is None
    assert remote.for_ui(cli.App(tailscale=lambda args: pytest.fail("no check")),
                         port=0, local=False) is None


# --- the gate on the remote origin -------------------------------------------------------


def test_a_device_through_serve_signs_in_with_a_secure_host_only_cookie(web):
    response = web.remote.get(f"/login?token={web.gate.new_token()}")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-fleetledger=")
    for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/"):
        assert flag in cookie
    assert "domain" not in cookie.lower()  # __Host-: this name only, never a parent
    assert 'http-equiv="refresh" content="0;url=/"' in response.text
    assert web.remote.get("/").status_code == 200
    # On this computer the cookie is as it always was (no Secure on plain http).
    local = web.local.get(f"/login?token={web.gate.new_token()}").headers["set-cookie"]
    assert local.startswith(f"fleetledger-{PORT}=") and "Secure" not in local


@pytest.mark.parametrize("drop, change", [
    ("Tailscale-User-Login", None),          # a tagged device, or Funnel: no identity
    (None, {"Tailscale-User-Login": " "}),
    ("X-Forwarded-Proto", None),             # not Serve's HTTPS
    (None, {"X-Forwarded-Proto": "http"}),
])
def test_the_remote_name_works_only_through_serve_from_a_persons_device(web, drop, change):
    headers = {k: v for k, v in SERVE.items() if k != drop}
    headers.update(change or {})
    client = TestClient(web.asgi, base_url=REMOTE, headers=headers, follow_redirects=False)
    for path in ("/", "/static/app.css", f"/login?token={web.gate.new_token()}"):
        response = client.get(path)
        assert response.status_code == 403, path
        assert "Tailscale Serve" in response.text
        assert response.headers["content-security-policy"]  # still the strict headers


def test_a_repeated_identity_header_is_not_trusted(web):
    headers = [(k, v) for k, v in SERVE.items()] + [("Tailscale-User-Login", "x@example.com")]
    response = web.remote.get("/static/app.css", headers=headers)
    assert response.status_code == 403


@pytest.mark.parametrize("host", [f"127.0.0.1:{PORT}", f"localhost:{PORT}",
                                  f"LOCALHOST:{PORT}"])
def test_a_request_through_serve_cannot_pass_as_one_from_this_computer(web, host):
    """Serve keeps the browser's Host, so a client there can name 127.0.0.1; its proxy
    headers still give it away (a Funnel or tagged-device request has no identity)."""
    without_login = {k: v for k, v in SERVE.items() if k != "Tailscale-User-Login"}
    for headers in (SERVE, without_login):
        client = TestClient(web.asgi, base_url=REMOTE, headers={**headers, "Host": host},
                            follow_redirects=False)
        for path in ("/", "/static/app.css", f"/login?token={web.gate.new_token()}"):
            response = client.get(path)
            assert response.status_code == 400, path
            assert "proxy" in response.text and "set-cookie" not in response.headers
        csrf = web.sign_in(web.local)
        cookie = f"fleetledger-{PORT}={web.local.cookies[f'fleetledger-{PORT}']}"
        assert client.post("/ships/ship-01/refresh", data={"csrf": csrf},
                           headers={**ORIGIN, "Cookie": cookie}).status_code == 400
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"ws://{host}/flows/1-00000000/ws",
                                          headers={**ORIGIN, "Cookie": cookie}) as ws:
                ws.send_text(json.dumps({"csrf": csrf}))
                ws.receive_text()


@pytest.mark.parametrize("header", ["X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto",
                                    "Forwarded", "Tailscale-User-Login", "Tailscale-User-Name",
                                    "Tailscale-App-Capabilities"])
def test_this_computers_address_takes_no_proxied_request_even_when_off(store, fake_repo,
                                                                       header):
    gate = Gate(PORT)
    asgi = webapp.create_app(cli.App(out=io.StringIO(), store_factory=lambda: store,
                                     repo_factory=lambda: fake_repo), gate)
    client = TestClient(asgi, base_url=BASE, follow_redirects=False)
    assert client.get("/static/app.css").status_code == 200
    response = client.get("/static/app.css", headers={header: "x"})
    assert response.status_code == 400 and response.headers["content-security-policy"]
    assert client.get(f"/login?token={gate.new_token()}",
                      headers={header: "x"}).status_code == 400


def test_the_header_says_where_the_browser_is(web, store, fake_repo):
    web.sign_in(web.remote)  # a device named after its browser
    assert re.search(r">browser, remote</a>", web.remote.get("/").text)
    web.sign_in(web.local)
    assert "this computer, and your devices" in web.local.get("/").text
    gate = Gate(PORT)
    asgi = webapp.create_app(cli.App(out=io.StringIO(), store_factory=lambda: store,
                                     repo_factory=lambda: fake_repo), gate)
    client = TestClient(asgi, base_url=BASE, follow_redirects=False)
    client.get(f"/login?token={gate.new_token()}")
    assert "this computer only" in client.get("/").text


@pytest.mark.parametrize("host", [f"{NAME}.evil.example", f"evil-{NAME}", f"x{NAME}",
                                  f"{NAME}:8443", f"{NAME}.", "example-tailnet.ts.net",
                                  f"ship-01.{NAME}"])
def test_only_the_exact_remote_name_is_accepted(web, host):
    response = web.remote.get("/static/app.css", headers={"Host": host})
    assert response.status_code == 400
    assert web.remote.get("/static/app.css",
                          headers={"Host": f"{NAME}:443"}).status_code == 200


def test_a_session_never_crosses_origins(web):
    web.sign_in(web.local)
    local_id = web.local.cookies[f"fleetledger-{PORT}"]
    web.sign_in(web.remote)
    remote_id = web.remote.cookies["__Host-fleetledger"]
    bare_remote = TestClient(web.asgi, base_url=REMOTE, headers=SERVE)
    bare_local = TestClient(web.asgi, base_url=BASE)
    # A session made here does not work on the remote name, nor the reverse, under
    # either cookie name.
    for client, cookie in ((bare_remote, f"__Host-fleetledger={local_id}"),
                           (bare_remote, f"fleetledger-{PORT}={local_id}"),
                           (bare_local, f"fleetledger-{PORT}={remote_id}"),
                           (bare_local, f"__Host-fleetledger={remote_id}")):
        assert client.get("/", headers={"Cookie": cookie}).status_code == 401, cookie
    assert bare_remote.get("/", headers={"Cookie": f"__Host-fleetledger={remote_id}"}
                           ).status_code == 200


def test_changes_on_the_remote_origin_need_its_exact_origin_and_csrf(web):
    csrf = web.sign_in(web.remote)
    path = "/ships/ship-01/refresh"
    for origin in ("http://" + NAME, f"http://127.0.0.1:{PORT}", f"{REMOTE}:8443",
                   "https://evil.example", f"{REMOTE}.evil.example", "null"):
        response = web.remote.post(path, data={"csrf": csrf}, headers={"Origin": origin})
        assert response.status_code == 403, origin
    assert web.remote.post(path, data={"csrf": csrf}).status_code == 403  # no Origin
    assert web.remote.post(path, data={"csrf": "wrong"},
                           headers=REMOTE_ORIGIN).status_code == 403
    assert web.remote.post(path, data={"csrf": csrf},
                           headers=REMOTE_ORIGIN).status_code == 303
    # And the remote origin is no good on this computer's address.
    local_csrf = web.sign_in(web.local)
    assert web.local.post(path, data={"csrf": local_csrf},
                          headers=REMOTE_ORIGIN).status_code == 403
    assert web.local.post(path, data={"csrf": local_csrf}, headers=ORIGIN).status_code == 303


def test_an_inbox_upload_passes_the_gate_only_with_the_remote_origin(web):
    csrf = web.sign_in(web.remote)
    upload = "/ships/ship-01/inbox/upload"
    head = {"Content-Type": "application/octet-stream", "X-CSRF-Token": csrf,
            "X-File-Name": "notes.txt"}
    wrong = web.remote.post(upload, content=b"abc", headers={**head, **ORIGIN})
    assert wrong.status_code == 403 and "Cross-origin" in wrong.text
    right = web.remote.post(upload, content=b"abc", headers={**head, **REMOTE_ORIGIN})
    assert right.status_code != 403 and "Cross-origin" not in right.text


def test_the_csp_allows_the_live_terminal_on_wss_for_the_remote_name_only(web):
    csp = web.remote.get("/static/app.css").headers["content-security-policy"]
    connect = next(p for p in csp.split("; ") if p.startswith("connect-src"))
    assert connect.split() == ["connect-src", "'self'", f"ws://127.0.0.1:{PORT}",
                               f"ws://localhost:{PORT}", f"wss://{NAME}"]
    assert "unsafe" not in csp and "frame-ancestors 'none'" in csp


def test_the_live_terminal_socket_on_the_remote_origin(web):
    csrf = web.sign_in(web.remote)
    cookie = {"Cookie": f"__Host-fleetledger={web.remote.cookies['__Host-fleetledger']}"}
    url = f"wss://{NAME}/flows/1-00000000/ws"
    hello = json.dumps({"csrf": csrf})

    def refused(client, headers):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url, headers=headers) as ws:
                ws.send_text(hello)
                ws.receive_text()

    refused(web.remote, {**cookie, "Origin": "http://" + NAME})
    refused(web.remote, {**cookie, **ORIGIN})
    refused(web.remote, cookie)  # no Origin
    without_login = {k: v for k, v in SERVE.items() if k != "Tailscale-User-Login"}
    refused(TestClient(web.asgi, base_url=REMOTE, headers=without_login),
            {**cookie, **REMOTE_ORIGIN})
    refused(TestClient(web.asgi, base_url=REMOTE, headers=SERVE), REMOTE_ORIGIN)  # no session
    with web.remote.websocket_connect(url, headers={**cookie, **REMOTE_ORIGIN}) as ws:
        ws.send_text(hello)
        assert json.loads(ws.receive_text()) == {"ended": True}  # no such terminal


# --- the address ---------------------------------------------------------------------


@pytest.mark.parametrize("text, normal", [
    (REMOTE, REMOTE),
    (REMOTE + "/", REMOTE),
    ("https://FleetLedger.Example-Tailnet.ts.net", REMOTE),
    (f"{REMOTE}:443", REMOTE),
    (f"{REMOTE}:8443", f"{REMOTE}:8443"),
])
def test_an_address_is_normalised(text, normal):
    assert remote.parse_origin(text) == normal


@pytest.mark.parametrize("text", [
    f"http://{NAME}", NAME, f"https://{NAME}/fleet", f"https://{NAME}/?x=1",
    f"https://you@{NAME}", f"https://{NAME}:8765", "https://192.0.2.7",
    "https://localhost", "https://fleet.localhost", "https://ts.net", "https://*.ts.net",
    f"https://{NAME}:notaport", "", f"wss://{NAME}",
])
def test_anything_but_https_and_a_full_name_is_refused(text):
    with pytest.raises(FleetError):
        remote.parse_origin(text)


@pytest.mark.parametrize("doc", [
    "not json", "[]", json.dumps({"origin": REMOTE}),
    json.dumps({"origin": REMOTE, "port": "8765"}),
    json.dumps({"origin": REMOTE, "port": True}),
    json.dumps({"origin": REMOTE, "port": 80}),
    json.dumps({"origin": REMOTE + "/", "port": PORT}),  # not as FleetLedger writes it
    json.dumps({"origin": f"http://{NAME}", "port": PORT}),
    json.dumps({"origin": REMOTE, "port": PORT, "logins": "*"}),
])
def test_a_setting_fleetledger_did_not_write_is_refused_not_guessed(home, doc):
    (home / "config" / remote.REMOTE_FILE).write_text(doc)
    with pytest.raises(ConfigError):
        remote.load()
    with pytest.raises(ConfigError):  # so ship-fleet ui refuses to start
        remote.for_ui(cli.App(tailscale=FakeTailscale()), port=0, local=False)
    # --local still starts it for this computer only.
    assert remote.for_ui(cli.App(), port=0, local=True) is None


# --- the Serve check ---------------------------------------------------------------------


def check(fake, origin=REMOTE, port=PORT):
    remote.check_serve(Remote(origin, port), fake)


def test_the_serve_check_passes_for_exactly_this_setup():
    fake = FakeTailscale()
    check(fake)
    assert fake.calls == [["status", "--json"], ["serve", "status", "--json"]]
    fake.handlers["/"]["Proxy"] = f"http://localhost:{PORT}/"
    check(fake)
    on_8443 = FakeTailscale(https=8443)
    check(on_8443, f"{REMOTE}:8443")
    # A foreground `tailscale serve` (no --bg) counts as well.
    fg = FakeTailscale()
    fg.serve = {"Foreground": {"abc": fg.serve}}
    check(fg)


@pytest.mark.parametrize("breaks, says", [
    (lambda f: f.status.update(BackendState="Stopped"), "not running"),
    (lambda f: f.status.update(Self={"DNSName": "desktop.example-tailnet.ts.net."}),
     "not this computer's Tailscale name"),
    (lambda f: f.status.pop("Self"), "not this computer's Tailscale name"),
    (lambda f: f.status.update(CertDomains=[]), "no HTTPS certificate"),
    (lambda f: f.status.pop("CertDomains"), "no HTTPS certificate"),
    (lambda f: setattr(f, "serve", {}), "not serving"),
    (lambda f: setattr(f, "serve", None), "not serving"),
    (lambda f: f.serve.update(AllowFunnel={f"{NAME}:443": True}), "Funnel"),
    (lambda f: f.serve.update(AllowFunnel={f"{NAME}:8443": True}), "Funnel"),
    (lambda f: f.serve["Web"].update({f"{NAME}:8443": {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}), "on port 8443"),
    (lambda f: f.serve["TCP"].update({"10000": {"TCPForward": "127.0.0.1:3000",
                                                "TerminateTLS": NAME}}), "on port 10000"),
    (lambda f: f.handlers.update({"/other": {"Proxy": "http://127.0.0.1:9000"}}),
     "to itself"),
    (lambda f: f.handlers.update({"/": {"Proxy": "http://127.0.0.1:9999"}}),
     "not to this UI"),
    (lambda f: f.handlers.update({"/": {"Proxy": f"https+insecure://127.0.0.1:{PORT}"}}),
     "not to this UI"),
    (lambda f: f.handlers.update({"/": {"Proxy": f"http://192.0.2.7:{PORT}"}}),
     "not to this UI"),
    (lambda f: f.handlers.update({"/": {"Text": "hello"}}), "not to this UI"),
    (lambda f: f.handlers.update({"/": {"Path": "/srv/www"}}), "not to this UI"),
])
def test_the_serve_check_refuses_anything_else(breaks, says):
    fake = FakeTailscale()
    breaks(fake)
    with pytest.raises(FleetError) as exc:
        check(fake)
    assert says in exc.value.message
    assert exc.value.hint  # always says what to do


def test_the_serve_check_looks_at_every_config_for_funnel_and_other_ports():
    # Funnel allowed in the background config, the handler in a foreground one.
    fg = FakeTailscale()
    fg.serve = {"Foreground": {"abc": fg.serve}, "AllowFunnel": {f"{NAME}:443": True}}
    with pytest.raises(FleetError, match="Funnel"):
        check(fg)
    # Another port of the name served in the foreground.
    fg = FakeTailscale()
    fg.serve["Foreground"] = {"abc": {"Web": {f"{NAME}:8443": {
        "Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}}
    with pytest.raises(FleetError, match="on port 8443") as exc:
        check(fg)
    assert "tailscale serve reset" in exc.value.hint
    # A plain TCP forward on another port (no TLS ended for the name) is not a web site
    # at it, and is left alone.
    tcp = FakeTailscale()
    tcp.serve["TCP"]["2222"] = {"TCPForward": "127.0.0.1:22"}
    check(tcp)


def test_the_serve_check_refuses_another_https_port_or_funnel_in_the_foreground():
    with pytest.raises(FleetError, match="not serving"):
        check(FakeTailscale(), f"{REMOTE}:8443")
    fg = FakeTailscale()
    fg.serve = {"Foreground": {"abc": {**fg.serve, "AllowFunnel": {f"{NAME}:443": True}}}}
    with pytest.raises(FleetError, match="Funnel"):
        check(fg)


def test_the_serve_check_refuses_when_tailscale_cannot_answer(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError("tailscale")

    monkeypatch.setattr(remote.subprocess, "run", missing)
    with pytest.raises(FleetError, match="not installed"):
        check(remote.run_tailscale)

    class Failed:
        returncode, stdout, stderr = 1, "", "access denied\n"

    monkeypatch.setattr(remote.subprocess, "run", lambda *a, **kw: Failed())
    with pytest.raises(FleetError, match="failed: access denied"):
        check(remote.run_tailscale)
    with pytest.raises(FleetError, match="did not print JSON"):
        check(lambda args: "garbage")


# --- ship-fleet ui remote / ship-fleet ui -------------------------------------------------


def run_cli(argv, fake=None):
    out = io.StringIO()
    app = cli.App(out=out, err=out, tailscale=fake or FakeTailscale())
    return cli.main(argv, app), out.getvalue()


def test_turning_it_on_checks_serve_then_saves_a_private_setting(home):
    code, said = run_cli(["ui", "remote"])
    assert code == 0 and "off" in said
    code, said = run_cli(["ui", "remote", REMOTE, "--port", str(PORT), "--login", LOGIN])
    assert code == 0, said
    assert "on: " + REMOTE in said and "risks" in said
    path = home / "config" / remote.REMOTE_FILE
    assert path.stat().st_mode & 0o777 == 0o600
    assert remote.load() == ON
    assert [e["action"] for e in audit.read()] == ["ui-remote"]
    code, said = run_cli(["ui", "remote"])
    assert code == 0 and "Serve check: ok" in said
    broken = FakeTailscale()
    broken.serve.update(AllowFunnel={f"{NAME}:443": True})
    code, said = run_cli(["ui", "remote"], broken)
    assert code == 1 and "NOT OK" in said and "Funnel" in said
    code, said = run_cli(["ui", "remote", "--off"])
    assert code == 0 and "off" in said and "tailscale serve --https=443 off" in said
    assert not path.exists() and remote.load() is None
    assert [e["outcome"] for e in audit.read()] == ["on", "off"]


def test_it_is_not_turned_on_when_the_check_fails(home):
    fake = FakeTailscale()
    fake.handlers["/"]["Proxy"] = "http://127.0.0.1:9999"
    code, said = run_cli(["ui", "remote", REMOTE, "--port", str(PORT), "--login", LOGIN], fake)
    assert code != 0 and "not to this UI" in said
    assert f"tailscale serve --bg 127.0.0.1:{PORT}" in said  # the fix
    assert remote.load() is None
    code, said = run_cli(["ui", "remote", f"http://{NAME}", "--port", str(PORT), "--login",
                          LOGIN])
    assert code != 0 and "https://" in said and remote.load() is None
    code, said = run_cli(["ui", "remote", REMOTE, "--login", LOGIN])
    assert code != 0 and "--port" in said and remote.load() is None
    code, said = run_cli(["ui", "remote", REMOTE, "--port", "80", "--login", LOGIN])
    assert code != 0 and remote.load() is None


def test_ship_fleet_ui_serves_the_remote_origin_only_after_the_check(home, monkeypatch):
    remote.save(ON)
    called = []
    monkeypatch.setattr(server, "serve", lambda app, **kw: called.append(kw) or 0)
    code, said = run_cli(["ui", "--no-browser"])
    assert code == 0, said
    assert called[-1]["remote"] == ON
    code, _ = run_cli(["ui", "--no-browser", "--local"])
    assert code == 0 and "remote" not in called[-1]
    # A failed check refuses to start (it never starts for this computer only by
    # surprise either): it says why and how.
    fake = FakeTailscale()
    fake.status["BackendState"] = "Stopped"
    code, said = run_cli(["ui", "--no-browser"], fake)
    assert code != 0 and "not running" in said and "--local" in said
    assert len(called) == 2
    code, said = run_cli(["ui", "--no-browser", "--port", "9999"])
    assert code != 0 and f"port {PORT}" in said and len(called) == 2


def test_setup_never_serves_the_remote_origin(home, monkeypatch):
    remote.save(ON)
    called = []
    monkeypatch.setattr(server, "serve", lambda app, **kw: called.append(kw) or 0)
    assert run_cli(["setup", "--no-browser"])[0] == 0
    assert "remote" not in called[-1]


def test_the_server_prints_a_link_for_each_address(store, monkeypatch, home):
    import uvicorn

    free = server.bind(0)
    port = free.getsockname()[1]
    free.close()
    seen = {}
    monkeypatch.setattr(uvicorn.Server, "run",
                        lambda self, sockets=None: seen.update(port=sockets[0].getsockname()))
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store)
    monkeypatch.setattr(server.sys.stdin, "isatty", lambda: False, raising=False)
    assert server.serve(app, open_browser=False, interval=3600,
                        remote=Remote(REMOTE, port, (LOGIN,))) == 0
    assert seen["port"] == ("127.0.0.1", port)  # still loopback only, on Serve's port
    said = out.getvalue()
    local = re.search(rf"http://127\.0\.0\.1:{port}/login\?token=(\S+)", said).group(1)
    far = re.search(rf"{re.escape(REMOTE)}/login\?token=(\S+)", said).group(1)
    # Two tokens: the browser opened here uses up its own, never the remote one.
    assert local != far


def test_the_browser_opened_here_leaves_the_remote_link_working(store, monkeypatch, home):
    import uvicorn

    free = server.bind(0)
    port = free.getsockname()[1]
    free.close()
    seen = {}

    def run(self, sockets=None):
        gate = self.config.app.gate
        opened = seen["opened"].read_text()
        token = re.search(r"token=([A-Za-z0-9_-]+)", opened).group(1)
        seen["browser"] = gate.redeem(token)  # the browser here, at once
        seen["gate"] = gate

    monkeypatch.setattr(uvicorn.Server, "run", run)
    real_open = server.open_file
    monkeypatch.setattr(server, "open_file",
                        lambda link, p: seen.setdefault("opened", real_open(link, p)))
    monkeypatch.setattr(server.webbrowser, "open", lambda uri: True)
    monkeypatch.setattr(server.sys.stdin, "isatty", lambda: False, raising=False)
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store)
    assert server.serve(app, open_browser=True, interval=3600,
                        remote=Remote(REMOTE, port, (LOGIN,))) == 0
    assert seen["browser"] is not None
    assert isinstance(seen["gate"].allowed, remote.Allowed)  # follows the file while it runs
    far = re.search(rf"{re.escape(REMOTE)}/login\?token=(\S+)", out.getvalue()).group(1)
    assert seen["gate"].redeem(far, REMOTE, login=LOGIN) is not None


def test_the_live_terminal_script_picks_wss_on_https():
    js = (Path(webapp.__file__).parent / "static" / "app.js").read_text()
    assert 'location.protocol === "https:" ? "wss://" : "ws://"' in js
    assert '"ws://" + location.host' not in js


def test_the_gate_module_still_documents_every_check():
    doc = security.__doc__
    assert "Tailscale Serve" in doc and "__Host-" in doc
