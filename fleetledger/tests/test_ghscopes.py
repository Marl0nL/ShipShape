"""Extra GitHub scopes (ghscopes.py): the catalogue, ship-fleet new/set/login, the
Sign-ins list's mismatch row, and the web UI's tickboxes."""

from __future__ import annotations

import copy
import json
import sys
import tomllib
from pathlib import Path

import pytest
from fleetledger.errors import FleetError
from test_cli import HERE, RUNNING, _seed_ship_toml, env  # noqa: F401 - env is a fixture
from test_signinlist import view
from test_web import wait_for, web  # noqa: F401 - web is a fixture

from fleetledger import ghscopes, ops, signinlist

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
sys.path.insert(0, str(CONTRACTS.parent / "ship" / "lib"))
from shipctl.status import scopes_differ as ship_scopes_differ  # noqa: E402 - the Ship's check

MIN = ["gist", "read:org", "repo"]


def scopes_status(chosen, token, *, attention=None):
    """A status document whose ship.toml chose these scopes and whose token has these
    (the Ship's own signin-scopes-differ item when they differ, as ship-ctl says it)."""
    doc = copy.deepcopy(RUNNING)
    settings = json.loads((CONTRACTS / "examples" / "ship-status" / "running-settings.json")
                          .read_text())["settings"]
    doc["settings"] = {**settings, "github": {"scopes": list(chosen)}}
    doc["auth"]["github"]["scopes"] = list(token)
    message = ship_scopes_differ(doc)
    if message if attention is None else attention:
        doc["attention"].append({"code": "signin-scopes-differ", "subject": "github",
                                 "message": message or "m"})
    return doc


# --- the catalogue -------------------------------------------------------------------


def test_the_catalogue_matches_the_contracts():
    ship_toml = json.loads((CONTRACTS / "ship.toml.schema.json").read_text())
    ships_toml = json.loads((CONTRACTS / "ships.toml.schema.json").read_text())
    entry = ships_toml["$defs"]["shipEntry"]["properties"]
    assert tuple(ship_toml["$defs"]["githubScope"]["enum"]) == ghscopes.IDS
    assert tuple(entry["github_scopes"]["items"]["enum"]) == ghscopes.IDS
    assert "workflow" in ghscopes.IDS
    assert not any(s.startswith(("delete", "admin")) for s in ghscopes.IDS)  # kept out
    assert all(s.explain and s.label for s in ghscopes.CATALOGUE)


def test_check_keeps_catalogue_order_and_refuses_the_rest():
    assert ghscopes.parse("write:packages,workflow") == ["workflow", "write:packages"]
    assert ghscopes.parse("") == [] and ghscopes.parse("none") == []
    with pytest.raises(FleetError, match="delete_repo"):
        ghscopes.parse("workflow,delete_repo")


def test_only_catalogue_scopes_count_as_a_difference():
    assert not ghscopes.difference([], [*MIN, "admin:org"])  # given by hand: not ours
    assert not ghscopes.difference(["workflow"], None)  # not signed in: nothing to compare
    d = ghscopes.difference(["workflow"], [*MIN, "project"])
    assert (d.missing, d.extra) == (("workflow",), ("project",))
    assert d.describe() == "lacks workflow and has project, not chosen"
    assert d.keep(["workflow"]) == ["workflow", "project"]


def test_the_ships_own_choice_comes_first_then_the_inventory_copy():
    from fleetledger.inventory import Ship

    ship = Ship(name="ship-01", id="x", driver="manual", names=("ship-01.local",),
                host_key="ssh-ed25519 AAAA", github_scopes=("project",))
    assert ghscopes.chosen(ship, None) == ["project"]
    assert ghscopes.chosen(ship, {"settings": {"github": {"scopes": ["workflow"]}}}) == [
        "workflow"]


# --- ship-fleet new, set, login, status ---------------------------------------------


def test_new_records_the_scopes_in_the_inventory_and_the_seed(env, home):  # noqa: F811
    code, _, err = env.run("new", "ship-21", "--github-scopes", "workflow,project")
    assert code == 0, err
    assert env.store.load().ship("ship-21").github_scopes == ("workflow", "project")
    assert _seed_ship_toml(home, "ship-21")["github"] == {"scopes": ["workflow", "project"]}
    assert env.run("new", "ship-22")[0] == 0  # the default: none, as before
    assert env.store.load().ship("ship-22").github_scopes == ()
    assert "github" not in _seed_ship_toml(home, "ship-22")
    code, _, err = env.run("new", "ship-23", "--github-scopes", "delete_repo")
    assert code == 1 and "Unknown GitHub scope delete_repo" in err


