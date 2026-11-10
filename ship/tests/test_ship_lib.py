"""Library pieces ship-ctl relies on: the schema validator, the TOML writer, schema copies."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ship" / "lib"))

from shipctl import fsutil, schema, tomlw  # noqa: E402

CONTRACTS = REPO / "contracts"
EXAMPLES = CONTRACTS / "examples"


def load_doc(path: Path):
    if path.suffix == ".toml":
        return tomllib.loads(path.read_text())
    return json.loads(path.read_text())


def all_examples():
    for schema_path in sorted(CONTRACTS.glob("*.schema.json")):
        name = schema_path.name.removesuffix(".schema.json")
        for path in sorted((EXAMPLES / name).rglob("*")):
            if path.suffix in (".toml", ".json"):
                yield name, path
        if name == "versions":
            yield name, REPO / "versions.toml"


@pytest.mark.parametrize(
    "name,path", list(all_examples()), ids=lambda x: x if isinstance(x, str) else x.name
)
def test_validator_agrees_with_jsonschema(name: str, path: Path):
    spec = json.loads((CONTRACTS / f"{name}.schema.json").read_text())
    doc = load_doc(path)
    expected = Draft202012Validator(spec).is_valid(doc)
    assert schema.Validator(spec).is_valid(doc) == expected


def test_validator_agrees_on_mutations():
    """Break valid documents in typical ways; both validators must reject them."""
    spec = json.loads((CONTRACTS / "ship.toml.schema.json").read_text())
    base = load_doc(EXAMPLES / "ship.toml/bypass.toml")
    mutations = [
        lambda d: d["ship"].update(name="Ship 01"),
        lambda d: d["claude"].update(allow_bypass=False),
        lambda d: d["claude"].update(remote_control=True),
        lambda d: d["autostart"][0].update(cwd="relative"),
        lambda d: d.update(extra=1),
        lambda d: d["ship"]["tags"].append(d["ship"]["tags"][0]),
        lambda d: d["network"].update(lan_guard="yes"),
        lambda d: d.update(schema=1.0),
    ]
    for mutate in mutations:
        doc = json.loads(json.dumps(base))
        mutate(doc)
        expected = Draft202012Validator(spec).is_valid(doc)
        assert schema.Validator(spec).is_valid(doc) == expected, doc


def test_validator_refuses_unknown_keywords():
    with pytest.raises(schema.UnsupportedSchema):
        schema.Validator({"type": "string", "unevaluatedProperties": False}).is_valid("x")


def test_bundled_schemas_match_contracts():
    """ship/schemas/ carries copies of the contracts, because bundles hold only ship/."""
    copies = sorted((REPO / "ship" / "schemas").glob("*.schema.json"))
    assert copies
    for copy in copies:
        assert copy.read_bytes() == (CONTRACTS / copy.name).read_bytes(), copy.name


@pytest.mark.parametrize("path", sorted((EXAMPLES / "ship.toml").glob("*.toml")))
def test_toml_round_trip(path: Path):
    doc = tomllib.loads(path.read_text())
    assert tomllib.loads(tomlw.dumps(doc, "header")) == doc


def test_toml_escapes():
    doc = {"a": 'quote " back \\ tab \t nl \n ctl \x01 emoji \U0001f600', "key with space": True}
    assert tomllib.loads(tomlw.dumps(doc)) == doc


def test_set_keys_creates_and_preserves():
    text = (
        '# mine\ntheme = "dark"\n\n[keys]\nprefix = "ctrl+b"\n\n'
        "[session]\nresume_agents_on_restore = false\n"
    )
    out = tomlw.set_keys(
        text,
        {
            ("", "onboarding"): False,
            ("update", "version_check"): False,
            ("session", "resume_agents_on_restore"): True,
        },
    )
    parsed = tomllib.loads(out)
    assert parsed["theme"] == "dark" and parsed["keys"]["prefix"] == "ctrl+b"
    assert parsed["onboarding"] is False
    assert parsed["update"]["version_check"] is False
    assert parsed["session"]["resume_agents_on_restore"] is True
    assert out.startswith("# mine\n")
    # Idempotent: a second pass changes nothing.
    assert tomlw.set_keys(out, {("", "onboarding"): False}) == out


def test_set_keys_empty_file():
    out = tomlw.set_keys("", {("", "onboarding"): False, ("update", "version_check"): False})
    assert tomllib.loads(out) == {"onboarding": False, "update": {"version_check": False}}


# --- the Ship's UTC timestamps (context.stamp) -------------------------------------
def test_stamp_and_parse_stamp_round_trip():
    from datetime import UTC, datetime, timedelta, timezone

    from shipctl.context import parse_stamp, stamp

    assert stamp(1767323045) == "2026-01-02T03:04:05Z"
    assert stamp(1767323045.9) == "2026-01-02T03:04:05Z"
    plus2 = timezone(timedelta(hours=2))
    assert stamp(datetime(2026, 1, 2, 5, 4, 5, tzinfo=plus2)) == "2026-01-02T03:04:05Z"
    assert parse_stamp("2026-01-02T03:04:05Z") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    with pytest.raises(ValueError):
        parse_stamp("2026-01-02T03:04:05")


# --- JSON state files (fsutil) -----------------------------------------------------
@pytest.mark.parametrize("text", [None, "", "not json", "[1, 2]", '"text"', "null"])
def test_read_json_dict_is_empty_unless_the_file_holds_an_object(tmp_path, text):
    path = tmp_path / "state.json"
    if text is not None:
        path.write_text(text)
    assert fsutil.read_json_dict(path) == {}


def test_write_json_writes_what_json_dumps_does_and_reports_changes(tmp_path):
    path = tmp_path / "state.json"
    doc = {"b": [1, 2], "a": "x"}
    assert fsutil.write_json(path, doc, 0o600, indent=2)
    assert path.read_text() == json.dumps(doc, indent=2) + "\n"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert fsutil.read_json_dict(path) == doc
    assert not fsutil.write_json(path, doc, 0o600, indent=2)  # unchanged
    assert fsutil.write_json(path, doc, 0o600)  # compact is other bytes
    assert path.read_text() == json.dumps(doc) + "\n"
