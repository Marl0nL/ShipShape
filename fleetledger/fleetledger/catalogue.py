"""Read the Ship catalogue: recipes in ship/tools/, profiles in ship/profiles/.

The Ship track owns these files (contracts/tools.md); FleetLedger only reads
them to list them and to check --tools, --profile and profile options.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleetledger import schemas
from fleetledger.errors import ConfigError, NotFound


@dataclass(frozen=True)
class Recipe:
    id: str
    description: str
    kind: str
    default: bool
    needs: tuple[str, ...]
    version: str | None
    bake: bool = False


@dataclass(frozen=True)
class Profile:
    id: str
    description: str
    tools: tuple[str, ...]
    options: Mapping[str, Mapping[str, Any]]


def _load_dir(folder: Path, contract: str, contracts: Path | None) -> dict[str, dict]:
    docs: dict[str, dict] = {}
    if not folder.is_dir():
        return docs
    for path in sorted(folder.glob("*.toml")):
        try:
            with path.open("rb") as fh:
                doc = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
        schemas.check(contract, doc, str(path), contracts)
        if doc["id"] != path.stem:
            raise ConfigError(f"{path}: id '{doc['id']}' does not match the file name.")
        docs[doc["id"]] = doc
    return docs


class Catalogue:
    def __init__(
        self, tools_dir: Path, profiles_dir: Path, contracts: Path | None = None
    ) -> None:
        raw_recipes = _load_dir(tools_dir, "tool-recipe", contracts)
        raw_profiles = _load_dir(profiles_dir, "profile", contracts)
        self.recipes = {
            rid: Recipe(
                id=rid,
                description=d["description"],
                kind=d["kind"],
                default=d.get("default", False),
                needs=tuple(d.get("needs", ())),
                version=d.get("version"),
                bake=d.get("bake", False),
            )
            for rid, d in raw_recipes.items()
        }
        self.profiles = {
            pid: Profile(
                id=pid,
                description=d["description"],
                tools=tuple(d["tools"]),
                options=d.get("options", {}),
            )
            for pid, d in raw_profiles.items()
        }
        for profile in self.profiles.values():
            missing = [t for t in profile.tools if t not in self.recipes]
            if missing:
                raise ConfigError(
                    f"Profile '{profile.id}' lists unknown tools: {', '.join(missing)}."
                )

    @classmethod
    def from_repo(cls, repo: Any) -> Catalogue:
        return cls(repo.tools_dir, repo.profiles_dir, repo.contracts)

    def default_tools(self) -> list[str]:
        return sorted(r.id for r in self.recipes.values() if r.default)

    def bake_tools(self) -> list[str]:
        """Recipes whose system parts may go into a baked image (contracts/image.md)."""
        return sorted(r.id for r in self.recipes.values() if r.bake)

    def check_tools(self, ids: Iterable[str]) -> None:
        unknown = [i for i in ids if i not in self.recipes]
        if unknown:
            known = ", ".join(sorted(self.recipes)) or "none yet"
            raise NotFound(f"Unknown tools: {', '.join(unknown)} (catalogue: {known}).")

    def check_profiles(self, ids: Iterable[str]) -> None:
        unknown = [i for i in ids if i not in self.profiles]
        if unknown:
            known = ", ".join(sorted(self.profiles)) or "none yet"
            raise NotFound(f"Unknown profiles: {', '.join(unknown)} (catalogue: {known}).")

    def resolve_profile_options(
        self, profile_id: str, values: Mapping[str, str]
    ) -> dict[str, str]:
        """Check a profile's options from ships.toml; return the values to copy.

        A missing required option, a value failing the option's pattern, or an
        option the profile does not declare stops seed creation.
        """
        profile = self.profiles[profile_id]
        problems = []
        for name in values:
            if name not in profile.options:
                problems.append(f"'{name}' is not an option of this profile")
        for name, spec in profile.options.items():
            value = values.get(name)
            if value is None:
                if spec.get("required", False):
                    problems.append(f"'{name}' is required ({spec['description']})")
                continue
            pattern = spec.get("pattern")
            if pattern and not re.search(pattern, value):  # JSON Schema semantics
                problems.append(f"'{name}' does not match {pattern}")
        if problems:
            listed = "\n".join(f"  - {p}" for p in problems)
            raise ConfigError(
                f"Profile '{profile_id}' options in ships.toml [profile_options.{profile_id}]:\n"
                f"{listed}"
            )
        return dict(values)
