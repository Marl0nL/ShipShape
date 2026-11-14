"""The web UI (ship-fleet ui): its gate, pages, actions and the concierge in a browser.

Every request goes through the real security gate. Ships are the fake Ship from
the CLI tests behind a real SSH object, and sign-ins play recorded CLI sessions
in a real terminal, so the engine is the one `ship-fleet login` uses.
"""

import copy
import io
import json
import logging
import re
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fl_harness import Clock, no_ship_answers, wait_for
from fleetledger.concierge import audit, engine
from fleetledger.errors import FleetError
from fleetledger.web import actions, flows, network, security, server
from fleetledger.web import app as webapp
from fleetledger.web.fleet import Fleet, attention_items
from fleetledger.web.flows import Concierge, WebFlow, WebPrompter
from fleetledger.web.security import Gate
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from test_cli import CLAUDE_CODE, EXAMPLES, RUNNING, VERSION, FakeShip, ShipSSH, result

from fleetledger import cli, ops, status

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_cli.py"
FIX = HERE / "fixtures" / "concierge"
WEB = HERE.parent / "fleetledger" / "web"
PORT = 8765
BASE = f"http://127.0.0.1:{PORT}"
ORIGIN = {"Origin": BASE}


NET_SUMMARY = json.loads((EXAMPLES / "net-summary" / "running.json").read_text())


class NetShip(FakeShip):
    """The CLI tests' fake Ship, plus phase 3's `ship-ctl net`."""

    def __init__(self):
        super().__init__()
        self.net = {}  # ship name -> net-summary document
        self.net_contract = True

    def __call__(self, ship, command):
        if command[:4] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "version"]:
            self.calls.append((ship.name, command))
            doc = copy.deepcopy(VERSION)
            if self.net_contract:
                doc["data"]["contracts"]["net-summary"] = 1
            return 0, json.dumps(doc), ""
        if command[:4] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "net"]:
            self.calls.append((ship.name, command))
            return 0, json.dumps(result("net", data=self.net[ship.name])), ""
        return super().__call__(ship, command)


def fresh_summary():
    """The contract's example summary, as if generated just now."""
    doc = copy.deepcopy(NET_SUMMARY)
    now = datetime.now(UTC)
    stamp = lambda delta: (now - delta).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    doc["generated_at"] = stamp(timedelta(0))
    doc["collector"]["updated_at"] = stamp(timedelta(seconds=30))
    for i, d in enumerate(doc["destinations"]):
        d["last_seen"] = stamp(timedelta(minutes=5))
        d["first_seen"] = stamp(timedelta(hours=2) if i == 1 else timedelta(days=3))
    return doc


@pytest.fixture
def web(store, fake_repo, monkeypatch, tmp_path):
    ship_side = NetShip()
    monkeypatch.setenv("FAKE_CLI_INPUT_LOG", str(tmp_path / "typed.log"))
    fixture = {"path": FIX / "claude.txt"}
    argvs = []

    def spawner(argv):
        argvs.append(argv)
        return engine.spawn([sys.executable, str(FAKE), str(fixture["path"])])

    app = cli.App(
        out=io.StringIO(), err=io.StringIO(),
        store_factory=lambda: store,
        repo_factory=lambda: fake_repo,
        ssh_factory=lambda inv: ShipSSH(inv, ship_side),
        spawner=spawner,
        stdin_isatty=lambda: False,
        sleep=lambda s: None,
    )
    app.github_visibility = lambda repo: "unknown"  # never ask GitHub (--repo)
    clock = Clock(1000.0)
    gate = Gate(PORT, clock=clock)
    fleet = Fleet(app, interval=30)
    concierge = Concierge(app, on_finished=lambda ship: fleet.poll([ship]),
                          flow_options={"pattern_timeout": 1})
    asgi = webapp.create_app(app, gate, fleet=fleet, concierge_=concierge)
    fleet.poll()

    class Env:
        pass

    env = Env()
    env.app, env.gate, env.fleet, env.concierge, env.ship = app, gate, fleet, concierge, ship_side
    env.clock, env.fixture, env.argvs, env.store = clock, fixture, argvs, store
    env.typed = tmp_path / "typed.log"
    env.client = TestClient(asgi, base_url=BASE, follow_redirects=False)
    env.app_ui = asgi.app.state.ui

    def sign_in(client=None):
        client = client or env.client
        response = client.get(f"/login?token={gate.new_token()}")
        assert response.status_code == 200
        page = client.get("/")
        env.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        return client

    def post(path, data=None, **kw):
        headers = {**ORIGIN, **kw.pop("headers", {})}
        body = {"csrf": env.csrf, **(data or {})}
        return env.client.post(path, data=body, headers=headers, **kw)

    env.sign_in, env.post = sign_in, post
    yield env
    concierge.cancel_all()


# --- the gate ----------------------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:9999", "localhost",
                                  "127.0.0.1.evil.example:8765", "[::1]:8765"])
def test_foreign_host_is_refused(web, host):
    """DNS rebinding: a page elsewhere that resolves to 127.0.0.1 is refused."""
    web.sign_in()
    response = web.client.get("/", headers={"Host": host})
    assert response.status_code == 400
    response = web.client.get(f"/login?token={web.gate.new_token()}", headers={"Host": host})
    assert response.status_code == 400


def test_a_repeated_host_header_is_refused(web):
    web.sign_in()
    response = web.client.get("/", headers=[("Host", f"127.0.0.1:{PORT}"),
                                            ("Host", "evil.example")])
    assert response.status_code == 400


def test_head_does_not_use_up_the_link(web):
    token = web.gate.new_token()
    assert web.client.head(f"/login?token={token}").status_code != 200
    assert web.client.get(f"/login?token={token}").status_code == 200


def test_localhost_name_is_accepted(web):
    web.sign_in()
    assert web.client.get("/", headers={"Host": f"localhost:{PORT}"}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/signin", "/ships/ship-01", "/audit",
                                  "/partials/fleet", "/ships/ship-01/network"])
def test_every_page_needs_a_session(web, path):
    response = web.client.get(path)
    assert response.status_code == 401
    assert "ship-fleet ui" in response.text


def test_static_files_need_no_session_but_still_a_local_host(web):
    assert web.client.get("/static/app.css").status_code == 200
    assert web.client.get("/static/app.css", headers={"Host": "evil.example"}).status_code == 400


def test_link_token_is_swapped_for_a_strict_cookie_once(web):
    token = web.gate.new_token()
    assert web.client.get("/login?token=wrong").status_code == 403
    response = web.client.get(f"/login?token={token}")
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"fleetledger-{PORT}=")
    for flag in ("HttpOnly", "SameSite=Strict", "Path=/"):
        assert flag in cookie
    assert token not in cookie
    # A same-origin refresh takes the token out of the address bar.
    assert 'http-equiv="refresh" content="0;url=/"' in response.text
    assert web.client.get("/").status_code == 200
    # One use only.
    fresh = TestClient(web.client.app, base_url=BASE)
    assert fresh.get(f"/login?token={token}").status_code == 403
    assert fresh.get("/").status_code == 401


def test_link_token_expires(web):
    token = web.gate.new_token()
    web.clock.now += security.TOKEN_TTL + 1
    assert web.client.get(f"/login?token={token}").status_code == 403
    assert web.client.get("/").status_code == 401


def test_only_a_hash_of_the_token_is_kept(web):
    token = web.gate.new_token()
    assert token not in repr(web.gate.__dict__)


def test_session_lapses_when_idle(web):
    web.sign_in()
    web.clock.now += security.SESSION_IDLE + 1
    assert web.client.get("/").status_code == 401


def test_changes_need_csrf_and_same_origin(web):
    web.sign_in()
    data = {"ship": "ship-01", "service": "claude"}
    good = {"csrf": web.csrf, **data}
    # No token, a wrong token, no Origin, a foreign or null Origin: all refused.
    assert web.client.post("/signin", data=data, headers=ORIGIN).status_code == 403
    assert web.client.post("/signin", data={**good, "csrf": "x"},
                           headers=ORIGIN).status_code == 403
    assert web.client.post("/signin", data=good).status_code == 403
    for origin in ("http://evil.example", "null", f"http://127.0.0.1:{PORT + 1}",
                   f"https://127.0.0.1:{PORT}"):
        response = web.client.post("/signin", data=good, headers={"Origin": origin})
        assert response.status_code == 403, origin
    assert not web.concierge.flows()
    # Same origin with the token (form field or header) works.
    response = web.client.post("/signin", data=good, headers=ORIGIN)
    assert response.status_code == 303
    web.concierge.cancel_all()


def test_csrf_header_also_works(web):
    web.sign_in()
    response = web.client.post("/ships/ship-01/refresh",
                               headers={**ORIGIN, "X-CSRF-Token": web.csrf})
    assert response.status_code == 303


def test_one_sessions_csrf_token_does_not_serve_another(web):
    web.sign_in()
    other = TestClient(web.client.app, base_url=BASE, follow_redirects=False)
    other.get(f"/login?token={web.gate.new_token()}")
    response = other.post("/ships/ship-01/refresh", data={"csrf": web.csrf}, headers=ORIGIN)
    assert response.status_code == 403


