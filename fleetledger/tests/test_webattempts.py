"""Edit and retry from the web UI: a failed New Ship job offers the form again, filled
in from the saved attempt (attempts.py), after a restart too; a retry removes what the
attempt left first (after the dialog), and a taken name is a normal form error."""

from __future__ import annotations

import html

from fleetledger.errors import FleetError
from fleetledger.web import app as webapp
from starlette.testclient import TestClient
from test_attempts import LeavingDriver
from test_cli import FakeDriver
from test_web import BASE, wait_for, web  # noqa: F401 - the web UI fixture

from fleetledger import attempts

TYPED = {"name": "ship-31", "tag": "work lab", "cores": "6", "memory_gb": "12",
         "disk_gb": "60", "signins_shown": "yes", "signin": ["claude", "github"],
         "agent_on": "yes", "agent": "lead", "cwd": "~/work/x",
         "repo": "someone/agent-setup", "hosts": "ship-31.example.net",
         "agent_sudo": "yes", "mode": "plan", "prompt": "Get going.",
         "timezone": "Europe/Berlin"}


def _fail_with(web, driver):  # noqa: F811
    web.app.driver_factory = lambda inv, driver_id, **kw: driver
    return driver


def _make(web, fields):  # noqa: F811
    response = web.post("/new", fields)
    assert response.status_code == 303, response.text
    job = web.app_ui.jobs.recent()[0]
    wait_for(lambda: job.done)
    return job


def test_a_failed_create_offers_edit_and_retry_with_everything_it_was_given(web):  # noqa: F811
    web.sign_in()
    _fail_with(web, FakeDriver(fail=True))
    job = _make(web, TYPED)
    assert not job.ok and job.result["retry"] == "ship-31"
    card = web.client.get(f"/jobs/{job.id}").text
    assert 'href="/new?retry=ship-31"' in card and "Edit and retry" in card
    assert not web.store.load().has_ship("ship-31")

    page = web.client.get("/new?retry=ship-31").text
    assert "Trying again: ship-31" in page and "Internal error (RuntimeError)." in page
    assert 'name="retry_of" value="ship-31"' in page
    for markup in ('name="name" value="ship-31"', 'name="tag" value="work lab"',
                   'name="cores" type="number"', 'value="6"', 'value="12"', 'value="60"',
                   'name="agent" value="lead"', 'name="cwd" value="~/work/x"',
                   'name="repo" value="someone/agent-setup"',
                   '(one per line)">ship-31.example.net</textarea>',
                   'name="agent_sudo" value="yes" checked', 'value="Get going."',
                   'value="Europe/Berlin"', "<option selected>plan</option>",
                   'name="signin" value="claude" checked', 'name="signin" value="github" checked',
                   '<details class="advanced" open>', "Make it again"):
        assert markup in page, markup
    assert 'name="signin" value="gcloud" checked' not in page
    assert "data-confirm=" not in page.split('class="new-ship"')[1].split(">")[0]  # nothing left


def test_a_failure_is_still_offered_after_a_restart(web):  # noqa: F811
    web.sign_in()
    _fail_with(web, FakeDriver(fail=True))
    _make(web, {"name": "ship-31"})
    fresh = webapp.create_app(web.app, web.gate, fleet=web.fleet, concierge_=web.concierge)
    client = TestClient(fresh, base_url=BASE, follow_redirects=False)
    web.sign_in(client)
    fleet = client.get("/partials/fleet").text
    assert "Make ship-31" in fleet and 'href="/new?retry=ship-31"' in fleet
    new = client.get("/new").text
    assert "Failed attempts" in new and 'action="/new/attempts/ship-31/discard"' in new
    assert 'name="name" value="ship-31"' in client.get("/new?retry=ship-31").text


def test_retry_after_it_works_starts_clean_and_forgets_the_attempt(web):  # noqa: F811
    web.sign_in()
    driver = _fail_with(web, FakeDriver(fail=True))
    _make(web, {"name": "ship-31", "cores": "2"})
    driver.fail = False
    job = _make(web, {"name": "ship-31", "cores": "4", "retry_of": "ship-31"})
    assert job.ok, job.snapshot()
    assert [(n, s["cores"]) for n, _, s in driver.created] == [("ship-31", 4)]
    assert attempts.get("ship-31") is None
    assert "Failed attempts" not in web.client.get("/new").text


