"""Extra GitHub scopes: ship.toml github.scopes, the token's scopes in status, and the
signin-scopes-differ attention item (contracts/settings.md, ship-ctl.md)."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest
from ship_harness import REPO, assert_valid

sys.path.insert(0, str(REPO / "ship" / "lib"))

from shipctl import shiptoml  # noqa: E402
from shipctl.status import github_scopes  # noqa: E402


def _item(doc: dict) -> dict | None:
    return next((a for a in doc["attention"] if a["code"] == "signin-scopes-differ"), None)


def test_status_reports_the_token_scopes_and_no_extra_by_default(ship):
    rc, doc, _ = ship.ctl("status", "--json")
    assert rc == 0
    assert_valid("ship-status", doc)
    assert doc["auth"]["github"]["scopes"] == ["gist", "read:org", "repo"]
    assert doc["settings"]["github"] == {"scopes": []}
    assert _item(doc) is None  # gh's minimum and nothing chosen: nothing to say


def test_set_github_scopes_then_the_token_lacks_one(ship):
    rc, doc, _ = ship.ctl("set", "github.scopes=workflow,project", "--json")
    assert rc == 0, doc
    toml = tomllib.loads(ship.p("/etc/shipshape/ship.toml").read_text())
    assert toml["github"]["scopes"] == ["workflow", "project"]
    _, doc, _ = ship.ctl("status", "--json")
    assert_valid("ship-status", doc)
    assert doc["settings"]["github"]["scopes"] == ["workflow", "project"]
    item = _item(doc)
    assert item["subject"] == "github"
    assert "lacks project, workflow" in item["message"]
    # Once the token has them, the item goes.
    ship.set(gh={"login": "example-user",
                 "scopes": ["gist", "project", "read:org", "repo", "workflow"]})
    _, doc, _ = ship.ctl("status", "--json")
    assert _item(doc) is None


def test_a_scope_no_longer_chosen_is_said_never_dropped(ship):
    ship.set(gh={"login": "example-user", "scopes": ["gist", "read:org", "repo", "workflow"]})
    _, doc, _ = ship.ctl("status", "--json")
    message = _item(doc)["message"]
    assert "has workflow, not chosen" in message
    # keeping it comes first; signing in again is said to drop it
    assert message.index("To keep workflow, tick it") < message.index("drops it")
    # Scopes outside the choice (given by hand) are not FleetLedger's to judge.
    ship.set(gh={"login": "example-user", "scopes": ["admin:org", "gist", "read:org", "repo"]})
    _, doc, _ = ship.ctl("status", "--json")
    assert _item(doc) is None


def test_nothing_is_said_while_github_is_signed_out_or_scopes_unknown(ship):
    ship.ctl("set", "github.scopes=workflow", "--json")
    ship.set(gh={"login": None})
    _, doc, _ = ship.ctl("status", "--json")
    assert_valid("ship-status", doc)
    assert "scopes" not in doc["auth"]["github"] and _item(doc) is None
    ship.set(gh={"login": "example-user", "scopes": None})  # no scopes line
    _, doc, _ = ship.ctl("status", "--json")
    assert_valid("ship-status", doc)
    assert doc["auth"]["github"]["scopes"] is None and _item(doc) is None


def test_set_refuses_a_scope_outside_the_catalogue(ship):
    before = ship.p("/etc/shipshape/ship.toml").read_text()
    rc, doc, _ = ship.ctl("set", "github.scopes=workflow,delete_repo", "--json")
    assert rc != 0 and doc["error"]["code"] == "invalid-ship-toml"
    assert ship.p("/etc/shipshape/ship.toml").read_text() == before
    rc, doc, _ = ship.ctl("set", "github.scopes=", "--json")  # none: back to the minimum
    assert rc == 0, doc
    assert tomllib.loads(ship.p("/etc/shipshape/ship.toml").read_text())["github"] == {
        "scopes": []}


@pytest.mark.parametrize("text,scopes", [
    ("  - Token scopes: 'gist', 'read:org', 'repo', 'workflow'", ["gist", "read:org", "repo",
                                                                 "workflow"]),
    ("  ✓ Token scopes: gist, repo", ["gist", "repo"]),
    ("  - Token scopes: none", []),
    ("  - Token: gho_****", None),
    ("  - Token scopes: '<b>', 'repo'", ["repo"]),
])
def test_scopes_are_read_from_gh_auth_status(text, scopes):
    assert github_scopes("github.com\n" + text + "\n") == scopes


def test_the_catalogue_is_the_schema_enum():
    schema = json.loads(Path(REPO, "contracts/ship.toml.schema.json").read_text())
    assert tuple(schema["$defs"]["githubScope"]["enum"]) == shiptoml.GITHUB_SCOPES