def test_set_github_scopes_goes_to_the_ship_and_the_inventory(env):  # noqa: F811
    code, _, err = env.run("set", "ship-01", "github_scopes=write:packages,workflow")
    assert code == 0, err
    sent = [c for name, c in env.ship.calls if c[3:4] == ["set"]]
    assert sent[-1][4:] == ["github.scopes=workflow,write:packages", "--json"]
    assert env.store.load().ship("ship-01").github_scopes == ("workflow", "write:packages")
    assert env.run("set", "ship-01", "github_scopes=")[0] == 0  # none again
    assert env.store.load().ship("ship-01").github_scopes == ()
    calls = len(env.ship.calls)
    code, _, err = env.run("set", "ship-01", "github_scopes=admin:org")
    assert code == 1 and "Unknown GitHub scope" in err
    assert len(env.ship.calls) == calls  # refused before reaching the Ship


def test_assignments_are_checked_like_every_other_key():
    assert ops.parse_assignments(["github_scopes=project,workflow"]) == [
        "github.scopes=workflow,project"]
    assert ops.parse_assignments(["github.scopes=none"]) == ["github.scopes="]


def github_spawner(e):
    """Play GitHub's recorded device flow, whatever command the Ship is asked to run."""
    from fleetledger.concierge import engine

    def spawn(argv):
        e.argvs.append(argv)
        return engine.spawn([sys.executable, str(HERE / "fake_cli.py"),
                             str(HERE / "fixtures" / "concierge" / "github.txt")])

    return spawn


def test_login_with_scopes_chooses_them_then_signs_in_with_them(env):  # noqa: F811
    env.ship.status_override["ship-01"] = scopes_status([], MIN)
    env.app.spawner = github_spawner(env)
    code, out, err = env.run("login", "ship-01", "github", "--scopes", "workflow")
    assert code == 0, err
    set_calls = [c for name, c in env.ship.calls if c[3:4] == ["set"]]
    assert set_calls[-1][4:] == ["github.scopes=workflow", "--json"]
    assert env.store.load().ship("ship-01").github_scopes == ("workflow",)
    # the fake Ship still reports none chosen: what it reports is what counts
    assert "ship-01: GitHub scopes workflow" in out
    code, _, err = env.run("login", "ship-01", "claude", "--scopes", "workflow")
    assert code == 1 and "--scopes is for the GitHub sign-in" in err


def test_login_refreshes_a_signed_in_token_whose_scopes_differ(env):  # noqa: F811
    env.ship.status_override["ship-01"] = scopes_status(["workflow"], MIN)
    env.app.spawner = github_spawner(env)
    code, out, err = env.run("login", "ship-01", "github")
    assert code == 0, err
    command = env.argvs[-1][-1]
    assert "gh auth refresh --hostname github.com --scopes workflow" in command
    assert "refreshes the token's scopes, to add workflow" in out


def test_status_shows_the_chosen_scopes_and_the_token(env):  # noqa: F811
    env.ship.status_override["ship-01"] = scopes_status(["workflow", "project"],
                                                        [*MIN, "workflow"])
    _, out, _ = env.run("status", "ship-01")
    assert "GitHub scopes: chosen workflow, project; the token has workflow" in out
    assert "Scopes differ" in out
    _, out, _ = env.run("ls")
    assert "Scopes differ" in out


# --- the Sign-ins list ----------------------------------------------------------------


def test_a_token_whose_scopes_differ_needs_you():
    item = {"code": "signin-scopes-differ", "subject": "github",
            "message": "The GitHub sign-in lacks workflow, chosen for this Ship: sign in "
                       "to GitHub again with the chosen scopes."}
    v = view("ship-01", attention=[item])
    row = next(r for r in signinlist.rows([v], None) if r.service == "github")
    assert (row.group, row.state, row.level) == ("action", "scopes differ", "warning")
    assert row.signed_in and "lacks workflow" in row.message
    assert signinlist.fix_command(row) == "ship-fleet login ship-01 github"
    assert signinlist.needing_action([v], None) == 1


def test_a_healthy_github_row_names_its_extra_scopes():
    v = view("ship-01", auth={"github": {"state": "ok", "account": "example-work",
                                         "scopes": [*MIN, "workflow"]}})
    row = next(r for r in signinlist.rows([v], None) if r.service == "github")
    assert row.group == "healthy" and row.message.endswith("Extra scopes: workflow.")


