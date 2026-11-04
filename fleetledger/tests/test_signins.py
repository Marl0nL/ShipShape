"""Which sign-ins a Ship uses (signins.py, ship-fleet signins)."""

from __future__ import annotations

import pytest
from fleetledger.errors import FleetError
from fleetledger.inventory import Ship

from fleetledger import signins


def ship(used=None):
    return Ship(name="ship-01", id="7c9e6679-7425-40de-944b-e07fc1f90ae7", driver="manual",
                names=("ship-01.local",), host_key="ssh-ed25519 AAAA",
                signins=None if used is None else tuple(used))


def test_absent_means_every_sign_in():
    assert signins.used(ship()) == ("claude", "gcloud", "github")
    assert signins.off_status_keys(ship()) == set()


def test_gcloud_off_covers_its_application_default_credentials():
    s = ship(["claude", "github"])
    assert signins.off_status_keys(s) == {"gcloud", "gcloud_adc"}
    assert not signins.provider_on(s, "gcloud") and not signins.provider_on(s, "gcloud-adc")
    assert signins.provider_on(s, "claude")


def test_changes():
    assert signins.change(ship(), ["-gcloud"]) == ["claude", "github"]
    assert signins.change(ship(["claude"]), ["+github", "+claude"]) == ["claude", "github"]
    assert signins.change(ship(), ["github,claude"]) == ["claude", "github"]
    with pytest.raises(FleetError, match="Unknown sign-in"):
        signins.change(ship(), ["+agy"])


def test_attention_about_unused_sign_ins_is_dropped():
    s = ship(["claude"])
    items = [{"code": "signin-missing", "subject": "claude", "message": "m"},
             {"code": "signin-expired", "subject": "gcloud_adc", "message": "m"},
             {"code": "signin-wrong-account", "subject": "github", "message": "m"},
             {"code": "tool-waiting", "subject": "gh-axi", "message": "m"},
             {"code": "tool-waiting", "subject": "both", "message": "m"},
             {"code": "reboot-required", "message": "m"}]
    tools = {"gh-axi": {"waiting_for": ["github"]}, "both": {"waiting_for": ["github", "claude"]}}
    kept = signins.filter_attention(s, items, tools)
    assert [(i["code"], i.get("subject")) for i in kept] == [
        ("signin-missing", "claude"), ("tool-waiting", "both"), ("reboot-required", None)]
    assert signins.filter_attention(ship(), items, tools) == items
