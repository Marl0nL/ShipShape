"""Dismissing job cards and the operations history (web/activity.py, the Operations
store in fleetledger/history.py, and what web/jobs.py saves there)."""

from __future__ import annotations

import re
import stat
import threading
import time

import pytest
from fl_harness import Clock
from fleetledger.history import Operation, Operations
from fleetledger.web import app as webapp
from fleetledger.web.jobs import Jobs
from starlette.testclient import TestClient
from test_web import BASE, ORIGIN, wait_for, web  # noqa: F401 - the web UI fixture

from fleetledger import history

TOKEN = "ghp_" + "A1b2C3d4" * 5  # looks like a token: never saved
EMAIL = "someone@example.com"


NOW = time.time()


def op(op_id, *, ships=("ship-01",), started=NOW - 10, ended=NOW, outcome="ok", **kw):
    return Operation(id=op_id, kind=kw.pop("kind", "update"), title=kw.pop("title", "Update"),
                     ships=tuple(ships), started=started, ended=ended, outcome=outcome, **kw)


# --- the store -----------------------------------------------------------------------


def test_a_saved_operation_comes_back_whole_and_redacted(home):
    store = Operations()
    store.save(op(7, title="Update", error=f"failed for {EMAIL}",
                  log=("\x1b[32mstarting\x1b[0m", f"token {TOKEN}",
                       "see https://example.com/x?code=123456", "", "done")))
    got = store.get(7)
    assert got.ships == ("ship-01",) and got.outcome == "ok" and got.took == 10
    assert got.log == ("starting", "token [redacted]", "see https://example.com/x?[…]", "done")
    assert got.lines == 4 and got.error == "failed for [email]"
    raw = history.history_path().read_bytes()
    assert TOKEN.encode() not in raw and EMAIL.encode() not in raw
    assert stat.S_IMODE(history.history_path().stat().st_mode) == 0o600


def test_ship_names_and_kinds_are_kept_whole_however_long(home):
    """Identifiers are never secrets: a 24+ character Ship name must not read as a
    token, or its history (and the per-Ship filter) would be lost."""
    long_name = "my-very-long-ship-name-here-01"  # 30 characters, a valid name
    store = Operations()
    store.save(op(1, ships=(long_name, "ship-01"), kind="overlay-check",
                  title=f"Take snapshot before-upgrade-2026-11-06-full of {long_name} "
                        f"for {EMAIL} https://example.com/x?code=123456",
                  log=(f"converged {long_name}",)))
    got = store.get(1)
    assert got.ships == (long_name, "ship-01") and got.kind == "overlay-check"
    assert got.title == (f"Take snapshot before-upgrade-2026-11-06-full of {long_name} "
                         "for [email] https://example.com/x?[…]")
    assert [o.id for o in store.find(ship=long_name)] == [1]
    assert store.ships() == [long_name, "ship-01"]
    # Something that is not an identifier where one belongs is scrubbed in full.
    store.save(op(2, ships=(TOKEN,), kind=TOKEN))
    assert store.get(2).ships == ("[redacted]",) and store.get(2).kind == "[redacted]"


def test_the_jobs_own_long_ship_names_stay_readable_in_its_log_and_error(home):
    """Several long-named Ships in one log: each line still says which Ship it is
    about. A name inside a longer run (a host name, a path, a secret) is still
    redacted whole, and so is a secret beside a name."""
    a, b = "my-very-long-ship-name-here-01", "my-very-long-ship-name-here-02"
    store = Operations()
    store.save(op(1, ships=(a, b), outcome="failed", error=f"{b} did not answer over SSH.",
                  log=(f"{a}: converged", f"{b}: failed: {b}: cannot reach it.",
                       f"Rebooting {a}.", f"reach {a}.example.invalid now",
                       f"key /srv/{a}/key", f"{a}={TOKEN}", f"{a} uses {TOKEN}",
                       f"glued s3cr3tPart{a} end", f"glued {a}s3cr3tPart end",
                       "another-ship-with-a-long-name-03 answered")))
    got = store.get(1)
    assert got.error == f"{b} did not answer over SSH."
    assert got.log == (f"{a}: converged", f"{b}: failed: {b}: cannot reach it.",
                       f"Rebooting {a}.", "reach [redacted] now", "key [redacted]",
                       "[redacted]", f"{a} uses [redacted]",
                       "glued [redacted] end", "glued [redacted] end",
                       "[redacted] answered")  # not this job's Ship: a token as ever
    assert TOKEN.encode() not in history.history_path().read_bytes()