def test_a_ship_that_does_not_use_github_has_no_scopes_item():
    from fleetledger import signins

    v = view("ship-01", used=["claude"])
    items = [{"code": "signin-scopes-differ", "subject": "github", "message": "m"}]
    assert signins.filter_attention(v.ship, items, None) == []


# --- the web UI -----------------------------------------------------------------------


def test_settings_tickboxes_save_the_choice_and_offer_a_new_sign_in(web):  # noqa: F811
    web.sign_in()
    page = web.client.get("/ships/ship-01/settings").text
    assert 'id="github-scopes"' in page
    for s in ghscopes.CATALOGUE:
        assert f'name="scope" value="{s.id}">' in page  # none ticked by default
    assert "Sign in again with these scopes" not in page
    web.ship.status_override["ship-01"] = scopes_status(["workflow"], MIN)
    answer = web.post("/ships/ship-01/github-scopes", {"scope": ["workflow"]})
    assert answer.status_code == 303
    sent = [c for name, c in web.ship.calls if name == "ship-01" and c[3:4] == ["set"]]
    assert sent[-1][4:] == ["github.scopes=workflow", "--json"]
    assert web.store.load().ship("ship-01").github_scopes == ("workflow",)
    page = web.client.get("/ships/ship-01/settings").text
    assert "GitHub scopes workflow." in page  # the flash
    assert "nothing changes on the Ship until you sign in again" in page
    assert 'name="scope" value="workflow" checked' in page
    assert "Sign in again with these scopes" in page and "lacks workflow" in page
    overview = web.client.get("/ships/ship-01").text
    assert "Extra scopes</a>: chosen workflow; the token has none" in overview
    assert "Sign in again with these scopes" in overview


def test_a_bad_scope_from_the_page_never_reaches_the_ship(web):  # noqa: F811
    web.sign_in()
    calls = len(web.ship.calls)
    web.post("/ships/ship-01/github-scopes", {"scope": ["delete_repo"]})
    assert len(web.ship.calls) == calls
    assert "Unknown GitHub scope" in web.client.get("/ships/ship-01/settings").text


def test_the_sign_ins_page_row_refreshes_the_scopes(web):  # noqa: F811
    web.sign_in()
    web.ship.status_override["ship-01"] = scopes_status(["workflow"], [*MIN, "project"])
    web.fleet.poll()
    page = web.client.get("/signin").text
    assert "scopes differ" in page and "Sign in again and drop project" in page
    assert "Keep project: tick it in Settings" in page  # keeping it is offered first
    assert page.index("Keep project") < page.index("Sign in again and drop project")
    web.fixture["path"] = web.fixture["path"].parent / "github.txt"
    web.post("/signin", {"ship": "ship-01", "service": "github", "from": "signin"})
    wait_for(lambda: web.argvs)
    command = web.argvs[-1][-1]
    assert ("gh auth refresh --hostname github.com --scopes workflow --remove-scopes project"
            in command)


def test_the_new_ship_form_offers_the_scopes_unticked(web):  # noqa: F811
    from fleetledger.web import newship

    web.sign_in()
    page = web.client.get("/new").text
    assert "Extra GitHub scopes" in page
    assert 'name="github_scope" value="workflow">' in page
    inv = web.store.load()
    _, argv = newship.argv({"name": ["ship-30"], "github_scope": ["workflow"]}, web.app, inv)
    assert argv[argv.index("--github-scopes") + 1] == "workflow"
    _, argv = newship.argv({"name": ["ship-31"]}, web.app, inv)
    assert "--github-scopes" not in argv
    nf = newship.refill(newship.choices(web.app, inv), {"github_scope": ["project"]})
    assert nf.github_scopes_on == ("project",)
    fields = newship.fields_from({"name": "ship-32", "github_scopes": "workflow"}, nf)
    assert fields["github_scope"] == ["workflow"]


def test_the_seed_ship_toml_validates(env, home):  # noqa: F811
    assert env.run("new", "ship-24", "--github-scopes", "write:packages")[0] == 0
    doc = _seed_ship_toml(home, "ship-24")
    from fleetledger import schemas

    assert schemas.errors("ship.toml", doc) == []
    assert tomllib  # ship.toml is TOML; _seed_ship_toml parsed it


# --- review fixes: what the Ship reports, a scope given by hand ------------------------


