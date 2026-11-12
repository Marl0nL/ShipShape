"""/fleet/actions: update, a setting, tools and logs for every Ship or a tag, each the
shared day-2 operation (fleetledger.ops) over ops.select's Ships, as a job with one
outcome per Ship; and the Every Ship menu's Renew certificates and link."""

from __future__ import annotations

import re

import pytest
from fl_harness import finished
from test_web import web  # noqa: F401 - the web UI fixture
from test_webtrust import cakeys, caweb  # noqa: F401 - the web UI with a CA

from fleetledger import ops


@pytest.fixture
def fa(web, monkeypatch):  # noqa: F811
    """The page, with the ops recorded (each tested with the CLI) and answering ok."""
    calls, failing = [], set()

    def record(name):
        def call(fleet, ships, *args, **kwargs):
            calls.append((name, [s.name for s in ships], args, kwargs))
            return [ops.Outcome(s.name, s.name not in failing,
                                f"{name} ok" if s.name not in failing else "cannot reach it",
                                data={"lines": [f"{s.name} \x1b[31mline\x1b[0m"]}
                                if name == "fleet_logs" and s.name not in failing else {})
                    for s in ships]
        return call

    for name in ("update", "set_settings", "change_tools", "fleet_logs"):
        monkeypatch.setattr(ops, name, record(name))
    web.sign_in()
    web.calls, web.failing = calls, failing
    return web


def _form(page, action):
    start = page.index(f'action="{action}"')
    return page[page.rindex("<form", 0, start):page.index("</form>", start)]


def test_the_page_aims_at_every_ship_or_a_tag_and_counts_them(fa):
    page = fa.client.get("/fleet/actions").text
    assert "every Ship (2)" in page and "every work Ship (1)" in page
    assert 'data-confirm-action="Update 2 Ships"' in _form(page, "/fleet/actions/update")
    # auto, the default, is never a reboot of a herdr-core Ship (for herdr or the core).
    assert ('<option value="auto">auto: only a herdr Ship whose herdr changed, never a '
            'herdr-core Ship</option>') in _form(page, "/fleet/actions/update")
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
    job = finished(fa)
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
        finished(fa, len(fa.app_ui.jobs.recent()))
        expected = [s.name for s in ops.select(inv, [], tag, everything=tag is None)]
        assert fa.calls[0][1] == expected
    fa.post("/fleet/actions/update", {"ships": "tag:Bad!", "reboot": "auto", "confirm": "yes"})
    assert "Choose every Ship or a tag" in fa.client.get("/fleet/actions").text
    fa.post("/fleet/actions/update", {"ships": "tag:nobody", "reboot": "auto", "confirm": "yes"})
    assert "No Ships with tag &#39;nobody&#39;" in fa.client.get("/fleet/actions").text


def test_a_setting_sends_only_what_was_chosen(fa):
    response = fa.post("/fleet/actions/set", {"ships": "tag:work", "remote_control": "off"})
    assert response.headers["location"] == "/fleet/actions?tag=work"
    assert fa.app_ui.jobs.recent() == [] and "Tick the box" in fa.client.get("/").text
    fa.post("/fleet/actions/set", {"ships": "all", "confirm": "yes"})
    assert "Choose at least one setting" in fa.client.get("/fleet/actions").text
    fa.post("/fleet/actions/set", {"ships": "all", "remote_control": "off", "sudo": "",
                                   "reboot_time": "03:30", "confirm": "yes"})
    job = finished(fa)
    name, ships, args, _ = fa.calls[0]
    assert (name, ships) == ("set_settings", ["ship-01", "ship-02"])
    assert args[0] == ["remote_control=off", "reboot_time=03:30"]
    assert job.ok and len(job.outcomes) == 2
    fa.post("/fleet/actions/set", {"ships": "all", "reboot_time": "25:99", "confirm": "yes"})
    assert len(fa.app_ui.jobs.recent()) == 1  # the CLI's own check refused it


def test_tools_add_and_remove_for_a_tag(fa):
    fa.post("/fleet/actions/tools", {"ships": "tag:work", "do": "add", "tool": ["gh-axi"]})
    assert fa.app_ui.jobs.recent() == []  # no tick: nothing
    fa.post("/fleet/actions/tools", {"ships": "tag:work", "do": "add", "tool": ["gh-axi"],
                                     "confirm": "yes"})
    finished(fa)
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
    fa.failing.add("ship-02")
    fa.post("/fleet/actions/logs", {"ships": "all", "source": "herdr", "lines": "50"})
    job = finished(fa)
    name, ships, args, _ = fa.calls[0]
    assert (name, ships, args[:2]) == ("fleet_logs", ["ship-01", "ship-02"], ("herdr", 50))
    card = fa.client.get(f"/jobs/{job.id}?v=0").text
    assert "ship-01: 1 line" in card and "ship-01 line" in card  # colour codes cleaned
    assert "\x1b" not in card and "[31m" not in card
    assert "ship-02: 0 lines" not in card and "ship-02: cannot reach it" in card
    assert not job.ok
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
    job = finished(caweb)
    assert argvs == [["ca", "sign", "--tag", "work"]] and job.ships == ["ship-01"]


def test_one_ship_that_fails_fails_the_job(fa):
    fa.failing.add("ship-02")
    fa.post("/fleet/actions/update", {"ships": "all", "reboot": "auto", "confirm": "yes"})
    job = finished(fa)
    assert not job.ok and [o.ok for o in job.outcomes] == [True, False]
    card = fa.client.get(f"/jobs/{job.id}?v=0").text
    assert ">failed<" in card and 'class="flash bad">ship-02: cannot reach it' in card


def test_chips_follow_the_effective_tags_and_an_unknown_tag_never_widens(fa):
    with fa.store.edit() as ed:
        ed.set_default("tags", ["lab"])
        ed.doc["ship"]["ship-02"].pop("tags")
    page = fa.client.get("/fleet/actions").text
    assert "every lab Ship (1)" in page  # ship-02, by defaults.tags, as ops.select has it
    form = _form(fa.client.get("/fleet/actions?tag=lab").text, "/fleet/actions/update")
    assert 'value="tag:lab"' in form and 'data-confirm-action="Update 1 Ship"' in form
    for bad in ("nobody", "Bad!"):
        page = fa.client.get(f"/fleet/actions?tag={bad}").text
        assert "No Ships with tag" in page
        assert 'action="/fleet/actions/update"' not in page  # no form aimed at anything


def test_renew_from_the_menu_refuses_an_unknown_or_malformed_tag(caweb):  # noqa: F811
    response = caweb.post("/settings/certs/renew", {"tag": "nobody", "from": "fleet"})
    assert response.status_code == 303 and caweb.app_ui.jobs.recent() == []
    assert "No Ships with tag" in caweb.client.get("/").text
    response = caweb.post("/settings/certs/renew", {"tag": "Bad!", "from": "fleet"})
    assert response.headers["location"] == "/" and caweb.app_ui.jobs.recent() == []
    assert "Choose every Ship or a tag" in caweb.client.get("/").text
