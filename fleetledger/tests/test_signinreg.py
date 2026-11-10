"""The sign-in registry (signinreg.py) and the ships.toml ids it checks."""

from __future__ import annotations

import shutil
import sys
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
    shutil.copytree(signinreg.SIGNINS_DIR, folder)
    return folder


EXTRA = """\
schema = 1
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


def test_a_misshapen_signin_table_is_a_config_error(tmp_path):
    folder = _copy(tmp_path)
    (folder / "exampledocs.toml").write_text(EXTRA.replace("order = 0\n", ""))
    with pytest.raises(ConfigError, match="lacks or misshapes 'order'"):
        signinreg.build(folder)


def test_a_broken_provider_file_is_a_clean_ship_fleet_error(monkeypatch):
    # Every ship-fleet command builds the registry; a broken provider file must say
    # so in one line, not as a traceback.
    import io

    from fleetledger import cli

    def broken():
        raise ConfigError("example.toml: [signin] lacks or misshapes 'order'")

    monkeypatch.setattr(signinreg, "registry", broken)
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["ls"], cli.App(out=out, err=err))
    assert code != 0
    assert err.getvalue().startswith("ship-fleet: example.toml: [signin] lacks")
    assert "Traceback" not in err.getvalue()


# --- the Ship reads the same files the same way --------------------------------------
# The Ship's registry (ship/lib/shipctl/signins.py) is its own stdlib-only copy, as its
# bundle holds only ship/; these tests keep the two from drifting apart.
sys.path.insert(0, str(signinreg.SIGNINS_DIR.parents[0] / "lib"))
from shipctl import signins as ship_signins  # noqa: E402 - the Ship's registry

SHIP_SCHEMA = signinreg.SIGNINS_DIR.parents[0] / "schemas" / "signin.schema.json"

# A sign-in with two status keys whose order ties a built-in's (broken by id), and the
# flow that signs in its second key alone: the shapes a toolkit's files can take.
TWO_KEYS = """\
schema = 1
id = "exampledocs"
label = "Example Docs"
mode = "device-code"
run = "example-cli auth login"
timeout = 300

[signin]
label = "Example Docs"
order = 2

[match]
url = 'https://auth\\.example\\.com/device'
code = 'code: (\\S+)'
success = 'signed in'

[[check]]
service = "exampledocs"
run = "example-cli auth status"
account = "stdout"

[[check]]
service = "exampledocs_api"
short = "API"
run = "example-cli api status"

[status]
run = "example-cli auth status --json"
ok_json = "signed_in"

[logout]
run = "example-cli auth logout"
"""

API_PART = """\
schema = 1
id = "exampledocs-api"
label = "Example Docs API"
mode = "device-code"
run = "example-cli api login"
timeout = 300

[signin]
part_of = "exampledocs"

[match]
url = 'https://auth\\.example\\.com/device'
code = 'code: (\\S+)'
success = 'signed in'

[status]
run = "example-cli api status --json"
ok_json = "signed_in"

[logout]
run = "example-cli api logout"
"""


def _both_sides(folder: Path) -> None:
    fl = signinreg.build(folder)
    ship = ship_signins.build(folder, SHIP_SCHEMA)
    # The status document's auth keys, in the same order.
    assert ship.keys == fl.service_keys
    # Each sign-in file signs in the same keys, in the same order, on both sides.
    docs = {p.stem: tomllib.loads(p.read_text()) for p in sorted(folder.glob("*.toml"))}
    fl_flows = {fid: tuple(c["service"] for c in signinreg.checked(fid, doc))
                for fid, doc in docs.items()}
    assert {fid: f.keys for fid, f in ship.flows.items()} == fl_flows
    assert set(ship.flows) == set(fl.provider_ids)
    for s in fl.signins:
        assert ship.flows[s.id].keys == s.services
    # The flow FleetLedger runs to sign in one key alone signs in that key on the Ship.
    for key, flow in fl.service_provider.items():
        assert key in ship.flows[flow].keys


def test_the_ship_derives_the_same_status_keys():
    _both_sides(signinreg.SIGNINS_DIR)


def test_the_ship_derives_the_same_status_keys_for_new_files(tmp_path):
    folder = _copy(tmp_path)
    (folder / "exampledocs.toml").write_text(TWO_KEYS)
    (folder / "exampledocs-api.toml").write_text(API_PART)
    assert signinreg.build(folder).service_keys == (
        "claude", "exampledocs", "exampledocs_api", "gcloud", "gcloud_adc", "github")
    _both_sides(folder)