def test_a_scope_this_fleetledger_does_not_know_never_breaks_a_page(web, env):  # noqa: F811
    """A newer FleetLedger's catalogue, or a hand-edited ship.toml: shown apart, never
    asked for, never an error."""
    doc = scopes_status(["workflow", "codespace"], [*MIN, "workflow"], attention=False)
    assert ghscopes.chosen(None, doc) == ["workflow"] and ghscopes.unknown(doc) == ["codespace"]
    web.sign_in()
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    assert web.client.get("/ships/ship-01").status_code == 200
    page = web.client.get("/ships/ship-01/settings").text
    assert "also chooses codespace, which this\n      FleetLedger does not know" in page
    env.ship.status_override["ship-01"] = doc
    code, out, _ = env.run("status", "ship-01")
    assert code == 0 and "GitHub scopes: chosen workflow" in out


def test_a_scope_given_by_hand_is_kept_first_never_dropped_by_the_suggested_fix(web):  # noqa: F811
    """workflow on the token, not ticked: keeping it (ticking it) is what is offered;
    signing in again says it drops it."""
    from fleetledger.web import dashboard
    from fleetledger.web.fleet import attention_items

    web.sign_in()
    doc = scopes_status([], [*MIN, "workflow"])
    message = doc["attention"][-1]["message"]
    assert message.index("To keep workflow, tick it") < message.index("drops it")
    web.ship.status_override["ship-01"] = doc
    web.fleet.poll()
    row = next(r for r in signinlist.rows(web.fleet.snapshot.views, None)
               if (r.ship, r.service) == ("ship-01", "github"))
    assert row.drops == ("workflow",) and row.keep == ("workflow",)
    assert signinlist.fix_command(row) == "ship-fleet set ship-01 github_scopes=workflow"
    settings = web.client.get("/ships/ship-01/settings").text
    assert "To keep workflow, tick it above and save." in settings
    assert "Sign in again and drop workflow" in settings
    assert "Sign in again with these scopes" not in settings
    overview = web.client.get("/ships/ship-01").text
    assert "Keep workflow: tick it in Settings" in overview
    assert "Sign in again with these scopes" not in overview
    view_ = web.fleet.snapshot.by_name["ship-01"]
    item = next(i for i in attention_items([view_]) if i.code == "signin-scopes-differ")
    action = dashboard.action_for(item.code, "ship-01", view_, item.signin)
    assert (action.kind, action.value) == ("link", "/ships/ship-01/settings#github-scopes")
    # Ticking it: nothing differs any more, nothing was signed in again
    web.ship.status_override["ship-01"] = scopes_status(["workflow"], [*MIN, "workflow"])
    web.fleet.poll()
    assert "Sign in again" not in web.client.get("/ships/ship-01/settings").text


def test_only_missing_scopes_offer_signing_in_again_from_needs_attention():
    """A token that only lacks a chosen scope: Needs attention's one button signs in."""
    from fleetledger.web import dashboard
    from fleetledger.web.fleet import attention_items

    v = view("ship-01")
    v.status = scopes_status(["workflow"], MIN)
    v.attention = [a for a in v.status["attention"] if a["code"] == "signin-scopes-differ"]
    item = next(i for i in attention_items([v]) if i.code == "signin-scopes-differ")
    assert item.signin == "github"
    action = dashboard.action_for(item.code, "ship-01", v, item.signin)
    assert (action.kind, action.label) == ("signin", "Sign in again with these scopes")


def test_no_scopes_row_for_a_github_sign_in_that_is_not_there():
    item = {"code": "signin-scopes-differ", "subject": "github", "message": "m"}
    v = view("ship-01", auth={"github": ("missing", None)}, attention=[item])
    row = next(r for r in signinlist.rows([v], None) if r.service == "github")
    assert row.state == "missing"


def test_login_never_signs_in_with_scopes_the_ship_refused(env, monkeypatch):  # noqa: F811
    from test_cli import FakeShip, result

    answer = FakeShip.__call__

    def refuse_set(self, ship, command):
        if command[3:4] == ["set"]:
            self.calls.append((ship.name, command))
            err = {"code": "invalid-ship-toml", "message": "github.scopes: not allowed"}
            return 1, json.dumps(result("set", ok=False, error=err)), ""
        return answer(self, ship, command)

    monkeypatch.setattr(FakeShip, "__call__", refuse_set)
    env.app.spawner = github_spawner(env)
    code, out, _ = env.run("login", "ship-01", "github", "--scopes", "workflow")
    assert code == 1 and "not allowed" in out
    assert env.argvs == []  # no sign-in started
    assert env.store.load().ship("ship-01").github_scopes == ()
