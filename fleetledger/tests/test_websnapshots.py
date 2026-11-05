"""A Ship's Snapshots tab: the list, and take / roll back / delete as jobs running
`ship-fleet snapshot` with a fake driver (and the manual driver's steps)."""

import re

import pytest
from fleetledger.drivers.base import Outcome, Snapshot
from fleetledger.errors import FleetError
from fleetledger.lifecycle import SNAPSHOT_WARNING
from fleetledger.web import flows
from markupsafe import escape
from test_web import _action_bar, flow_of, wait_for, web  # noqa: F401 - the web UI fixture


class FakeDriver:
    id = "proxmox"

    def __init__(self):
        self.calls = []
        self.snaps = {"ship-01": [Snapshot("before-upgrade", "2026-01-01T00:00:00Z", "a note")]}

    def snapshots(self, ship):
        return list(self.snaps.get(ship.name, []))

    def snapshot(self, ship, name, description=""):
        self.calls.append(("take", ship.name, name, description))
        return Outcome(True, [f"Took snapshot {name} of {ship.name}."])

    def rollback(self, ship, name):
        self.calls.append(("rollback", ship.name, name))
        return Outcome(True, [f"Rolled {ship.name} back to {name}."])

    def delete_snapshot(self, ship, name):
        self.calls.append(("rm", ship.name, name))
        return Outcome(True, [f"Deleted {name}."])


@pytest.fixture
def driven(web):  # noqa: F811
    """ship-01 on a (fake) Proxmox; ship-02 stays on the manual driver."""
    from fleetledger.drivers import get_driver

    fake = FakeDriver()
    with web.store.edit() as ed:
        ed.doc["ship"]["ship-01"]["driver"] = "proxmox"
    web.app.driver_factory = lambda inv, d: fake if d == "proxmox" else get_driver(inv, d)
    web.fleet.poll()
    web.sign_in()
    web.fake = fake
    return web


def _finished(web, count=1):  # noqa: F811
    wait_for(lambda: len(web.app_ui.jobs.recent()) >= count
             and all(j.done for j in web.app_ui.jobs.recent()))
    return web.app_ui.jobs.recent()[0]


def test_the_tab_lists_snapshots_and_offers_take_rollback_delete(driven):
    page = driven.client.get("/ships/ship-01/snapshots").text
    assert "before-upgrade" in page and "a note" in page and str(escape(SNAPSHOT_WARNING)) in page
    assert 'id="take"' in page
    rollback = page[page.index('action="/ships/ship-01/snapshots/rollback"'):]
    rollback = rollback[:rollback.index("</form>")]
    assert 'data-confirm-type="ship-01"' in rollback and "data-confirm-danger" in rollback
    assert 'name="confirm_name"' in rollback and "data-confirm-typed" in rollback
    assert 'name="snapshot" value="before-upgrade"' in rollback
    bar = _action_bar(page)
    assert 'href="/ships/ship-01/snapshots#take"' in bar and "(by hand)" not in bar


def test_take_runs_the_cli_command_as_a_job(driven):
    driven.post("/ships/ship-01/snapshots/take", {"snapshot": "", "note": "-- before work"})
    job = _finished(driven)
    assert job.ok and job.kind == "snapshot" and job.ships == ["ship-01"]
    (call,) = driven.fake.calls
    assert call[0] == "take" and re.fullmatch(r"snap-\d{8}-\d{6}", call[2])
    assert call[3] == "-- before work"  # a note that starts with '-' stays a note
    card = driven.client.get(f"/jobs/{job.id}").text
    assert f"Took snapshot {call[2]} of ship-01." in card


def test_rollback_needs_the_ships_name_typed(driven):
    for typed in ("", "ship-02", "yes"):
        response = driven.post("/ships/ship-01/snapshots/rollback",
                               {"snapshot": "before-upgrade", "confirm_name": typed,
                                "confirm": "yes"})
        assert response.headers["location"] == "/ships/ship-01/snapshots"
    assert driven.app_ui.jobs.recent() == [] and driven.fake.calls == []
    driven.post("/ships/ship-01/snapshots/rollback",
                {"snapshot": "before-upgrade", "confirm_name": "ship-01"})
    assert _finished(driven).ok
    assert driven.fake.calls == [("rollback", "ship-01", "before-upgrade")]


