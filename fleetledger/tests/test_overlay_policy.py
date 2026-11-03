"""The tailnet policy: HuJSON handling, merging the Ships' rules, and checking them."""

from __future__ import annotations

import pytest
from fleetledger.overlay import hujson
from fleetledger.overlay import rules as pol

TAGS = ["tag:ship"]

USER_POLICY = """{
  // My tailnet. "Comments" with quotes, commas, ] and } must survive.
  "groups": {"group:admins": ["you@example.com"]},
  "tagOwners": {
    "tag:server": ["group:admins"], // a trailing comment
  },
  "acls": [
    {"action": "accept", "src": ["group:admins"], "dst": ["tag:server:*"]},
  ],
  /* a block comment */
}
"""


# --- HuJSON ---------------------------------------------------------------------------


def test_hujson_parses_comments_and_trailing_commas():
    doc = hujson.parse(USER_POLICY)
    assert doc["tagOwners"] == {"tag:server": ["group:admins"]}
    assert doc["acls"][0]["dst"] == ["tag:server:*"]


@pytest.mark.parametrize("text, value", [
    ('{"a": "x // not a comment"}', "x // not a comment"),
    ('{"a": "x /* not */ y"}', "x /* not */ y"),
    ('{"a": "comma, ] inside",}', "comma, ] inside"),
    ('{"a": "esc \\" quote"}', 'esc " quote'),
])
def test_hujson_leaves_strings_alone(text, value):
    assert hujson.parse(text)["a"] == value


@pytest.mark.parametrize("bad", ['{"a": 1', '{"a": "open}', '{/* never closed "a": 1}'])
def test_hujson_refuses_broken_input(bad):
    with pytest.raises(hujson.HuJSONError):
        hujson.parse(bad)


def test_insertion_keeps_everything_else():
    text = hujson.insert_first(USER_POLICY, "acls", '{"x": 1}')
    for kept in ("// My tailnet.", "// a trailing comment", "/* a block comment */",
                 '"tag:server": ["group:admins"]'):
        assert kept in text
    assert hujson.parse(text)["acls"][0] == {"x": 1}
    text = hujson.add_key(text, "tests", "[]")
    assert hujson.parse(text)["tests"] == []


# --- merging ----------------------------------------------------------------------------


