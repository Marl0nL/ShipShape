"""A Ship's Access tab, Leave, Settings ▸ Access ▸ Overlay (the rules and Check), the
overlay-down alert and "Join every Ship not on it", with the fake Ships."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from fleetledger.overlay.providers import Check
from fleetledger.web import actions, dashboard, overlayweb
from test_cli import EXAMPLES, Keys
from test_web import wait_for, web  # noqa: F401 - the web UI fixture
from test_webtrust import cakeys, caweb  # noqa: F401 - the web UI with a CA

from fleetledger import overlay, overlay_setup, trustcmd


@pytest.fixture
def ov(web):  # noqa: F811
    """A fleet with a Tailscale overlay (keys pasted); ship-01 joined to it."""
    with web.store.edit() as ed:
        ed.doc["overlay"] = {"provider": "tailscale", "tags": ["tag:ship"]}
        ed.set_field("ship-01", "overlay_node", "nship01")
        ed.set_field("ship-01", "names", ["ship-01.example-tailnet.ts.net",
                                           *web.store.load().ship("ship-01").names])
    web.ship.overlay["ship-01"] = {
        "provider": "tailscale", "joined": True, "state": "running",
        "name": "ship-01.example-tailnet.ts.net", "addresses": ["192.0.2.10"],
        "tags": ["tag:ship"], "ssh_listen": "any", "node_id": "nship01"}
    web.ship.status_override["ship-01"] = json.loads(
        (EXAMPLES / "ship-status" / "running-overlay.json").read_text())
    web.app.overlay_keys = Keys()
    web.fleet.poll()
    web.sign_in()
    return web


def _finished(web, count=1):  # noqa: F811
    wait_for(lambda: len(web.app_ui.jobs.recent()) >= count
             and all(j.done for j in web.app_ui.jobs.recent()))
    return web.app_ui.jobs.recent()[0]


def test_the_access_tab_lists_every_way_to_attach_and_contacts_nothing(ov, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the Access tab ran a command")

    monkeypatch.setattr(subprocess, "run", refuse)
    calls = len(ov.ship.calls)
    page = ov.client.get("/ships/ship-01/access").text
    assert len(ov.ship.calls) == calls  # nothing asked of the Ship
    assert 'data-copy="ship-fleet ssh ship-01"' in page
    assert 'data-copy="ship-fleet ssh ship-01 --as owner"' in page
    assert 'data-copy="herdr --remote ship-01"' not in page  # this computer is not trusted
    assert "Host certificate" in page and "no certificate authority" in page
    assert '<a href="/ships/ship-01/access" class="on">Access</a>' in page


def test_once_this_computer_attaches_it_lists_herdr_and_ssh(ov, monkeypatch):
    from fleetledger.web import app as webapp

    monkeypatch.setattr(trustcmd, "attaches_here", lambda inv, ship: True)
    monkeypatch.setattr(trustcmd, "this_computer_until", lambda inv: "2026-11-12T10:00:00Z")
    monkeypatch.setattr(webapp, "_attach_context", webapp._attach_context)
    page = ov.client.get("/ships/ship-01/access").text
    owner = ov.store.load().owner
    for line in ("herdr --remote ship-01", "ssh ship-01", f"ssh {owner}@ship-01"):
        assert f'data-copy="{line}"' in page
    assert "valid until 2026-11-12 10:00 UTC" in page


def test_the_access_tab_shows_the_overlay_and_offers_leave(ov):
    page = ov.client.get("/ships/ship-01/access").text
    overlay_part = page[page.index("<h2>Overlay</h2>"):page.index("<h2>Host certificate")]
    assert "ship-01.example-tailnet.ts.net" in overlay_part and "192.0.2.10" in overlay_part
    assert 'action="/ships/ship-01/overlay/leave"' in overlay_part
    assert "data-confirm-danger" in overlay_part
    other = ov.client.get("/ships/ship-02/access").text
    other = other[other.index("<h2>Overlay</h2>"):other.index("<h2>Host certificate")]
    assert "Not on the overlay" in other and "Join the overlay" in other


def test_leave_needs_the_tick_then_clears_the_node_and_the_overlay_name(ov):
    assert 'action="/ships/ship-01/overlay/leave"' in ov.client.get("/ships/ship-01").text
    response = ov.post("/ships/ship-01/overlay/leave", {"from": "access"})
    assert response.headers["location"] == "/ships/ship-01/access"
    assert ov.app_ui.jobs.recent() == []
    ov.post("/ships/ship-01/overlay/leave", {"confirm": "yes"})
    job = _finished(ov)
    assert job.ok, job.snapshot()
    assert job.kind == "leave" and job.ships == ["ship-01"]
    ship = ov.store.load().ship("ship-01")
    assert ship.overlay_node is None
    assert "ship-01.example-tailnet.ts.net" not in ship.names
    response = ov.client.get(f"/jobs/{job.id}?v=0")
    assert response.headers.get("HX-Refresh") == "true"  # the header and tab redraw


def test_leave_is_refused_while_a_sign_in_is_open(ov, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(ov.app_ui.concierge, "active", lambda: [SimpleNamespace(ship="ship-01")])
        ov.post("/ships/ship-01/overlay/leave", {"confirm": "yes"})
    assert ov.app_ui.jobs.recent() == []
    assert "a sign-in is open on ship-01" in ov.client.get("/ships/ship-01").text


def test_settings_shows_the_rules_with_copy_and_a_check_form(ov):
    page = ov.client.get("/settings").text
    part = page[page.index('id="overlay"'):page.index("Keys and certificates")]
    rules = overlay.policy(ov.store.load().overlay).rstrip()
    assert "tag:ship" in rules and 'data-copy="' in part
    assert 'action="/settings/overlay/check"' in part
    assert f'maxlength="{overlayweb.POLICY_MAX}"' in part and 'name="probe"' in part


def test_check_runs_the_cli_checks_as_a_job_and_shows_its_items(ov, monkeypatch):
    seen = []

    def fake(app, *, policy_text=None, probe=False):
        seen.append((policy_text, probe))
        return [Check("join keys", "ok", "pasted one at a time"),
                Check("access rules", "skip", "export your policy"),
                Check("ship-01 overlay", "fail", "stopped as ship-01")]

    monkeypatch.setattr(overlay_setup, "check_items", fake)
    response = ov.post("/settings/overlay/check", {"policy": "  ", "probe": "yes"})
    assert response.headers["location"] == "/settings?said=overlay#overlay"
    job = _finished(ov)
    assert seen == [(None, True)] and not job.ok  # an item failed
    card = ov.client.get(f"/jobs/{job.id}?v=0").text
    assert "ship-01 overlay" in card and ">failed<" in card and ">skipped<" in card
    page = ov.client.get("/settings?said=overlay").text
    assert page.index("Check the overlay and probe") > page.index('id="overlay"')


def test_a_pasted_policy_is_checked_with_the_same_items_as_the_cli(ov):
    rules = overlay.policy(ov.store.load().overlay)
    ov.post("/settings/overlay/check", {"policy": rules})
    job = _finished(ov)
    names = [c.name for c in job.result["checks"]]
    assert "access rules" not in names  # read from the pasted policy, not skipped
    assert "ship-01 overlay" in names
    cli = overlay_setup.check_items(ov.app, policy_text=rules)
    assert [c.name for c in cli] == names


def test_a_policy_over_the_cap_is_refused_with_the_cli_command(ov):
    ov.post("/settings/overlay/check", {"policy": "x" * (overlayweb.POLICY_MAX + 1)})
    assert ov.app_ui.jobs.recent() == []
    page = ov.client.get("/settings?said=overlay").text
    assert "ship-fleet overlay check --policy FILE" in page


def test_check_without_an_overlay_is_refused(web):  # noqa: F811
    web.sign_in()
    web.post("/settings/overlay/check", {})
    assert web.app_ui.jobs.recent() == []
    assert "No overlay is set up" in web.client.get("/settings").text


def test_the_overlay_down_alert_links_to_check():
    action = dashboard.action_for("overlay-down", "ship-01", None, None)
    assert (action.kind, action.value) == ("link", "/settings?said=overlay#overlay")


def test_join_every_ship_not_on_it(ov, monkeypatch):
    started = []
    monkeypatch.setattr(actions, "start_operation",
                        lambda jobs, app, op, names: started.append((op, names)))
    page = ov.client.get("/").text
    assert "not on the overlay (1)" in page  # ship-02 only
    ov.post("/operations", {"operation": "join", "confirm": "yes"})
    assert started == [("join", ["ship-02"])]
    with ov.store.edit() as ed:
        ed.set_field("ship-02", "overlay_node", "nship02")
    ov.fleet.poll()
    ov.post("/operations", {"operation": "join", "confirm": "yes"})
    assert len(started) == 1
    assert "on the overlay already" in ov.client.get("/").text


def test_the_host_certificate_on_the_access_tab_with_renew(caweb):  # noqa: F811
    page = caweb.client.get("/ships/ship-01/access").text
    cert = page[page.index("<h2>Host certificate"):]
    assert "signed by another CA" in cert and "ship-01, ship-01.local" in cert
    assert 'action="/ships/ship-01/certs/renew"' in cert and 'value="access"' in cert
    response = caweb.post("/ships/ship-01/certs/renew", {"from": "access"})
    assert response.headers["location"] == "/ships/ship-01/access"
    assert 'action="/attach-here"' in page  # the CA exists: this computer can be trusted


def test_trusting_this_computer_from_the_access_tab_returns_there(ov, monkeypatch):
    monkeypatch.setattr(trustcmd, "trust_this_computer",
                        lambda app, store, say, create=True: True)
    response = ov.post("/attach-here", {"ship": "ship-01", "from": "access"})
    assert response.headers["location"] == "/ships/ship-01/access"
    assert ov.post("/attach-here", {"ship": "ship-01"}).headers["location"] == "/ships/ship-01"
