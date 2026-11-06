"""The Inbox tab: the gate's one exception for a file's raw bytes, the spool, the job
that sends spooled files to the Ship, the list and Remove. Every request goes
through the real gate; the Ship is test_inbox's fake."""

from __future__ import annotations

import io
import re
from urllib.parse import quote

import pytest
from fleetledger.web import app as webapp
from fleetledger.web import inboxweb
from fleetledger.web.fleet import Fleet
from fleetledger.web.security import MAX_UPLOAD, Gate
from starlette.testclient import TestClient
from test_inbox import InboxShip, InboxSSH
from test_web import BASE, ORIGIN, PORT, wait_for

from fleetledger import cli

UPLOAD = "/ships/ship-01/inbox/upload"


@pytest.fixture
def web(store, fake_repo):
    side = InboxShip()
    app = cli.App(out=io.StringIO(), err=io.StringIO(), store_factory=lambda: store,
                  repo_factory=lambda: fake_repo, ssh_factory=lambda inv: InboxSSH(inv, side),
                  stdin_isatty=lambda: False, sleep=lambda s: None)
    gate = Gate(PORT)
    fleet = Fleet(app, interval=30)
    asgi = webapp.create_app(app, gate, fleet=fleet)
    fleet.poll()

    class Env:
        pass

    env = Env()
    env.side, env.gate = side, gate
    env.ui = asgi.app.state.ui
    env.client = TestClient(asgi, base_url=BASE, follow_redirects=False)

    def sign_in(client=None):
        client = client or env.client
        client.get(f"/login?token={gate.new_token()}")
        page = client.get("/")
        return re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)

    def upload(data: bytes, name: str, csrf=None, client=None, **headers):
        client = client or env.client
        head = {**ORIGIN, "Content-Type": "application/octet-stream",
                "X-CSRF-Token": env.csrf if csrf is None else csrf,
                "X-File-Name": quote(name), **headers}
        return client.post(UPLOAD, content=data, headers=head)

    def post(path, data, client=None):
        return (client or env.client).post(path, data={"csrf": env.csrf, **data}, headers=ORIGIN)

    env.csrf = sign_in()
    env.sign_in, env.upload, env.post = sign_in, upload, post
    return env


def flashes(env, path="/ships/ship-01/inbox"):
    return env.client.get(path).text


# --- the gate ------------------------------------------------------------------------


def test_an_upload_needs_the_csrf_header_not_a_form_field(web):
    r = web.upload(b"x", "a.txt", csrf="wrong")
    assert r.status_code == 403
    r = web.client.post(UPLOAD, content=b"x", headers={
        **ORIGIN, "Content-Type": "application/octet-stream"})
    assert r.status_code == 403  # no header: a body "csrf=..." is never read for an upload


@pytest.mark.parametrize("headers, status", [
    ({"Origin": "http://evil.example.invalid"}, 403),
    ({"Content-Type": "multipart/form-data; boundary=x"}, 415),
    ({"Content-Type": "application/x-www-form-urlencoded"}, 415),
    ({"Content-Length": str(MAX_UPLOAD + 1)}, 413),
])
def test_the_gate_refuses_an_odd_upload_before_reading_it(web, headers, status):
    r = web.upload(b"x", "a.txt", **headers)
    assert r.status_code == status
    assert web.ui.spool is None or not web.ui.spool._items


def test_an_upload_without_a_session_is_refused(web, store):
    other = TestClient(web.client.app, base_url=BASE)
    r = other.post(UPLOAD, content=b"x", headers={
        **ORIGIN, "Content-Type": "application/octet-stream", "X-CSRF-Token": web.csrf})
    assert r.status_code == 401


def test_other_posts_still_need_a_plain_form(web):
    r = web.client.post("/ships/ship-01/inbox/send", content=b"x", headers={
        **ORIGIN, "Content-Type": "application/octet-stream", "X-CSRF-Token": web.csrf})
    assert r.status_code == 415


# --- upload, then send ------------------------------------------------------------------


def test_drop_then_send_puts_the_files_in_the_ships_inbox(web):
    r1 = web.upload(b"%PDF report", "Q3 report.pdf")
    r2 = web.upload(b"hello", "../$(id).txt")
    assert r1.status_code == 200 and r2.status_code == 200
    one, two = r1.json(), r2.json()
    assert one["name"] == "Q3 report.pdf" and one["size"] == 11
    assert two["name"] == "$(id).txt" and two["stored_as"] == "_(id).txt"
    spooled = web.ui.spool._items[one["id"]].path
    assert spooled.read_bytes() == b"%PDF report"
    assert oct(spooled.stat().st_mode & 0o777) == "0o600"
    assert oct(spooled.parent.stat().st_mode & 0o777) == "0o700"

    r = web.post("/ships/ship-01/inbox/send", {"file": [one["id"], two["id"]], "tell": "yes"})
    assert r.status_code == 303 and r.headers["location"] == "/ships/ship-01/inbox"
    wait_for(lambda: web.ui.jobs.recent() and web.ui.jobs.recent()[0].done)
    job = web.ui.jobs.recent()[0]
    assert job.ok is True and job.kind == "inbox" and job.ships == ["ship-01"]
    assert job.title == "Send 2 files to ship-01's inbox"
    assert web.side.files["ship-01"] == {"Q3 report.pdf": b"%PDF report", "_(id).txt": b"hello"}
    assert [f["state"] for f in job.result["files"]] == ["stored", "stored"]
    assert job.result["files"][1]["stored"] == "_(id).txt"
    assert job.result["told"].startswith("told main")
    assert not spooled.exists() and web.ui.spool._items == {}

    card = web.client.get(f"/jobs/{job.id}").text
    assert "✓ in ~/inbox" in card and "as _(id).txt" in card
    assert "Autostart agents: told main" in card
    page = web.client.get("/ships/ship-01/inbox").text
    assert "Sending 2 files to ship-01" in page
    assert "Q3 report.pdf" in page and "_(id).txt" in page


