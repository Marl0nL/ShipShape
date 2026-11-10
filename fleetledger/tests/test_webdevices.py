"""Who may sign in to the remote web UI (web/devices.py, web/devicesweb.py and the gate).

Layered on test_webremote's simulated Tailscale Serve: only the Tailscale logins you
allowed get past the gate, and each browser then pairs once with a one-time link,
making a named device kept hashed on disk (so a restart keeps it), tied to its
login, with idle and absolute lifetimes, revocable, and fresh enough for danger
actions only for a few hours. The wall clock is a fake one, so lapses are instant.
"""

from __future__ import annotations

import io
import json
import re
import time

import pytest
from fl_harness import Clock
from fleetledger.concierge import audit
from fleetledger.errors import FleetError
from fleetledger.web import app as webapp
from fleetledger.web import devices as devices_mod
from fleetledger.web import remote
from fleetledger.web.devices import Devices
from fleetledger.web.fleet import Fleet
from fleetledger.web.remote import Remote
from fleetledger.web.security import Gate
from starlette.testclient import TestClient
from test_cli import FakeShip, ShipSSH
from test_web import BASE, ORIGIN, PORT
from test_webremote import LOGIN, NAME, ON, REMOTE, REMOTE_ORIGIN, SERVE, FakeTailscale

from fleetledger import cli

COOKIE = "__Host-fleetledger"
OTHER = "someone@example.com"


def csrf_of(page_text):
    return re.search(r'name="csrf-token" content="([^"]+)"', page_text).group(1)


@pytest.fixture
def web(store, fake_repo, home, monkeypatch):
    out = io.StringIO()
    app = cli.App(out=out, err=out, store_factory=lambda: store,
                  repo_factory=lambda: fake_repo,
                  ssh_factory=lambda inv: ShipSSH(inv, FakeShip()),
                  stdin_isatty=lambda: False, sleep=lambda s: None,
                  tailscale=FakeTailscale())
    remote.save(ON)
    wall = Clock()

    class OnTheWall(Devices):  # ship-fleet ui link/devices/revoke: the same clock
        def __init__(self, folder=None, wall_=None):
            super().__init__(folder, wall_ or wall)

    monkeypatch.setattr(devices_mod, "Devices", OnTheWall)

    class Env:
        pass

    env = Env()
    env.app, env.out, env.wall, env.home, env.store = app, out, wall, home, store

    def start(setting=ON):
        """A fresh `ship-fleet ui` run: a new gate and app over the same files, set up
        as server.serve does (who may use it follows remote-ui.json)."""
        if setting is not None:
            remote.save(setting)
        env.devices = Devices(wall=wall)
        env.gate = Gate(PORT, remote=setting, devices=env.devices,
                        allowed=remote.Allowed(setting) if setting else None)
        fleet = Fleet(app, interval=30)
        env.asgi = webapp.create_app(app, env.gate, fleet=fleet)
        fleet.poll()
        env.local = TestClient(env.asgi, base_url=BASE, follow_redirects=False)
        env.remote = client()

    def client(login=LOGIN, **headers):
        return TestClient(env.asgi, base_url=REMOTE, follow_redirects=False,
                          headers={**SERVE, "Tailscale-User-Login": login, **headers})

    def link(name=None):
        """`ship-fleet ui link`, as the owner would run it (over ssh, say)."""
        argv = ["ui", "link"] + (["--name", name] if name else [])
        said = io.StringIO()
        assert cli.main(argv, cli.App(out=said, err=said)) == 0, said.getvalue()
        return re.search(rf"{re.escape(REMOTE)}/login\?token=(\S+)", said.getvalue()).group(0)

    def pair(c=None, name="laptop"):
        c = c or env.remote
        response = c.get(link(name).removeprefix(REMOTE))
        assert response.status_code == 200, response.text
        page = c.get("/")
        assert page.status_code == 200, page.text
        return csrf_of(page.text)

    def sign_in_here():
        assert env.local.get(f"/login?token={env.gate.new_token()}").status_code == 200
        return csrf_of(env.local.get("/settings").text)

    env.start, env.client, env.link, env.pair, env.sign_in_here = (
        start, client, link, pair, sign_in_here)
    start()
    return env


