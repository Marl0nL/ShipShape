"""Sign-in providers: one TOML file per service in fleetledger/providers/.

Adding a service is one file. Each names the command run on the Ship (as
agent, in a terminal), the patterns that find the link, code, prompt and
outcome in its output, the checks that confirm the result, and how to sign out.

Modes:
- paste-back: you open the link, approve, and paste the code shown back;
  FleetLedger types it into the Ship's terminal.
- device-code: the Ship shows a code; you type it at the link yourself.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from jsonschema import Draft202012Validator

from fleetledger.errors import ConfigError, NotFound

PROVIDERS_DIR = Path(__file__).resolve().parent.parent / "providers"

# "all" signs in to these, in this order (gcloud covers ADC too).
ALL = ("claude", "gcloud", "github")

SERVICE = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"}
COMMAND = {"type": "string", "minLength": 1}
FLAG = {"type": "string", "pattern": "^--[a-z][a-z-]{0,31}$"}
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "label", "mode", "run", "timeout", "match", "check", "logout"],
    "properties": {
        "id": {"type": "string", "pattern": "^[a-z][a-z0-9-]{0,31}$"},
        "label": {"type": "string", "minLength": 1},
        "mode": {"enum": ["paste-back", "device-code"]},
        "run": COMMAND,
        "timeout": {"type": "integer", "minimum": 30, "maximum": 3600},
        "after": {"type": "array", "items": COMMAND},
        "match": {
            "type": "object",
            "additionalProperties": False,
            "required": ["url", "success"],
            "properties": {
                "url": {"type": "string"},
                "code": {"type": "string"},
                "prompt": {"type": "string"},
                "success": {"type": "string"},
                "failure": {"type": "string"},
                "send": {"type": "object", "additionalProperties": {"type": "string"}},
            },
        },
        "check": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["service", "run"],
                "properties": {
                    "service": SERVICE,
                    "run": COMMAND,
                    "ok_json": {"type": "string"},
                    "account": {"type": "string", "pattern": "^(stdout|json:[A-Za-z0-9_.]+)$"},
                },
            },
        },
        "logout": {
            "type": "object",
            "additionalProperties": False,
            "required": ["run"],
            "properties": {"run": COMMAND},
        },
        # Extra scopes (ghscopes): `add` is the flag run takes them with; `refresh`
        # changes a signed-in token's scopes in the same kind of flow, with `add` for
        # the ones to add and `remove` for the ones to drop.
        "scopes": {
            "type": "object",
            "additionalProperties": False,
            "required": ["add", "refresh", "remove"],
            "properties": {"add": FLAG, "refresh": COMMAND, "remove": FLAG},
        },
    },
    "allOf": [
        {
            "if": {"properties": {"mode": {"const": "paste-back"}}},
            "then": {"properties": {"match": {"required": ["prompt"]}}},
        },
        {
            "if": {"properties": {"mode": {"const": "device-code"}}},
            "then": {"properties": {"match": {"required": ["code"]}}},
        },
    ],
}


@dataclass(frozen=True)
class Check:
    service: str
    run: str
    ok_json: str | None = None
    account: str | None = None  # "stdout" or "json:<key path>"


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    mode: str
    run: str
    timeout: int
    url: re.Pattern[str]
    success: re.Pattern[str]
    checks: tuple[Check, ...]
    logout: str
    code: re.Pattern[str] | None = None
    prompt: re.Pattern[str] | None = None
    failure: re.Pattern[str] | None = None
    send: tuple[tuple[re.Pattern[str], str], ...] = ()
    after: tuple[str, ...] = field(default_factory=tuple)
    scopes_add: str | None = None  # the [scopes] table: this provider takes extra scopes
    scopes_refresh: str | None = None
    scopes_remove: str | None = None

    @property
    def services(self) -> list[str]:
        return [c.service for c in self.checks]

    @property
    def takes_scopes(self) -> bool:
        return self.scopes_add is not None

    def with_scopes(self, scopes: Sequence[str]) -> Provider:
        """The sign-in, asking for these extra scopes too (none: as it is)."""
        if not scopes or not self.takes_scopes:
            return self
        return replace(self, run=f"{self.run} {self.scopes_add} {_scope_list(scopes)}")

    def refreshing(self, add: Sequence[str], remove: Sequence[str]) -> Provider:
        """The signed-in token's scopes changed in place: these added, those dropped."""
        if not self.takes_scopes:
            raise ValueError(f"{self.id} takes no scopes")
        run = self.scopes_refresh or ""
        if add:
            run += f" {self.scopes_add} {_scope_list(add)}"
        if remove:
            run += f" {self.scopes_remove} {_scope_list(remove)}"
        return replace(self, run=run)


def _scope_list(scopes: Sequence[str]) -> str:
    # Scopes come from the catalogue (ghscopes.check), never free text; quoted anyway.
    return shlex.quote(",".join(scopes))


def _compile(path: Path, key: str, pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"{path}: match.{key} is not a valid pattern: {exc}") from exc


def parse(path: Path) -> Provider:
    with path.open("rb") as fh:
        doc = tomllib.load(fh)
    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(doc), key=lambda e: e.path)
    if errors:
        raise ConfigError(f"{path}: {errors[0].message}")
    if doc["id"] != path.stem:
        raise ConfigError(f"{path}: id '{doc['id']}' does not match the file name.")
    m = doc["match"]
    return Provider(
        id=doc["id"],
        label=doc["label"],
        mode=doc["mode"],
        run=doc["run"],
        timeout=doc["timeout"],
        url=_compile(path, "url", m["url"]),
        success=_compile(path, "success", m["success"]),
        code=_compile(path, "code", m["code"]) if "code" in m else None,
        prompt=_compile(path, "prompt", m["prompt"]) if "prompt" in m else None,
        failure=_compile(path, "failure", m["failure"]) if "failure" in m else None,
        send=tuple((_compile(path, "send", k), v) for k, v in m.get("send", {}).items()),
        checks=tuple(Check(**c) for c in doc["check"]),
        logout=doc["logout"]["run"],
        after=tuple(doc.get("after", ())),
        scopes_add=doc.get("scopes", {}).get("add"),
        scopes_refresh=doc.get("scopes", {}).get("refresh"),
        scopes_remove=doc.get("scopes", {}).get("remove"),
    )


def load_all(folder: Path = PROVIDERS_DIR) -> dict[str, Provider]:
    return {p.stem: parse(p) for p in sorted(folder.glob("*.toml"))}


def resolve(names: list[str] | str, folder: Path = PROVIDERS_DIR) -> list[Provider]:
    """Providers for service names; "all" means claude, gcloud (with ADC), github."""
    if isinstance(names, str):
        names = [names]
    known = load_all(folder)
    wanted = list(ALL) if names == ["all"] else names
    unknown = [n for n in wanted if n not in known]
    if unknown:
        listed = ", ".join([*sorted(known), "all"])
        raise NotFound(f"Unknown sign-in service: {', '.join(unknown)} (known: {listed}).")
    return [known[n] for n in wanted]
