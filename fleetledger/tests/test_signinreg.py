"""The sign-in registry (signinreg.py) and the ships.toml ids it checks."""

from __future__ import annotations

import shutil
import tomllib
from pathlib import Path

import pytest
from fleetledger.concierge import providers
from fleetledger.errors import ConfigError
from fleetledger.store import validate

from fleetledger import schemas, signinreg

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
EXAMPLE = CONTRACTS / "examples" / "ships.toml" / "basic.toml"


def test_built_ins_as_before():
    reg = signinreg.registry()
    assert reg.choices == ("claude", "gcloud", "github")
    assert reg.labels == {"claude": "Claude", "gcloud": "Google Cloud", "github": "GitHub"}
    assert reg.status_keys == {"claude": ("claude",), "gcloud": ("gcloud", "gcloud_adc"),
                               "github": ("github",)}
    assert reg.providers == {"claude": ("claude",), "gcloud": ("gcloud", "gcloud-adc"),
                             "github": ("github",)}
    assert reg.provider_ids == ("claude", "gcloud", "gcloud-adc", "github")
    assert reg.service_keys == ("claude", "gcloud", "gcloud_adc", "github")
    assert reg.service_labels == {"claude": "Claude", "gcloud": "Google Cloud",
                                  "gcloud_adc": "Google Cloud ADC", "github": "GitHub"}
    assert reg.service_shorts == {"claude": "Claude", "gcloud": "gcloud",
                                  "gcloud_adc": "ADC", "github": "GitHub"}
    assert reg.service_provider == {"claude": "claude", "gcloud": "gcloud",
                                    "gcloud_adc": "gcloud-adc", "github": "github"}
    assert reg.signin_of_service("gcloud_adc") == "gcloud"
    assert reg.signin_of_service("unknown_key") == "unknown_key"
    assert reg.login_help() == "claude, gcloud (with ADC), gcloud-adc, github or all"


def test_every_flow_is_registered():
    """Each provider file is a sign-in or part of one, and still parses."""
    assert set(providers.load_all()) == set(signinreg.registry().provider_ids)


def _copy(tmp_path: Path) -> Path:
    folder = tmp_path / "providers"
    shutil.copytree(signinreg.PROVIDERS_DIR, folder)
    return folder


EXTRA = """\
id = "exampledocs"
label = "Example Docs"
mode = "device-code"
run = "example-cli auth login"
timeout = 300

[signin]
label = "Example Docs"
order = 0

[match]
url = 'https://auth\\.example\\.com/device'
code = 'code: (\\S+)'
success = 'signed in'

[[check]]
service = "exampledocs"
run = "example-cli auth status"
account = "stdout"

[logout]
run = "example-cli auth logout"
"""


def test_a_new_file_is_a_new_sign_in(tmp_path):
    folder = _copy(tmp_path)
    (folder / "exampledocs.toml").write_text(EXTRA)
    assert "exampledocs" in providers.load_all(folder)  # the provider schema takes it
    reg = signinreg.build(folder)
    assert reg.choices == ("exampledocs", "claude", "gcloud", "github")  # by order
    assert reg.labels["exampledocs"] == "Example Docs"
    assert reg.service_provider["exampledocs"] == "exampledocs"
    assert reg.service_shorts["exampledocs"] == "Example Docs"


def test_part_of_an_unknown_sign_in_is_refused(tmp_path):
    folder = _copy(tmp_path)
    path = folder / "gcloud-adc.toml"
    path.write_text(path.read_text().replace('part_of = "gcloud"', 'part_of = "nothing"'))
    with pytest.raises(ConfigError, match="part_of names 'nothing', which is no sign-in"):
        signinreg.build(folder)


def test_a_status_key_belongs_to_one_sign_in(tmp_path):
    folder = _copy(tmp_path)
    (folder / "exampledocs.toml").write_text(EXTRA.replace('service = "exampledocs"',
                                                            'service = "github"'))
    with pytest.raises(ConfigError, match="status key 'github' is already"):
        signinreg.build(folder)


# --- ships.toml: ids by pattern, checked against the registry ----------------
def _doc() -> dict:
    return tomllib.loads(EXAMPLE.read_text())


def test_registered_ids_are_accepted():
    doc = _doc()
    doc["ship"]["ship-01"]["signins"] = ["claude", "github"]
    doc.setdefault("tag", {})["work"] = {"gcloud": "work@example.com"}
    validate(doc)


def test_unknown_signin_id_is_refused_clearly():
    doc = _doc()
    doc["ship"]["ship-01"]["signins"] = ["claude", "exampledocs"]
    with pytest.raises(ConfigError) as exc:
        validate(doc, "ships.toml")
    assert ("ship.ship-01.signins: unknown sign-in 'exampledocs' "
            "(known: claude, gcloud, github)") in exc.value.message


def test_unknown_tag_account_key_is_refused_clearly():
    doc = _doc()
    doc.setdefault("tag", {})["work"] = {"claude": "work@example.com",
                                         "exampledocs": "work@example.com"}
    with pytest.raises(ConfigError) as exc:
        validate(doc, "ships.toml")
    assert ("tag.work: unknown sign-in 'exampledocs' (known: claude, gcloud, github)"
            in exc.value.message)


def test_a_registered_new_id_is_accepted(tmp_path, monkeypatch):
    folder = _copy(tmp_path)
    (folder / "exampledocs.toml").write_text(EXTRA)
    monkeypatch.setattr(signinreg, "registry", lambda: signinreg.build(folder))
    doc = _doc()
    doc["ship"]["ship-01"]["signins"] = ["claude", "exampledocs"]
    doc.setdefault("tag", {})["work"] = {"exampledocs": "work@example.com"}
    validate(doc)


def test_the_schema_takes_any_id_shaped_name_only():
    doc = _doc()
    doc["ship"]["ship-01"]["signins"] = ["exampledocs"]
    doc.setdefault("tag", {})["work"] = {"exampledocs": "work@example.com"}
    schemas.check("ships.toml", doc, "ships.toml")  # the pattern; the registry is separate
    for bad in ("Example", "example-docs", "1docs"):
        doc["ship"]["ship-01"]["signins"] = [bad]
        with pytest.raises(ConfigError):
            schemas.check("ships.toml", doc, "ships.toml")


def test_a_sign_in_needs_an_id_ships_toml_can_name(tmp_path):
    folder = _copy(tmp_path)
    (folder / "example-docs.toml").write_text(EXTRA.replace('id = "exampledocs"',
                                                            'id = "example-docs"'))
    with pytest.raises(ConfigError, match="'example-docs' is not a sign-in id"):
        signinreg.build(folder)