# --- the allow-list -------------------------------------------------------------------


def test_a_login_you_did_not_allow_is_refused_before_anything_else(web):
    web.pair()
    cookie = web.remote.cookies[COOKIE]
    stranger = web.client(OTHER)
    for path in ("/static/app.css", "/", f"/login?token={web.gate.new_token()}",
                 web.link().removeprefix(REMOTE)):
        response = stranger.get(path, headers={"Cookie": f"{COOKIE}={cookie}"})
        assert response.status_code == 403, path
        assert "may not use this FleetLedger" in response.text
    # Changes too, with a session of an allowed login's.
    response = stranger.post("/ships/ship-01/refresh", headers={
        **REMOTE_ORIGIN, "Cookie": f"{COOKIE}={cookie}"})
    assert response.status_code == 403


def test_logins_match_whatever_their_case(web):
    shouting = web.client(LOGIN.upper())
    assert shouting.get("/static/app.css").status_code == 200
    web.pair(shouting)


def test_a_device_works_only_with_the_login_that_paired_it(web):
    web.start(Remote(REMOTE, PORT, (LOGIN, OTHER)))
    web.pair()
    cookie = {"Cookie": f"{COOKIE}={web.remote.cookies[COOKIE]}"}
    response = web.client(OTHER).get("/", headers=cookie)
    assert response.status_code == 401 and "Pair it once" in response.text
    assert web.client(LOGIN).get("/", headers=cookie).status_code == 200


def test_taking_a_login_off_the_list_locks_its_devices_out_at_once(web):
    web.pair()
    cookie = {"Cookie": f"{COOKIE}={web.remote.cookies[COOKIE]}"}
    out = io.StringIO()
    assert cli.main(["ui", "remote", "--login", OTHER], cli.App(out=out, err=out)) == 0
    assert remote.load().logins == (OTHER,) and "running" in out.getvalue()
    response = web.client(LOGIN).get("/", headers=cookie)  # the same run
    assert response.status_code == 403 and "may not use" in response.text
    assert web.client(OTHER).get("/static/app.css").status_code == 200  # added at once too
    # And its devices are revoked, not only refused: putting the login back does not
    # let them in again.
    assert "Signed out 1 device(s)" in out.getvalue() and web.devices.all() == []
    assert cli.main(["ui", "remote", "--login", LOGIN], cli.App(out=out, err=out)) == 0
    assert web.client(LOGIN).get("/", headers=cookie).status_code == 401
    assert [e["outcome"] for e in audit.read() if e["action"] == "ui-revoke"] == ["1 device(s)"]


def test_turning_it_on_again_with_other_logins_revokes_the_old_ones_devices(web):
    web.pair()
    out = io.StringIO()
    app = cli.App(out=out, err=out, tailscale=FakeTailscale())
    assert cli.main(["ui", "remote", REMOTE, "--port", str(PORT), "--login", OTHER], app) == 0
    assert "Signed out 1 device(s)" in out.getvalue() and web.devices.all() == []


def test_a_request_from_another_login_does_not_keep_a_device_alive(web):
    web.start(Remote(REMOTE, PORT, (LOGIN, OTHER)))
    web.pair()
    cookie = {"Cookie": f"{COOKIE}={web.remote.cookies[COOKIE]}"}
    web.wall.now += devices_mod.IDLE - 60
    assert web.client(OTHER).get("/", headers=cookie).status_code == 401
    web.wall.now += devices_mod.IDLE - 60  # its own last use: nearly 24 hours ago
    assert web.client(LOGIN).get("/", headers=cookie).status_code == 401


def test_another_logins_old_cookie_is_neither_replaced_nor_lends_its_name(web):
    web.start(Remote(REMOTE, PORT, (LOGIN, OTHER)))
    web.pair(name="laptop")
    cookie = {"Cookie": f"{COOKIE}={web.remote.cookies[COOKIE]}"}
    other = web.client(OTHER)
    assert other.get(web.link().removeprefix(REMOTE), headers=cookie).status_code == 200
    devices = {d.login: d for d in web.devices.all()}
    assert devices[LOGIN].name == "laptop" and devices[OTHER].name != "laptop"
    assert web.client(LOGIN).get("/", headers=cookie).status_code == 200  # still there