def test_a_long_log_is_cut_and_says_so(home):
    store = Operations()
    store.save(op(1, log=tuple(f"line {i} " + "x" * 2000 for i in range(2500))))
    got = store.get(1)
    assert len(got.log) == history.OPS_MAX_LINES + 1
    assert got.log[-1] == "(500 more lines not kept)"
    assert max(len(line) for line in got.log) <= history.OPS_MAX_LINE


def test_it_is_pruned_by_age_and_by_count(home):
    wall = Clock(100 * 86400.0)
    store = Operations(keep=86400.0, keep_count=3, wall=wall)
    store.save(op(1, started=0, ended=wall.now - 2 * 86400))  # older than keep
    store.save(op(2, started=0, ended=wall.now - 10))
    assert [o.id for o in store.find()] == [2]
    for i in range(3, 7):
        store.save(op(i, started=0, ended=wall.now - 10 + i))
    assert [o.id for o in store.find()] == [6, 5, 4]  # the newest keep_count


def test_find_filters_by_ship_and_outcome_newest_first_in_pages(home):
    store = Operations()
    store.save(op(1, ships=("ship-01", "ship-02")))
    store.save(op(2, ships=("ship-02",), outcome="failed"))
    store.save(op(3, ships=("ship-01",)))
    store.save(op(4, ships=()))
    assert [o.id for o in store.find()] == [4, 3, 2, 1]
    assert [o.id for o in store.find(ship="ship-02")] == [2, 1]
    assert [o.id for o in store.find(ship="ship-0")] == []  # a whole name, not a part
    assert [o.id for o in store.find(ship="hip-01")] == []
    assert [o.id for o in store.find(outcome="failed")] == [2]
    assert [o.id for o in store.find(before=3, limit=1)] == [2]
    assert store.find()[0].log == ()  # lists leave the logs out
    assert store.ships() == ["ship-01", "ship-02"]
    assert store.last_id() == 4


def test_an_unwritten_store_reads_as_empty(home):
    store = Operations()
    assert store.find() == [] and store.get(1) is None and store.last_id() == 0
    assert store.ships() == [] and not history.history_path().exists()


def test_samples_and_operations_share_the_file(home):
    """One history file: the charts' samples are untouched by operations."""
    samples = history.History()
    samples.add([history.Sample("ship-01", NOW, up=True, cpu=5.0)])
    Operations().save(op(1))
    assert len(samples.since(0)["ship-01"]) == 1 and Operations().get(1) is not None


# --- jobs, saved ---------------------------------------------------------------------


def _wait(job, timeout=10):
    wait_for(lambda: job.done, timeout)
    return job


def test_a_finished_job_is_saved_with_its_card_text(home):
    from fleetledger.web.actions import Outcome

    jobs = Jobs(app=None, store=Operations())

    def run(job):
        job.say("rebooting")
        job.say(f"using {TOKEN}")
        job.outcomes.append(Outcome("ship-01", True, "back up", ("agents in their modes",)))
        return 0

    job = _wait(jobs.start("reboot", ["ship-01"], run, title="Reboot"))
    got = Operations().get(job.id)
    assert got.title == "Reboot" and got.kind == "reboot" and got.outcome == "ok"
    assert got.log == ("rebooting", "using [redacted]", "--- result ---",
                       "ship-01: back up; agents in their modes")
    assert got.started == job.started and got.ended == job.ended >= job.started


