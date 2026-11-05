"""The Ship Settings tab's fields (web/shipsettings.py): current values from the
Ship's own report, and only what changed sent as `ship-fleet set` assignments."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fleetledger.errors import FleetError
from fleetledger.web import shipsettings

from fleetledger import ops

REPORTED = {  # a status `settings` block, as ship-ctl status reports it (ui-6s)
    "ship": {"tags": ["work"], "timezone": "UTC"},
    "claude": {"remote_control": "on", "default_mode": "auto", "allow_bypass": True,
               "update_channel": "latest", "session_prefix": "ship-01"},
    "agent": {"sudo": False},
    "network": {"lan_guard": True, "lan_guard_allow": ["192.0.2.0/24"], "ssh_listen": "any"},
    "updates": {"auto_reboot": False, "reboot_time": "04:00"},
    "owner": None,
    "autostart": [],
}


@dataclass
class View:
    status: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    extra: dict = field(default_factory=dict)


def posted(**values):
    return {k: [v] for k, v in values.items()}


def test_every_set_key_but_tags_is_on_the_tab():
    """Tags have their own section (and owner is the fleet's, ship-fleet owner)."""
    on_tab = set(shipsettings.FIELDS) | {"ssh_listen"}
    assert on_tab == set(ops.SET_KEYS) - {"tags"}


def test_current_values_come_from_the_ships_report():
    view = View(status={"claude": {}}, settings=REPORTED)
    now = {k: shipsettings.current(view, f) for k, f in shipsettings.FIELDS.items()}
    assert now["allow_bypass"] == "true" and now["sudo"] == "false"
    assert now["lan_guard_allow"] == "192.0.2.0/24" and now["timezone"] == "UTC"
    assert now["session_prefix"] == "ship-01" and now["reboot_time"] == "04:00"
    assert shipsettings.current(view, shipsettings.SSH_LISTEN) == "any"


def test_an_older_ship_claims_no_value_it_does_not_know():
    old = View(status={"claude": {"remote_control": "on", "default_mode": "plan",
                                  "allow_bypass": False, "channel": "stable"}})
    for f in shipsettings.FIELDS.values():
        assert shipsettings.current(old, f) is None
    # what its status does say, elsewhere: update_channel is the claude block's channel
    assert shipsettings.older_hint(old, shipsettings.FIELDS["update_channel"]) == "stable"
    assert shipsettings.older_hint(old, shipsettings.FIELDS["allow_bypass"]) == "false"
    assert shipsettings.older_hint(old, shipsettings.FIELDS["timezone"]) is None
    assert shipsettings.current(None, shipsettings.FIELDS["sudo"]) is None  # never answered


def test_only_what_changed_is_sent():
    view = View(status={}, settings=REPORTED)
    unchanged = {k: [shipsettings.current(view, f)] for k, f in shipsettings.FIELDS.items()}
    assert shipsettings.assignments(unchanged, view) == []
    changed = {**unchanged, **posted(sudo="true", timezone="Europe/Berlin",
                                     session_prefix="lab", lan_guard_allow="192.0.2.0/24, ")}
    assert shipsettings.assignments(changed, view) == [
        "session_prefix=lab", "sudo=true", "timezone=Europe/Berlin"]
    # a group posts only its own fields: the rest are left alone
    assert shipsettings.assignments(posted(reboot_time="03:30"), view) == ["reboot_time=03:30"]


def test_for_an_older_ship_only_what_was_chosen_is_sent():
    view = View(status={"claude": {}})
    fields = posted(remote_control="", default_mode="plan", sudo="", reboot_time="",
                    lan_guard_allow="")
    assert shipsettings.assignments(fields, view) == ["default_mode=plan"]


@pytest.mark.parametrize("fields,message", [
    (posted(remote_control="maybe"), "Remote Control: not one"),
    (posted(sudo="yes"), "Agent sudo: not one"),
    (posted(reboot_time="25:99"), "HH:MM"),
    (posted(timezone="not a zone"), "IANA"),
    (posted(session_prefix="-bad"), "session_prefix"),
    (posted(lan_guard_allow="not-an-address"), "addresses or CIDRs"),
])
def test_bad_values_are_refused_before_anything_is_sent(fields, message):
    with pytest.raises(FleetError, match=message):
        shipsettings.assignments(fields, View(status={}, settings=REPORTED))


def test_ssh_listen_is_its_own_form():
    view = View(status={}, settings=REPORTED)
    fields = posted(ssh_listen="overlay", sudo="true")
    assert shipsettings.assignments(fields, view, ("ssh_listen",)) == ["ssh_listen=overlay"]
    with pytest.raises(FleetError, match="SSH listens: not one"):
        shipsettings.assignments(posted(ssh_listen="lan"), view, ("ssh_listen",))
