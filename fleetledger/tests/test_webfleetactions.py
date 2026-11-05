"""/fleet/actions: update, a setting, tools and logs for every Ship or a tag, each the
shared day-2 operation (fleetledger.ops) over ops.select's Ships, as a job with one
outcome per Ship; and the Every Ship menu's Renew certificates and link."""

from __future__ import annotations

import re

import pytest
from test_web import wait_for, web  # noqa: F401 - the web UI fixture
from test_webtrust import cakeys, caweb  # noqa: F401 - the web UI with a CA

from fleetledger import ops


@pytest.fixture
def fa(web, monkeypatch):  # noqa: F811
    """The page, with the ops recorded (each tested with the CLI) and answering ok."""
    calls = []

    def record(name):
        def call(fleet, ships, *args, **kwargs):
            calls.append((name, [s.name for s in ships], args, kwargs))
            return [ops.Outcome(s.name, True, f"{name} ok",
                                data={"lines": [f"{s.name} line"]} if name == "fleet_logs"
                                else {}) for s in ships]
        return call

    for name in ("update", "set_settings", "change_tools", "fleet_logs"):
        monkeypatch.setattr(ops, name, record(name))
    web.sign_in()
    web.calls = calls
    return web


def _finished(web, count=1):  # noqa: F811
    wait_for(lambda: len(web.app_ui.jobs.recent()) >= count
             and all(j.done for j in web.app_ui.jobs.recent()))
    return web.app_ui.jobs.recent()[0]


def _form(page, action):
    start = page.index(f'action="{action}"')
    return page[page.rindex("<form", 0, start):page.index("</form>", start)]


def test_the_page_aims_at_every_ship_or_a_tag_and_counts_them(fa):
    page = fa.client.get("/fleet/actions").text
    assert "every Ship (2)" in page and "every work Ship (1)" in page
    assert 'data-confirm-action="Update 2 Ships"' in _form(page, "/fleet/actions/update")
    page = fa.client.get("/fleet/actions?tag=work").text
    form = _form(page, "/fleet/actions/update")
    assert 'data-confirm-action="Update 1 Ship"' in form and 'value="tag:work"' in form
    assert 'data-confirm-action="Apply to 1 Ship"' in _form(page, "/fleet/actions/set")
    assert "ssh_listen" not in page  # per Ship only


def test_update_with_options_runs_ops_update_over_the_tags_ships(fa):
    fa.post("/fleet/actions/update", {"ships": "tag:work", "reboot": "never", "force": "yes"})
    assert fa.app_ui.jobs.recent() == []  # no tick: nothing
    response = fa.post("/fleet/actions/update", {"ships": "tag:work", "reboot": "never",
                                                 "force": "yes", "confirm": "yes"})
    assert response.headers["location"] == "/fleet/actions?tag=work"
    job = _finished(fa)
    name, ships, _, kwargs = fa.calls[0]
    assert (name, ships) == ("update", ["ship-01"])
    assert (kwargs["reboot_policy"], kwargs["force"], kwargs["wait"]) == ("never", True, True)
    assert job.ok and [o.ship for o in job.outcomes] == ["ship-01"]
    card = fa.client.get(f"/jobs/{job.id}?v=0").text
    assert "ship-01: update ok" in card
    fa.post("/fleet/actions/update", {"ships": "all", "reboot": "sometimes", "confirm": "yes"})
    assert len(fa.app_ui.jobs.recent()) == 1  # not a policy


def test_selection_is_ops_select(fa):
    inv = fa.store.load()
    for value, tag in (("all", None), ("tag:work", "work"), ("tag:personal", "personal")):
        fa.calls.clear()
        fa.post("/fleet/actions/update", {"ships": value, "reboot": "auto", "confirm": "yes"})
        _finished(fa, len(fa.app_ui.jobs.recent()))
        expected = [s.name for s in ops.select(inv, [], tag, everything=tag is None)]
        assert fa.calls[0][1] == expected
    fa.post("/fleet/actions/update", {"ships": "tag:Bad!", "reboot": "auto", "confirm": "yes"})
    assert "Choose every Ship or a tag" in fa.client.get("/fleet/actions").text
    fa.post("/fleet/actions/update", {"ships": "tag:nobody", "reboot": "auto", "confirm": "yes"})
    assert "No Ships with tag &#39;nobody&#39;" in fa.client.get("/fleet/actions").text


