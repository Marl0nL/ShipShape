"""Destroy and Forget in the web UI (web/destroy.py): `ship-fleet destroy` as a job,
only after typing the Ship's name, never while something else runs on it."""

from __future__ import annotations

import threading
import time

from fleetledger.concierge import audit
from fleetledger.drivers import get_driver
from test_cli import FakeDriver
from test_web import _action_bar, wait_for, web  # noqa: F401  (web is a fixture)


def on_a_fake_hypervisor(web, ship="ship-02"):  # noqa: F811
    driver = FakeDriver()
    with web.store.edit() as ed:
        ed.set_field(ship, "driver", "proxmox")
        ed.set_field(ship, "driver_ref", {"vmid": 9102})
    web.app.driver_factory = lambda inv, d, **kw: (
        driver if d == "proxmox" else get_driver(inv, d, **kw))
    web.fleet.poll()
    return driver


def last_job(web):  # noqa: F811
    wait_for(lambda: web.app_ui.jobs.recent() and web.app_ui.jobs.recent()[0].done)
    return web.app_ui.jobs.recent()[0]


def _form(page, action):
    start = page.rindex("<form", 0, page.index(f'action="{action}"') + len(action) + 10)
    return page[start:page.index("</form>", start)]


def test_destroy_is_offered_typed_under_more_and_in_the_danger_zone(web):  # noqa: F811
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-01").text)
    menu = bar[bar.index('<details class="menu">'):]
    form = _form(menu, "/ships/ship-01/destroy")
    assert 'data-confirm-type="ship-01"' in form and "data-confirm-danger" in form
    assert 'name="confirm_name"' in form and "data-confirm-typed" in form
    assert 'name="action"' not in form  # the dialog script owns that name
    settings = web.client.get("/ships/ship-01/settings").text
    zone = settings[settings.index('class="danger-zone"'):]
    assert 'action="/ships/ship-01/destroy"' in zone and 'action="/ships/ship-01/forget"' in zone
    assert 'data-confirm-type="ship-01"' in _form(zone, "/ships/ship-01/forget")


def test_a_wrong_or_missing_name_destroys_nothing(web):  # noqa: F811
    driver = on_a_fake_hypervisor(web)
    web.sign_in()
    for typed in ({}, {"confirm_name": "ship-01"}, {"confirm_name": "yes"},
                  {"confirm_name": "SHIP-02"}):
        response = web.post("/ships/ship-02/destroy", typed)
        assert response.status_code == 303
        assert "type ship-02 to confirm" in web.client.get("/ships/ship-02").text
    assert web.app_ui.jobs.recent() == [] and driver.destroyed == []
    assert web.store.load().has_ship("ship-02")


def test_the_right_name_destroys_it_once_and_the_page_says_so(web):  # noqa: F811
    driver = on_a_fake_hypervisor(web)
    web.sign_in()
    response = web.post("/ships/ship-02/destroy", {"confirm_name": "ship-02"})
    assert response.headers["location"] == "/ships/ship-02"
    job = last_job(web)
    assert job.ok and job.kind == "destroy" and job.result["gone"]
    assert driver.destroyed == ["ship-02"]  # exactly once
    assert not web.store.load().has_ship("ship-02")
    assert ("ship-02", "destroy", "proxmox", "ok") in [
        (e["ship"], e["action"], e["service"], e["outcome"]) for e in audit.read()]
    from_ship = {"HX-Current-URL": "http://127.0.0.1:8765/ships/ship-02/settings"}
    card = web.client.get(f"/jobs/{job.id}", headers=from_ship)
    assert card.headers.get("HX-Redirect") == "/"  # its page is gone: to the Fleet page
    from_fleet = {"HX-Current-URL": "http://127.0.0.1:8765/?tag=work"}
    card = web.client.get(f"/jobs/{job.id}", headers=from_fleet)
    assert "HX-Redirect" not in card.headers  # the Fleet page keeps its filter
    gone = web.client.get("/ships/ship-02")
    assert gone.status_code == 410 and "ship-02 was destroyed at" in gone.text
    web.app_ui.destroyed["ship-02"] = (time.time() - 3700, False)  # an hour later: not found
    assert web.client.get("/ships/ship-02").status_code == 404