def test_delete_needs_the_tick(driven):
    driven.post("/ships/ship-01/snapshots/delete", {"snapshot": "before-upgrade"})
    assert driven.app_ui.jobs.recent() == []
    driven.post("/ships/ship-01/snapshots/delete", {"snapshot": "before-upgrade",
                                                    "confirm": "yes"})
    assert _finished(driven).ok and driven.fake.calls == [("rm", "ship-01", "before-upgrade")]


@pytest.mark.parametrize("bad", ["1st", "x", "current", "a b", "--yes"])
def test_bad_snapshot_names_are_refused(driven, bad):
    for route, extra in (("take", {}), ("rollback", {"confirm_name": "ship-01"}),
                         ("delete", {"confirm": "yes"})):
        driven.post(f"/ships/ship-01/snapshots/{route}", {"snapshot": bad, **extra})
    assert driven.app_ui.jobs.recent() == [] and driven.fake.calls == []


def test_the_manual_driver_cannot_list_and_shows_the_steps(driven):
    page = driven.client.get("/ships/ship-02/snapshots").text
    assert "cannot list snapshots" in page and "Roll back or delete, by name" in page
    bar = _action_bar(page)
    assert "Snapshot (by hand)" in bar and 'href="/ships/ship-02/snapshots#take"' in bar
    outside_more = bar[:bar.index('<details class="menu">')]
    assert "snapshots#take" not in outside_more  # no primary Snapshot on a by-hand driver
    driven.post("/ships/ship-02/snapshots/take", {"snapshot": "before-work"})
    job = _finished(driven)
    assert job.result["outcome"].done is False
    card = driven.client.get(f"/jobs/{job.id}").text
    assert "Do this on your hypervisor" in card and "before-work" in card
    assert 'data-copy="' in card
    assert '<span class="pill muted">by hand</span>' in card and ">done</span>" not in card


def test_one_job_at_a_time_per_ship(driven, monkeypatch):
    import threading

    release = threading.Event()
    slow = driven.fake.snapshot

    def held(ship, name, description=""):
        release.wait(10)
        return slow(ship, name, description)

    monkeypatch.setattr(driven.fake, "snapshot", held)
    driven.post("/ships/ship-01/snapshots/take", {"snapshot": "first"})
    driven.post("/ships/ship-01/snapshots/take", {"snapshot": "second"})
    page = driven.client.get("/ships/ship-01/snapshots").text
    assert "already running" in page
    release.set()
    assert _finished(driven).ok
    assert [c[2] for c in driven.fake.calls] == ["first"]


def test_unknown_ships_are_404(driven):
    assert driven.post("/ships/nope/snapshots/take", {}).status_code == 404
    assert driven.client.get("/ships/nope/snapshots").status_code == 404


def test_a_driver_that_cannot_be_reached_says_so(driven, monkeypatch):
    def down(ship):
        raise FleetError("Cannot reach the Proxmox API at pve.example.invalid.")

    monkeypatch.setattr(driven.fake, "snapshots", down)
    response = driven.client.get("/ships/ship-01/snapshots")
    assert response.status_code == 200
    assert "Cannot reach the Proxmox API" in response.text and 'class="flash bad"' in response.text
    assert 'id="take"' in response.text  # taking one may still work


def test_no_rollback_while_a_sign_in_is_open_on_the_ship(driven):
    driven.post("/signin", {"ship": "ship-01", "service": "claude"})
    flow = flow_of(driven)
    wait_for(lambda: flow.state in (flows.LINK, flows.CODE))
    driven.post("/ships/ship-01/snapshots/rollback",
                {"snapshot": "before-upgrade", "confirm_name": "ship-01"})
    assert driven.app_ui.jobs.recent() == [] and driven.fake.calls == []
    assert "a sign-in is open on ship-01" in driven.client.get("/ships/ship-01/snapshots").text
    flow.cancel()