def test_a_spooled_file_belongs_to_its_session_and_ship(web):
    sent = web.upload(b"x", "a.txt").json()["id"]
    other = TestClient(web.client.app, base_url=BASE, follow_redirects=False)
    csrf = web.sign_in(other)
    r = other.post("/ships/ship-02/inbox/send", data={"csrf": csrf, "file": sent},
                   headers=ORIGIN)
    assert r.status_code == 303
    r = other.post("/ships/ship-01/inbox/send", data={"csrf": csrf, "file": sent},
                   headers=ORIGIN)
    assert "no longer waiting to be sent" in other.get("/ships/ship-01/inbox").text
    assert web.ui.jobs.recent() == []
    assert sent in web.ui.spool._items  # still the first session's


def test_an_unknown_or_expired_upload_is_not_sent(web):
    sent = web.upload(b"x", "a.txt").json()["id"]
    web.ui.spool.clock = lambda: 10**12  # much later
    web.post("/ships/ship-01/inbox/send", {"file": sent})
    assert "no longer waiting to be sent" in flashes(web)
    assert web.ui.jobs.recent() == [] and web.ui.spool._items == {}


def test_a_cut_short_upload_keeps_nothing(web):
    r = web.upload(b"abc", "a.txt", **{"Content-Length": "10"})
    assert r.status_code in (400, 500) or r.json().get("error")
    assert web.ui.spool._items == {}


def test_too_many_files_waiting_is_refused(web, monkeypatch):
    monkeypatch.setattr("fleetledger.inbox.MAX_FILES", 2)
    assert web.upload(b"1", "a").status_code == 200
    assert web.upload(b"2", "b").status_code == 200
    r = web.upload(b"3", "c")
    assert r.status_code == 429 and "At most 2 files" in r.json()["error"]


def test_an_upload_to_a_busy_ship_is_refused_before_it_is_spooled(web):
    import threading

    release = threading.Event()
    web.ui.jobs.start("update", ["ship-01"], lambda job: release.wait(5))
    try:
        r = web.upload(b"x", "a.txt")
        assert r.status_code == 409 and "Something is running on ship-01" in r.json()["error"]
    finally:
        release.set()


def test_a_ship_without_the_inbox_fails_the_job_and_drops_the_spool(web):
    web.side.contract = False
    sent = web.upload(b"x", "a.txt").json()["id"]
    path = web.ui.spool._items[sent].path
    web.post("/ships/ship-01/inbox/send", {"file": sent})
    wait_for(lambda: web.ui.jobs.recent() and web.ui.jobs.recent()[0].done)
    job = web.ui.jobs.recent()[0]
    assert job.ok is False and "predates the file inbox" in job.error
    assert not path.exists()


def test_the_spool_starts_empty(tmp_path):
    root = tmp_path / "spool"
    root.mkdir()
    (root / "left-over").write_bytes(b"from an earlier run")
    spool = inboxweb.Spool(root)
    spool.new("s", "ship-01", "a.txt", 1)
    assert not (root / "left-over").exists()


# --- the tab --------------------------------------------------------------------------


def test_the_tab_lists_the_inbox_with_remove(web):
    web.side.files["ship-01"] = {"spec.pdf": b"x" * 2048}
    page = web.client.get("/ships/ship-01/inbox").text
    assert 'id="inbox-send"' in page and "Drop files for this Ship" in page
    assert f'data-inbox-max="{MAX_UPLOAD}"' in page
    assert "spec.pdf" in page and "2 KB" in page and "2026-01-02 03:04:05" in page
    assert 'action="/ships/ship-01/inbox/remove"' in page
    assert "<script" not in page.split("</head>")[1]  # no inline script (CSP)


def test_the_tab_says_when_the_ship_needs_an_update(web):
    web.side.contract = False
    page = web.client.get("/ships/ship-01/inbox").text
    assert "predates the file inbox" in page and "ship-fleet update ship-01" in page


def test_remove_needs_the_confirm_box_and_removes_the_exact_name(web):
    web.side.files["ship-01"] = {" spaced .txt": b"x", "keep.txt": b"y"}
    web.post("/ships/ship-01/inbox/remove", {"name": " spaced .txt"})
    assert "Tick the box to confirm." in flashes(web)
    assert " spaced .txt" in web.side.files["ship-01"]
    web.post("/ships/ship-01/inbox/remove", {"name": " spaced .txt", "confirm": "yes"})
    assert web.side.files["ship-01"] == {"keep.txt": b"y"}
    assert "removed  spaced .txt" in flashes(web)