def test_outcomes_failed_by_hand_and_error(home):
    jobs = Jobs(app=None, store=Operations())
    failed = _wait(jobs.start("update", ["ship-01"], lambda job: 1))

    def by_hand(job):
        job.result["by_hand"] = True
        return 1

    manual = _wait(jobs.start("image", [], by_hand))

    def boom(job):
        from fleetledger.errors import FleetError

        raise FleetError("No way through.", hint="Try the other door.")

    broke = _wait(jobs.start("update", ["ship-02"], boom))

    def by_hand_but_broke(job):
        job.result["by_hand"] = True
        boom(job)

    both = _wait(jobs.start("image", [], by_hand_but_broke))
    store = Operations()
    assert store.get(both.id).outcome == "failed"  # an error is never "by hand"
    assert store.get(failed.id).outcome == "failed"
    assert store.get(manual.id).outcome == "by-hand"
    assert store.get(broke.id).error == "No way through. Try the other door."


def test_ids_carry_on_after_a_restart_and_records_survive_it(home):
    first = Jobs(app=None, store=Operations())
    done = [_wait(first.start("update", [f"ship-0{i}"], lambda job: 0)) for i in (1, 2)]
    again = Jobs(app=None, store=Operations())  # FleetLedger started again
    later = _wait(again.start("update", ["ship-01"], lambda job: 0))
    assert later.id == done[-1].id + 1
    assert [o.id for o in Operations().find()] == [later.id, done[1].id, done[0].id]


def test_a_job_cut_short_by_stopping_is_saved_as_stopped(home):
    jobs = Jobs(app=None, store=Operations())
    release = threading.Event()
    job = jobs.start("create", ["ship-03"], lambda j: (j.say("making it"), release.wait(5)))
    wait_for(lambda: job.snapshot())
    assert jobs.stop() == [job]
    got = Operations().get(job.id)
    assert got.outcome == "stopped" and "stopped while" in got.error
    assert got.log == ("making it",)
    release.set()


def test_an_unreadable_history_at_start_never_reuses_an_old_id(home):
    store = Operations()
    store.save(op(5))

    class Busy(Operations):
        def last_id(self):
            raise OSError("database is locked")

    jobs = Jobs(app=None, store=Busy())
    job = _wait(jobs.start("update", ["ship-01"], lambda j: 0))
    assert job.id > 5 and Operations().get(5) is not None


def test_a_broken_history_never_stops_a_job(home):
    class Broken(Operations):
        def save(self, op):
            raise OSError("disk full")

        def last_id(self):
            raise OSError("disk full")

    jobs = Jobs(app=None, store=Broken())
    job = _wait(jobs.start("update", ["ship-01"], lambda j: 0))
    assert job.ok


def test_only_a_finished_job_can_be_dismissed_and_the_store_says_so(home):
    jobs = Jobs(app=None, store=Operations())
    release = threading.Event()
    running = jobs.start("update", ["ship-01"], lambda j: release.wait(5))
    done = _wait(jobs.start("update", ["ship-02"], lambda j: 0))
    assert jobs.dismiss([running.id, done.id]) == [done]
    assert jobs.shown() == [running] and len(jobs.recent()) == 2
    assert jobs.for_ship("ship-02", shown=True) == [] and jobs.for_ship("ship-02") == [done]
    assert Operations().get(done.id).dismissed
    assert jobs.dismiss([done.id]) == []  # once is enough
    release.set()
    _wait(running)
    assert not Operations().get(running.id).dismissed


# --- the web UI: dismissing ----------------------------------------------------------


@pytest.fixture
def act(web):  # noqa: F811
    """The web UI with an operations history, signed in."""
    web.app_ui.jobs = Jobs(web.app, store=Operations())
    web.sign_in()
    web.jobs = web.app_ui.jobs

    def finished(ship="ship-01", title="Update", run=None, kind="update"):
        return _wait(web.jobs.start(kind, [ship], run or (lambda j: j.say("all good")),
                                    title=title))

    web.finished = finished
    return web


