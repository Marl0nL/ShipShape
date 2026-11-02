"""Contract tests: schemas are valid, examples behave, shared definitions agree.

Layout (see contracts/README.md):
  contracts/<name>.schema.json
  contracts/examples/<name>/*.{toml,json}          must validate
  contracts/examples/<name>/invalid/*.{toml,json}  must fail
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = ROOT / "contracts"
EXAMPLES = CONTRACTS / "examples"

SCHEMAS = sorted(CONTRACTS.glob("*.schema.json"))
SCHEMA_NAMES = [p.name.removesuffix(".schema.json") for p in SCHEMAS]

# Every contract must have examples; versions.toml at the repo root is its valid example.
EXTRA_VALID = {"versions": [ROOT / "versions.toml"]}


def load_doc(path: Path) -> object:
    if path.suffix == ".toml":
        with path.open("rb") as fh:
            return tomllib.load(fh)
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def validator(name: str) -> Draft202012Validator:
    schema = json.loads((CONTRACTS / f"{name}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def example_files(name: str, invalid: bool) -> list[Path]:
    folder = EXAMPLES / name / "invalid" if invalid else EXAMPLES / name
    files = sorted(p for p in folder.glob("*") if p.suffix in {".toml", ".json"})
    if not invalid:
        files += EXTRA_VALID.get(name, [])
    return files


def cases(invalid: bool) -> list[tuple[str, Path]]:
    return [(name, path) for name in SCHEMA_NAMES for path in example_files(name, invalid)]


def case_id(case: tuple[str, Path]) -> str:
    name, path = case
    return f"{name}:{path.relative_to(ROOT)}"


def test_schemas_exist():
    expected = {
        "ship.toml",
        "ships.toml",
        "ship-status",
        "ship-ctl-result",
        "tool-recipe",
        "profile",
        "bundle-manifest",
        "versions",
    }
    assert expected <= set(SCHEMA_NAMES)


@pytest.mark.parametrize("path", SCHEMAS, ids=lambda p: p.name)
def test_schema_is_valid_draft_2020_12(path: Path):
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["$id"].endswith("/" + path.name)
    assert schema.get("title"), "every schema needs a title"


def test_schema_ids_are_unique():
    ids = [json.loads(p.read_text(encoding="utf-8"))["$id"] for p in SCHEMAS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_every_contract_has_valid_and_invalid_examples(name: str):
    assert example_files(name, invalid=False), f"no valid examples for {name}"
    assert example_files(name, invalid=True), f"no invalid examples for {name}"


@pytest.mark.parametrize("case", cases(invalid=False), ids=case_id)
def test_valid_examples_validate(case: tuple[str, Path]):
    name, path = case
    errors = sorted(validator(name).iter_errors(load_doc(path)), key=lambda e: e.path)
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


@pytest.mark.parametrize("case", cases(invalid=True), ids=case_id)
def test_invalid_examples_fail(case: tuple[str, Path]):
    name, path = case
    assert not validator(name).is_valid(load_doc(path)), f"{path} should not validate"


SOURCE_ROOTS = ("contracts", "ship", "fleetledger", "tests", "scripts", "docs", ".github")
CACHE_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".venv"}


def test_source_files_are_not_git_ignored():
    """A file that exists locally but is ignored would pass here and vanish in CI."""
    files = [
        str(p.relative_to(ROOT))
        for top in SOURCE_ROOTS
        for p in (ROOT / top).rglob("*")
        if p.is_file() and not CACHE_PARTS.intersection(p.relative_to(ROOT).parts)
    ]
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", *files], cwd=ROOT, capture_output=True, text=True
    )
    assert result.stdout.strip() == "", f"git-ignored source files:\n{result.stdout}"


def test_shared_definitions_are_identical():
    """Schemas are self-contained, so shared $defs are copies; keep them in step."""
    seen: dict[str, tuple[str, object]] = {}
    for path in SCHEMAS:
        defs = json.loads(path.read_text(encoding="utf-8")).get("$defs", {})
        for key, value in defs.items():
            if key in seen:
                first_file, first_value = seen[key]
                assert value == first_value, (
                    f"$defs.{key} differs between {first_file} and {path.name}"
                )
            else:
                seen[key] = (path.name, value)


def test_status_examples_cover_every_state_in_docs():
    states = {load_doc(p)["state"] for p in example_files("ship-status", invalid=False)}
    assert {"running", "needs-sign-in", "provisioning"} <= states


def test_versions_toml_npm_pins_match_tool_recipes():
    """A recipe that pins an npm package listed in versions.toml must use that version."""
    pins = load_doc(ROOT / "versions.toml")["npm"]
    recipes = sorted((ROOT / "ship" / "tools").glob("*.toml"))
    for recipe_path in recipes:
        recipe = load_doc(recipe_path)
        assert recipe_path.stem == recipe["id"], f"{recipe_path} must be named <id>.toml"
        assert validator("tool-recipe").is_valid(recipe), f"{recipe_path} fails the schema"
        if recipe.get("kind") == "npm" and recipe["package"] in pins:
            assert recipe["version"] == pins[recipe["package"]], recipe_path


def test_profiles_in_repo_validate():
    for profile_path in sorted((ROOT / "ship" / "profiles").glob("*.toml")):
        profile = load_doc(profile_path)
        assert profile_path.stem == profile["id"], f"{profile_path} must be named <id>.toml"
        assert validator("profile").is_valid(profile), f"{profile_path} fails the schema"


@pytest.mark.parametrize(
    ("address", "ok"),
    [
        ("192.0.2.10", True),
        ("2001:db8::10", True),
        ("::1", True),
        ("::ffff:192.0.2.1", True),  # IPv4-mapped: producers should not, but it must not break
        ("fe80::1%eth0", False),  # zone index: producers strip it
        ("<b>", False),
    ],
)
def test_net_summary_address_forms(address: str, ok: bool):
    doc = load_doc(EXAMPLES / "net-summary" / "running.json")
    dest = doc["destinations"][0]
    dest["addresses"] = [address]
    assert validator("net-summary").is_valid(doc) is ok
    dest["name"], dest["kind"] = address, "address"
    assert validator("net-summary").is_valid(doc) is ok