def test_a_retry_removes_what_the_attempt_left_once_confirmed(web):  # noqa: F811
    web.sign_in()
    leaving = _fail_with(web, LeavingDriver())
    job = _make(web, {"name": "ship-31"})
    assert job.result["retry"] == "ship-31"
    page = web.client.get("/new?retry=ship-31").text
    form = page.split('class="new-ship"')[1].split(">")[0]
    assert "data-confirm=" in form and "VM 9101 (ship-31)" in html.unescape(form)
    assert 'name="confirm" value="yes" data-confirm-field' in page
    assert "The failed attempt at ship-31 left VM 9101 (ship-31)" in page
    assert 'name="clear_left" value="ship-31"' in page

    leaving.leave = False
    refused = web.post("/new", {"name": "ship-31", "retry_of": "ship-31"})
    assert refused.status_code == 400 and "confirm it in the dialog" in refused.text
    assert leaving.removed == [] and leaving.created == []

    job = _make(web, {"name": "ship-31", "retry_of": "ship-31", "confirm": "yes",
                     "clear_left": "ship-31"})
    assert job.ok, job.snapshot()
    assert leaving.removed == [("ship-31", [{"kind": "vm", "vmid": 9101, "name": "ship-31"}])]
    assert [c[0] for c in leaving.created] == ["ship-31"]
    assert attempts.get("ship-31") is None


def test_a_renamed_retry_still_removes_what_the_old_attempt_left(web):  # noqa: F811
    web.sign_in()
    leaving = _fail_with(web, LeavingDriver())
    _make(web, {"name": "ship-31"})
    leaving.leave = False
    job = _make(web, {"name": "ship-32", "retry_of": "ship-31", "confirm": "yes",
                     "clear_left": "ship-31"})
    assert job.ok, job.snapshot()
    assert leaving.removed == [("ship-31", [{"kind": "vm", "vmid": 9101, "name": "ship-31"}])]
    assert attempts.get("ship-31") is None and attempts.get("ship-32") is None


def test_a_retry_blocked_by_a_leftover_says_what_to_do(web):  # noqa: F811
    web.sign_in()
    leaving = _fail_with(web, LeavingDriver())
    _make(web, {"name": "ship-31"})
    leaving.leave, leaving.remove_fails = False, True
    job = _make(web, {"name": "ship-31", "retry_of": "ship-31", "confirm": "yes",
                     "clear_left": "ship-31"})
    assert not job.ok and leaving.created == []
    log = "\n".join(job.snapshot())
    assert "cannot remove it: Proxmox DELETE failed: 500 VM is locked (clone)" in log
    assert "Delete it on the hypervisor by hand" in log
    assert job.result["retry"] == "ship-31"
    assert 'href="/new?retry=ship-31"' in web.client.get(f"/jobs/{job.id}").text


def test_a_taken_name_is_a_normal_form_error(web):  # noqa: F811
    web.sign_in()
    _fail_with(web, FakeDriver(fail=True))
    _make(web, {"name": "ship-31", "cores": "3"})
    response = web.post("/new", {"name": "ship-01", "cores": "3", "retry_of": "ship-31"})
    assert response.status_code == 400
    assert "ship-01 is already in the inventory: choose another name." in response.text
    assert 'name="retry_of" value="ship-31"' in response.text  # still the retry
    assert 'name="name" value="ship-01"' in response.text


def test_a_ship_made_but_not_enrolled_blocks_its_retry_with_the_way_out(web, monkeypatch):  # noqa: F811
    from fleetledger.errors import FleetError

    from fleetledger import commands

    def no_enrol(*a, **kw):
        raise FleetError("ship-31 did not finish provisioning: cloud-init failed.")

    monkeypatch.setattr(commands, "_enrol", no_enrol)
    web.sign_in()
    _fail_with(web, FakeDriver())
    job = _make(web, {"name": "ship-31"})
    assert not job.ok and job.result["retry"] == "ship-31"
    page = web.client.get("/new?retry=ship-31").text
    assert "still in the inventory from the failed attempt" in page
    assert 'href="/ships/ship-31"' in page
    response = web.post("/new", {"name": "ship-31", "retry_of": "ship-31"})
    assert response.status_code == 400 and "Finish enrolling" in response.text


def test_discard_forgets_an_attempt(web):  # noqa: F811
    web.sign_in()
    _fail_with(web, FakeDriver(fail=True))
    _make(web, {"name": "ship-31"})
    assert web.post("/new/attempts/ship-31/discard").status_code == 303
    assert attempts.get("ship-31") is None
    response = web.client.get("/new?retry=ship-31")
    assert response.status_code == 303
    assert "no saved attempt at ship-31" in web.client.get("/new").text


def test_discard_needs_the_csrf_token(web):  # noqa: F811
    web.sign_in()
    attempts.save(attempts.Attempt("ship-31", 1.0, {"name": "ship-31"}, "x"))
    response = web.client.post("/new/attempts/ship-31/discard", data={"csrf": "wrong"},
                               headers={"Origin": BASE})
    assert response.status_code == 403
    assert attempts.get("ship-31") is not None


# --- review fixes ---------------------------------------------------------------------