def _card(page, job):
    start = page.index(f'id="job-{job.id}"')
    return page[page.rindex("<article", 0, start):page.index("</article>", start)]


def test_a_finished_card_has_dismiss_a_running_one_does_not(act):
    release = threading.Event()
    running = act.jobs.start("update", ["ship-02"], lambda j: release.wait(5), title="Update")
    done = act.finished()
    page = act.client.get("/").text
    card = _card(page, done)
    assert f'action="/jobs/{done.id}/dismiss"' in card and 'hx-swap="delete"' in card
    assert 'name="next" value="/"' in card and f'value="{act.csrf}"' in card
    assert 'aria-label="Dismiss: Update · ship-01"' in card
    assert "/dismiss" not in _card(page, running)
    # A card polled from a Ship's page comes back there.
    polled = act.client.get(f"/jobs/{done.id}",
                            headers={"HX-Current-URL": f"{BASE}/ships/ship-01/logs"})
    assert 'name="next" value="/ships/ship-01/logs"' in polled.text
    release.set()


def test_dismiss_hides_the_card_everywhere_for_every_session(act):
    done = act.finished()
    other = TestClient(act.client.app, base_url=BASE, follow_redirects=False)
    other.get(f"/login?token={act.gate.new_token()}")
    assert f'id="job-{done.id}"' in other.get("/").text
    response = act.post(f"/jobs/{done.id}/dismiss", {"next": "/ships/ship-01"})
    assert response.status_code == 303 and response.headers["location"] == "/ships/ship-01"
    for client in (act.client, other):
        assert f'id="job-{done.id}"' not in client.get("/").text
        assert f'id="job-{done.id}"' not in client.get("/ships/ship-01").text
        assert f'id="job-{done.id}"' not in client.get("/partials/fleet").text
    assert act.client.get("/").text.count("<section class=\"operations activity\">") == 0


def test_other_tabs_polling_the_fleet_get_the_list_without_the_card(act):
    """Another tab's 5 s poll sends the version it drew: after a dismiss it must get
    a redraw (200, without the card), not "unchanged" (204)."""
    done = act.finished()
    page = act.client.get("/").text
    poll = re.search(r'hx-get="(/partials/fleet\?v=[^"]+)"', page).group(1)
    assert act.client.get(poll).status_code == 204
    act.post(f"/jobs/{done.id}/dismiss", {"next": "/"}, headers={"HX-Request": "true"})
    redraw = act.client.get(poll)
    assert redraw.status_code == 200 and f'id="job-{done.id}"' not in redraw.text


def test_dismiss_buttons_have_ids_so_focus_survives_the_fleet_poll(act):
    """htmx puts the focus back after a swap only on an element with an id."""
    a, b = act.finished("ship-01"), act.finished("ship-02")
    page = act.client.get("/partials/fleet").text
    assert f'id="dismiss-job-{a.id}"' in page and f'id="dismiss-job-{b.id}"' in page
    assert 'id="dismiss-all-fleet"' in page
    assert 'id="dismiss-all-ship-01"' not in act.client.get("/ships/ship-01").text  # one card
    act.finished("ship-01")
    assert 'id="dismiss-all-ship-01"' in act.client.get("/ships/ship-01").text


@pytest.mark.parametrize("kind, page", [
    ("fleet-update", "/fleet/actions"),  # the Every Ship page
    ("image", "/settings"),  # Settings > Images
    ("scan", "/new/existing"),  # Add an existing Ship
])
def test_a_dismissed_card_leaves_every_page_that_lists_jobs(act, kind, page):
    job = act.finished(kind=kind, title=f"Card {kind}", run=lambda j: 1)
    assert f'id="job-{job.id}"' in act.client.get(page).text
    act.post(f"/jobs/{job.id}/dismiss", {"next": page})
    assert f'id="job-{job.id}"' not in act.client.get(page).text


