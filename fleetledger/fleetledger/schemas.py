"""Validate documents against the JSON Schemas in contracts/."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator

from fleetledger.errors import ConfigError
from fleetledger.repo import find_root


@cache
def _validator(contracts: Path, name: str) -> Draft202012Validator:
    path = contracts / f"{name}.schema.json"
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"Cannot read the {name} contract ({path}).") from exc
    return Draft202012Validator(schema)


def errors(name: str, doc: object, contracts: Path | None = None) -> list[str]:
    """Human-readable schema errors for doc against contracts/<name>.schema.json."""
    validator = _validator(contracts or find_root() / "contracts", name)
    found = []
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path)):
        where = ".".join(str(p) for p in err.absolute_path) or "(top level)"
        found.append(f"{where}: {err.message}")
    return found


def is_valid(name: str, doc: object, contracts: Path | None = None) -> bool:
    return not errors(name, doc, contracts)


def check(name: str, doc: object, what: str, contracts: Path | None = None) -> None:
    """Raise ConfigError listing every problem if doc does not match the contract."""
    problems = errors(name, doc, contracts)
    if problems:
        listed = "\n".join(f"  - {p}" for p in problems[:20])
        raise ConfigError(f"{what} does not match contracts/{name}.schema.json:\n{listed}")