def test_a_wrong_token_never_opens_a_waiting_link(web):
    link = web.link().removeprefix(REMOTE)
    right = link.split("token=")[1]
    near = right[:-1] + ("B" if right[-1] == "A" else "A")  # one character off, never equal
    assert near != right
    for wrong in ("x" * 43, near, ""):
        assert web.client().get(f"/login?token={wrong}").status_code == 403
    assert web.devices.all() == []
    assert web.remote.get(link).status_code == 200  # the right one still works, once


def test_the_csrf_token_is_not_the_session_id_and_no_page_shows_the_id(web):
    csrf = web.pair()
    session_id = web.remote.cookies[COOKIE]
    assert csrf != session_id and session_id not in csrf
    for path in ("/", "/settings", "/ships/ship-01"):
        assert session_id not in web.remote.get(path).text, path


def test_device_times_say_their_zone(web):
    web.pair(name="laptop")
    section = web.remote.get("/settings").text
    section = section[section.index('id="devices"'):section.index('id="images"')]
    assert re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d UTC", section)
    assert re.search(r"paired \d{4}-\d\d-\d\d \d\d:\d\d UTC", run(["ui", "devices"])[1])


@pytest.mark.parametrize("change", [
    lambda home: run(["ui", "remote", "--off"]),
    lambda home: (home / "config" / remote.REMOTE_FILE).write_text("{broken"),
    lambda home: remote.save(Remote(REMOTE, PORT + 1, (LOGIN,))),
    lambda home: remote.save(Remote(REMOTE + ":8443", PORT, (LOGIN,))),
])
def test_a_running_ui_refuses_the_remote_address_once_its_setting_changes(web, change):
    web.pair()
    assert web.remote.get("/").status_code == 200
    change(web.home)
    assert web.remote.get("/").status_code == 403
    assert web.remote.get("/static/app.css").status_code == 403
    assert web.local.get("/static/app.css").status_code == 200  # this computer: as ever


def test_nobody_may_sign_in_remotely_without_an_allowed_login(home, store):
    (home / "config" / remote.REMOTE_FILE).write_text(
        json.dumps({"origin": REMOTE, "port": PORT}))  # as piece 1 wrote it
    assert remote.load().logins == ()
    with pytest.raises(FleetError, match="Nobody may use"):
        remote.for_ui(cli.App(tailscale=FakeTailscale()), port=0, local=False)
    out = io.StringIO()
    assert cli.main(["ui", "link"], cli.App(out=out, err=out)) != 0
    assert "Nobody may sign in" in out.getvalue()
    gate = Gate(PORT, remote=Remote(REMOTE, PORT), devices=Devices())
    asgi = webapp.create_app(cli.App(out=io.StringIO(), store_factory=lambda: store), gate)
    client = TestClient(asgi, base_url=REMOTE, headers=SERVE)
    assert client.get(f"/login?token={gate.new_token()}").status_code == 403


def test_turning_it_on_needs_a_login(home):
    out = io.StringIO()
    app = cli.App(out=out, err=out, tailscale=FakeTailscale())
    assert cli.main(["ui", "remote", REMOTE, "--port", str(PORT)], app) != 0
    assert "--login" in out.getvalue() and remote.load() is None
    assert cli.main(["ui", "remote", REMOTE, "--port", str(PORT), "--login", "Not A Login"],
                    app) != 0
    assert remote.load() is None
    assert cli.main(["ui", "remote", "--login", LOGIN], app) != 0  # off: nothing to change
    assert "off" in out.getvalue() and remote.load() is None
    assert cli.main(["ui", "remote", REMOTE, "--port", str(PORT), "--login", LOGIN,
                     "--login", f" {OTHER.upper()} ", "--login", LOGIN], app) == 0
    assert remote.load().logins == (LOGIN, OTHER)


