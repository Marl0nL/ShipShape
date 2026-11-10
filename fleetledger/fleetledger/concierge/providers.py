"""Sign-in providers: one sign-in file per flow, in ship/signins/.

The files are a contract (contracts/signin.schema.json) shared with the Ship, which
reads them from its tooling bundle for its status checks. Adding a service is one
file. Each names the command run on the Ship (as agent, in a terminal), the patterns
that find the link, code, prompt and outcome in its output, the status keys it signs
in, how the Ship checks them, and how to sign out.

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

from fleetledger import schemas, signinreg
from fleetledger.errors import ConfigError, NotFound

PROVIDERS_DIR = signinreg.SIGNINS_DIR
CONTRACTS = PROVIDERS_DIR.parents[1] / "contracts"

# "all" signs in to these, in this order (gcloud covers ADC too): every sign-in.
ALL = signinreg.registry().choices


@dataclass(frozen=True)
class Check:
    """A [[check]]: what FleetLedger runs itself after a sign-in on a Ship whose ship-ctl
    has no `signin check` (older tooling)."""

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
    checks: tuple[Check, ...]  # may be empty: the Ship checks it (ship-ctl signin check)
    logout: str
    keys: tuple[str, ...] = ()  # the status keys it signs in
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
        return list(self.keys)

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


def _one_group(path: Path, what: str, spec: str | None) -> None:
    if spec and spec.startswith("re:") and _compile(path, what, spec[3:]).groups != 1:
        raise ConfigError(f"{path}: {what} needs exactly one group in its pattern.")


def check_rules(path: Path, doc: dict) -> None:
    """What the contract's schema cannot say (the Ship checks the same: shipctl/signins.py)."""
    if doc["id"] != path.stem:
        raise ConfigError(f"{path}: id '{doc['id']}' does not match the file name.")
    status = doc.get("status")
    if status is None:
        return
    key = doc["id"].replace("-", "_")
    listed = [c["service"] for c in doc.get("check", [])]
    if listed and key not in listed:
        raise ConfigError(f"{path}: [status] checks {key}, which is none of its [[check]] "
                          "services.")
    _one_group(path, "status.account", status.get("account"))
    _one_group(path, "status.expires_at", status.get("expires_at"))


def parse(path: Path) -> Provider:
    try:
        with path.open("rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if (doc.get("secrets") or {}).get("store") == "keyring":
        raise ConfigError(f"{path}: secrets.store = keyring: a Ship has no unlocked password "
                          "store; pin the tool to its file store.")
    errors = schemas.errors("signin", doc, CONTRACTS)
    if errors:
        raise ConfigError(f"{path}: {errors[0]}")
    check_rules(path, doc)
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
        checks=tuple(Check(**{k: v for k, v in c.items() if k not in ("label", "short")})
                     for c in doc.get("check", ())),
        logout=doc["logout"]["run"],
        keys=tuple(c["service"] for c in signinreg.checked(doc["id"], doc)),
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