def test_a_setting_sends_only_what_was_chosen(fa):
    fa.post("/fleet/actions/set", {"ships": "all", "confirm": "yes"})
    assert "Choose at least one setting" in fa.client.get("/fleet/actions").text
    fa.post("/fleet/actions/set", {"ships": "all", "remote_control": "off", "sudo": "",
                                   "reboot_time": "03:30", "confirm": "yes"})
    job = _finished(fa)
    name, ships, args, _ = fa.calls[0]
    assert (name, ships) == ("set_settings", ["ship-01", "ship-02"])
    assert args[0] == ["remote_control=off", "reboot_time=03:30"]
    assert job.ok and len(job.outcomes) == 2
    fa.post("/fleet/actions/set", {"ships": "all", "reboot_time": "25:99", "confirm": "yes"})
    assert len(fa.app_ui.jobs.recent()) == 1  # the CLI's own check refused it


def test_tools_add_and_remove_for_a_tag(fa):
    fa.post("/fleet/actions/tools", {"ships": "tag:work", "do": "add", "tool": ["gh-axi"],
                                     "confirm": "yes"})
    _finished(fa)
    name, ships, args, _ = fa.calls[0]
    assert (name, ships, args[:2]) == ("change_tools", ["ship-01"], ("add", ["gh-axi"]))
    fa.post("/fleet/actions/tools", {"ships": "tag:work", "do": "remove",
                                     "tool": ["not-a-tool"], "confirm": "yes"})
    assert "catalogue" in fa.client.get("/fleet/actions").text
    page = fa.client.get("/fleet/actions").text
    at = page.index('name="do" value="remove"')
    remove = page[at - 1500:at]
    assert "data-confirm-danger" in remove.split("<form")[-1]  # removing asks, as danger


def test_logs_from_every_ship_show_per_ship(fa):
    fa.post("/fleet/actions/logs", {"ships": "all", "source": "provision", "lines": "50"})
    job = _finished(fa)
    name, ships, args, _ = fa.calls[0]
    assert (name, ships, args[:2]) == ("fleet_logs", ["ship-01", "ship-02"], ("provision", 50))
    card = fa.client.get(f"/jobs/{job.id}?v=0").text
    assert "ship-01: 1 line" in card and "ship-02 line" in card
    assert "fleet_logs ok" not in card  # each Ship's lines say it
    fa.post("/fleet/actions/logs", {"ships": "all", "source": "provision", "lines": "99999"})
    assert len(fa.app_ui.jobs.recent()) == 1


def test_the_every_ship_menu_links_here(fa):
    page = fa.client.get("/?tag=work").text
    assert 'href="/fleet/actions?tag=work"' in page
    assert 'action="/settings/certs/renew"' not in page  # no CA in this fleet


def test_renew_certificates_for_a_tag_from_the_menu(caweb, monkeypatch):  # noqa: F811
    from fleetledger.web import jobs

    argvs = []
    monkeypatch.setattr(jobs, "run_cli", lambda app, argv, job: argvs.append(argv) or 0)
    page = caweb.client.get("/?tag=work").text
    renew = _form(page, "/settings/certs/renew")
    assert re.search(r'name="tag" value="work"', renew) and 'value="fleet"' in renew
    response = caweb.post("/settings/certs/renew", {"tag": "work", "from": "fleet"})
    assert response.headers["location"] == "/?tag=work"
    job = _finished(caweb)
    assert argvs == [["ca", "sign", "--tag", "work"]] and job.ships == ["ship-01"]
