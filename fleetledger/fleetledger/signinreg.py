"""The sign-in registry: every sign-in FleetLedger knows, from the provider files.

Each provider file (fleetledger/providers/<id>.toml, concierge/providers.py) is one
sign-in flow. Its optional [signin] table says how people see it:

- `label` and `order`: the flow is a sign-in people turn on or off per Ship
  (ships.toml ship.<name>.signins, tag accounts), named so and listed in that order;
- `part_of = "<id>"`: the flow only signs in part of another sign-in again, as
  gcloud-adc does for gcloud. It is no choice of its own.

A sign-in covers the status auth keys its own flow checks ([[check]] service, in
order); a check's `label` and `short` name that key on the Sign-ins page and in the
fleet table (default: the sign-in's label). Every other list (choices, status keys,
labels, which flow signs in which key) is derived here, so adding a sign-in is one
file. Only the provider files are read: this module imports no other FleetLedger
module, so anything may import it.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from fleetledger.errors import ConfigError

PROVIDERS_DIR = Path(__file__).resolve().parent / "providers"
# A sign-in id: what ships.toml, tags and status auth keys call it.
ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


@dataclass(frozen=True)
class Service:
    """One status auth key (the Ship's status document's auth.<key>)."""

    key: str
    label: str  # Sign-ins page
    short: str  # fleet table and cards
    signin: str  # the sign-in (choice) it belongs to; its tag account applies
    provider: str  # the flow that signs in this key (alone if one does)


@dataclass(frozen=True)
class SignIn:
    """One sign-in a Ship may use (a choice)."""

    id: str
    label: str
    providers: tuple[str, ...]  # its own flow first, then flows for part of it
    services: tuple[str, ...]  # the status auth keys it covers


@dataclass(frozen=True)
class Registry:
    signins: tuple[SignIn, ...]
    services: tuple[Service, ...]

    @property
    def choices(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.signins)

    @property
    def labels(self) -> dict[str, str]:
        return {s.id: s.label for s in self.signins}

    @property
    def status_keys(self) -> dict[str, tuple[str, ...]]:
        return {s.id: s.services for s in self.signins}

    @property
    def providers(self) -> dict[str, tuple[str, ...]]:
        return {s.id: s.providers for s in self.signins}

    @property
    def provider_ids(self) -> tuple[str, ...]:
        """Every flow, each sign-in's own first."""
        return tuple(p for s in self.signins for p in s.providers)

    @property
    def service_keys(self) -> tuple[str, ...]:
        return tuple(s.key for s in self.services)

    @property
    def service_labels(self) -> dict[str, str]:
        return {s.key: s.label for s in self.services}

    @property
    def service_shorts(self) -> dict[str, str]:
        return {s.key: s.short for s in self.services}

    @property
    def service_provider(self) -> dict[str, str]:
        return {s.key: s.provider for s in self.services}

    def signin_of_service(self, key: str) -> str:
        """The sign-in a status key belongs to (unknown keys: themselves)."""
        return next((s.signin for s in self.services if s.key == key), key)

    def login_help(self) -> str:
        """The services `ship-fleet login` takes, in words."""
        shorts = self.service_shorts
        words = []
        for s in self.signins:
            for p in s.providers:
                extra = [shorts[k] for k in s.services[1:]] if p == s.providers[0] else []
                words.append(f"{p} (with {' and '.join(extra)})" if extra else p)
        return ", ".join(words) + " or all"

    def unknown(self, ids) -> list[str]:
        return [i for i in ids if i not in self.choices]

    def unknown_message(self, where: str, ids) -> str:
        bad = self.unknown(ids)
        return (f"{where}: unknown sign-in {', '.join(repr(i) for i in bad)} "
                f"(known: {', '.join(self.choices)})")


def _read(path: Path) -> dict:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def build(folder: Path = PROVIDERS_DIR) -> Registry:
    """The registry from a folder of provider files."""
    docs = {p.stem: _read(p) for p in sorted(folder.glob("*.toml"))}
    own: list[tuple[int, str, dict]] = []
    parts: dict[str, list[str]] = {}
    for pid, doc in docs.items():
        meta = doc.get("signin")
        if meta is None:
            continue
        if "part_of" in meta:
            parts.setdefault(meta["part_of"], []).append(pid)
            continue
        if not ID.match(pid):
            raise ConfigError(f"{folder / (pid + '.toml')}: '{pid}' is not a sign-in id "
                              "(lower case letters, digits and _).")
        own.append((meta["order"], pid, doc))
    own.sort(key=lambda t: (t[0], t[1]))
    known = {pid for _, pid, _ in own}
    for parent, pids in parts.items():
        if parent not in known:
            raise ConfigError(f"{folder / (pids[0] + '.toml')}: part_of names '{parent}', "
                              "which is no sign-in.")
    signins, services = [], []
    seen: dict[str, str] = {}
    for _, pid, doc in own:
        label = doc["signin"]["label"]
        sub = sorted(parts.get(pid, []))
        keys = []
        for check in doc["check"]:
            key = check["service"]
            if key in seen:
                raise ConfigError(f"{folder / (pid + '.toml')}: status key '{key}' is "
                                  f"already {seen[key]}'s.")
            seen[key] = pid
            keys.append(key)
            # The flow that signs in this key alone, if one does; else the sign-in's own.
            alone = next((p for p in sub if [c["service"] for c in docs[p]["check"]] == [key]),
                         pid)
            name = check.get("label", label)
            services.append(Service(key=key, label=name, short=check.get("short", name),
                                    signin=pid, provider=alone))
        signins.append(SignIn(id=pid, label=label, providers=(pid, *sub), services=tuple(keys)))
    return Registry(signins=tuple(signins), services=tuple(services))


@cache
def registry() -> Registry:
    """The registry from FleetLedger's own provider files."""
    return build()
