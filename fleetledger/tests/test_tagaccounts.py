"""Tag accounts from the command line (tagaccounts.py, ship-fleet signins --accounts,
--set-account, --clear-account): listed, set and cleared in ships.toml, reaching no Ship."""

from __future__ import annotations

import io
import json
import tomllib

import pytest

from fleetledger import cli, schemas, tagaccounts


@pytest.fixture
def run(store, fake_repo):
    out, err = io.StringIO(), io.StringIO()

    def no_ship(inv):
        raise AssertionError("tag accounts reach no Ship")

    app = cli.App(out=out, err=err, store_factory=lambda: store,
                  repo_factory=lambda: fake_repo, ssh_factory=no_ship)

    def go(*argv):
        out.seek(0), out.truncate(), err.seek(0), err.truncate()
        code = cli.main(list(argv), app)
        return code, out.getvalue(), err.getvalue()

    return go


def on_disk(store) -> dict:
    return tomllib.loads(store.path.read_text(encoding="utf-8")).get("tag", {})


def test_listing_shows_every_tag_with_its_ships(run):
    code, out, _ = run("signins", "--accounts")
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["TAG", "CLAUDE", "GCLOUD", "GITHUB", "SHIPS"]
    assert lines[1].split() == ["personal", "you@example.com", "-", "example-user", "ship-02"]
    assert lines[2].split() == ["work", "work@example.com", "work@example.com",
                                "example-work", "ship-01"]
    assert "--set-account" in out


def test_json_listing_matches_its_contract(run):
    code, out, _ = run("signins", "--accounts", "--json")
    assert code == 0
    doc = json.loads(out)
    assert schemas.errors("fleetledger-tag-accounts", doc) == []
    assert doc["schema"] == tagaccounts.SCHEMA
    assert doc["signins"] == ["claude", "gcloud", "github"]
    assert doc["tags"] == [
        {"tag": "personal", "accounts": {"claude": "you@example.com",
                                         "github": "example-user"}, "ships": ["ship-02"]},
        {"tag": "work", "accounts": {"claude": "work@example.com",
                                     "gcloud": "work@example.com",
                                     "github": "example-work"}, "ships": ["ship-01"]}]
    assert doc["conflicts"] == []
    code, out, _ = run("signins", "--accounts", "--tag", "personal", "--json")
    assert [t["tag"] for t in json.loads(out)["tags"]] == ["personal"]
    code, out, _ = run("signins", "--accounts", "--tag", "nosuchtag", "--json")
    assert code == 0 and json.loads(out)["tags"] == []


def test_set_writes_ships_toml_keeping_its_comments(run, store):
    code, out, err = run("signins", "--tag", "personal", "--set-account",
                         "gcloud=you@example.com", "--set-account", "github=example-other")
    assert code == 0, err
    assert "Tag personal expects: claude you@example.com, gcloud you@example.com, " \
        "github example-other." in out
    assert "ship-02" in out
    assert on_disk(store)["personal"] == {"claude": "you@example.com",
                                          "gcloud": "you@example.com",
                                          "github": "example-other"}
    text = store.path.read_text(encoding="utf-8")
    assert "# My fleet (a comment that must survive edits)" in text
    assert oct(store.path.stat().st_mode & 0o777) == "0o600"
    inv = store.load()
    assert inv.expected_account(inv.ship("ship-02"), "gcloud_adc").accounts == {
        "you@example.com"}  # its application default credentials follow gcloud


def test_set_a_new_tag_and_say_when_no_ship_has_it(run, store):
    code, out, _ = run("signins", "--tag", "lab", "--set-account", "claude=lab@example.com")
    assert code == 0
    assert "No Ship has tag lab yet." in out
    assert on_disk(store)["lab"] == {"claude": "lab@example.com"}
    assert "lab" in run("signins", "--accounts")[1]