def test_dismiss_with_htmx_answers_empty_so_the_card_goes_in_place(act):
    done = act.finished()
    response = act.post(f"/jobs/{done.id}/dismiss", {"next": "/"},
                        headers={"HX-Request": "true"})
    assert response.status_code == 200 and response.text == ""
    assert done.dismissed


def test_a_running_job_cannot_be_dismissed(act):
    release = threading.Event()
    running = act.jobs.start("update", ["ship-01"], lambda j: release.wait(5), title="Update")
    assert act.post(f"/jobs/{running.id}/dismiss", {}, headers={"HX-Request": "true"}
                    ).status_code == 409
    response = act.post(f"/jobs/{running.id}/dismiss", {"next": "/"})
    assert response.status_code == 303 and not running.dismissed
    assert "still running; it can be dismissed once it ends" in act.client.get("/").text
    release.set()


def test_dismiss_needs_csrf_and_never_redirects_off_this_server(act):
    done = act.finished()
    assert act.client.post(f"/jobs/{done.id}/dismiss", data={"next": "/"},
                           headers=ORIGIN).status_code == 403
    assert not done.dismissed
    for bad in ("//example.com/x", "https://example.com/", "/\\example.com", "javascript:x"):
        response = act.post(f"/jobs/{done.id}/dismiss", {"next": bad})
        assert response.headers["location"] == "/"


def test_dismiss_all_finished_keeps_running_cards(act):
    a, b = act.finished("ship-01"), act.finished("ship-02")
    release = threading.Event()
    running = act.jobs.start("reboot", ["ship-01"], lambda j: release.wait(5), title="Reboot")
    page = act.client.get("/").text
    assert 'action="/jobs/dismiss"' in page and "All past operations" in page
    response = act.post("/jobs/dismiss", {"next": "/"})
    assert response.status_code == 303
    page = act.client.get("/").text
    assert "Dismissed 2 finished cards. They stay in Activity." in page
    assert f'id="job-{running.id}"' in page and a.dismissed and b.dismissed
    release.set()


def test_dismiss_all_finished_on_a_ship_page_is_that_ships_only(act):
    a1, a2, b = act.finished("ship-01"), act.finished("ship-01"), act.finished("ship-02")
    page = act.client.get("/ships/ship-01").text
    form = page[page.index('action="/jobs/dismiss"'):]
    assert 'name="ship" value="ship-01"' in form[:form.index("</form>")]
    act.post("/jobs/dismiss", {"next": "/ships/ship-01", "ship": "ship-01"})
    assert a1.dismissed and a2.dismissed and not b.dismissed


def test_one_finished_card_has_no_dismiss_all(act):
    act.finished()
    assert 'action="/jobs/dismiss"' not in act.client.get("/").text


# --- the web UI: the Activity page ---------------------------------------------------


def test_activity_lists_past_operations_including_dismissed(act):
    a = act.finished("ship-01", title="Update")
    b = act.finished("ship-02", title="Reboot", kind="reboot", run=lambda j: 1)
    act.post(f"/jobs/{a.id}/dismiss", {"next": "/"})
    page = act.client.get("/activity").text
    assert page.index(f'id="op-{b.id}"') < page.index(f'id="op-{a.id}"')  # newest first
    first = page[page.index(f'id="op-{a.id}"'):]
    assert ">dismissed</span>" in first[:first.index("</article>")]
    assert ">failed</span>" in page and ">done</span>" in page
    assert f'hx-get="/activity/{a.id}/log"' in page and f'href="/activity/{a.id}"' in page
    assert '<a href="/activity">Activity</a>' in page  # the header


def test_a_long_ship_name_has_its_own_history(act):
    long_name = "my-very-long-ship-name-here-01"
    job = act.finished(long_name)
    page = act.client.get(f"/activity?ship={long_name}").text
    assert f'id="op-{job.id}"' in page and f'href="/activity?ship={long_name}"' in page
    assert "[redacted]" not in page


