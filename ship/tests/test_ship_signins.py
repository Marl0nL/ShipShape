"""The Ship's sign-in registry (ship/lib/shipctl/signins.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ship" / "lib"))

from shipctl import notice, signins, status  # noqa: E402


def test_status_keys_and_checks():
    assert signins.KEYS == ("claude", "gcloud", "gcloud_adc", "github")
    assert status.AUTH_SERVICES == signins.KEYS
    assert status.AUTH_PROBES == {
        "claude": status.probe_claude_auth,
        "gcloud": status.probe_gcloud_auth,
        "gcloud_adc": status.probe_adc,
        "github": status.probe_github_auth,
    }
    assert signins.CORE == {"claude"}  # missing is always an attention item
    assert status.SERVICE_LABELS["gcloud_adc"] == "Application Default Credentials"


def test_status_document_names_every_key():
    """The status contract requires an entry for each registered key."""
    doc = json.loads((REPO / "contracts" / "ship-status.schema.json").read_text())
    assert set(doc["properties"]["auth"]["required"]) == set(signins.KEYS)


def test_notify_services_are_fleetledgers_provider_ids():
    assert sorted(signins.NOTIFY) == ["claude", "gcloud", "gcloud-adc", "github"]
    assert notice.SERVICES["gcloud-adc"] == ("gcloud_adc",
                                             "Google Application Default Credentials")
    assert all(n.key in signins.KEYS for n in signins.NOTICES)