@pytest.mark.parametrize("text", ["you", "@example.com", "you@", "you@-example.invalid",
                                  "you@exa mple.com", "*@example.com", "you@example..com",
                                  "a" * 65 + "@example.com", ""])
def test_anything_but_a_login_is_refused(text):
    with pytest.raises(FleetError):
        remote.parse_login(text)


def test_a_login_list_fleetledger_did_not_write_is_refused(home):
    from fleetledger.errors import ConfigError

    for logins in (["*"], [LOGIN.upper()], [LOGIN, LOGIN], [1], "you@example.com"):
        (home / "config" / remote.REMOTE_FILE).write_text(
            json.dumps({"origin": REMOTE, "port": PORT, "logins": logins}))
        with pytest.raises(ConfigError):
            remote.load()


# --- pairing ----------------------------------------------------------------------------


def test_a_pairing_link_makes_a_named_device_kept_only_as_a_hash(web):
    link = web.link("laptop")
    token = link.split("token=")[1]
    response = web.remote.get(link.removeprefix(REMOTE))
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    for flag in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/",
                 f"Max-Age={devices_mod.ABSOLUTE}"):
        assert flag in cookie
    session_id = web.remote.cookies[COOKIE]
    [device] = web.devices.all()
    assert (device.name, device.login, device.origin, device.how) == (
        "laptop", LOGIN, REMOTE, devices_mod.FROM_LINK)
    folder = web.home / "data" / "web"
    assert folder.stat().st_mode & 0o777 == 0o700
    for f in (web.devices.devices_file, web.devices.pairings_file):
        assert f.stat().st_mode & 0o777 == 0o600
        text = f.read_text()
        assert session_id not in text and token not in text
    assert device.key == devices_mod.digest(session_id)
    assert devices_mod.csrf_for(session_id) not in web.devices.devices_file.read_text()
    assert "Signed in as" not in web.remote.get("/").text  # just the page
    entries = audit.read()
    assert [e["action"] for e in entries] == ["ui-link", "ui-pair"]
    assert entries[-1]["account"] == LOGIN
    assert token not in json.dumps(entries) and session_id not in json.dumps(entries)


def test_a_pairing_link_works_once_and_not_after_15_minutes(web):
    link = web.link().removeprefix(REMOTE)
    assert web.remote.get(link).status_code == 200
    again = web.client().get(link)
    assert again.status_code == 403 and "ship-fleet ui link" in again.text
    late = web.link().removeprefix(REMOTE)
    web.wall.now += devices_mod.PAIR_TTL + 1
    assert web.client().get(late).status_code == 403
    assert len(web.devices.all()) == 1


def test_a_pairing_link_is_for_the_remote_address_only(web):
    link = web.link().removeprefix(REMOTE)
    assert web.local.get(link).status_code == 403
    assert web.devices.all() == []
    assert web.remote.get(link).status_code == 200


def test_the_terminals_link_pairs_a_device_named_after_its_browser(web):
    phone = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0)"}
    assert web.remote.get(f"/login?token={web.gate.new_token()}",
                          headers=phone).status_code == 200
    [device] = web.devices.all()
    assert device.name == "iPhone" and device.how == devices_mod.FROM_TERMINAL


def test_a_device_stays_signed_in_across_a_restart(web):
    web.pair()
    cookie = {"Cookie": f"{COOKIE}={web.remote.cookies[COOKIE]}"}
    web.start()  # ship-fleet ui stopped and started again
    assert web.client().get("/", headers=cookie).status_code == 200
    # A session on this computer does not: it lives in memory, as before.
    local = web.local
    assert local.get(f"/login?token={web.gate.new_token()}").status_code == 200
    local_cookie = {"Cookie": f"fleetledger-{PORT}={local.cookies[f'fleetledger-{PORT}']}"}
    web.start()
    assert web.local.get("/", headers=local_cookie).status_code == 401