def test_activity_filters_by_ship_and_each_ship_page_links_there(act):
    a, b = act.finished("ship-01"), act.finished("ship-02")
    assert 'href="/activity?ship=ship-01"' in act.client.get("/ships/ship-01").text
    page = act.client.get("/activity?ship=ship-01").text
    assert f'id="op-{a.id}"' in page and f'id="op-{b.id}"' not in page
    assert act.client.get("/activity?ship=../x").status_code == 200  # ignored, not used
    page = act.client.get("/activity?outcome=failed").text
    assert "No past operations match." in page


def test_a_log_loads_on_demand_and_whole_on_its_own_page(act):
    job = act.finished(run=lambda j: (j.say("step one"), j.say("step two")))
    log = act.client.get(f"/activity/{job.id}/log").text
    assert '<pre class="log">step one\nstep two</pre>' in log and "<html" not in log
    page = act.client.get(f"/activity/{job.id}").text
    assert "step one\nstep two" in page and "<html" in page
    assert act.client.get("/activity/999").status_code == 404
    assert "no longer kept" in act.client.get("/activity/999/log").text


def test_activity_pages_older_operations(act, monkeypatch):
    from fleetledger.web import activity

    monkeypatch.setattr(activity, "PAGE", 2)
    jobs = [act.finished() for _ in range(3)]
    page = act.client.get("/activity").text
    assert f'id="op-{jobs[0].id}"' not in page
    older = re.search(r'href="(/activity\?before=\d+)">Older', page).group(1)
    page = act.client.get(older).text
    assert f'id="op-{jobs[0].id}"' in page and ">Newest</a>" in page


def test_activity_survives_a_restart(act):
    job = act.finished(run=lambda j: j.say(f"secret {TOKEN}"))
    fresh = webapp.create_app(act.app, act.gate, fleet=act.fleet, concierge_=act.concierge,
                              jobs=Jobs(act.app, store=Operations()))
    client = TestClient(fresh, base_url=BASE, follow_redirects=False)
    client.get(f"/login?token={act.gate.new_token()}")
    assert f'id="job-{job.id}"' not in client.get("/").text  # Activity cards: this run's
    page = client.get("/activity").text
    assert f'id="op-{job.id}"' in page
    log = client.get(f"/activity/{job.id}/log").text
    assert "secret [redacted]" in log and TOKEN not in log


def test_running_operations_are_pointed_to_not_listed(act):
    release = threading.Event()
    act.jobs.start("update", ["ship-01"], lambda j: release.wait(5), title="Update")
    page = act.client.get("/activity?ship=ship-01").text
    assert "1 running now: Update (ship-01)" in page and 'id="op-' not in page
    assert "running now" not in act.client.get("/activity?ship=ship-02").text
    assert "1 running now" in act.client.get("/activity").text
    release.set()


def test_without_a_store_the_page_says_so(web):  # noqa: F811
    web.sign_in()
    assert "keeps no operations history" in web.client.get("/activity").text


def test_a_dismissed_failed_create_does_not_come_back_as_its_attempt(act):
    """Activity shows a saved failed attempt only when no job card covers it; a
    dismissed card still covers it (it stays on the New Ship page)."""
    from fleetledger import attempts

    saved = attempts.Attempt(name="ship-09", at=1234.5, options={"name": "ship-09"},
                             error="no room", stage="create")
    attempts.save(saved)

    def run(job):
        job.result["retry"], job.result["retry_at"] = "ship-09", saved.at
        return 1

    job = act.finished("ship-09", title="Make ship-09", kind="create", run=run)
    page = act.client.get("/").text
    assert f'id="job-{job.id}"' in page and 'id="attempt-ship-09"' not in page
    act.post(f"/jobs/{job.id}/dismiss", {"next": "/"})
    page = act.client.get("/").text
    assert f'id="job-{job.id}"' not in page and 'id="attempt-ship-09"' not in page
    assert 'id="attempt-ship-09"' in act.client.get("/new").text