def test_the_plain_form_never_removes_leftovers_without_the_dialog(web):  # noqa: F811
    web.sign_in()
    leaving = _fail_with(web, LeavingDriver())
    _make(web, {"name": "ship-31"})
    leaving.leave = False
    fresh = web.client.get("/new").text
    assert 'name="name" value="ship-31"' not in fresh  # the default skips a saved attempt
    form = fresh.split('class="new-ship"')[1].split(">")[0]
    assert "data-confirm" not in form
    refused = web.post("/new", {"name": "ship-31"})  # typed by hand: no retry_of
    assert refused.status_code == 400
    assert "The failed attempt at ship-31 left VM 9101 (ship-31)" in refused.text
    form = refused.text.split('class="new-ship"')[1].split(">")[0]
    assert "data-confirm=" in form and 'name="clear_left" value="ship-31"' in refused.text
    assert leaving.removed == [] and leaving.created == []
    job = _make(web, {"name": "ship-31", "confirm": "yes", "clear_left": "ship-31"})
    assert job.ok, job.snapshot()
    assert leaving.removed == [("ship-31", [{"kind": "vm", "vmid": 9101, "name": "ship-31"}])]


def test_confirming_one_attempts_leftovers_does_not_remove_anothers(web):  # noqa: F811
    web.sign_in()
    leaving = _fail_with(web, LeavingDriver())
    _make(web, {"name": "ship-31"})
    _make(web, {"name": "ship-32"})
    leaving.leave = False
    refused = web.post("/new", {"name": "ship-32", "retry_of": "ship-31", "confirm": "yes",
                                "clear_left": "ship-31"})
    assert refused.status_code == 400 and "The failed attempt at ship-32 left" in refused.text
    assert 'name="clear_left" value="ship-31"' in refused.text
    assert 'name="clear_left" value="ship-32"' in refused.text
    assert leaving.removed == [] and leaving.created == []
    job = _make(web, {"name": "ship-32", "retry_of": "ship-31", "confirm": "yes",
                      "clear_left": ["ship-31", "ship-32"]})
    assert job.ok, job.snapshot()
    assert sorted(n for n, _ in leaving.removed) == ["ship-31", "ship-32"]


def test_an_older_card_stops_offering_an_attempt_it_no_longer_holds(web):  # noqa: F811
    web.sign_in()
    _fail_with(web, FakeDriver(fail=True))
    first = _make(web, {"name": "ship-31", "cores": "2"})
    second = _make(web, {"name": "ship-31", "cores": "3"})
    old, new = (web.client.get(f"/jobs/{j.id}").text for j in (first, second))
    assert 'href="/new?retry=ship-31"' in new
    assert 'href="/new?retry=ship-31"' not in old and "A later attempt at ship-31" in old
    fleet = web.client.get("/partials/fleet").text
    assert fleet.count('href="/new?retry=ship-31"') == 1  # no second card for it
    web.post("/new/attempts/ship-31/discard")
    assert 'href="/new?retry=ship-31"' not in web.client.get(f"/jobs/{second.id}").text


def test_a_saved_attempt_shows_once_in_activity(web):  # noqa: F811
    web.sign_in()
    attempts.save(attempts.Attempt("ship-31", 1.0, {"name": "ship-31"}, "x"))
    assert web.client.get("/partials/fleet").text.count('id="attempt-ship-31"') == 1


def test_a_destroyed_ships_attempt_no_longer_says_it_is_in_the_inventory(web, monkeypatch):  # noqa: F811
    from fleetledger.errors import FleetError

    from fleetledger import commands

    def no_enrol(*a, **kw):
        raise FleetError("cloud-init failed.")

    monkeypatch.setattr(commands, "_enrol", no_enrol)
    web.sign_in()
    _fail_with(web, FakeDriver())
    _make(web, {"name": "ship-31"})
    assert "still in the inventory" in web.client.get("/new").text
    with web.store.edit() as ed:
        ed.remove_ship("ship-31")  # destroyed from its page
    page = web.client.get("/new").text
    assert "still in the inventory" not in page and 'href="/ships/ship-31"' not in page


def test_two_retries_of_one_attempt_cannot_run_at_once(web):  # noqa: F811
    import threading

    from fleetledger.web import newship

    web.sign_in()
    attempts.save(attempts.Attempt("ship-31", 1.0, {"name": "ship-31"}, "x"))
    gate = threading.Event()
    jobs = web.app_ui.jobs
    jobs.start("create", ["ship-32"], lambda job: gate.wait(5), locks=["attempt ship-31"])
    try:
        import pytest

        with pytest.raises(FleetError, match="already running on attempt ship-31"):
            newship.start(web.app_ui, "ship-33", ["new", "ship-33"], retry_of="ship-31")
    finally:
        gate.set()


def test_saved_attempts_show_their_date_once_it_is_not_today():
    import time

    from fleetledger.web.app import _clock_dated

    assert ":" in _clock_dated(time.time()) and len(_clock_dated(time.time())) == 8
    assert "2024" in _clock_dated(time.mktime((2024, 3, 5, 9, 30, 0, 0, 0, -1)))