def test_a_device_lapses_when_idle_and_after_30_days(web):
    web.pair()
    web.wall.now += devices_mod.IDLE - 60
    assert web.remote.get("/").status_code == 200  # used: idle starts again
    web.wall.now += devices_mod.IDLE - 60
    assert web.remote.get("/").status_code == 200
    web.wall.now += devices_mod.IDLE + 1
    response = web.remote.get("/")
    assert response.status_code == 401 and "Pair it once" in response.text
    assert web.devices.all() == []
    # However busy, 30 days after pairing it lapses.
    web.pair()
    for _ in range(devices_mod.ABSOLUTE // (devices_mod.IDLE - 60)):
        web.wall.now += devices_mod.IDLE - 60
        web.remote.get("/static/app.css")
        web.remote.get("/")
    web.wall.now += devices_mod.IDLE - 60
    assert web.remote.get("/").status_code == 401


def test_last_use_is_written_down_so_a_restart_keeps_the_idle_clock(web):
    web.pair()
    cookie = {"Cookie": f"{COOKIE}={web.remote.cookies[COOKIE]}"}
    web.wall.now += devices_mod.IDLE - 60
    assert web.remote.get("/").status_code == 200
    web.start()
    web.wall.now += devices_mod.IDLE - 60
    assert web.client().get("/", headers=cookie).status_code == 200


def test_pairing_again_in_the_same_browser_replaces_its_device(web):
    web.pair(name="laptop")
    old = web.remote.cookies[COOKIE]
    response = web.remote.get(web.link().removeprefix(REMOTE))  # no name: keeps "laptop"
    assert response.status_code == 200
    [device] = web.devices.all()
    assert device.name == "laptop" and device.key != devices_mod.digest(old)
    assert web.client().get("/", headers={"Cookie": f"{COOKIE}={old}"}).status_code == 401
    assert [e["outcome"] for e in audit.read() if e["action"] == "ui-pair"] == ["ok", "again"]


def test_too_many_devices_are_refused(web, monkeypatch):
    monkeypatch.setattr(devices_mod, "MAX_DEVICES", 2)
    web.pair(web.client(), "one")
    web.pair(web.client(), "two")
    response = web.client().get(web.link().removeprefix(REMOTE))
    assert response.status_code == 403 and "revoke some" in response.text


def test_too_many_waiting_links_are_refused(web, monkeypatch):
    monkeypatch.setattr(devices_mod, "MAX_PAIRINGS", 2)
    web.link(), web.link()
    out = io.StringIO()
    assert cli.main(["ui", "link"], cli.App(out=out, err=out)) != 0
    assert "already waiting" in out.getvalue()


def _device_doc(**change):
    item = {"key": "a" * 64, "name": "laptop", "login": LOGIN, "origin": REMOTE,
            "paired": time.time(), "seen": time.time(), "how": "x", "machine": None}
    return json.dumps({"schema": 1, "items": [{**item, **change}]})


@pytest.mark.parametrize("damage", [
    lambda f: f.write_text("not json"),
    lambda f: f.write_text(_device_doc(paired="yesterday")),
    lambda f: f.write_text(_device_doc(seen=True)),
    lambda f: f.write_text(_device_doc(name=None)),
    lambda f: f.write_text(_device_doc(login=["x"])),
    lambda f: f.write_text(_device_doc(machine=7)),
    lambda f: f.write_text(json.dumps({"schema": 2, "items": []})),
    lambda f: f.write_text(json.dumps({"schema": 1, "items": [{"key": "zz"}]})),
    lambda f: f.write_text(json.dumps({"schema": 1, "items": [{"key": "0" * 64,
                                                                "extra": 1}]})),
])
def test_a_devices_file_that_cannot_be_read_signs_nobody_in(web, damage):
    web.pair()
    damage(web.devices.devices_file)
    assert web.remote.get("/").status_code == 401
    assert web.devices.all() == []
    assert run(["ui", "devices"])[0] == 0  # and lists nothing, without failing


def test_a_record_as_fleetledger_writes_it_is_read_back():
    assert Devices._read is not None
    record = json.loads(_device_doc())["items"][0]
    assert devices_mod._typed(devices_mod.Device(**record))


def test_one_devices_csrf_token_does_not_serve_another(web):
    laptop, phone = web.client(), web.client()
    laptop_csrf = web.pair(laptop, "laptop")
    web.pair(phone, "phone")
    path = "/ships/ship-01/refresh"
    assert phone.post(path, data={"csrf": laptop_csrf},
                      headers=REMOTE_ORIGIN).status_code == 403
    assert laptop.post(path, data={"csrf": laptop_csrf},
                       headers=REMOTE_ORIGIN).status_code == 303


def test_the_live_terminal_socket_needs_an_allowed_login_too(web):
    from starlette.websockets import WebSocketDisconnect

    csrf = web.pair()
    cookie = {"Cookie": f"{COOKIE}={web.remote.cookies[COOKIE]}", **REMOTE_ORIGIN}
    url = f"wss://{NAME}/flows/1-00000000/ws"
    with pytest.raises(WebSocketDisconnect):
        with web.client(OTHER).websocket_connect(url, headers=cookie) as ws:
            ws.send_text(json.dumps({"csrf": csrf}))
            ws.receive_text()
    with web.remote.websocket_connect(url, headers=cookie) as ws:
        ws.send_text(json.dumps({"csrf": csrf}))
        assert json.loads(ws.receive_text()) == {"ended": True}


# --- danger actions need a fresh pairing ---------------------------------------------------


@pytest.mark.parametrize("path", [
    "/ships/ship-01/destroy", "/ships/ship-01/forget", "/ships/ship-01/snapshots/rollback",
    "/ships/ship-01/snapshots/delete", "/ships/ship-01/repin/confirm",
    "/settings/machines/trust", "/settings/machines/untrust", "/settings/machines/scope",
    "/settings/images/remove", "/settings/devices/pair", "/setup/fleet/ca",
    "/setup/keys/load", "/setup/proxmox/token",
])
def test_danger_actions_from_a_device_paired_hours_ago_are_refused(web, path):
    assert devices_mod.needs_fresh(path)
    csrf = web.pair()
    web.wall.now += devices_mod.FRESH + 1
    web.remote.get("/")  # still signed in for everything else
    response = web.remote.post(path, data={"csrf": csrf}, headers=REMOTE_ORIGIN)
    assert response.status_code == 403
    assert "fresh pairing" in response.text and "ship-fleet ui link" in response.text


@pytest.mark.parametrize("path", ["/ships/ship-01/refresh", "/operations", "/signin",
                                  "/ships/ship-01/power", "/settings/devices/revoke",
                                  "/settings/devices/rename"])
def test_everyday_actions_are_not_held_to_it(path):
    assert not devices_mod.needs_fresh(path)


def test_a_fresh_device_and_this_computer_pass_the_check(web):
    csrf = web.pair()
    path = "/ships/ship-01/destroy"  # without the typed name: the page says so
    response = web.remote.post(path, data={"csrf": csrf}, headers=REMOTE_ORIGIN)
    assert response.status_code == 303
    web.wall.now += devices_mod.FRESH + 1
    assert web.remote.post(path, data={"csrf": csrf}, headers=REMOTE_ORIGIN).status_code == 403
    # Pairing again refreshes it.
    csrf = web.pair()
    assert web.remote.post(path, data={"csrf": csrf}, headers=REMOTE_ORIGIN).status_code == 303
    here = web.sign_in_here()
    web.wall.now += 30 * 24 * 3600
    assert web.local.post(path, data={"csrf": here}, headers=ORIGIN).status_code == 303


# --- Settings ▸ Devices ------------------------------------------------------------------------


def test_settings_lists_the_devices_and_marks_this_browser(web):
    web.pair(web.client(), "phone")
    web.pair(name="laptop")
    page = web.remote.get("/settings").text
    assert 'id="devices"' in page and REMOTE in page and LOGIN in page
    assert re.search(r"laptop <span class=\"quiet\">\(this browser\)</span>", page)
    assert "phone <span" not in page
    assert "Sign out every other device" in page
    assert "laptop, remote" in page  # the header says where this browser is
    here = web.sign_in_here()
    assert here and "this computer, and your devices" in web.local.get("/settings").text
    assert "Sign out every device" in web.local.get("/settings").text


def test_settings_says_how_to_turn_it_on_when_it_is_off(web):
    (web.home / "config" / remote.REMOTE_FILE).unlink()
    web.start(None)
    web.sign_in_here()
    page = web.local.get("/settings").text
    assert "The web UI is for this computer only" in page
    assert "Pair a device" not in page and "this computer only" in page


def test_pairing_a_phone_from_this_computer_shows_the_link_once_with_a_qr_code(web):
    csrf = web.sign_in_here()
    response = web.local.post("/settings/devices/pair", data={"csrf": csrf, "name": "phone"},
                              headers=ORIGIN)
    assert response.status_code == 200
    link = re.search(rf"{re.escape(REMOTE)}/login\?token=[A-Za-z0-9_-]+", response.text).group(0)
    assert '<svg class="qr"' in response.text
    path = re.search(r'<path fill="#000" d="([^"]+)"', response.text).group(1)
    assert re.fullmatch(r"[Mhvz0-9 -]+", path)
    assert link not in web.local.get("/settings").text  # never shown again
    phone = web.client()
    assert phone.get(link.removeprefix(REMOTE)).status_code == 200
    [device] = web.devices.all()
    assert device.name == "phone" and device.how == devices_mod.FROM_HERE


def test_pairing_from_a_device_says_which(web):
    csrf = web.pair(name="laptop")
    response = web.remote.post("/settings/devices/pair", data={"csrf": csrf},
                               headers=REMOTE_ORIGIN)
    assert response.status_code == 200
    link = re.search(rf"{re.escape(REMOTE)}/login\?token=[A-Za-z0-9_-]+", response.text).group(0)
    assert web.client().get(link.removeprefix(REMOTE)).status_code == 200
    assert {d.how for d in web.devices.all()} == {devices_mod.FROM_LINK, "laptop"}


def test_a_bad_device_name_is_refused(web):
    csrf = web.sign_in_here()
    response = web.local.post("/settings/devices/pair", data={"csrf": csrf, "name": "<b>"},
                              headers=ORIGIN)
    assert response.status_code == 303
    assert "device name" in web.local.get(response.headers["location"]).text
    out = io.StringIO()
    assert cli.main(["ui", "link", "--name", "x" * 41], cli.App(out=out, err=out)) != 0


def test_revoke_from_settings_signs_a_device_out_at_once(web):
    phone = web.client()
    web.pair(phone, "phone")
    csrf = web.pair(name="laptop")
    key = next(d.key for d in web.devices.all() if d.name == "phone")
    response = web.remote.post("/settings/devices/revoke", data={"csrf": csrf, "device": key},
                               headers=REMOTE_ORIGIN)
    assert response.status_code == 303 and len(web.devices.all()) == 2  # not confirmed
    response = web.remote.post("/settings/devices/revoke",
                               data={"csrf": csrf, "device": key, "confirm": "yes"},
                               headers=REMOTE_ORIGIN)
    assert response.status_code == 303
    assert "Signed out phone" in web.remote.get(response.headers["location"]).text
    assert phone.get("/").status_code == 401
    assert [e["action"] for e in audit.read()][-1] == "ui-revoke"


def test_revoking_this_browser_signs_it_out(web):
    csrf = web.pair(name="laptop")
    [device] = web.devices.all()
    response = web.remote.post("/settings/devices/revoke",
                               data={"csrf": csrf, "device": device.key, "confirm": "yes"},
                               headers=REMOTE_ORIGIN)
    assert response.status_code == 200 and "signed out" in response.text
    assert web.remote.get("/").status_code == 401


def test_sign_out_every_other_device_keeps_this_one(web):
    phone, tablet = web.client(), web.client()
    web.pair(phone, "phone")
    web.pair(tablet, "tablet")
    csrf = web.pair(name="laptop")
    response = web.remote.post("/settings/devices/revoke-others",
                               data={"csrf": csrf, "confirm": "yes"}, headers=REMOTE_ORIGIN)
    assert response.status_code == 303
    assert [d.name for d in web.devices.all()] == ["laptop"]
    assert phone.get("/").status_code == 401 and tablet.get("/").status_code == 401
    assert web.remote.get("/").status_code == 200


def test_rename_a_device(web):
    csrf = web.pair(name="laptop")
    [device] = web.devices.all()
    data = {"csrf": csrf, "device": device.key, "name": "work laptop"}
    assert web.remote.post("/settings/devices/rename", data=data,
                           headers=REMOTE_ORIGIN).status_code == 303
    assert web.devices.all()[0].name == "work laptop"
    data["name"] = "<script>"
    web.remote.post("/settings/devices/rename", data=data, headers=REMOTE_ORIGIN)
    assert web.devices.all()[0].name == "work laptop"


# --- ship-fleet ui link / devices / revoke ----------------------------------------------------


def run(argv):
    out = io.StringIO()
    return cli.main(argv, cli.App(out=out, err=out)), out.getvalue()


def test_link_refuses_while_the_remote_ui_is_off(home):
    code, said = run(["ui", "link"])
    assert code != 0 and "off" in said and "remote-ui.md" in said


def test_link_can_print_a_qr_code(web):
    code, said = run(["ui", "link", "--name", "phone", "--qr"])
    assert code == 0 and "Pairing link for phone" in said
    assert "█" in said or "▀" in said  # segno's compact terminal QR


def test_devices_and_revoke_from_the_command_line(web):
    code, said = run(["ui", "devices"])
    assert code == 0 and "No device" in said
    web.pair(web.client(), "phone")
    web.pair(web.client(), "phone")
    web.pair(name="laptop")
    code, said = run(["ui", "devices"])
    assert code == 0 and said.count("phone") == 2 and LOGIN in said
    assert "ship-fleet ui link" in said  # where each was paired from
    code, said = run(["ui", "revoke", "phone"])
    assert code != 0 and "2 devices are called" in said
    code, said = run(["ui", "revoke", "nothing"])
    assert code != 0 and "No paired device" in said
    code, said = run(["ui", "revoke"])
    assert code != 0 and "--all" in said
    laptop = next(d for d in web.devices.all() if d.name == "laptop")
    code, said = run(["ui", "revoke", laptop.short])
    assert code == 0 and "Signed out laptop" in said
    assert web.remote.get("/").status_code == 401  # the running UI sees it at once
    code, said = run(["ui", "revoke", "--all", "phone"])
    assert code != 0
    code, said = run(["ui", "revoke", "--all"])
    assert code == 0 and "Signed out 2" in said and web.devices.all() == []


def test_devices_says_which_need_pairing_again_for_danger_actions(web):
    web.pair(name="laptop")
    code, said = run(["ui", "devices"])
    assert code == 0 and "needs pairing again" not in said
    web.wall.now += devices_mod.FRESH + 1
    code, said = run(["ui", "devices"])
    assert code == 0 and "needs pairing again" in said


def test_the_qr_code_draws_only_dark_squares():
    qr = devices_mod.qr_code(f"{REMOTE}/login?token=abc")
    assert qr.size >= 21 + 8
    assert re.fullmatch(r"(M\d+ \d+h\d+v1h-\d+z)+", qr.path)


def test_the_qr_code_draws_the_same_squares_as_segnos_own_svg():
    import segno

    text = f"{REMOTE}/login?token=" + "x" * 43
    mine = set()
    for x, y, n in re.findall(r"M(\d+) (\d+)h(\d+)", devices_mod.qr_code(text).path):
        mine |= {(int(x) + i, int(y)) for i in range(int(n))}
    # segno draws each row as stroked runs at y + 0.5, with relative moves between them.
    svg = segno.make(text, error="m").svg_inline(scale=1, border=4)
    path = re.search(r'class="qrline"[^>]* d="([^"]+)"', svg).group(1)
    theirs, x, y = set(), 0.0, 0.0
    for op, a, b in re.findall(r"([Mmh])(-?[\d.]+)(?: (-?[\d.]+))?", path):
        if op == "M":
            x, y = float(a), float(b)
        elif op == "m":
            x, y = x + float(a), y + float(b)
        else:
            theirs |= {(int(x) + i, int(y - 0.5)) for i in range(int(a))}
            x += float(a)
    assert mine == theirs and mine