def test_merge_adds_only_what_is_missing_and_keeps_the_rest():
    new, added = pol.merge(USER_POLICY, TAGS)
    doc = hujson.parse(new)
    assert doc["tagOwners"] == {"tag:ship": ["autogroup:admin"],
                                "tag:server": ["group:admins"]}
    assert {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:ship:22"]} in doc["acls"]
    assert {"action": "accept", "src": ["group:admins"], "dst": ["tag:server:*"]} in doc["acls"]
    assert doc["tests"] == [{"src": "tag:ship", "deny": ["tag:ship:22"]}]
    assert "// My tailnet." in new and "/* a block comment */" in new
    assert len(added) == 3
    # Merging again changes nothing.
    again, added_again = pol.merge(new, TAGS)
    assert again == new and added_again == []


def test_merge_never_changes_an_existing_owner():
    text = '{"tagOwners": {"tag:ship": ["group:ops"]}, "acls": []}'
    new, added = pol.merge(text, TAGS)
    assert hujson.parse(new)["tagOwners"] == {"tag:ship": ["group:ops"]}
    assert not any("tagOwners" in a for a in added)


def test_merge_uses_grants_when_the_policy_does():
    text = '{"grants": [{"src": ["group:admins"], "dst": ["tag:db"], "ip": ["*"]}]}'
    doc = hujson.parse(pol.merge(text, TAGS)[0])
    assert {"src": ["autogroup:member"], "dst": ["tag:ship"], "ip": ["tcp:22"]} in doc["grants"]
    assert "acls" not in doc


def test_merge_into_an_empty_policy():
    doc = hujson.parse(pol.merge("{}", TAGS)[0])
    assert set(doc) == {"tagOwners", "acls", "tests"}


# --- findings -------------------------------------------------------------------------


def by_name(findings):
    return {f.name: f for f in findings}


def test_findings_on_the_template_policy_are_all_ok():
    import json

    from fleetledger.overlay.providers import policy_doc

    found = pol.findings(json.loads(json.dumps(policy_doc(TAGS))), TAGS)
    assert all(f.ok for f in found), [f for f in found if not f.ok]


def test_the_default_allow_all_policy_fails():
    """Tailscale's starter policy lets everything reach everything, Ships included."""
    doc = {"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}
    f = by_name(pol.findings(doc, TAGS))
    assert not f["Ships cannot start connections"].ok
    assert "your laptop" in f["Ships cannot start connections"].detail
    assert f["your devices reach Ships on port 22"].ok  # * covers members too


def test_a_rule_from_the_ship_tag_fails():
    doc = {"tagOwners": {"tag:ship": ["autogroup:admin"]},
           "acls": [{"action": "accept", "src": ["autogroup:member"], "dst": ["tag:ship:22"]},
                    {"action": "accept", "src": ["tag:ship"], "dst": ["tag:db:5432"]}]}
    assert not by_name(pol.findings(doc, TAGS))["Ships cannot start connections"].ok


def test_a_grant_from_the_ship_tag_fails():
    doc = {"grants": [{"src": ["tag:ship"], "dst": ["autogroup:member"], "ip": ["*"]}]}
    assert not by_name(pol.findings(doc, TAGS))["Ships cannot start connections"].ok


@pytest.mark.parametrize("owners", [["autogroup:member"], ["*"], ["group:x", "autogroup:member"]])
def test_wide_tag_owners_fail(owners):
    doc = {"tagOwners": {"tag:ship": owners}}
    assert not by_name(pol.findings(doc, TAGS))["tag owner tag:ship"].ok


def test_missing_pieces_are_reported():
    f = by_name(pol.findings({}, TAGS))
    assert not f["tag owner tag:ship"].ok
    assert not f["your devices reach Ships on port 22"].ok
    test = f["policy test: Ships cannot reach each other"]
    assert not test.ok and test.severity == "warn"


def test_a_rule_to_ships_on_another_port_is_not_ssh():
    doc = {"acls": [{"action": "accept", "src": ["autogroup:member"], "dst": ["tag:ship:443"]}]}
    assert not by_name(pol.findings(doc, TAGS))["your devices reach Ships on port 22"].ok


@pytest.mark.parametrize("src", ["autogroup:tagged", "autogroup:danger-all", "*", "tag:ship"])
def test_sources_that_cover_ships_fail(src):
    doc = {"acls": [{"action": "accept", "src": [src], "dst": ["*:*"]}]}
    assert not by_name(pol.findings(doc, TAGS))["Ships cannot start connections"].ok


def test_a_rule_from_members_to_another_host_is_not_ssh_to_ships():
    doc = {"acls": [{"action": "accept", "src": ["autogroup:member"], "dst": ["tag:db:22"]}]}
    assert not by_name(pol.findings(doc, TAGS))["your devices reach Ships on port 22"].ok


@pytest.mark.parametrize("rule", [
    {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:ship:20-30"]},
    {"action": "accept", "src": ["autogroup:member"], "dst": ["tag:ship:443,22"]},
])
def test_port_lists_and_ranges_count(rule):
    assert by_name(pol.findings({"acls": [rule]}, TAGS))["your devices reach Ships on port 22"].ok


@pytest.mark.parametrize("ip, ok", [(["tcp:20-30"], True), (["udp:22"], False),
                                    (["tcp:443"], False), (["*"], True)])
def test_grant_ports(ip, ok):
    doc = {"grants": [{"src": ["autogroup:member"], "dst": ["tag:ship"], "ip": ip}]}
    assert by_name(pol.findings(doc, TAGS))["your devices reach Ships on port 22"].ok is ok


def test_empty_tag_owners_mean_admins():
    assert by_name(pol.findings({"tagOwners": {"tag:ship": []}}, TAGS))["tag owner tag:ship"].ok


def test_merge_with_a_comment_between_key_and_bracket():
    text = '{\n  "tests": // none yet\n  [\n  ],\n  "acls": /* mine */ [],\n}\n'
    new, added = pol.merge(text, TAGS)
    doc = hujson.parse(new)
    assert doc["tests"] == [{"src": "tag:ship", "deny": ["tag:ship:22"]}]
    assert "// none yet" in new and "/* mine */" in new and len(added) == 3
