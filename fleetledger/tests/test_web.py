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
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fleetledger.concierge import audit, engine
from fleetledger.web import app as webapp
from fleetledger.web import flows, network, security, server
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


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


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
    clock = Clock()
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


def wait_for(predicate, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    raise AssertionError("timed out")


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
    web.clock.t += security.TOKEN_TTL + 1
    assert web.client.get(f"/login?token={token}").status_code == 403
    assert web.client.get("/").status_code == 401


def test_only_a_hash_of_the_token_is_kept(web):
    token = web.gate.new_token()
    assert token not in repr(web.gate.__dict__)


def test_session_lapses_when_idle(web):
    web.sign_in()
    web.clock.t += security.SESSION_IDLE + 1
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
    app = cli.App(out=out, err=out, store_factory=lambda: store)
    monkeypatch.setattr(server.sys.stdin, "isatty", lambda: False, raising=False)
    assert server.serve(app, port=0, open_browser=True, interval=3600) == 0
    assert opened and opened[0].startswith("file://") and "token" not in opened[0]
    assert seen_file["mode"] == 0o600
    assert "/login?token=" in seen_file["text"]
    # Printed too (this terminal only), for browsers that cannot read the file.
    assert "/login?token=" in out.getvalue() and "snap" in out.getvalue()
    assert not Path(opened[0][len("file://"):]).exists()  # removed on exit


def test_ui_command_is_wired(monkeypatch):
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
    clock = Clock()
    polls = []
    app = cli.App(store_factory=lambda: store, repo_factory=lambda: fake_repo)
    f = Fleet(app, interval=30, idle_after=120, clock=clock)
    f.poll = lambda names=None: polls.append(names) or setattr(f, "_last_poll", clock())
    assert f.active()
    clock.t += 121
    assert not f.active()
    f.touch()
    assert f.active() and f._due() is True
    f.poll()
    assert not f._due()
    clock.t += 31
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
])
def test_actions_on_a_ship_the_poller_does_not_know_are_404(web, path, data):
    """Every per-Ship action route checks the Ship exists (app._known_ship)."""
    web.sign_in()
    for name in ("nope", "BAD!"):  # well-formed but unknown; not a Ship name at all
        response = web.post(path.format(name), data)
        assert response.status_code == 404
        assert "No such Ship in the inventory." in response.text
    if path.endswith("/refresh") and "network" not in path:
        assert web.post(path.format("ship-01"), data).status_code == 303  # a known Ship works


def _action_bar(page):
    start = page.index('<section class="action-bar">')
    return page[start:page.index("</section>", start)]


@pytest.mark.parametrize("tab", ["", "/settings", "/logs", "/network"])
def test_every_ship_tab_has_the_action_bar_above_the_tabs(web, tab):
    web.sign_in()
    page = web.client.get(f"/ships/ship-01{tab}").text
    bar = _action_bar(page)
    assert page.index('class="action-bar"') < page.index('<nav class="tabs">')
    assert 'data-copy="ship-fleet ssh ship-01"' in bar  # this computer is not trusted here
    assert 'action="/ships/ship-01/refresh"' in bar  # Check now
    assert 'name="operation" value="update"' in bar and 'name="operation" value="reboot"' in bar
    assert 'class="ops"' not in page  # nothing left at the bottom of the Overview


def test_a_ship_that_does_not_answer_keeps_check_now_only(web):
    web.ship.down.add("ship-02")
    web.fleet.poll()
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-02").text)
    assert 'action="/ships/ship-02/refresh"' in bar
    assert 'name="operation"' not in bar  # no Update or Reboot for a Ship that is not there
    assert "It does not answer" in bar


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
    assert 'name="tag" value="work"' in page[menu:page.index('class="tiles')]


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
    monkeypatch.setattr(lifecycle, "resize", lambda app, store, name, size, restart=False:
                        calls.append((name, size, restart)) or ["Set 8 cores."])
    web.sign_in()
    text = web.client.get("/ships/ship-01/settings").text
    assert "never shrink" in text and 'name="disk_gb" type="number" min="40"' in text
    web.post("/ships/ship-01/size", {"cores": "8", "memory_gb": "8", "disk_gb": "40",
                                     "restart": "yes"})
    assert calls == [("ship-01", {"cores": 8}, True)]  # only what changed
    web.post("/ships/ship-01/size", {"cores": "4", "memory_gb": "8", "disk_gb": "40"})
    assert len(calls) == 1  # nothing changed: nothing done


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
    assert "Updates and reboots" in page and "converging to 0.2.0" in page
    # One at a time per Ship.
    web.post("/operations", {"operation": "reboot", "ship": "ship-01", "confirm": "yes"})
    assert "already running" in web.client.get("/").text
    release.set()
    wait_for(lambda: all(op.done for op in web.app_ui.operations.recent()))
    assert "updated to 0.2.0" in web.client.get("/partials/fleet").text
    web.post("/operations", {"operation": "reboot", "ship": "ship-01", "confirm": "yes"})
    wait_for(lambda: len(calls) == 2 and web.app_ui.operations.recent()[0].done)
    assert calls[1][:2] == ("reboot", ["ship-01"]) and calls[1][2]["wait"] is True
    page = web.client.get("/partials/fleet").text
    assert "did not come back" in page and "agent main did not come back" in page
    web.post("/operations", {"operation": "rm", "ship": "ship-01", "confirm": "yes"})
    assert len(calls) == 2


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


def test_waiting_sign_in_shows_in_needs_attention(web):
    web.sign_in()
    web.post("/signin", {"ship": "ship-01", "service": "claude"})
    flow = flow_of(web)
    wait_for(lambda: flow.state in (flows.LINK, flows.CODE))
    page = web.client.get("/partials/fleet").text
    assert "Claude Code sign-in is waiting for you." in page
    assert 'class="badge warn">1<' in web.client.get("/").text


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
    page = web.client.get("/ships/ship-01").text
    assert "ship-fleet ssh ship-01 --as owner" in page
