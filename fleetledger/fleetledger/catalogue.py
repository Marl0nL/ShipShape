"""Read the Ship catalogue: recipes in ship/tools/, profiles in ship/profiles/.

The Ship track owns these files (contracts/tools.md); FleetLedger only reads
them to list them and to check --tools, --profile and profile options.

Given the inventory, the catalogue also holds the tools and profiles of your own
toolkits (toolkits.py, contracts/toolkit.md), named <toolkit>/<id>. Without one it
is the repo's catalogue alone, as before.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleetledger import schemas, signinreg
from fleetledger.errors import ConfigError, FleetError, NotFound


@dataclass(frozen=True)
class Recipe:
    id: str
    description: str
    kind: str
    default: bool
    needs: tuple[str, ...]
    version: str | None
    bake: bool = False
    toolkit: str | None = None  # None: the repo's catalogue


@dataclass(frozen=True)
class Profile:
    id: str
    description: str
    tools: tuple[str, ...]
    options: Mapping[str, Mapping[str, Any]]
    toolkit: str | None = None


NOT_SENT_YET = ("FleetLedger lists and checks toolkit tools and profiles, but does not send "
                "toolkits to Ships yet")


def _recipe(rid: str, d: Mapping[str, Any], toolkit: str | None = None) -> Recipe:
    return Recipe(
        id=rid,
        description=d["description"],
        kind=d["kind"],
        default=d.get("default", False),
        needs=tuple(d.get("needs", ())),
        version=d.get("version"),
        bake=d.get("bake", False),
        toolkit=toolkit,
    )


def _profile(pid: str, d: Mapping[str, Any], toolkit: str | None = None) -> Profile:
    return Profile(id=pid, description=d["description"], tools=tuple(d["tools"]),
                   options=d.get("options", {}), toolkit=toolkit)


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
        self.recipes = {rid: _recipe(rid, d) for rid, d in raw_recipes.items()}
        self.profiles = {pid: _profile(pid, d) for pid, d in raw_profiles.items()}
        self.toolkits: dict[str, Any] = {}  # id -> toolkits.Toolkit, loaded
        self.broken: dict[str, str] = {}  # id -> why it did not load
        # Every sign-in known: FleetLedger's own, then each loaded toolkit's.
        self.signins: signinreg.Registry = signinreg.registry()
        for profile in self.profiles.values():
            missing = [t for t in profile.tools if t not in self.recipes]
            if missing:
                raise ConfigError(
                    f"Profile '{profile.id}' lists unknown tools: {', '.join(missing)}."
                )

    @classmethod
    def from_repo(cls, repo: Any, inventory: Any = None) -> Catalogue:
        """The repo's catalogue; with the inventory, plus its toolkits that load
        (the others are in .broken, with the reason)."""
        catalogue = cls(repo.tools_dir, repo.profiles_dir, repo.contracts)
        if inventory is not None:
            from fleetledger import toolkits

            loaded, broken = toolkits.load_configured(inventory, catalogue, repo.contracts)
            for kit in loaded:
                catalogue.add_toolkit(kit)
            catalogue.broken.update(broken)
        return catalogue

    def add_toolkit(self, kit: Any) -> None:
        """Merge a loaded toolkit's tools and profiles in, as <toolkit>/<id>."""
        if kit.id in self.toolkits:
            raise ConfigError(f"Toolkit {kit.id} is loaded twice.")
        if kit.registry is not None:
            self.signins = signinreg.merge(self.signins, kit.registry, f"toolkit {kit.id}")
        self.toolkits[kit.id] = kit
        for rid, doc in kit.recipes.items():
            self.recipes[kit.qualified(rid)] = _recipe(kit.qualified(rid), doc, kit.id)
        for pid, doc in kit.profiles.items():
            self.profiles[kit.qualified(pid)] = _profile(kit.qualified(pid), doc, kit.id)

    def toolkit_of(self, item_id: str) -> str | None:
        """The toolkit a tool or profile id names (toolkit/x), or None."""
        kit, _, _ = item_id.rpartition("/")
        return kit or None

    def _resolve(self, ref: str, table: Mapping[str, Any], what: str) -> str | None:
        """A tool or profile id as given (a bare id from one toolkit is enough)."""
        if ref in table:
            return ref
        kit = self.toolkit_of(ref)
        if kit is not None:
            if kit in self.broken:
                raise ConfigError(f"'{ref}': toolkit {kit} did not load.\n{self.broken[kit]}",
                                  hint=f"ship-fleet toolkit check {kit}")
            return None
        hits = sorted(i for i, item in table.items()
                      if item.toolkit is not None and i.rpartition("/")[2] == ref)
        if len(hits) > 1:
            raise FleetError(f"'{ref}' is a {what} of more than one toolkit: give one of "
                             f"{', '.join(hits)}.")
        return hits[0] if hits else None

    def resolve_tools(self, refs: Iterable[str]) -> list[str]:
        """Tool ids as the CLI takes them: catalogue ids, toolkit/tool, or a toolkit
        tool's bare id when only one toolkit has it. Unknown ids stop it."""
        refs = list(refs)
        found = [self._resolve(r, self.recipes, "tool") for r in refs]
        self.check_tools([f or r for f, r in zip(found, refs, strict=True)])
        return [f for f in found if f is not None]

    def resolve_profiles(self, refs: Iterable[str]) -> list[str]:
        refs = list(refs)
        found = [self._resolve(r, self.profiles, "profile") for r in refs]
        self.check_profiles([f or r for f, r in zip(found, refs, strict=True)])
        return [f for f in found if f is not None]

    def check_sendable(self, tools: Sequence[str] = (), profiles: Sequence[str] = ()) -> None:
        """Refuse toolkit tools and profiles where they would go to a Ship."""
        kits = [i for i in (*tools, *profiles) if self.toolkit_of(i) is not None]
        if kits:
            raise FleetError(f"{', '.join(kits)}: {NOT_SENT_YET}.",
                             hint="Use catalogue tools and profiles on Ships for now.")

    def default_tools(self) -> list[str]:
        return sorted(r.id for r in self.recipes.values() if r.default and r.toolkit is None)

    def bake_tools(self) -> list[str]:
        """Recipes whose system parts may go into a baked image (contracts/image.md)."""
        return sorted(r.id for r in self.recipes.values() if r.bake and r.toolkit is None)

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