def test_destroy_waits_for_other_jobs_and_open_sign_ins(web, monkeypatch):  # noqa: F811
    driver = on_a_fake_hypervisor(web)
    web.sign_in()
    release = threading.Event()
    web.app_ui.jobs.start("snapshot", ["ship-02"], lambda job: release.wait(10))
    web.post("/ships/ship-02/destroy", {"confirm_name": "ship-02"})
    assert "already running" in web.client.get("/ships/ship-02").text
    release.set()
    wait_for(lambda: web.app_ui.jobs.recent()[0].done)

    class Flow:
        ship, needs_you = "ship-02", False

        def cancel(self):
            pass

    monkeypatch.setattr(web.concierge, "active", lambda: [Flow()])
    web.post("/ships/ship-02/destroy", {"confirm_name": "ship-02"})
    assert "a sign-in is open on ship-02" in web.client.get("/ships/ship-02").text
    assert driver.destroyed == [] and len(web.app_ui.jobs.recent()) == 1


def test_the_manual_driver_gives_steps_then_forget_removes_it(web):  # noqa: F811
    web.sign_in()
    web.post("/ships/ship-01/destroy", {"confirm_name": "ship-01"})
    job = last_job(web)
    assert job.ok and job.result["by_hand"] and not job.result["gone"]
    assert web.store.load().has_ship("ship-01")  # until you have run the steps
    card = web.client.get(f"/jobs/{job.id}").text
    assert "Do this on your hypervisor" in card and "HX-Redirect" not in card
    forget = _form(card, "/ships/ship-01/forget")
    assert 'data-confirm-type="ship-01"' in forget
    web.post("/ships/ship-01/forget", {"confirm_name": "nope"})
    assert web.store.load().has_ship("ship-01")
    web.post("/ships/ship-01/forget", {"confirm_name": "ship-01"})
    job = last_job(web)
    assert job.kind == "forget" and job.ok and job.result["gone"]
    assert not web.store.load().has_ship("ship-01")
    first = web.app_ui.jobs.recent()[1]  # the destroy's card: nothing left to forget
    assert 'action="/ships/ship-01/forget"' not in web.client.get(f"/jobs/{first.id}").text
    gone = web.client.get("/ships/ship-01")
    assert gone.status_code == 410 and "was forgotten at" in gone.text
    assert "its VM was not touched" in gone.text


def test_a_failed_destroy_can_still_be_forgotten(web):  # noqa: F811
    """A create cut short leaves a Ship with no VM id: destroy fails, forget cleans up."""
    driver = on_a_fake_hypervisor(web)

    def broken(ship):
        raise RuntimeError("no such VM")

    driver.destroy = broken
    web.sign_in()
    web.post("/ships/ship-02/destroy", {"confirm_name": "ship-02"})
    job = last_job(web)
    assert not job.ok and not job.result["gone"]
    assert 'action="/ships/ship-02/forget"' in web.client.get(f"/jobs/{job.id}").text
    web.post("/ships/ship-02/forget", {"confirm_name": "ship-02"})
    assert last_job(web).ok and not web.store.load().has_ship("ship-02")


def test_unknown_ships_are_404(web):  # noqa: F811
    web.sign_in()
    for path in ("/ships/nope/destroy", "/ships/nope/forget", "/ships/BAD!/destroy"):
        assert web.post(path, {"confirm_name": "nope"}).status_code == 404


def test_a_ship_not_enrolled_yet_offers_destroy_beside_finish_enrolling(web):  # noqa: F811
    with web.store.edit() as ed:
        ed.set_field("ship-02", "enrolled", None)
    web.ship.down.add("ship-02")
    web.fleet.poll()
    web.sign_in()
    bar = _action_bar(web.client.get("/ships/ship-02").text)
    primary = bar[:bar.index('<details class="menu">')]
    assert 'action="/ships/ship-02/enrol"' in primary
    assert 'data-confirm-type="ship-02"' in _form(primary, "/ships/ship-02/destroy")


def test_notes_printed_during_destroy_stay_on_the_card(web, monkeypatch):  # noqa: F811
    """The overlay's by-hand step (no API to remove the device) shows beside the
    driver's answer, as the CLI prints both."""
    from fleetledger import lifecycle

    on_a_fake_hypervisor(web)
    monkeypatch.setattr(lifecycle.overlay, "before_destroy", lambda app, inv, ship: app.note(
        "ship-02: remove its device in your overlay's admin console."))
    web.sign_in()
    web.post("/ships/ship-02/destroy", {"confirm_name": "ship-02"})
    job = last_job(web)
    card = web.client.get(f"/jobs/{job.id}").text
    assert "remove its device in your overlay&#39;s admin console" in card
    assert "ship-02 is removed from the inventory." in card
    assert card.count("Destroyed VM 9102.") == 1  # the driver's answer, once
