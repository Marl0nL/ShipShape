"""Adding an existing Ship from the browser (web/adopt.py): scan as a job, adopt in
two steps (the host checked before any keyscan, then "It matches"), and re-pin."""

from __future__ import annotations

from fleetledger.scan import Announcement
from fleetledger.seed import hostkey
from test_cli import RUNNING
from test_web import _action_bar, wait_for, web  # noqa: F401  (web is a fixture)

from fleetledger import ca


def new_key(name="ship-07"):
    key = hostkey.generate(name)
    return " ".join(key.public.split()[:2]), key.fingerprint


def last_job(web):  # noqa: F811
    wait_for(lambda: web.app_ui.jobs.recent() and web.app_ui.jobs.recent()[0].done)
    return web.app_ui.jobs.recent()[0]


def test_scan_runs_as_a_job_and_lists_what_it_heard(web):  # noqa: F811
    ship_01 = web.store.load().ship("ship-01")
    web.app.browse = lambda timeout: [
        Announcement("ship-01", "ship-01.local", 22, ("192.0.2.11",),
                     {"name": "ship-01", "id": ship_01.id}),
        Announcement("ship-09", "ship-09.local", 22, ("192.0.2.19",), {"name": "ship-09"}),
    ]
    web.sign_in()
    assert 'href="/new/existing"' in web.client.get("/new").text  # the second tab
    web.post("/new/scan")
    job = last_job(web)
    assert job.kind == "scan" and job.ok
    page = web.client.get("/new/existing").text
    assert '<a href="/ships/ship-01">ship-01</a>' in page  # known
    assert 'href="/new/existing?name=ship-09&amp;host=ship-09.local"' in page  # Adopt


def test_an_invalid_host_is_refused_before_any_keyscan(web):  # noqa: F811
    scanned = []
    web.app.keyscan = lambda host: scanned.append(host) or new_key()[0]
    web.sign_in()
    for host in ("-oProxyCommand=x", "a b", "x" * 254, "host;rm"):
        response = web.post("/new/existing/check", {"name": "ship-07", "host": host})
        assert response.status_code == 400 and "A host is a name or address" in response.text
    response = web.post("/new/existing/check", {"name": "ship-01", "host": "h.example"})
    assert "already in the inventory" in response.text
    response = web.post("/new/existing/check", {"name": "Bad", "host": "h.example"})
    assert "A Ship name" in response.text
    assert scanned == []


def test_adopt_shows_the_key_then_adopts_on_it_matches(web):  # noqa: F811
    key, fp = new_key()
    web.app.keyscan = lambda host: key
    web.ship.status_override["ship-07"] = RUNNING
    web.sign_in()
    page = web.post("/new/existing/check", {"name": "ship-07", "host": "192.0.2.17",
                                            "tags": "lab"}).text
    assert fp in page and "It matches: adopt ship-07" in page
    assert not web.store.load().has_ship("ship-07")  # nothing trusted yet
    response = web.post("/new/existing/adopt", {"name": "ship-07", "host": "192.0.2.17",
                                                "tags": "lab", "fingerprint": fp,
                                                "matches": "yes"})
    assert response.headers["location"] == "/"
    job = last_job(web)
    assert job.ok and job.title == "Adopt ship-07"
    ship = web.store.load().ship("ship-07")
    assert ship.host_key.startswith(key) and ship.enrolled and ship.tags == ("lab",)


def test_a_fingerprint_mismatch_is_refused(web):  # noqa: F811
    key, _ = new_key()
    _, other = new_key()
    web.app.keyscan = lambda host: key  # the key changed after you compared it
    web.ship.status_override["ship-07"] = RUNNING
    web.sign_in()
    web.post("/new/existing/adopt", {"name": "ship-07", "host": "192.0.2.17",
                                     "fingerprint": other, "matches": "yes"})
    job = last_job(web)
    assert not job.ok and any("not the fingerprint you gave" in line for line in job.snapshot())
    assert not web.store.load().has_ship("ship-07")
    web.post("/new/existing/adopt", {"name": "ship-07", "host": "192.0.2.17",
                                     "fingerprint": other})  # not "It matches"
    assert len(web.app_ui.jobs.recent()) == 1


def test_repin_shows_both_keys_and_revokes_the_old_one_with_a_ca(web, tmp_path):  # noqa: F811
    with web.store.edit() as ed:
        ed.doc["ca"] = {"host_key": str(tmp_path / "host_ca"),
                        "user_key": str(tmp_path / "user_ca")}
    old = web.store.load().ship("ship-01").host_key_type_and_blob
    key, fp = new_key("ship-01")
    web.app.keyscan = lambda host: key
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-01").text)
    assert 'action="/ships/ship-01/repin"' in bar[bar.index('<details class="menu">'):]
    page = web.post("/ships/ship-01/repin").text
    assert fp in page and ca.fingerprint(old) in page and "revoked" in page
    web.post("/ships/ship-01/repin/confirm", {"fingerprint": fp, "matches": "yes"})
    job = last_job(web)
    assert job.ok and job.kind == "repin"
    inv = web.store.load()
    assert inv.ship("ship-01").host_key.startswith(key)
    assert old in inv.doc["ca"].get("revoked_host_keys", [])


def test_repin_with_the_same_key_says_there_is_nothing_to_do(web):  # noqa: F811
    web.app.keyscan = lambda host: web.store.load().ship("ship-01").host_key_type_and_blob
    web.sign_in()
    page = web.post("/ships/ship-01/repin").text
    assert "nothing to re-pin" in page and "repin/confirm" not in page


def test_repin_says_so_when_the_pinned_key_cannot_be_read(web):  # noqa: F811
    with web.store.edit() as ed:
        ed.set_field("ship-01", "host_key", "ssh-ed25519 AAAAC3N ship-01")  # bad padding
    key, fp = new_key("ship-01")
    web.app.keyscan = lambda host: key
    web.sign_in()
    response = web.post("/ships/ship-01/repin")
    assert response.status_code == 200 and fp in response.text
    assert "a key FleetLedger cannot read" in response.text