def test_only_small_plain_forms_are_accepted(web):
    web.sign_in()
    response = web.client.post("/ships/ship-01/refresh", files={"f": ("a", b"x")},
                               headers={**ORIGIN, "X-CSRF-Token": web.csrf})
    assert response.status_code == 415
    response = web.client.post("/ships/ship-01/refresh",
                               data={"csrf": web.csrf, "x": "y" * security.MAX_BODY},
                               headers=ORIGIN)
    assert response.status_code == 413

    def chunks():  # no Content-Length: the cap must hold while reading
        yield f"csrf={web.csrf}&x=".encode()
        for _ in range(security.MAX_BODY // 1024 + 1):
            yield b"y" * 1024

    response = web.client.post("/ships/ship-01/refresh", content=chunks(),
                               headers={**ORIGIN,
                                        "Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == 413


def test_security_headers_on_every_response(web):
    responses = [web.client.get("/"), web.client.get("/", headers={"Host": "evil.example"}),
                 web.client.get("/static/app.css")]
    web.sign_in()
    responses.append(web.client.get("/"))
    for response in responses:
        csp = response.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "script-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp and "unsafe" not in csp
        assert response.headers["x-frame-options"] == "DENY"
        # no-referrer would make browsers send `Origin: null` on form posts.
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["cache-control"] == "no-store"


def test_templates_have_no_inline_script_or_handlers():
    """The CSP forbids inline code; a template that used it would silently break."""
    for path in (WEB / "templates").glob("*.html"):
        text = path.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", text), path.name
        assert not re.search(r"\son[a-z]+\s*=", text), path.name
        assert "style=" not in text, path.name


def test_websocket_needs_origin_cookie_and_csrf(web):
    web.sign_in()
    url = f"ws://127.0.0.1:{PORT}/flows/1-00000000/ws"
    hello = json.dumps({"csrf": web.csrf})

    def refused(client, headers):
        # Even with the right CSRF token, the handshake itself must be refused.
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(url, headers=headers) as ws:
                ws.send_text(hello)
                ws.receive_text()

    refused(web.client, {"Origin": "http://evil.example"})
    refused(web.client, {"Origin": "null"})
    refused(web.client, {})  # no Origin
    refused(web.client, {**ORIGIN, "Host": "evil.example"})  # DNS rebinding
    refused(TestClient(web.client.app, base_url=BASE), ORIGIN)  # no session
    with web.client.websocket_connect(url, headers=ORIGIN) as ws:  # all good: accepted
        ws.send_text(hello)
        assert json.loads(ws.receive_text()) == {"ended": True}  # no such terminal
    with web.client.websocket_connect(url, headers=ORIGIN) as ws:  # wrong CSRF token
        ws.send_text(json.dumps({"csrf": "wrong"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1008


def test_a_copy_that_goes_on_standby_under_a_running_ui_stops_acting_there(web):
    """The copy can change under a running UI (a marker written by hand, say): every
    request then refuses with the CLI's line, the poller reaches no Ship, an open
    terminal socket closes at its next message, no queued sign-in starts, and it stays
    so until restarted, even once the marker goes."""
    from fleetledger import handover

    web.sign_in()
    url = f"ws://127.0.0.1:{PORT}/flows/1-00000000/ws"
    hello = json.dumps({"csrf": web.csrf})
    assert web.client.get("/").status_code == 200
    with web.client.websocket_connect(url, headers=ORIGIN) as ws:  # open before the change
        handover.write(web.store, handover.Marker("standby", handover.now(), "old.example", 2))
        ws.send_text(hello)
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
        assert exc.value.code == 1008
    calls = len(web.ship.calls)
    web.fleet.poll()
    assert len(web.ship.calls) == calls and "on standby" in web.fleet.snapshot.error
    page = web.client.get("/")
    assert page.status_code == 409 and "on standby" in page.text and "old.example" in page.text
    assert "hx-" not in page.text  # nothing on it polls
    partial = web.client.get("/partials/fleet", headers={"HX-Request": "true"})
    assert partial.status_code == 409 and partial.text.startswith("This FleetLedger is on standby")
    assert partial.headers["hx-refresh"] == "true"  # the open page reloads to the line
    assert web.post("/ships/ship-01/refresh").status_code == 409
    assert web.client.get("/static/app.css").status_code == 200
    with pytest.raises(WebSocketDisconnect):  # a new socket: refused at its handshake
        with web.client.websocket_connect(url, headers=ORIGIN) as ws:
            ws.send_text(hello)
            ws.receive_text()
    web.concierge.start(["ship-01"], "claude")  # a sign-in queued before the change
    wait_for(lambda: all(f.done for f in web.concierge._flows.values()))
    flow = next(iter(web.concierge._flows.values()))
    assert "on standby" in flow.detail and web.argvs == []  # never spawned
    assert len(web.ship.calls) == calls

    (web.store.path.parent / "standby.json").write_text("{not json")  # damaged: no either
    assert web.client.get("/").status_code == 409
    assert "cannot be read" in web.post("/ships/ship-01/refresh").text

    handover.clear(web.store, "standby")  # gone, but this UI saw it: restart it to act
    assert web.client.get("/").status_code == 409
    web.fleet.poll()
    assert len(web.ship.calls) == calls


def test_an_active_copy_is_served_as_ever(web):
    """No marker, ever: the checks stop nothing."""
    web.sign_in()
    assert web.client.get("/").status_code == 200
    assert web.post("/ships/ship-01/refresh").status_code == 303
    assert web.fleet._may_poll() and web.fleet.snapshot.error is None


# --- the server ---------------------------------------------------------------------


def test_server_listens_on_loopback_only_and_logs_no_requests():
    sock = server.bind(0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
    finally:
        sock.close()
    config = server.uvicorn_config(lambda *a: None)
    assert config.access_log is False
    assert config.proxy_headers is False
    import inspect

    assert "host" not in inspect.signature(server.serve).parameters


def test_browser_gets_a_private_file_not_the_token(store, monkeypatch, home):
    """webbrowser passes its argument on a command line, so the token never goes there."""
    opened = []
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url) or True)
    import uvicorn

    seen_file = {}

    def fake_run(self, sockets=None):
        path = Path(opened[0][len("file://"):])
        seen_file["mode"] = path.stat().st_mode & 0o777
        seen_file["text"] = path.read_text()

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store, ssh_factory=no_ship_answers)
    monkeypatch.setattr(server.sys.stdin, "isatty", lambda: False, raising=False)
    assert server.serve(app, port=0, open_browser=True, interval=3600) == 0
    assert opened and opened[0].startswith("file://") and "token" not in opened[0]
    assert seen_file["mode"] == 0o600
    assert "/login?token=" in seen_file["text"]
    # Printed too (this terminal only), for browsers that cannot read the file.
    assert "/login?token=" in out.getvalue() and "snap" in out.getvalue()
    assert not Path(opened[0][len("file://"):]).exists()  # removed on exit


def test_ui_command_is_wired(monkeypatch, home):
    called = {}
    monkeypatch.setattr(server, "serve", lambda app, **kw: called.update(kw) or 0)
    assert cli.main(["ui", "--no-browser", "--port", "0"], cli.App()) == 0
    assert called == {"port": 0, "open_browser": False, "interval": 30.0}


# --- the fleet page ---------------------------------------------------------------------


def view(ship_name="ship-01", problem=None, attention=()):
    from fleetledger.inventory import Ship

    ship = Ship(name=ship_name, id="x", driver="manual", names=("a",), host_key="k")
    v = status.ShipView(ship, status=None if problem else copy.deepcopy(RUNNING))
    v.problem = problem
    v.message = problem and f"{problem} problem"
    v.attention = list(attention)
    return v


def test_attention_uses_the_shared_ranking_and_offers_sign_ins():
    views = [
        view("ship-01", attention=[
            {"code": "reboot-required", "message": "Reboot."},
            {"code": "signin-missing", "subject": "gcloud_adc", "message": "ADC missing."},
            {"code": "agent-waiting", "subject": "main", "message": "main waits."},
            {"code": "brand-new-code", "message": "Something new."},
        ]),
        view("ship-02", problem="host-key"),
        view("ship-03", attention=[
            {"code": "signin-wrong-account", "subject": "claude", "message": "Wrong."}]),
    ]
    items = attention_items(views)
    # Exactly ops.attention's order and severities (what `ship-fleet attention` shows).
    shared = ops.attention(views)
    assert [(i.ship, i.code, i.level) for i in items] == [
        (i.ship, i.code, i.severity) for i in shared]
    assert [i.code for i in items[:2]] == ["host-key", "signin-wrong-account"]
    by_code = {i.code: i for i in items}
    assert by_code["signin-missing"].signin == "gcloud-adc"
    assert by_code["signin-wrong-account"].signin == "claude"
    assert by_code["agent-waiting"].signin is None
    assert by_code["brand-new-code"].level == "medium"
    assert by_code["agent-waiting"].key == "ship-01/agent-waiting/main"


def test_fleet_page_shows_ships_and_needs_attention(web):
    web.sign_in()
    page = web.client.get("/").text
    assert "ship-01" in page and "ship-02" in page
    assert "Needs attention" in page
    # running.json has a waiting agent and ADC missing.
    assert 'data-waiting="ship-01: Agent &#39;main&#39; is waiting for you."' in page
    assert "Wrong account" in page  # ship-02 is personal but signed in as the work account
    assert 'name="service" value="claude"' in page  # one-click re-sign for the wrong account


def test_tag_filter(web):
    web.sign_in()
    page = web.client.get("/?tag=personal").text
    assert "/ships/ship-02" in page and "/ships/ship-01" not in page
    assert web.client.get("/?tag=BAD!").status_code == 200  # ignored, not an error


def test_ship_page_lists_recorded_agents_that_are_not_running(web):
    doc = copy.deepcopy(RUNNING)
    stamp = "2026-01-02T00:00:00Z"
    doc["agent_record"] = {"runtime": "herdr", "agents": [
        {"name": "helper", "cwd": "/home/agent/work/side", "mode": "plan", "autostart": False,
         "state": "failed", "relaunch_failures": 3, "created_at": stamp, "updated_at": stamp}],
        "resume": {"at": stamp, "method": "ship-ctl", "resumed": 2, "failed": 1}}
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    row = page[page.index("<td>not running</td>"):]
    row = row[:row.index("</tr>")]
    assert "<td>helper</td>" in row and "failed" in row and "/home/agent/work/side" in row
    assert 'name="agent" value="helper"' in row and "helper is not running" in row
    assert '<option value="">the mode it was in (plan)</option>' in row  # not the default
    assert "Last resume 2026-01-02T00:00:00Z (by ShipShape, from its agent record)" in page
    assert "2 Claude agents running" in page  # a recorded one not running is not counted


def test_ship_page_shows_a_tmp_waiting_for_its_reboot(web):
    doc = copy.deepcopy(RUNNING)
    doc["host"]["tmp"] = {"fs": "tmpfs", "next_boot": "disk"}
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    assert "<dt>/tmp</dt><dd>in memory (tmpfs)" in page
    assert "on the disk after a reboot" in page
    doc["host"]["tmp"] = {"fs": "disk", "next_boot": "disk"}
    web.fleet.poll()
    page = web.client.get("/ships/ship-01").text
    assert "<dt>/tmp</dt><dd>on the disk</dd>" in page


def test_ship_text_is_escaped(web):
    doc = copy.deepcopy(RUNNING)
    doc["attention"] = [{"code": "agent-waiting", "subject": "x",
                         "message": "<script>alert(1)</script>"}]
    doc["herdr"]["agents"][0]["name"] = "<img src=x>"
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    for path in ("/", "/ships/ship-01"):
        page = web.client.get(path).text
        assert "<script>alert" not in page and "<img src=x>" not in page
        assert "&lt;script&gt;" in page or "&lt;img" in page


def test_fleet_partial_answers_204_when_nothing_changed(web):
    web.sign_in()
    first = web.client.get("/partials/fleet")
    assert first.status_code == 200
    version = re.search(r"v=([0-9.]+)", first.text).group(1)
    assert web.client.get(f"/partials/fleet?v={version}").status_code == 204
    web.fleet.poll()
    assert web.client.get(f"/partials/fleet?v={version}").status_code == 200


def test_waiting_agent_shows_within_a_poll(web):
    """A waiting agent reaches Needs attention on the next poll (every 30 s)."""
    doc = copy.deepcopy(RUNNING)
    doc["attention"] = []
    doc["herdr"]["agents"][0]["state"] = "working"
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    assert 'data-waiting="ship-01' not in web.client.get("/partials/fleet").text
    del web.ship.status_override["ship-01"]
    web.fleet.poll()
    assert 'data-waiting="ship-01' in web.client.get("/partials/fleet").text


def test_poller_polls_only_while_someone_looks(store, fake_repo):
    clock = Clock(1000.0)
    polls = []
    app = cli.App(store_factory=lambda: store, repo_factory=lambda: fake_repo)
    f = Fleet(app, interval=30, idle_after=120, clock=clock)
    f.poll = lambda names=None: polls.append(names) or setattr(f, "_last_poll", clock())
    assert f.active()
    clock.now += 121
    assert not f.active()
    f.touch()
    assert f.active() and f._due() is True
    f.poll()
    assert not f._due()
    clock.now += 31
    assert f._due()


def test_unreachable_ship_is_shown_not_fatal(web):
    web.ship.down.add("ship-02")
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/").text
    assert "unreachable" in page and "Cannot reach ship-02" in page


# --- one Ship ---------------------------------------------------------------------------


@pytest.mark.parametrize("path, data", [
    ("/ships/{}/refresh", {}),
    ("/ships/{}/settings", {"remote_control": "on"}),
    ("/ships/{}/logout", {"service": "claude", "confirm": "yes"}),
    ("/ships/{}/network/refresh", {}),
    ("/ships/{}/power", {"power": "start"}),
])
def test_actions_on_a_ship_the_poller_does_not_know_are_404(web, path, data):
    """Every per-Ship action route checks the Ship exists (common.ship_of)."""
    web.sign_in()
    for name in ("nope", "BAD!"):  # well-formed but unknown; not a Ship name at all
        response = web.post(path.format(name), data)
        assert response.status_code == 404
        assert "No such Ship in the inventory." in response.text
    if path.endswith("/refresh") and "network" not in path:
        assert web.post(path.format("ship-01"), data).status_code == 303  # a known Ship works


def _ship_post_routes(asgi):
    """Every POST route under /ships/{name}/, from the app's own route table."""
    while not hasattr(asgi, "routes"):  # under the gate and the vault guard
        asgi = asgi.app
    return sorted(r.path for r in asgi.routes
                  if r.path.startswith("/ships/{name}/") and "POST" in (r.methods or ()))


def test_every_per_ship_post_route_is_404_for_a_ship_the_poller_does_not_know(web):
    """Not only the routes above: each POST /ships/{name}/... route checks the Ship
    before it reads the form or starts anything (inbox upload answers in JSON)."""
    web.sign_in()
    paths = _ship_post_routes(web.client.app)
    assert len(paths) >= 20 and "/ships/{name}/snapshots/take" in paths
    wrong = []
    for path in paths:
        for name in ("nope", "BAD!"):
            url = path.replace("{name}", name)
            if path.endswith("/inbox/upload"):  # a file's bytes, not a form
                if name == "BAD!":
                    continue  # the gate takes bytes only on a Ship-name path (415 here)
                response = web.client.post(url, content=b"hi", headers={
                    **ORIGIN, "Content-Type": "application/octet-stream",
                    "X-CSRF-Token": web.csrf, "X-File-Name": "a.txt"})
                said = response.json().get("error", "") if response.status_code == 404 else ""
            else:
                response = web.post(url, {"confirm": "yes"})
                said = response.text
            if response.status_code != 404 or "No such Ship in the inventory." not in said:
                wrong.append((path, name, response.status_code))
    assert wrong == []
    assert web.argvs == [] and web.app_ui.jobs.recent() == []  # nothing ran


def _action_bar(page):
    start = page.index('<section class="action-bar">')
    return page[start:page.index("</section>", start)]


@pytest.mark.parametrize("tab", ["", "/settings", "/snapshots", "/logs", "/network"])
def test_every_ship_tab_has_the_action_bar_above_the_tabs(web, tab):
    web.sign_in()
    page = web.client.get(f"/ships/ship-01{tab}").text
    bar = _action_bar(page)
    assert page.index('class="action-bar"') < page.index('<nav class="tabs">')
    assert 'data-copy="ship-fleet ssh ship-01"' in bar  # this computer is not trusted here
    assert 'action="/ships/ship-01/refresh"' in bar  # Check now
    assert 'name="operation" value="update"' in bar and 'name="operation" value="reboot"' in bar
    assert 'class="ops"' not in page  # nothing left at the bottom of the Overview
    forms = bar.split("<form")[1:]
    assert len(forms) == 8 and all('name="csrf"' in f for f in forms)  # else the gate refuses
    assert 'action="/ships/ship-01/repin"' in forms[6]  # under More
    update, reboot = forms[1], forms[2]
    start, stop, force = forms[3], forms[4], forms[5]  # under More
    assert 'value="start"' in start and "data-confirm" not in start
    assert 'value="stop"' in stop and "data-confirm-danger" in stop
    assert 'name="force" value="yes"' in force and "data-confirm-danger" in force
    assert 'action="/ships/ship-01/destroy"' in forms[7]  # under More, typed, last
    assert 'data-confirm-title="Update ship-01?"' in update and "data-confirm-danger" not in update
    assert 'data-confirm-title="Reboot ship-01?"' in reboot and "data-confirm-danger" in reboot


def test_a_ship_that_does_not_answer_keeps_check_now_only(web):
    web.ship.down.add("ship-02")
    web.fleet.poll()
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-02").text)
    assert 'action="/ships/ship-02/refresh"' in bar
    assert 'name="operation"' not in bar  # no Update or Reboot for a Ship that is not there
    assert "It does not answer" in bar


def test_a_ship_with_another_problem_says_what_it_is(web):
    """Only a Ship that does not answer gets 'check its VM is on': one that answered
    with something wrong says what was wrong, on every tab."""
    doc = copy.deepcopy(RUNNING)
    del doc["attention"]  # breaks the status contract: the view's problem is "invalid"
    web.ship.status_override["ship-02"] = doc
    web.fleet.poll()
    web.sign_in()
    for tab in ("", "/settings"):
        bar = _action_bar(web.client.get(f"/ships/ship-02{tab}").text)
        assert 'name="operation"' not in bar and "It does not answer" not in bar
        assert "Status breaks the contract" in bar


def test_the_attach_line_uses_herdr_once_this_computer_is_trusted(web, monkeypatch):
    from fleetledger import trustcmd

    monkeypatch.setattr(trustcmd, "attaches_here", lambda inv, ship: True)
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-01").text)
    assert 'data-copy="herdr --remote ship-01"' in bar and "ship-fleet ssh" not in bar


def test_the_ship_header_shows_its_overlay(web):
    web.sign_in()
    assert "overlay:" not in web.client.get("/ships/ship-01").text.split("</section>")[0]
    doc = json.loads((EXAMPLES / "ship-status" / "running-overlay.json").read_text())
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    head = web.client.get("/ships/ship-01").text.split("</section>")[0]
    assert "overlay: ship-01.example-tailnet.ts.net (running), SSH on the overlay only" in head


def test_the_fleet_page_offers_every_ship_actions_above_the_tiles(web):
    web.sign_in()
    page = web.client.get("/?tag=work").text
    menu = page.index('<details class="menu">')
    assert menu < page.index('class="tiles')
    assert "Every work Ship" in page[menu:menu + 200]
    forms = page[menu:page.index('class="tiles')].split("<form")[1:]
    assert len(forms) == 3  # sign in, update, reboot: each for the work Ships only
    assert all('name="tag" value="work"' in f and 'name="csrf"' in f for f in forms)


def _alert_form(page, key):
    """The hidden fields, tick boxes and attributes of an alert's button form."""
    row = page[page.index(f'data-key="{key}'):]
    row = row[:row.index("</li>")]
    attrs, body = re.search(r"<form\b([^>]*)>(.*?)</form>", row, re.S).groups()
    fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', body))
    fields.pop("csrf")
    boxes = re.findall(r'<input type="checkbox" name="([^"]+)"[^>]*data-confirm-field', body)
    return attrs, fields, boxes


@pytest.mark.parametrize("code, operation", [
    ("reboot-required", "reboot"), ("herdr-restart-pending", "reboot"),
    ("behind-tooling", "update"),
])
def test_alert_buttons_go_through_the_dialog_and_the_server_check(web, monkeypatch, code,
                                                                  operation):
    calls = []
    for name in ("update", "reboot"):
        monkeypatch.setattr(ops, name, lambda fleet, ships, _n=name, **kw: calls.append(
            (_n, [s.name for s in ships])) or [ops.Outcome(s.name, True, "done") for s in ships])
    doc = copy.deepcopy(RUNNING)
    doc["attention"] = [{"code": code, "message": "Needs it."}]
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    attrs, fields, boxes = _alert_form(web.client.get("/").text, f"ship-01/{code}/")
    # A tick box, hidden by script and ticked by the dialog; never a prefilled field.
    assert "data-confirm=" in attrs and boxes == ["confirm"]
    assert fields == {"operation": operation, "ship": "ship-01", "from": "fleet"}
    # Without the tick, the server refuses: nothing runs.
    refused = web.post("/operations", fields)
    assert refused.headers["location"] == "/" and not calls
    done = web.post("/operations", {**fields, "confirm": "yes"})
    assert done.headers["location"] == "/"  # back on the Fleet page it was started from
    wait_for(lambda: calls)
    assert calls == [(operation, ["ship-01"])]


def test_join_is_never_one_click(web, store):
    with store.edit() as ed:
        ed.doc["overlay"] = {"provider": "tailscale", "tags": ["tag:ship"]}
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/").text
    attrs, fields, boxes = _alert_form(page, "ship-01/not-on-overlay/")
    assert fields["operation"] == "join" and boxes == ["confirm"]
    bar = _action_bar(web.client.get("/ships/ship-01").text)
    assert 'name="confirm" value="yes" data-confirm-field' in bar
    assert '<input type="hidden" name="confirm"' not in bar
    web.post("/operations", {"operation": "join", "ship": "ship-01"})
    assert web.app_ui.jobs.recent() == []  # refused without the confirm box


def test_check_now_from_an_alert_needs_no_dialog_and_returns_to_the_fleet(web):
    web.ship.down.add("ship-02")
    web.fleet.poll()
    web.sign_in()
    attrs, fields, boxes = _alert_form(web.client.get("/").text, "ship-02/unreachable/")
    assert "data-confirm" not in attrs and boxes == []
    assert web.post("/ships/ship-02/refresh", fields).headers["location"] == "/"
    assert web.post("/ships/ship-02/refresh").headers["location"] == "/ships/ship-02"


def test_actions_started_on_a_ship_page_return_to_it(web, monkeypatch):
    for name in ("update", "reboot"):
        monkeypatch.setattr(ops, name, lambda fleet, ships, **kw: [
            ops.Outcome(s.name, True, "done") for s in ships])
    doc = copy.deepcopy(RUNNING)
    doc["attention"] = [{"code": "reboot-required", "message": "Reboot."}]
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    # An alert's button on the Ship's own page does not say from=fleet: it returns there.
    _attrs, fields, _boxes = _alert_form(web.client.get("/ships/ship-01").text,
                                         "ship-01/reboot-required/")
    assert "from" not in fields
    response = web.post("/operations", {**fields, "confirm": "yes"})
    assert response.headers["location"] == "/ships/ship-01"
    response = web.post("/operations", {"operation": "update", "ship": "ship-01",
                                        "confirm": "yes"})
    assert response.headers["location"] == "/ships/ship-01"
    refused = web.post("/operations", {"operation": "update", "ship": "ship-01"})
    assert refused.headers["location"] == "/ships/ship-01"
    tagged = web.post("/operations", {"operation": "update", "tag": "work"})
    assert tagged.headers["location"] == "/"


def test_sign_out_is_on_each_signed_in_row(web):
    from fleetledger.web.fleet import SERVICE_PROVIDER

    doc = copy.deepcopy(RUNNING)
    doc["auth"]["gcloud_adc"].update(state="ok", account="work@example.com")
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    start = page.index("<h2>Sign-ins</h2>")
    table = page[start:page.index("</table>", start)]
    signed_in = [k for k, e in doc["auth"].items() if e["state"] in ("ok", "expiring")]
    assert table.count('action="/ships/ship-01/logout"') == len(signed_in)
    # Each posts the provider id the server takes (gcloud_adc -> gcloud-adc), not the key.
    posted = {f.split('name="service" value="')[1].split('"')[0]
              for f in table.split('action="/ships/ship-01/logout"')[1:]}
    assert posted == {SERVICE_PROVIDER[k] for k in signed_in} and "gcloud-adc" in posted
    assert "<select name=\"service\">" not in page  # the separate drop-down is gone


def test_a_ship_that_does_not_answer_offers_no_sign_ins(web):
    web.ship.down.add("ship-02")
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/ships/ship-02").text
    section = page[page.index("<h2>Sign-ins</h2>"):]
    section = section[:section.index("</section>")]
    assert 'action="/signin"' not in section and "logout" not in section
    assert "Sign-ins need the Ship to answer." in section


def test_ship_page(web):
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    assert "work@example.com" in page  # signed-in account and the tag's expectation
    assert "example-work" in page
    assert "builder" in page and "bypassPermissions" in page
    assert "hidden-client" in page  # last resume
    assert web.client.get("/ships/nope").status_code == 404
    assert web.client.get("/ships/BAD!").status_code == 404
    assert web.client.get("/ships/ship-01/nonsense").status_code == 404


def _status(web, ship, change):
    import copy

    from test_cli import RUNNING

    doc = copy.deepcopy(RUNNING)
    change(doc)
    web.ship.status_override[ship] = doc
    web.fleet.poll()


def test_ship_page_says_whether_its_agents_show_in_the_claude_app(web):
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    assert "with Remote Control on" in page and "in the\n      Claude app" in page
    assert "Sessions they start themselves do not" in page  # "on": the Ship's agents only
    _status(web, "ship-01", lambda d: d["claude"].update(remote_control="all"))
    page = web.client.get("/ships/ship-01").text
    assert "with Remote Control on" in page and "Sessions they start" not in page
    _status(web, "ship-01", lambda d: d["claude"].update(remote_control="off"))
    assert "Remote Control is off" in web.client.get("/ships/ship-01").text
    _status(web, "ship-01", lambda d: d["claude"].update(remote_control="on"))
    _status(web, "ship-01", lambda d: d["herdr"].update(agents=[]))
    page = web.client.get("/ships/ship-01").text
    assert "No agent is running on ship-01" in page and 'name="agent" value="main"' in page
    _status(web, "ship-01", lambda d: d["auth"]["claude"].update(state="missing", account=None))
    assert "Claude is not signed in on ship-01" in web.client.get("/ships/ship-01").text


def test_an_agent_can_be_added_from_the_ship_page(web):
    web.sign_in()
    web.post("/ships/ship-01/agents", {"agent": "docs", "cwd": "~/docs", "mode": "plan"})
    sent = [c[3:] for name, c in web.ship.calls if name == "ship-01" and c[3:4] in (
        ["set"], ["autostart"])]
    assert ["set", "autostart.add=docs:~/docs:plan", "--json"] in sent
    assert any(c[0] == "autostart" for c in sent)
    before = len(web.ship.calls)
    web.post("/ships/ship-01/agents", {"agent": "Bad Name", "cwd": "~/docs"})
    assert len(web.ship.calls) == before  # refused before reaching the Ship


def test_the_size_is_changed_from_the_ships_settings(web, monkeypatch):
    from fleetledger import lifecycle

    calls = []
    monkeypatch.setattr(lifecycle, "resize",
                        lambda app, store, name, size, restart=False, record_only=False:
                        calls.append((name, size, restart)) or ["Set 8 cores."])
    web.sign_in()
    text = web.client.get("/ships/ship-01/settings").text
    assert "never shrink" in text and 'name="disk_gb" type="number" min="40"' in text
    web.post("/ships/ship-01/size", {"cores": "8", "memory_gb": "8", "disk_gb": "40",
                                     "restart": "yes"})
    assert calls == [("ship-01", {"cores": 8}, True)]  # only what changed
    web.post("/ships/ship-01/size", {"cores": "4", "memory_gb": "8", "disk_gb": "40"})
    assert len(calls) == 1  # nothing changed: nothing done
    with web.store.edit() as ed:
        ed.set_field("ship-02", "driver", "proxmox")
    records = []
    monkeypatch.setattr(lifecycle, "resize",
                        lambda app, store, name, size, restart=False, record_only=False:
                        records.append(record_only) or ["Set 8 cores."])
    web.post("/ships/ship-02/size", {"cores": "8", "memory_gb": "8", "disk_gb": "40",
                                     "record_only": "yes"})
    assert records == [False]  # record only is for a Ship made by hand


def test_record_only_notes_a_manual_ships_size_without_its_driver(web):
    def no_driver(*args, **kw):
        raise AssertionError("record only must not call the driver")

    web.app.driver_factory = no_driver
    web.sign_in()
    page = web.client.get("/ships/ship-01/settings").text  # ship-01 is on the manual driver
    assert 'name="record_only" value="yes"' in page and 'name="restart"' not in page
    web.post("/ships/ship-01/size", {"cores": "6", "memory_gb": "8", "disk_gb": "40",
                                     "record_only": "yes"})
    assert web.store.load().ship("ship-01").size["cores"] == 6
    assert "its VM was not touched" in web.client.get("/ships/ship-01/settings").text


def test_sign_ins_a_ship_does_not_use_are_off_everywhere(web):
    web.sign_in()
    assert 'name="signin" value="gcloud" checked' in web.client.get("/ships/ship-01/settings").text
    web.post("/ships/ship-01/signins", {"signin": ["claude", "github"]})
    assert web.store.load().ship("ship-01").signins == ("claude", "github")
    assert any("gcloud auth revoke" in c[-1] for _, c in web.ship.calls)  # signed out there
    web.fleet.poll()
    page = web.client.get("/ships/ship-01").text
    assert "off (Settings)" in page
    with pytest.raises(Exception, match="do not use that sign-in"):
        web.concierge.start(["ship-01"], "gcloud")  # no flow is ever started for it
    web.post("/ships/ship-01/signins", {"signin": ["claude", "github", "gcloud"]})
    assert web.store.load().ship("ship-01").signins is None


def test_an_agent_in_a_repo_from_the_ship_page_and_its_waiting_item(web):
    from fleetledger.web.fleet import attention_items

    web.sign_in()
    web.post("/ships/ship-01/agents", {"agent": "docs", "repo": "someone/agent-setup"})
    assert ["set", "autostart.add=docs:github:someone/agent-setup", "--json"] in [
        c[3:] for _, c in web.ship.calls]
    _status(web, "ship-01", lambda d: d["attention"].append({
        "code": "agent-repo-waiting", "subject": "docs",
        "message": "Agent 'docs': waits for the GitHub sign-in to clone someone/agent-setup"}))
    items = attention_items(list(web.fleet.snapshot.by_name.values()))
    waiting = next(i for i in items if i.code == "agent-repo-waiting")
    assert waiting.signin == "github"  # one click to the sign-in it waits for


def test_the_attach_command_shown_is_one_that_works_here(web):
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    assert "herdr --remote agent@" not in page  # an unresolvable .local name, untrusted
    assert "<code>ship-fleet ssh ship-01</code>" in page
    assert "Let this computer use ssh ship-01" not in page  # no CA: nothing to offer


def test_a_ship_not_on_the_overlay_can_join_it_in_one_click(web, monkeypatch):
    from test_cli import Keys

    with web.store.edit() as ed:
        ed.doc["overlay"] = {"provider": "tailscale", "tags": ["tag:ship"]}
    web.app.overlay_keys = Keys()
    web.fleet.poll()
    web.sign_in()
    assert "Join the overlay" in web.client.get("/ships/ship-01").text
    from fleetledger import trustcmd

    refreshed = []
    monkeypatch.setattr(trustcmd, "trust_this_computer",
                        lambda app, store, say, create=True: refreshed.append(create))
    web.post("/operations", {"operation": "join", "ship": "ship-01", "confirm": "yes"})
    assert wait_for(lambda: web.store.load().ship("ship-01").names[0]
                    == "ship-01.example-tailnet.ts.net")
    assert wait_for(lambda: refreshed == [False])  # this computer's config: the new name
    web.fleet.poll()
    assert "Join the overlay" not in web.client.get("/ships/ship-01").text


@pytest.mark.parametrize("net", [None, {"window_h": 24, "rx_mb": 0, "tx_mb": 0,
                                         "new_destinations": [], "collector": "unavailable"}])
def test_network_tab_without_a_collector(web, net):
    doc = copy.deepcopy(RUNNING)
    doc["net"] = net
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/ships/ship-01/network").text
    assert "not available" in page


def test_network_tab_shows_the_status_digest_before_a_full_summary(web):
    doc = copy.deepcopy(RUNNING)
    doc["net"] = {"window_h": 24, "rx_mb": 12.5, "tx_mb": 3.25,
                  "new_destinations": ["new.example.com", "<b>x</b>.example"],
                  "collector": "stopped", "source": "ss", "destinations": 14,
                  "connections": 602}
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/ships/ship-01/network").text
    assert "not available" not in page and "own 24-hour digest" in page
    assert "12.5 MB" in page and "new.example.com" in page
    assert "<b>x</b>" not in page
    assert "602" in page and "collector is stopped" in page and "short connections" in page


def test_network_tab_fetches_the_full_summary_through_fleetledger_net(web):
    web.ship.net["ship-01"] = fresh_summary()
    web.sign_in()
    assert "Fetch the latest summary" in web.client.get("/ships/ship-01/network").text
    assert not [c for _, c in web.ship.calls if c[3:4] == ["net"]]  # pages never fetch
    response = web.post("/ships/ship-01/network/refresh")
    assert response.headers["location"] == "/ships/ship-01/network"
    assert [c for _, c in web.ship.calls if c[3:4] == ["net"]]
    page = web.client.get("/ships/ship-01/network").text
    assert "Top destinations" in page and "api.example.com" in page
    assert "new to the fleet" in page and "new-service.example.net" in page
    assert "Programs (7 days)" in page
    week = web.client.get("/ships/ship-01/network?week=1").text
    assert "fd00:db8::55" in week


def test_network_refresh_reports_a_ship_without_visibility(web):
    web.ship.net_contract = False
    web.sign_in()
    web.post("/ships/ship-01/network/refresh")
    assert "predates network visibility" in web.client.get("/ships/ship-01/network").text


def test_fleet_network_page_new_destinations_and_who_talked(web):
    web.ship.net["ship-01"] = fresh_summary()
    web.ship.net["ship-02"] = fresh_summary()
    web.sign_in()
    assert "no summary yet" in web.client.get("/network").text
    web.post("/network/refresh", {"tag": "work"})
    assert [n for n, c in web.ship.calls if c[3:4] == ["net"]] == ["ship-01"]
    page = web.client.get("/network").text
    assert "new-service.example.net" in page and "new to the fleet" in page
    found = web.client.get("/network?who=example.com").text
    assert "/ships/ship-01/network" in found and "api.example.com" in found
    assert "No Ship reached" in web.client.get("/network?who=nothing.example").text
    bad = web.client.get("/network?who=%3Cscript%3E").text
    assert "Give a domain name" in bad and "<script>" not in bad
    personal = web.client.get("/network?tag=personal").text
    assert "new-service.example.net" not in personal


def test_status_digest_tolerates_bad_data():
    v = view()
    v.status["net"] = {"window_h": "x"}
    assert network.status_digest(v) is None
    v.status["net"] = None
    assert network.status_digest(v) is None


def test_settings_go_through_ship_ctl_and_the_inventory(web):
    web.sign_in()
    page = web.client.get("/ships/ship-01/settings").text
    assert "(unchanged: on)" in page
    response = web.post("/ships/ship-01/settings",
                        {"remote_control": "off", "default_mode": "", "sudo": ""})
    assert response.status_code == 303
    sets = [c for _, c in web.ship.calls if c[3:4] == ["set"]]
    assert sets[-1] == ["sudo", "-n", "/usr/local/bin/ship-ctl", "set",
                        "claude.remote_control=off", "--json"]
    assert web.store.load().ship("ship-01").claude["remote_control"] == "off"
    assert "ship-01:" in web.client.get("/ships/ship-01/settings").text  # the flash


def test_settings_refuse_unknown_values(web):
    web.sign_in()
    web.post("/ships/ship-01/settings", {"default_mode": "yolo"})
    assert not [c for _, c in web.ship.calls if c[3:4] == ["set"]]


def test_tags_edit(web):
    web.sign_in()
    web.post("/ships/ship-01/tags", {"tags": "work, team-a"})
    assert set(web.store.load().ship("ship-01").tags) == {"work", "team-a"}
    web.post("/ships/ship-01/tags", {"tags": "Bad Tag!"})
    assert set(web.store.load().ship("ship-01").tags) == {"work", "team-a"}


def test_sign_out_needs_confirmation(web):
    web.sign_in()
    web.post("/ships/ship-01/logout", {"service": "github"})
    assert not [c for _, c in web.ship.calls if "gh auth logout" in c[-1]]
    web.post("/ships/ship-01/logout", {"service": "github", "confirm": "yes"})
    assert [c for _, c in web.ship.calls if "gh auth logout" in c[-1]]
    assert audit.read()[-1]["action"] == "logout"


def test_logs_tab(web):
    web.sign_in()
    page = web.client.get("/ships/ship-01/logs?source=herdr").text
    assert "line one" in page and "line two" in page
    assert web.client.get("/ships/ship-01/logs?source=../etc").status_code == 200


def test_update_and_reboot_run_the_shared_operations_in_the_background(web, monkeypatch):
    calls = []
    release = threading.Event()

    def fake_update(fleet, ships, **kw):
        calls.append(("update", [s.name for s in ships], kw))
        kw["on_event"](ships[0].name, "converging to 0.2.0")
        release.wait(10)
        return [ops.Outcome(s.name, True, "updated to 0.2.0") for s in ships]

    def fake_reboot(fleet, ships, **kw):
        calls.append(("reboot", [s.name for s in ships], kw))
        return [ops.Outcome(ships[0].name, False, "did not come back",
                            ["agent main did not come back"])]

    monkeypatch.setattr(ops, "update", fake_update)
    monkeypatch.setattr(ops, "reboot", fake_reboot)
    web.sign_in()
    # Confirmation first.
    web.post("/operations", {"operation": "update", "tag": "work"})
    assert not calls
    response = web.post("/operations", {"operation": "update", "tag": "work", "confirm": "yes"})
    assert response.status_code == 303  # returns at once; the update runs on
    wait_for(lambda: calls)
    assert calls[0][:2] == ("update", ["ship-01"])
    assert calls[0][2]["reboot_policy"] == "auto" and calls[0][2]["wait"] is True
    page = web.client.get("/partials/fleet").text
    assert "Activity" in page and "converging to 0.2.0" in page
    # One at a time per Ship.
    web.post("/operations", {"operation": "reboot", "ship": "ship-01", "confirm": "yes"})
    assert "already running" in web.client.get("/").text
    release.set()
    wait_for(lambda: all(op.done for op in web.app_ui.jobs.recent()))
    assert "updated to 0.2.0" in web.client.get("/partials/fleet").text
    web.post("/operations", {"operation": "reboot", "ship": "ship-01", "confirm": "yes"})
    wait_for(lambda: len(calls) == 2 and web.app_ui.jobs.recent()[0].done)
    assert calls[1][:2] == ("reboot", ["ship-01"]) and calls[1][2]["wait"] is True
    page = web.client.get("/partials/fleet").text
    assert "did not come back" in page and "agent main did not come back" in page
    assert web.app_ui.jobs.recent()[0].ok is False  # a Ship that failed: not "done"
    assert web.app_ui.jobs.recent()[1].ok is True
    web.post("/operations", {"operation": "rm", "ship": "ship-01", "confirm": "yes"})
    assert len(calls) == 2


def _force_form(page: str, ship: str) -> str:
    """The Force reboot form for ship on a page ("" if there is none)."""
    for form in re.findall(r"<form .*?</form>", page, re.S):
        if 'name="force" value="yes"' in form and f'name="ship" value="{ship}"' in form:
            return form
    return ""


def test_a_refused_reboot_offers_a_force_reboot_behind_its_own_dialog(web, monkeypatch):
    """A shutdown lock refused the reboot: the job card says what holds it and offers
    Force reboot, which needs its danger dialog ticked and reboots with force."""
    calls = []

    def fake_reboot(fleet, ships, **kw):
        calls.append(kw)
        if kw.get("force"):
            return [ops.Outcome(ships[0].name, True, "back after 40 s",
                                data={"waited": True, "forced": True})]
        return [ops.Outcome(ships[0].name, False,
                            "reboot refused: a login session is blocking it: apt (PID 4242, "
                            "as root) in fleet's SSH login session on pts/0 holds a shutdown "
                            "lock",
                            ["Let it finish, or close fleet's session on the Ship"],
                            {"waited": False, "forced": False,
                             "refused": {"said": "denied", "blockers": []}})]

    monkeypatch.setattr(ops, "reboot", fake_reboot)
    web.sign_in()
    web.post("/operations", {"operation": "reboot", "ship": "ship-01", "confirm": "yes"})
    wait_for(lambda: calls and web.app_ui.jobs.recent()[0].done)
    assert calls[0]["force"] is False
    page = web.client.get("/partials/fleet").text
    assert "fleet&#39;s SSH login session on pts/0" in page
    form = _force_form(page, "ship-01")
    assert 'data-confirm-title="Force reboot ship-01?"' in form
    assert "data-confirm-danger" in form and 'name="confirm"' in form
    assert "sudo dpkg --configure -a" in form and "herdr saves every agent first" in form
    # Its dialog first; then a forced reboot of that one Ship.
    web.post("/operations", {"operation": "reboot", "ship": "ship-01", "force": "yes"})
    assert len(calls) == 1
    web.post("/operations", {"operation": "reboot", "ship": "ship-01", "force": "yes",
                             "confirm": "yes"})
    wait_for(lambda: len(calls) == 2 and web.app_ui.jobs.recent()[0].done)
    assert calls[1]["force"] is True
    job = web.app_ui.jobs.recent()[0]
    assert job.title == "Force reboot" and job.ok is True
    assert not _force_form(web.client.get("/partials/fleet").text.split("</article>")[0],
                           "ship-01")  # done: nothing to force on its card


def test_force_reboot_returns_to_the_page_it_was_sent_from(web, monkeypatch):
    """From the Fleet page's Activity the form says from=fleet and comes back there;
    on the Ship's own page (and its card refreshed by htmx there) it stays."""
    calls = []

    def fake_reboot(fleet, ships, **kw):
        calls.append(kw)
        return [ops.Outcome(ships[0].name, False, "reboot refused: apt holds a shutdown lock",
                            ["Wait for it to finish, then reboot again."],
                            {"waited": False, "refused": {"said": "denied", "blockers": []}})]

    monkeypatch.setattr(ops, "reboot", fake_reboot)
    web.sign_in()
    web.post("/operations", {"operation": "reboot", "ship": "ship-01", "confirm": "yes"})
    wait_for(lambda: calls and web.app_ui.jobs.recent()[0].done)
    job = web.app_ui.jobs.recent()[0]
    from_fleet = 'name="from" value="fleet"'
    assert from_fleet in _force_form(web.client.get("/partials/fleet").text, "ship-01")
    on_ship = web.client.get("/ships/ship-01").text
    card = on_ship[on_ship.index('<section class="ship-jobs"'):]
    assert _force_form(card, "ship-01") and from_fleet not in _force_form(card, "ship-01")
    for page, sent in (("/ships/ship-01", False), ("/", True)):
        polled = web.client.get(f"/jobs/{job.id}",
                                headers={"HX-Current-URL": f"http://127.0.0.1{page}"}).text
        assert (from_fleet in _force_form(polled, "ship-01")) is sent
    response = web.post("/operations", {"operation": "reboot", "ship": "ship-01",
                                        "force": "yes", "confirm": "yes", "from": "fleet"})
    assert response.headers["location"].split("?")[0] == "/"
    wait_for(lambda: len(calls) == 2 and web.app_ui.jobs.recent()[0].done)
    assert calls[1]["force"] is True


def test_an_update_whose_reboot_was_refused_offers_force_reboot(web, monkeypatch):
    """The update is done; its card says why the reboot was not, and offers Force reboot."""
    def fake_update(fleet, ships, **kw):
        return [ops.Outcome(ships[0].name, False,
                            "updated to 0.2.0; reboot refused: apt (PID 4242, as root) in "
                            "sam's SSH login session on pts/0 holds a shutdown lock",
                            ["Let it finish, or close sam's session on the Ship"],
                            {"rebooted": False, "reboot": {
                                "waited": False, "refused": {"said": "denied",
                                                             "blockers": []}}})]

    monkeypatch.setattr(ops, "update", fake_update)
    web.sign_in()
    web.post("/operations", {"operation": "update", "ship": "ship-01", "confirm": "yes"})
    wait_for(lambda: web.app_ui.jobs.recent() and web.app_ui.jobs.recent()[0].done)
    page = web.client.get("/partials/fleet").text
    assert "updated to 0.2.0; reboot refused: apt (PID 4242" in page
    assert "<li>Let it finish, or close sam&#39;s session on the Ship</li>" in page
    assert _force_form(page, "ship-01")


def test_only_a_reboot_job_can_be_forced(web):
    with pytest.raises(FleetError, match="Only a reboot can be forced"):
        actions.start_operation(web.app_ui.jobs, web.app_ui.app, "update", ["ship-01"],
                                force=True)
    assert web.app_ui.jobs.recent() == []


def test_only_one_ships_reboot_can_be_forced(web, monkeypatch):
    calls = []
    monkeypatch.setattr(actions, "start_operation", lambda *a, **kw: calls.append((a, kw)))
    web.sign_in()
    for fields in ({"operation": "update", "ship": "ship-01"},
                   {"operation": "reboot", "tag": "work"},
                   {"operation": "reboot"}):
        web.post("/operations", {**fields, "force": "yes", "confirm": "yes"})
        assert "reboot can be forced" in web.client.get("/").text
    assert not calls


def test_the_ship_page_offers_force_reboot_under_more(web):
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    more = page[page.index('<details class="menu">'):]
    assert _force_form(more, "ship-01")


def test_free_text_settings_are_checked_by_ops(web):
    web.sign_in()
    web.post("/ships/ship-01/settings", {"reboot_time": "25:99"})
    assert not [c for _, c in web.ship.calls if c[3:4] == ["set"]]
    assert "reboot_time is HH:MM" in web.client.get("/ships/ship-01/settings").text
    web.post("/ships/ship-01/settings", {"reboot_time": "03:30", "auto_reboot": "true"})
    sets = [c for _, c in web.ship.calls if c[3:4] == ["set"]]
    assert sets[-1][4:-1] == ["updates.auto_reboot=true", "updates.reboot_time=03:30"]


# --- the concierge in the browser ---------------------------------------------------------


def flow_of(web, ship="ship-01"):
    return next(f for f in web.concierge.flows() if f.ship == ship)


def test_paste_back_sign_in_from_the_browser(web, caplog):
    caplog.set_level(logging.DEBUG)
    web.sign_in()
    response = web.post("/signin", {"ship": "ship-01", "service": "claude"})
    assert response.status_code == 303 and "/signin?batch=" in response.headers["location"]
    flow = flow_of(web)
    wait_for(lambda: flow.state in (flows.LINK, flows.CODE))
    card = web.client.get(f"/flows/{flow.id}/card").text
    assert 'href="https://claude.com/cai/oauth/authorize?' in card
    assert 'rel="noopener noreferrer"' in card and 'type="password"' in card
    assert web.client.get(f"/flows/{flow.id}/card?v={flow.version}").status_code == 204

    web.post(f"/flows/{flow.id}/code", {"code": CLAUDE_CODE})
    wait_for(lambda: flow.done)
    assert flow.outcome == "ok", flow.detail
    assert web.typed.read_text().splitlines() == [CLAUDE_CODE]  # typed into the terminal
    # The code, and once done the link, are nowhere: not on the flow, not on
    # any page, not in the audit log, not in any log record.
    assert CLAUDE_CODE not in repr(vars(flow))
    assert flow.url is None
    for path in ("/signin", f"/flows/{flow.id}/card", "/audit", "/"):
        assert CLAUDE_CODE not in web.client.get(path).text
    log = (Path(audit.paths.audit_log_path())).read_text()
    assert CLAUDE_CODE not in log and "oauth" not in log
    assert audit.read()[-1]["outcome"] == "ok"
    assert all(CLAUDE_CODE not in r.getMessage() for r in caplog.records)
    assert "Signed in" in web.client.get(f"/flows/{flow.id}/card").text
    # The code never went on a command line.
    assert all(CLAUDE_CODE not in " ".join(argv) for argv in web.argvs)


def test_device_code_sign_in(web):
    web.fixture["path"] = FIX / "github.txt"
    web.sign_in()
    web.post("/signin", {"ship": "ship-01", "service": "github"})
    flow = flow_of(web)
    wait_for(lambda: flow.state == flows.LINK or flow.done)
    if not flow.done:
        card = web.client.get(f"/flows/{flow.id}/card").text
        assert "ABCD-1234" in card and 'type="password"' not in card
    wait_for(lambda: flow.done)
    assert flow.outcome in ("ok", "wrong-account")
    assert flow.device_code is None


def test_codes_are_refused_unless_a_paste_back_flow_waits(web):
    f = WebFlow("1-00000000", "b", "ship-01", "github", "GitHub", "device-code", state=flows.LINK)
    assert f.submit_code("x") == "This sign-in does not take a pasted code."
    f = WebFlow("1-00000000", "b", "ship-01", "claude", "Claude", "paste-back")
    assert f.submit_code("x") == "This sign-in is not waiting for a code."
    f.state = flows.CODE
    assert f.submit_code("") == "Paste the code first."
    assert f.submit_code("a\nb") is not None
    assert f.submit_code("x" * (flows.MAX_CODE + 1)) is not None
    assert f.submit_code("good") is None and f.state == flows.CHECKING


def test_unsafe_links_are_not_links():
    for bad in ("javascript:alert(1)", "http://claude.com/oauth", "https://x.example/\"><b>",
                "data:text/html,x", "https://x.example/a b"):
        assert flows.safe_url(bad) is None, bad
    assert flows.safe_url("https://claude.com/cai/oauth/authorize?a=b&c=%20")
    f = WebFlow("1-00000000", "b", "ship-01", "claude", "Claude", "paste-back")
    WebPrompter(f).show_link("javascript:alert(1)", None, 60)
    assert f.url is None and f.unsafe_url
    env = webapp.make_env()
    html = env.get_template("_flow.html").render(flow=f, csrf="t")
    assert "javascript:" not in html and "not a plain https address" in html


def test_cancel_types_nothing(web):
    web.sign_in()
    web.post("/signin", {"ship": "ship-01", "service": "claude"})
    flow = flow_of(web)
    wait_for(lambda: flow.state in (flows.LINK, flows.CODE))
    web.post(f"/flows/{flow.id}/cancel")
    wait_for(lambda: flow.done)
    assert flow.outcome == "cancelled"
    assert not web.typed.exists() or web.typed.read_text() == ""


def test_batch_sign_in_per_tag_and_no_duplicates(web):
    web.sign_in()
    web.post("/signin", {"tag": "work", "service": "claude"})
    flows_now = web.concierge.flows()
    assert [f.ship for f in flows_now] == ["ship-01"]
    # The same Ship and service again while it is open: refused.
    web.post("/signin", {"ship": "ship-01", "service": "claude"})
    assert len(web.concierge.flows()) == 1
    assert "already under way" in web.client.get("/signin").text
    # A Ship that does not exist, or a bad service: refused.
    web.post("/signin", {"ship": "ship-09", "service": "claude"})
    web.post("/signin", {"ship": "ship-01", "service": "rm -rf"})
    assert len(web.concierge.flows()) == 1


def test_all_queues_one_flow_per_service_in_order(web):
    web.sign_in()
    web.post("/signin", {"ship": "ship-01", "service": "all"})
    assert [f.provider for f in reversed(web.concierge.flows())] == ["claude", "gcloud",
                                                                      "github"]
    web.concierge.cancel_all()
    wait_for(lambda: all(f.done for f in web.concierge.flows()))


def _badge(web):
    found = re.search(r'<a href="/signin"[^>]*>Sign-ins(?: <span class="badge warn"[^>]*>(\d+)<)?',
                      web.client.get("/").text)
    return int(found.group(1) or 0)


def test_waiting_sign_in_shows_in_needs_attention(web):
    web.sign_in()
    before = _badge(web)
    web.post("/signin", {"ship": "ship-01", "service": "claude"})
    flow = flow_of(web)
    wait_for(lambda: flow.state in (flows.LINK, flows.CODE))
    page = web.client.get("/partials/fleet").text
    assert "Claude Code sign-in is waiting for you." in page
    assert _badge(web) == before + 1  # its row (signed in until now) needs you


def test_live_terminal_when_the_patterns_miss(web, tmp_path):
    script = tmp_path / "manual.txt"
    script.write_text("> Sign in at our new page to continue.\n? by-hand\n"
                      "> Login successful.\n! 0\n")
    web.fixture["path"] = script
    web.sign_in()
    web.post("/signin", {"ship": "ship-01", "service": "claude"})
    flow = flow_of(web)
    wait_for(lambda: flow.state == flows.LIVE_OFFERED)
    card = web.client.get(f"/flows/{flow.id}/card").text
    assert "No sign-in link appeared." in card and "new page" in card
    response = web.post(f"/flows/{flow.id}/live", {"take_over": "yes"})
    assert response.headers["location"] == f"/flows/{flow.id}/terminal"
    assert "terminal" in web.client.get(f"/flows/{flow.id}/terminal").text
    with web.client.websocket_connect(f"ws://127.0.0.1:{PORT}/flows/{flow.id}/ws",
                                     headers=ORIGIN) as ws:
        ws.send_text(json.dumps({"csrf": web.csrf}))
        seen = ""
        ws.send_text(json.dumps({"in": "by-hand\r"}))
        while True:
            data = json.loads(ws.receive_text())
            seen += data.get("out", "")
            if data.get("ended"):
                break
    assert "Login successful" in seen
    wait_for(lambda: flow.done)
    assert flow.outcome == "ok", flow.detail
    assert flow.terminal is None  # the buffer is gone with the flow


def test_live_terminal_declined(web):
    web.fixture["path"] = FIX / "changed-wording.txt"
    web.sign_in()
    web.post("/signin", {"ship": "ship-01", "service": "claude"})
    flow = flow_of(web)
    wait_for(lambda: flow.state == flows.LIVE_OFFERED)
    web.post(f"/flows/{flow.id}/live", {"take_over": "no"})
    wait_for(lambda: flow.done)
    assert flow.outcome == "failed" and "No sign-in link" in flow.detail


def test_terminal_bridge_is_bounded():
    bridge = flows.TerminalBridge()
    for _ in range(100):
        bridge.output("x" * 4096)
    text, position = bridge.read_since(0)
    assert len(text) <= flows.TERMINAL_BUFFER + 4096 and position == 100 * 4096
    assert bridge.send("y" * (flows.MAX_INPUT + 1)) is False
    bridge.close()
    assert bridge.send("y") is False


def test_audit_page(web):
    audit.record("ship-01", "login", "claude", "ok", "work@example.com")
    web.sign_in()
    page = web.client.get("/audit").text
    assert "work@example.com" in page and "login" in page


def test_an_agent_can_stop_being_started_from_the_ship_page(web):
    web.sign_in()
    assert "Stop starting it" in web.client.get("/ships/ship-01").text
    web.post("/ships/ship-01/agents", {"do": "remove", "agent": "docs"})
    assert ["set", "autostart.remove=docs", "--json"] in [c[3:] for _, c in web.ship.calls]


def test_the_ship_page_says_how_to_connect_as_yourself(web):
    web.sign_in()
    page = web.client.get("/ships/ship-01/access").text  # every way to attach
    assert "ship-fleet ssh ship-01 --as owner" in page
    assert 'href="/ships/ship-01/access"' in web.client.get("/ships/ship-01").text


def test_a_job_shows_on_the_fleet_page_its_ships_page_and_in_any_session(web, monkeypatch):
    release = threading.Event()

    def slow_update(fleet, ships, **kw):
        kw["on_event"](ships[0].name, "converging")
        release.wait(10)
        return [ops.Outcome(s.name, True, "updated") for s in ships]

    monkeypatch.setattr(ops, "update", slow_update)
    web.sign_in()
    response = web.post("/operations", {"operation": "update", "ship": "ship-01",
                                        "confirm": "yes"})
    assert response.headers["location"].startswith("/ships/ship-01")  # back to the Ship
    job = web.app_ui.jobs.recent()[0]
    wait_for(lambda: job.snapshot())
    assert f'id="job-{job.id}"' in web.client.get("/ships/ship-01").text
    other = TestClient(web.client.app, base_url=BASE, follow_redirects=False)
    web.sign_in(other)
    assert "converging" in other.get("/partials/fleet").text  # not tied to a session
    card = web.client.get(f"/jobs/{job.id}?v={job.version}")
    assert card.status_code == 204  # unchanged
    release.set()
    wait_for(lambda: job.done)
    card = web.client.get(f"/jobs/{job.id}?v=0")
    assert card.status_code == 200 and "updated" in card.text and "hx-get" not in card.text
    assert web.client.get("/jobs/9999").status_code == 286  # gone: stop polling
    assert web.client.get("/jobs/nope").status_code == 286


# --- start and stop the VM (ship-fleet start|stop) --------------------------------------


def _on_a_fake_hypervisor(web, store, ship="ship-02"):
    from fleetledger.drivers import get_driver
    from test_cli import FakeDriver

    driver = FakeDriver()
    with store.edit() as ed:
        ed.set_field(ship, "driver", "proxmox")
    web.app.driver_factory = lambda inv, d, **kw: (
        driver if d == "proxmox" else get_driver(inv, d, **kw))
    return driver


def test_an_unreachable_ship_on_a_hypervisor_offers_start_the_vm(web, store):
    driver = _on_a_fake_hypervisor(web, store)
    web.ship.down.add("ship-02")
    web.fleet.poll()
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-02").text)
    primary = bar[:bar.index('<details class="menu">')]
    assert 'name="power" value="start"' in primary and "Start the VM" in primary
    assert 'value="stop"' not in primary  # Stop only under More, behind its dialog
    response = web.post("/ships/ship-02/power", {"power": "start"})
    assert response.status_code == 303 and response.headers["location"] == "/ships/ship-02"
    wait_for(lambda: web.app_ui.jobs.recent() and web.app_ui.jobs.recent()[0].done)
    job = web.app_ui.jobs.recent()[0]
    assert job.ok and job.title == "Start ship-02's VM" and "Started ship-02." in job.snapshot()
    assert driver.power == [("start", "ship-02")]
    card = web.client.get(f"/jobs/{job.id}")
    assert card.headers.get("HX-Refresh") == "true"  # on now: the page redraws its bar


@pytest.mark.parametrize("kind, refresh", [
    ("start", True), ("stop", True),  # the VM is on or off: the action bar changes
    ("certs", True), ("trust", True), ("leave", True),  # the Access tables change
    ("agent-restart", True),  # the agent list shows the new modes
    ("scan", True),  # the adopt page lists what it heard
    ("update", False), ("reboot", False), ("snapshot", False), ("image", False),
    ("destroy", False), ("enrol", False), ("repin", False),
])
def test_a_finished_job_card_redraws_the_page_only_when_the_page_changed(web, kind, refresh):
    web.sign_in()
    job = web.app_ui.jobs.start(kind, ["ship-01"], lambda job: 0, kind)
    wait_for(lambda: job.done)
    for where in (f"{BASE}/ships/ship-01", f"{BASE}/"):
        card = web.client.get(f"/jobs/{job.id}", headers={"HX-Current-URL": where})
        assert card.status_code == 200
        assert card.headers.get("HX-Refresh") == ("true" if refresh else None)


def test_a_finished_inbox_job_redraws_only_that_ships_page(web):
    web.sign_in()
    job = web.app_ui.jobs.start("inbox", ["ship-01"], lambda job: 0, "inbox")
    wait_for(lambda: job.done)
    seen = {where: web.client.get(f"/jobs/{job.id}", headers={
        "HX-Current-URL": f"{BASE}{where}"}).headers.get("HX-Refresh")
        for where in ("/ships/ship-01/inbox", "/ships/ship-02/inbox", "/")}
    assert seen == {"/ships/ship-01/inbox": "true", "/ships/ship-02/inbox": None, "/": None}


def test_stop_needs_its_dialog_and_runs_ship_fleet_stop(web, store):
    driver = _on_a_fake_hypervisor(web, store)
    web.fleet.poll()
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-02").text)
    stop = bar[bar.rindex("<form", 0, bar.index('value="stop"')):]
    stop = stop[:stop.index("</form>")]
    assert "data-confirm-danger" in stop and "herdr saves every agent" in stop
    assert 'name="confirm" value="yes" data-confirm-field' in stop
    assert 'name="action"' not in stop  # the dialog script owns that name
    web.post("/ships/ship-02/power", {"power": "stop"})  # no tick: refused
    assert "Tick the box" in web.client.get("/ships/ship-02").text
    assert not web.app_ui.jobs.recent() and not getattr(driver, "power", [])
    web.post("/ships/ship-02/power", {"power": "stop", "confirm": "yes"})
    wait_for(lambda: web.app_ui.jobs.recent() and web.app_ui.jobs.recent()[0].done)
    assert driver.power == [("stop", "ship-02")]
    assert web.app_ui.jobs.recent()[0].title == "Stop ship-02's VM"
    web.post("/ships/ship-02/power", {"power": "reboot-hard", "confirm": "yes"})
    assert "Unknown power action" in web.client.get("/ships/ship-02").text
    assert len(web.app_ui.jobs.recent()) == 1


def test_stop_waits_for_an_open_sign_in(web, store, monkeypatch):
    driver = _on_a_fake_hypervisor(web, store)
    web.fleet.poll()
    web.sign_in()

    class Flow:
        ship, needs_you = "ship-02", False

        def cancel(self):
            pass

    monkeypatch.setattr(web.concierge, "active", lambda: [Flow()])
    web.post("/ships/ship-02/power", {"power": "stop", "confirm": "yes"})
    assert "a sign-in is open on ship-02" in web.client.get("/ships/ship-02").text
    assert not web.app_ui.jobs.recent() and not getattr(driver, "power", [])
    web.post("/ships/ship-02/power", {"power": "start"})  # starting cuts nothing
    wait_for(lambda: web.app_ui.jobs.recent() and web.app_ui.jobs.recent()[0].done)
    assert driver.power == [("start", "ship-02")]


def test_power_on_the_manual_driver_shows_the_steps_and_waits_its_turn(web, monkeypatch):
    web.ship.down.add("ship-01")
    web.fleet.poll()
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-01").text)
    primary = bar[:bar.index('<details class="menu">')]
    assert 'name="power"' not in primary  # no primary Start on the manual driver
    web.ship.down.discard("ship-01")
    web.fleet.poll()
    bar = _action_bar(web.client.get("/ships/ship-01").text)
    assert 'value="start"' in bar[bar.index('<details class="menu">'):]  # under More only
    assert "FleetLedger shows the step to run" in bar
    release = threading.Event()
    web.app_ui.jobs.start("update", ["ship-01"], lambda job: release.wait(10))
    web.post("/ships/ship-01/power", {"power": "start"})  # busy: refused
    assert "already running" in web.client.get("/ships/ship-01").text
    release.set()
    wait_for(lambda: web.app_ui.jobs.recent()[0].done)
    web.post("/ships/ship-01/power", {"power": "start"})
    wait_for(lambda: len(web.app_ui.jobs.recent()) == 2 and web.app_ui.jobs.recent()[0].done)
    job = web.app_ui.jobs.recent()[0]
    assert job.ok and job.result["by_hand"]  # not "done": nothing has started it yet
    card = web.client.get(f"/jobs/{job.id}").text
    assert 'class="pill muted">by hand' in card and "Do this on your hypervisor" in card
    assert 'data-copy="Start ship-01' in card or 'data-copy="qm start' in card


# --- New Ship (/new) and "Not enrolled yet" --------------------------------------------


def _form_fields(page, action):
    form = page.split(f'action="{action}"', 1)[1].split("</form>", 1)[0]
    return set(re.findall(r'name="([^"]+)"', form)), len(re.findall(r'\bname="', form))


def test_the_new_ship_page_offers_every_option_under_the_field_cap(web):
    web.sign_in()
    page = web.client.get("/new").text
    names, count = _form_fields(page, "/new")
    for field in ("name", "tag", "cores", "memory_gb", "disk_gb", "signin", "agent_on",
                  "agent", "cwd", "repo", "image", "hosts", "mode", "agent_mode", "prompt",
                  "allow_bypass", "agent_sudo", "timezone", "tool", "profile"):
        assert field in names, field
    assert count < 100  # the gate's parse_qs(max_num_fields=100)
    assert 'href="/new"' in web.client.get("/").text  # the header's + New Ship


def test_bad_input_on_new_is_flashed_and_starts_nothing(web):
    web.sign_in()
    response = web.post("/new", {"name": "ship-01"})  # taken
    assert response.status_code == 400 and "already in the inventory" in response.text
    response = web.post("/new", {"name": "ship-31", "cores": "0"})
    assert response.status_code == 400 and "cores: 1 to" in response.text
    assert web.app_ui.jobs.recent() == []
    assert not web.store.load().has_ship("ship-31")


def test_a_refused_new_ship_form_keeps_what_was_typed(web):
    web.sign_in()
    typed = {"name": "ship-01", "tag": "work lab", "cores": "6", "memory_gb": "12",
             "disk_gb": "60", "signins_shown": "yes", "signin": ["claude"],
             "agent_on": "yes", "agent": "lead", "cwd": "~/work/x",
             "repo": "someone/agent-setup", "hosts": "ship-01.example.net",
             "agent_sudo": "yes", "prompt": "Get going."}
    page = web.post("/new", typed).text
    assert "already in the inventory" in page
    for html in ('name="name" value="ship-01"', 'name="tag" value="work lab"',
                 'name="cores" type="number"', 'value="6"', 'value="12"', 'value="60"',
                 'name="agent" value="lead"', 'name="cwd" value="~/work/x"',
                 'name="repo" value="someone/agent-setup"',
                 '(one per line)">ship-01.example.net</textarea>',
                 'name="agent_sudo" value="yes" checked', 'value="Get going."',
                 '<details class="advanced" open>'):
        assert html in page, html
    assert 'name="signin" value="claude" checked' in page
    assert 'name="signin" value="github" checked' not in page  # unticked stays unticked
    fresh = web.client.get("/new").text  # a new visit starts from the defaults
    assert 'name="agent" value="main"' in fresh and '<details class="advanced">' in fresh


def test_new_before_setup_goes_to_setup_and_without_a_checkout_says_why(web, monkeypatch):
    from fleetledger.errors import ConfigError

    web.sign_in()
    monkeypatch.setattr(web.app, "repo_factory", lambda: (_ for _ in ()).throw(
        ConfigError("Cannot find the ShipShape checkout.")))
    response = web.client.get("/new")
    assert response.status_code == 503 and "Cannot find the ShipShape checkout" in response.text
    monkeypatch.setattr(web.store, "exists", lambda: False)
    monkeypatch.setattr(web.store, "load", lambda: (_ for _ in ()).throw(
        ConfigError("No inventory yet.")))
    response = web.client.get("/new")
    assert response.status_code == 303 and response.headers["location"] == "/setup"


def test_a_ship_that_answers_but_never_enrolled_offers_finish_enrolling(web):
    """Made by hand and booted before you came back: it answers, but its seed, overlay
    and certificate still wait for the enrolment."""
    web.sign_in()
    with web.store.edit() as ed:
        ed.set_field("ship-02", "enrolled", None)
    web.fleet.poll()
    view = [v for v in web.fleet.snapshot.views if v.ship.name == "ship-02"][0]
    assert view.problem is None and view.unenrolled and view.state != "not-enrolled"
    bar = _action_bar(web.client.get("/ships/ship-02").text)
    assert 'action="/ships/ship-02/enrol"' in bar and 'value="update"' in bar
    from fleetledger import ops

    items = [(i.code, i.severity) for i in ops.attention(web.fleet.snapshot.views)
             if i.ship == "ship-02"]
    assert ("not-enrolled", "low") in items
    fleet = web.client.get("/partials/fleet").text
    assert 'href="/ships/ship-02"' in fleet and "Finish enrolling" in fleet
    web.post("/ships/ship-02/enrol")
    job = web.app_ui.jobs.recent()[0]
    wait_for(lambda: job.done)
    assert job.ok and web.store.load().ship("ship-02").enrolled is not None


def test_finish_enrolling_refuses_an_enrolled_ship(web):
    web.sign_in()
    web.post("/ships/ship-01/enrol")
    assert "enrolled already" in web.client.get("/ships/ship-01").text
    assert web.app_ui.jobs.recent() == []


def test_a_ship_made_from_new_by_hand_shows_not_enrolled_yet_with_finish_enrolling(web):
    web.sign_in()
    response = web.post("/new", {"name": "ship-31", "tag": "work", "agent_on": "yes",
                                 "agent": "main", "cwd": "~/work"})
    assert response.status_code == 303 and response.headers["location"] == "/"
    job = web.app_ui.jobs.recent()[0]
    assert job.kind == "create" and job.ships == ["ship-31"]
    wait_for(lambda: job.done)
    assert job.ok and job.result["manual"] and not job.result["enrolled"]
    assert any("Create the VM" in line for line in job.snapshot())  # the manual steps
    ship = web.store.load().ship("ship-31")
    assert ship.enrolled is None and ship.tags == ("work",)
    web.ship.down.add("ship-31")  # its VM is not made yet
    web.fleet.poll()
    page = web.client.get("/ships/ship-31").text
    assert "not-enrolled" in page and 'action="/ships/ship-31/enrol"' in page
    assert 'class="flash bad">Cannot reach' not in page  # not an outage
    fleet = web.client.get("/partials/fleet").text
    assert "not-enrolled" in fleet and "Not enrolled yet" in fleet
    assert 'href="/ships/ship-31"' in fleet and "Finish enrolling" in fleet
    alert = [a for a in web.app_ui.fleet.snapshot.views if a.ship.name == "ship-31"][0]
    assert alert.state == "not-enrolled"
    from fleetledger import ops

    items = [i for i in ops.attention(web.fleet.snapshot.views) if i.ship == "ship-31"]
    assert [(i.code, i.severity) for i in items] == [("not-enrolled", "low")]  # no outage
    # Finish enrolling: `ship-fleet status ship-31 --wait` as a job.
    web.ship.down.discard("ship-31")
    web.post("/ships/ship-31/enrol")
    enrol = web.app_ui.jobs.recent()[0]
    assert enrol.kind == "enrol" and enrol.title == "Finish enrolling ship-31"
    wait_for(lambda: enrol.done)
    assert web.store.load().ship("ship-31").enrolled is not None


def test_after_a_restart_a_half_made_ship_is_still_not_enrolled_yet(web, store):
    """Jobs live in memory; the inventory says what state a Ship is in."""
    from fleetledger.inventory import Ship
    from fleetledger.store import ship_entry

    with store.edit() as ed:  # what a create cut short by Ctrl-C leaves behind
        ed.add_ship("ship-32", ship_entry(Ship(
            name="ship-32", id="0c3d1f2e-1111-4222-8333-944455566677", driver="manual",
            names=("ship-32.local",), host_key="ssh-ed25519 AAAA ship-32")))
    web.ship.down.add("ship-32")
    fresh = webapp.create_app(web.app, web.gate, fleet=web.fleet, concierge_=web.concierge)
    client = TestClient(fresh, base_url=BASE, follow_redirects=False)
    web.sign_in(client)
    web.fleet.poll()
    page = client.get("/ships/ship-32").text
    assert "not-enrolled" in page and 'action="/ships/ship-32/enrol"' in page
    fleet = client.get("/partials/fleet").text
    assert "CRITICAL" not in fleet.split("ship-32")[0][-400:]  # not a critical alert



# --- the Ship Settings tab, grouped, with current values (web.shipsettings) -------------


def _reporting(web, ship="ship-01", **changes):
    """The Ship reports its settings (ui-6s) and these tools."""
    doc = copy.deepcopy(RUNNING)
    doc["ship"]["name"], doc["ship"]["id"] = ship, web.store.load().ship(ship).id
    doc["settings"] = json.loads(
        (EXAMPLES / "ship-status" / "running-settings.json").read_text())["settings"]
    doc["settings"]["ship"]["tags"] = list(web.store.load().ship(ship).tags)
    doc["tools"] = {"gh-axi": {"state": "ok"}}
    for key, value in changes.items():
        doc[key] = value
    web.ship.status_override[ship] = doc
    web.fleet.poll()
    return doc


def test_settings_are_grouped_and_show_what_the_ship_reports(web):
    _reporting(web)
    web.sign_in()
    page = web.client.get("/ships/ship-01/settings").text
    heads = [h for h in re.findall(r"<h2>([^<]+)</h2>", page)]
    for group in ("Claude Code", "Agent", "Sign-ins it uses", "Tools", "Network", "Updates",
                  "Size", "Tags"):
        assert group in heads, group
    assert heads.index("Claude Code") < heads.index("Tools") < heads.index("Network")
    assert '<option value="Europe/Berlin"' not in page  # free text, not a choice
    assert 'name="timezone" value="Europe/Berlin"' in page
    assert 'name="session_prefix" value="ship-01"' in page
    assert '<option value="latest" selected>' in page  # update_channel, as reported
    assert "(unchanged" not in page  # it reports every value
    assert 'name="ssh_listen"' not in page  # only for a Ship on the overlay


def test_an_older_ship_keeps_unchanged_and_the_channel_from_its_claude_block(web):
    doc = copy.deepcopy(RUNNING)
    doc["claude"]["channel"] = "stable"
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/ships/ship-01/settings").text
    assert "does not report its settings yet" in page
    channel = page[page.index('name="update_channel"'):]
    assert channel.index("(unchanged: stable)") < channel.index("</select>")
    assert 'name="timezone" value=""' in page and 'placeholder="(unchanged)"' in page


def test_applying_a_group_sends_only_what_changed(web):
    _reporting(web)
    web.sign_in()
    web.post("/ships/ship-01/settings", {"auto_reboot": "true", "reboot_time": "04:00",
                                         "timezone": "Europe/Berlin"})
    sets = [c for _, c in web.ship.calls if c[3:4] == ["set"]]
    assert sets[-1][4:-1] == ["updates.auto_reboot=true"]
    web.post("/ships/ship-01/settings", {"timezone": "not a zone"})
    assert "IANA zone name" in web.client.get("/ships/ship-01/settings").text
    assert len([c for _, c in web.ship.calls if c[3:4] == ["set"]]) == len(sets)


def test_ssh_listen_is_offered_on_the_overlay_behind_a_dialog(web, store):
    with store.edit() as ed:
        ed.doc["overlay"] = {"provider": "tailscale", "tags": ["tag:ship"]}
        ed.set_field("ship-01", "overlay_node", "nAbC123")
    _reporting(web)
    web.sign_in()
    page = web.client.get("/ships/ship-01/settings").text
    form = page[page.rindex("<form", 0, page.index('name="ssh_listen"')):]
    form = form[:form.index("</form>")]
    assert "data-confirm-title" in form and 'data-confirm-field' in form
    assert "data-confirm-danger" in form  # Cancel has the focus
    web.post("/ships/ship-01/settings", {"part": "ssh", "ssh_listen": "overlay"})  # no tick
    assert "Tick the box to confirm" in web.client.get("/ships/ship-01/settings").text
    assert not [c for _, c in web.ship.calls if c[3:4] == ["set"]]  # nothing sent
    web.post("/ships/ship-01/settings", {"part": "ssh", "ssh_listen": "overlay",
                                         "confirm": "yes", "sudo": "true"})
    sets = [c for _, c in web.ship.calls if c[3:4] == ["set"]]
    assert sets[-1][4:-1] == ["network.ssh_listen=overlay"]  # that form's key only


def test_ssh_listen_is_refused_for_a_ship_off_the_overlay(web):
    _reporting(web)
    web.sign_in()
    web.post("/ships/ship-01/settings", {"part": "ssh", "ssh_listen": "overlay",
                                         "confirm": "yes"})
    assert "not on the overlay" in web.client.get("/ships/ship-01/settings").text
    assert not [c for _, c in web.ship.calls if c[3:4] == ["set"]]


def test_tools_wait_for_the_ship_and_a_failed_add_removes_nothing(web, monkeypatch):
    web.ship.down.add("ship-01")
    web.fleet.poll()
    web.sign_in()
    web.post("/ships/ship-01/tools", {"tool": ["tasks-axi"]})
    assert "not known until it answers" in web.client.get("/ships/ship-01/settings").text
    assert web.app_ui.jobs.recent() == []
    web.ship.down.discard("ship-01")
    _reporting(web)
    from fleetledger.web import jobs as jobs_mod

    real = jobs_mod.run_cli
    ran = []

    def failing_add(app, argv, job):
        ran.append(argv[:2])
        return 1 if argv[:2] == ["tools", "add"] else real(app, argv, job)

    monkeypatch.setattr(jobs_mod, "run_cli", failing_add)
    web.post("/ships/ship-01/tools", {"tool": ["tasks-axi"]})  # add one, remove gh-axi
    job = web.app_ui.jobs.recent()[0]
    wait_for(lambda: job.done)
    assert not job.ok and ran == [["tools", "add"]]  # the remove never ran


def test_tools_are_ticked_from_the_ship_and_changed_as_a_job(web):
    _reporting(web)
    web.sign_in()
    page = web.client.get("/ships/ship-01/settings").text
    assert 'name="tool" value="gh-axi" checked' in page
    assert 'name="tool" value="tasks-axi">' in page
    web.post("/ships/ship-01/tools", {"tool": ["gh-axi"]})
    assert "Nothing to change" in web.client.get("/ships/ship-01/settings").text
    web.post("/ships/ship-01/tools", {"tool": ["tasks-axi"]})  # add one, remove one
    job = web.app_ui.jobs.recent()[0]
    assert job.kind == "tools" and "add tasks-axi" in job.title and "remove gh-axi" in job.title
    wait_for(lambda: job.done)
    tools = [c[1][3:] for c in web.ship.calls if c[1][3:4] == ["tools"]]
    assert ["tools", "add", "tasks-axi", "--json"] in tools
    assert ["tools", "remove", "gh-axi", "--json"] in tools
    web.post("/ships/ship-01/tools", {"tool": ["nope"]})
    assert "not one of the catalogue" in web.client.get("/ships/ship-01/settings").text


def test_logs_show_the_chosen_number_of_lines(web, monkeypatch):
    seen = []
    monkeypatch.setattr(webapp.actions, "logs",
                        lambda app, ship, source, lines: seen.append(lines) or (True, ["x"]))
    web.sign_in()
    page = web.client.get("/ships/ship-01/logs?source=provision&lines=1000").text
    assert seen == [1000] and "<option selected>1000</option>" in page
    web.client.get("/ships/ship-01/logs?source=provision&lines=999999")
    assert seen[-1] == 300  # not a choice: the default


# --- restarting agents in a new mode (ship-fleet agent restart) ---------------------


def _agent_calls(web):
    return [c for _, c in web.ship.calls if c[3:4] == ["agent"]]


def test_each_agent_has_restart_in_mode_behind_a_dialog(web):
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    forms = re.findall(r'<form method="post" action="/ships/ship-01/agents/restart"(.*?)</form>',
                       page, re.S)
    assert len(forms) == 2  # main and builder
    main = next(f for f in forms if 'name="agent" value="main"' in f)
    assert 'data-confirm-title="Restart main on ship-01?"' in main
    assert 'name="confirm" value="yes" data-confirm-field' in main
    assert "that question is dismissed" in main  # main is waiting for an answer
    assert 'name="force"' not in main
    assert '<option value="plan">' in main
    assert '<option value="bypassPermissions">' not in main  # not allowed on ship-01
    builder = next(f for f in forms if 'name="agent" value="builder"' in f)
    assert 'name="force" value="yes"' in builder and "It is working now" in builder


def test_restart_in_mode_runs_ship_fleet_agent_restart_as_a_job(web):
    web.sign_in()
    refused = web.post("/ships/ship-01/agents/restart", {"agent": "main", "mode": "plan"})
    assert refused.headers["location"] == "/ships/ship-01"
    assert web.app_ui.jobs.recent() == [] and not _agent_calls(web)
    done = web.post("/ships/ship-01/agents/restart",
                    {"agent": "main", "mode": "plan", "fresh": "yes", "confirm": "yes"})
    assert done.headers["location"] == "/ships/ship-01"
    [job] = web.app_ui.jobs.recent()
    assert job.kind == "agent-restart" and job.title == "Restart main on ship-01 in plan"
    wait_for(lambda: job.done)
    assert _agent_calls(web)[-1][3:] == ["agent", "restart", "main", "--mode", "plan",
                                         "--fresh", "--json"]
    card = web.client.get(f"/jobs/{job.id}")
    assert card.headers.get("HX-Refresh") == "true"  # the agent list shows the new mode


@pytest.mark.parametrize("fields,why", [
    ({"agent": "main", "mode": "yolo"}, "Mode: not one of the choices."),
    ({"agent": "Main!"}, "Not an agent name."),
    ({}, "Which agents?"),
])
def test_restart_in_mode_refuses_bad_choices_on_the_page(web, fields, why):
    web.sign_in()
    web.post("/ships/ship-01/agents/restart", {**fields, "confirm": "yes"})
    assert web.app_ui.jobs.recent() == []
    assert why in web.client.get("/ships/ship-01").text


def test_settings_offer_to_apply_the_default_mode_to_running_agents(web):
    doc = _reporting(web)
    web.sign_in()
    page = web.client.get("/ships/ship-01/settings").text
    block = page[page.index("<h3>Running agents</h3>"):]
    # builder runs in bypassPermissions but would start in the default, auto.
    assert "builder (bypassPermissions; starts in auto)" in block
    form = block[:block.index("</form>")]
    assert re.findall(r'name="agent" value="([^"]+)"', form) == ["builder"]
    assert "builder goes from bypassPermissions to auto." in form
    assert 'name="force" value="yes"' in form  # builder is working
    response = web.post("/ships/ship-01/agents/restart",
                        {"agent": "builder", "from": "settings", "confirm": "yes"})
    assert response.headers["location"] == "/ships/ship-01/settings"
    [job] = web.app_ui.jobs.recent()
    wait_for(lambda: job.done)
    assert _agent_calls(web)[-1][3:] == ["agent", "restart", "builder", "--json"]
    # Every agent in the mode it starts in: nothing to apply.
    doc["herdr"]["agents"][1]["mode"] = "auto"
    web.fleet.poll()
    page = web.client.get("/ships/ship-01/settings").text
    assert "Every running agent is in the mode it starts in." in page
    assert "Apply the default mode to running agents</button>" not in page


def test_changing_the_default_mode_says_running_agents_keep_theirs(web):
    _reporting(web)
    web.sign_in()
    web.post("/ships/ship-01/settings", {"default_mode": "plan"})
    page = web.client.get("/ships/ship-01/settings").text
    assert "Running agents keep the mode they are in" in page
    assert "Apply the default mode to running agents, below" in page
    assert "ship-fleet agent restart ship-01 --all" not in page  # the CLI's own hint


def test_interrupt_it_while_it_works_passes_force(web):
    web.sign_in()
    web.post("/ships/ship-01/agents/restart",
             {"agent": "builder", "mode": "acceptEdits", "force": "yes", "confirm": "yes"})
    [job] = web.app_ui.jobs.recent()
    wait_for(lambda: job.done)
    assert _agent_calls(web)[-1][3:] == ["agent", "restart", "builder", "--mode",
                                         "acceptEdits", "--force", "--json"]


# --- every sign-in (/signin) --------------------------------------------------------------


def _rows(page):
    """(ship, service) of each sign-in row on the page, in order."""
    return re.findall(r'data-ship="([^"]+)" data-service="([^"]+)"', page)


def _groups(page):
    return re.findall(r'<tbody class="group-([a-z]+)">', page)


def test_the_sign_ins_page_lists_every_sign_in_what_needs_you_first(web):
    web.ship.down.add("ship-02")
    expired = copy.deepcopy(RUNNING)
    expired["auth"]["github"].update(state="expired")
    web.ship.status_override["ship-01"] = expired
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/signin").text
    rows = _rows(page)
    assert len(rows) == 8  # two Ships, four services each, no filter
    assert rows[0] == ("ship-01", "github")
    assert _groups(page) == ["action", "degraded", "healthy"]
    assert "sev-icon lvl-critical" in page and ">critical<" in page.lower()  # shape and word
    # the row's fix is the existing sign-in form, for its Ship and provider
    first = page[page.index('data-ship="ship-01" data-service="github"'):]
    first = first[:first.index("</tr>")]
    assert 'action="/signin"' in first and 'name="service" value="github"' in first
    # a Ship that does not answer: checked again from here, back to this page
    down = page[page.index('data-ship="ship-02" data-service="claude"'):]
    down = down[:down.index("</tr>")]
    assert 'action="/ships/ship-02/refresh"' in down and 'name="from" value="signin"' in down
    response = web.post("/ships/ship-02/refresh", {"from": "signin", "f_group": "degraded"})
    assert response.headers["location"] == "/signin?group=degraded"
    # the nav's badge counts the sign-ins that need you, and links here
    assert re.search(r'<a href="/signin"[^>]*>Sign-ins <span class="badge warn"[^>]*>1<', page)


def test_sign_in_filters_live_in_the_url_with_a_count_per_group(web):
    web.sign_in()
    page = web.client.get("/signin?service=claude").text
    assert _rows(page) == [("ship-02", "claude"), ("ship-01", "claude")]  # wrong account first
    assert 'href="/signin?service=claude&amp;group=action"' in page
    assert re.search(r'group=action"[^>]*>Needs you <span class="n">1<', page)
    assert re.search(r'class="chip on" aria-current="true">All <span class="n">2<', page)
    page = web.client.get("/signin?service=claude&group=healthy").text
    assert _rows(page) == [("ship-01", "claude")]
    page = web.client.get("/signin?tag=personal").text
    assert {s for s, _ in _rows(page)} == {"ship-02"}
    page = web.client.get("/signin?ship=ship-01&service=gcloud_adc").text
    assert _rows(page) == [("ship-01", "gcloud_adc")]
    assert '<option value="ship-01" selected>' in page
    # anything not a known Ship name, service, tag or group is ignored, never echoed
    page = web.client.get('/signin?ship=<b>&service=rm+-rf&group=x"y&tag=a/b').text
    assert len(_rows(page)) == 8 and "<b>" not in page and 'x"y' not in page


def test_the_sign_in_list_redraws_only_when_something_changed(web):
    web.sign_in()
    page = web.client.get("/signin?group=action").text
    url = re.search(r'hx-get="(/partials/signins\?[^"]+)"', page).group(1).replace("&amp;", "&")
    assert "group=action" in url
    assert web.client.get(url).status_code == 204
    web.fleet.bump()
    fresh = web.client.get(url)
    assert fresh.status_code == 200 and 'id="signin-list"' in fresh.text
    assert 'id="signin-groups" aria-label="Sign-ins by group" hx-swap-oob="true"' in fresh.text


def test_a_waiting_sign_in_is_first_with_its_card_and_continue(web):
    web.sign_in()
    web.post("/signin", {"ship": "ship-01", "service": "claude"})
    flow = flow_of(web)
    wait_for(lambda: flow.state in (flows.LINK, flows.CODE))
    page = web.client.get("/signin").text
    assert _rows(page)[0] == ("ship-01", "claude")
    assert f'href="#flow-{flow.id}">Continue<' in page
    card = page[page.index(f'id="flow-{flow.id}"'):]  # its card, on this page
    card = card[:card.index("</article>")]
    assert "&amp;from=signin" in card and 'name="from" value="signin"' in card
    # a card goes with its row's group
    assert f'id="flow-{flow.id}"' in web.client.get("/signin?group=action").text
    for group in ("underway", "healthy"):
        assert f'id="flow-{flow.id}"' not in web.client.get(f"/signin?group={group}").text
    # its buttons come back here, not to the batch
    response = web.post(f"/flows/{flow.id}/cancel", {"from": "signin", "f_service": "claude"})
    assert response.headers["location"] == "/signin?service=claude"
    wait_for(lambda: flow.done)
    assert "Finished sign-ins (1)" in web.client.get("/signin").text


def test_sign_out_from_the_sign_ins_page_returns_to_it(web, monkeypatch):
    seen = []
    monkeypatch.setattr(webapp.actions, "logout", lambda app, name, service: seen.append(
        (name, service)) or webapp.actions.Outcome(name, True, "Signed out."))
    web.sign_in()
    page = web.client.get("/signin?ship=ship-01").text
    row = page[page.index('data-ship="ship-01" data-service="gcloud"'):]
    row = row[:row.index("</tr>")]
    assert 'action="/ships/ship-01/logout"' in row and "data-confirm-danger" in row
    assert 'name="f_ship" value="ship-01"' in row
    response = web.post("/ships/ship-01/logout", {"service": "gcloud", "confirm": "yes",
                                                   "from": "signin", "f_ship": "ship-01",
                                                   "f_group": "<script>"})
    assert seen == [("ship-01", "gcloud")]
    assert response.headers["location"] == "/signin?ship=ship-01"
    # without the tick box nothing is signed out
    web.post("/ships/ship-01/logout", {"service": "gcloud", "from": "signin"})
    assert len(seen) == 1


def test_a_batch_still_shows_only_its_own_cards(web):
    web.sign_in()
    response = web.post("/signin", {"ship": "ship-01", "service": "claude"})
    page = web.client.get(response.headers["location"]).text
    assert "Show every sign-in" in page and _rows(page) == []
    assert page.count('<article class="flow') == 1


def test_counts_per_group_stay_the_scopes_whichever_group_is_shown(web):
    web.sign_in()
    every = web.client.get("/signin").text
    healthy = web.client.get("/signin?group=healthy").text
    chips = r'group=([a-z]+)"[^>]*>[^<]+<span class="n">(\d+)<'
    assert re.findall(chips, every) == re.findall(chips, healthy)
    assert int(dict(re.findall(chips, healthy))["action"]) > 0
    assert set(g for g in _groups(healthy)) == {"healthy"}


def test_a_rows_sign_in_comes_back_to_the_same_view_at_its_card(web):
    web.sign_in()
    page = web.client.get("/signin?service=claude&group=action").text
    row = page[page.index('data-ship="ship-02" data-service="claude"'):]
    row = row[:row.index("</tr>")]
    sign_in = row[row.index('action="/signin"'):]  # the Sign in form, not Sign out's
    sign_in = sign_in[:sign_in.index("</form>")]
    assert 'name="from" value="signin"' in sign_in
    assert 'name="f_service" value="claude"' in sign_in
    assert 'name="f_group" value="action"' in sign_in
    response = web.post("/signin", {"ship": "ship-01", "service": "claude", "from": "signin",
                                    "f_service": "claude", "f_group": "action"})
    flow = flow_of(web)
    # the group is dropped: a sign-in just started is in no group's cards yet
    assert response.headers["location"] == f"/signin?service=claude#flow-{flow.id}"
    landing = web.client.get(response.headers["location"].split("#")[0]).text
    assert f'id="flow-{flow.id}"' in landing
    # refused (already under way): the same view, with why
    response = web.post("/signin", {"ship": "ship-01", "service": "claude", "from": "signin",
                                    "f_service": "claude"})
    assert response.headers["location"] == "/signin?service=claude"
    assert "already under way" in web.client.get("/signin?service=claude").text
    web.concierge.cancel_all()


def test_continue_under_a_group_filter_opens_the_view_with_the_card(web):
    web.sign_in()
    flow = WebFlow("1-00000000", "ab" * 6, "ship-01", "claude", "Claude Code", "paste-back",
                   state=flows.CODE, url="https://example.com/oauth")
    with web.concierge._lock:
        web.concierge._flows[flow.id] = flow
        web.concierge._order.append(flow.id)
    page = web.client.get("/signin?service=claude&group=action").text
    assert f'href="/signin?service=claude#flow-{flow.id}">Continue<' in page
    assert f'id="flow-{flow.id}"' in web.client.get("/signin?service=claude").text
    page = web.client.get("/signin?service=claude").text  # no group: the card is here
    assert f'href="#flow-{flow.id}">Continue<' in page


def test_a_timed_out_sign_in_says_so_and_offers_try_again(web):
    web.sign_in()
    flow = WebFlow("1-00000000", "ab" * 6, "ship-01", "gcloud-adc", "gcloud ADC", "paste-back",
                   state=flows.DONE, outcome="timeout", detail="Timed out.")
    with web.concierge._lock:
        web.concierge._flows[flow.id] = flow
        web.concierge._order.append(flow.id)
    page = web.client.get("/signin").text
    row = page[page.index('data-ship="ship-01" data-service="gcloud_adc"'):]
    row = row[:row.index("</tr>")]
    assert "The last sign-in attempt timed out." in row and ">Try again<" in row
    tr = re.search(r'<tr class="([^"]*)" data-ship="ship-01" data-service="gcloud_adc"', page)
    assert tr.group(1) == "lvl-notice" and "sev-tag lvl-notice" in row  # missing, unused


def test_a_ship_the_fleet_does_not_have_is_dropped_from_the_filters(web):
    web.sign_in()
    page = web.client.get("/signin?ship=ship-99&group=healthy").text
    assert "ship-99" not in page and "group=healthy" in page and _rows(page)


# --- herdr-core Ships -------------------------------------------------------------------


def _agents_table(page):
    table = page[page.index('<table class="agents">'):]
    return table[:table.index("</table>")]


def test_a_herdr_ships_agents_look_as_before(web):
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    table = _agents_table(page)
    assert "<th>Pane</th>" in table and "<th>Terminal</th>" not in table
    assert "w1:p2" in table and '<span class="quiet">for ' not in table
    assert "herdr 0.9.5, server running." in page and "herdr-core" not in page
    assert "starts again in the same pane and folder" in page
    settings = web.client.get("/ships/ship-01/settings").text
    assert "Agent runtime" not in settings


def test_a_core_ships_page_shows_its_runtime_terminals_and_what_agents_wait_for(web):
    from test_cli import RUNNING_CORE

    web.ship.status_override["ship-01"] = copy.deepcopy(RUNNING_CORE)
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/ships/ship-01").text
    table = _agents_table(page)
    assert "<th>Terminal</th>" in table and "<th>Pane</th>" not in table
    main = table[table.index("<td>main</td>"):]
    main = main[:main.index("</tr>")]
    assert ">waiting</span>" in main and "for approval: npm test" in main
    assert '<td class="quiet">t3</td>' in main
    assert "herdr-core 0.1.19, core running." in page and "herdr 0.1.19" not in page
    assert "starts again in a new terminal in the same folder" in page
    assert "the same pane" not in page
    # Its Update and Reboot dialogs do not promise what herdr does.
    assert "herdr saves" not in page and "If its herdr changed" not in page
    # The waiting alert quotes what Claude asked.
    assert "waiting for you to approve something: npm test" in page


def test_a_core_ship_never_offers_a_reboot_for_herdr(web):
    """A herdr-core Ship on older tooling may report herdr-restart-pending for another
    tool's herdr server: herdr is not its runtime, so no alert offers a reboot for
    it, and the fleet's Update dialog does not promise that Ship a reboot."""
    from test_cli import RUNNING_CORE

    core = copy.deepcopy(RUNNING_CORE)
    core["attention"] = [{"code": "herdr-restart-pending", "message": "herdr was updated."}]
    web.ship.status_override["ship-01"] = core
    classic = copy.deepcopy(RUNNING)
    classic["attention"] = [{"code": "herdr-restart-pending", "message": "herdr was updated."}]
    web.ship.status_override["ship-02"] = classic
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/").text
    assert 'data-key="ship-01/herdr-restart-pending/' not in page
    assert 'data-key="ship-02/herdr-restart-pending/' in page  # a herdr Ship: as before
    assert "A herdr Ship whose herdr changed reboots" in page
    assert "a herdr-core Ship is not rebooted" in page


def test_a_classic_herdr_ship_is_told_how_to_move_on_every_page(web):
    """runtime-deprecated shows in Needs attention and on the Ship's page, with the
    command that moves it (the web UI has no runtime switch); a core Ship has none."""
    from test_cli import RUNNING_CORE

    web.ship.status_override["ship-01"] = json.loads(
        (EXAMPLES / "ship-status" / "running-herdr.json").read_text())
    web.ship.status_override["ship-02"] = copy.deepcopy(RUNNING_CORE)
    web.fleet.poll()
    web.sign_in()
    command = '<code class="cmd">ship-fleet runtime ship-01 core</code>'
    for path in ("/", "/ships/ship-01"):
        page = web.client.get(path).text
        assert "Classic herdr is deprecated" in page, path
        assert command in page, path
        assert 'data-copy="ship-fleet runtime ship-01 core"' in page, path
    page = web.client.get("/ships/ship-02").text
    assert "Classic herdr is deprecated" not in page
    assert "ship-fleet runtime ship-02" not in page


def test_a_core_ships_settings_show_the_runtime_read_only(web):
    from test_cli import RUNNING_CORE

    web.ship.status_override["ship-01"] = copy.deepcopy(RUNNING_CORE)
    web.fleet.poll()
    web.sign_in()
    page = web.client.get("/ships/ship-01/settings").text
    note = page[page.index("Agent runtime"):]
    note = note[:note.index("</p>")]
    assert "herdr-core" in note and 'runtime = "core"' in note
    assert 'name="runtime"' not in page  # nothing to set here: the migration moves it