def test_clear_drops_the_account_and_an_empty_tag(run, store):
    code, out, _ = run("signins", "--tag", "personal", "--clear-account", "github")
    assert code == 0 and "Tag personal expects: claude you@example.com." in out
    assert on_disk(store)["personal"] == {"claude": "you@example.com"}
    code, out, _ = run("signins", "--tag", "personal", "--clear-account", "claude")
    assert code == 0 and "expects: no accounts" in out
    assert "personal" not in on_disk(store)
    # Clearing what is not there changes nothing and is no error.
    assert run("signins", "--tag", "personal", "--clear-account", "claude")[0] == 0
    for tag in ("work",):
        for signin in ("claude", "gcloud", "github"):
            run("signins", "--tag", tag, "--clear-account", signin)
    assert "tag" not in tomllib.loads(store.path.read_text(encoding="utf-8"))
    store.load()  # still a valid inventory


def test_a_ship_whose_tags_disagree_is_named(run, store):
    with store.edit() as ed:
        ed.set_tags("ship-01", ["personal", "work"])
    code, out, _ = run("signins", "--tag", "work", "--set-account", "github=example-user")
    assert code == 0 and "ship-01: its tags expect different claude accounts" in out
    doc = json.loads(run("signins", "--accounts", "--json")[1])
    assert {"ship": "ship-01", "signin": "claude",
            "accounts": ["work@example.com", "you@example.com"]} in doc["conflicts"]


@pytest.mark.parametrize(("argv", "says"), [
    (["--set-account", "agy=you@example.com"], "Unknown sign-in 'agy'"),
    (["--set-account", "gcloud_adc=you@example.com"], "Unknown sign-in 'gcloud_adc'"),
    (["--set-account", "claude"], "SIGNIN=ACCOUNT"),
    (["--set-account", "claude="], "No account given for claude"),
    (["--set-account", "claude=two words@example.com"], "no spaces"),
    (["--set-account", "github=ghp_EXAMPLEEXAMPLEEXAMPLEEXAMPLE0000"], "looks like a token"),
    (["--set-account", "claude=a@example.com", "--clear-account", "claude"],
     "Both set and cleared"),
])
def test_invalid_changes_are_refused_and_nothing_is_written(run, store, argv, says):
    before = store.path.read_bytes()
    code, _, err = run("signins", "--tag", "work", *argv)
    assert code != 0 and says in err
    assert store.path.read_bytes() == before


@pytest.mark.parametrize(("argv", "says"), [
    (["signins", "--tag", "Work!", "--set-account", "claude=a@example.com"], "not a tag"),
    (["signins", "--set-account", "claude=a@example.com"], "--tag TAG"),
    (["signins", "ship-01", "--tag", "work", "--set-account", "claude=a@example.com"],
     "a Ship's name"),
    (["signins", "--accounts", "--service", "claude"], "--service"),
    (["signins", "--tag", "work", "--clear-account", "claude", "--json"], "--json"),
])
def test_wrong_combinations_are_refused(run, store, argv, says):
    before = store.path.read_bytes()
    code, _, err = run(*argv)
    assert code != 0 and says in err
    assert store.path.read_bytes() == before


def test_the_store_refuses_what_the_hand_edited_file_may_not_hold(store):
    """The setter writes through the same validation as every change."""
    from fleetledger.errors import ConfigError

    with pytest.raises(ConfigError, match="unknown sign-in"), store.edit() as ed:
        ed.set_tag_account("work", "agy", "you@example.com")
    with pytest.raises(ConfigError), store.edit() as ed:
        ed.set_tag_account("Not A Tag", "claude", "you@example.com")


def test_listing_runs_while_the_vault_is_locked_and_setting_does_not(run, monkeypatch):
    from fleetledger import vault

    monkeypatch.setattr(vault, "state", lambda: "locked")
    assert run("signins", "--accounts")[0] == 0
    code, _, err = run("signins", "--tag", "work", "--set-account", "claude=a@example.com")
    assert code == 1 and "vault is locked" in err
