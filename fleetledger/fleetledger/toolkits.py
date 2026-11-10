"""Your own toolkits: recipes, profiles, sign-ins and skills from a folder of yours.

A toolkit is laid out like ShipShape's own catalogue (contracts/toolkit.md):

    toolkit.toml            schema, id, version, description, instructions, files
    tools/<id>.toml         recipes (contracts/tool-recipe.schema.json), scripts beside them
    profiles/<id>.toml      profiles (contracts/profile.schema.json)
    signins/<id>.toml       sign-in files (FleetLedger's provider format)
    skills/<name>/SKILL.md  Agent Skills
    instructions/*.md       standing instructions for the Ship's agents
    files/                  vendored files, pinned by SHA-256 in toolkit.toml [files]

ships.toml [toolkit.<id>] names the folder. This module reads and checks one; the
Catalogue (catalogue.py) merges its tools and profiles in as <toolkit>/<id>. A
toolkit with any problem does not load at all, so nothing half-checked is listed.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from fleetledger import paths, schemas, signinreg
from fleetledger.errors import ConfigError

MANIFEST = "toolkit.toml"
ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# An exact version: npm installs exactly this, never a range or a tag such as latest.
EXACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
# A Debian package name, whole: no .deb file, path, repository or version. Never a
# trailing "-", which apt-get install reads as "remove this package".
APT_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]*[a-z0-9+]")
SKIPPED = {".git"}  # a checkout's own git folder is not part of the toolkit


class ToolkitError(ConfigError):
    """A toolkit that does not load, with every problem found."""

    def __init__(self, where: str, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        listed = "\n".join(f"  - {p}" for p in self.problems[:30])
        more = len(self.problems) - 30
        if more > 0:
            listed += f"\n  - and {more} more"
        super().__init__(f"{where} is not a usable toolkit:\n{listed}")


@dataclass(frozen=True)
class Config:
    """One ships.toml [toolkit.<id>] entry."""

    id: str
    path: Path
    allow_root: bool = False
    only_tags: tuple[str, ...] | None = None
    options: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_entry(cls, toolkit_id: str, entry: Mapping[str, Any]) -> Config:
        only = entry.get("only_tags")
        return cls(id=toolkit_id, path=paths.expand(entry["path"]),
                   allow_root=bool(entry.get("allow_root", False)),
                   only_tags=None if only is None else tuple(only),
                   options=dict(entry.get("options", {})))


def configured(inventory: Any) -> list[Config]:
    """The toolkits ships.toml names, in id order."""
    entries = inventory.doc.get("toolkit", {}) if inventory is not None else {}
    return [Config.from_entry(tid, entries[tid]) for tid in sorted(entries)]


@dataclass(frozen=True)
class Toolkit:
    id: str
    version: str
    description: str
    root: Path
    recipes: Mapping[str, Mapping[str, Any]]  # own id -> recipe document
    profiles: Mapping[str, Mapping[str, Any]]  # own id -> profile, tools qualified
    signins: tuple[str, ...]  # sign-in ids (flat, as status keys and ships.toml use them)
    flows: tuple[str, ...]  # every sign-in file's id, the sign-ins' own included
    services: tuple[str, ...]  # status auth keys its sign-ins check
    skills: tuple[str, ...]
    instructions: tuple[str, ...]
    files: Mapping[str, str]  # files/... -> sha256
    config: Config | None = None
    registry: signinreg.Registry | None = None  # its own sign-ins, as the registry has them

    def qualified(self, own_id: str) -> str:
        return f"{self.id}/{own_id}"


# --- reading -------------------------------------------------------------------------


def _toml(path: Path, problems: list[str], root: Path) -> dict | None:
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        problems.append(f"{_rel(path, root)}: not valid TOML: {exc}")
    except OSError as exc:
        problems.append(f"{_rel(path, root)}: cannot be read ({exc.strerror})")
    return None


def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _walk(root: Path) -> tuple[list[Path], list[str]]:
    """Every file in the toolkit, and the problems a walk finds (symlinks, odd files)."""
    found: list[Path] = []
    problems: list[str] = []
    for top, dirs, files in os.walk(root, followlinks=False):
        here = Path(top)
        for name in sorted(dirs):
            full = here / name
            if here == root and name in SKIPPED:
                dirs.remove(name)
            elif full.is_symlink():
                problems.append(f"{_rel(full, root)}: a symlink (a toolkit holds real "
                                "files and folders only)")
                dirs.remove(name)
        for name in sorted(files):
            full = here / name
            if here == root and name in SKIPPED:
                continue  # a worktree's .git file
            mode = full.lstat().st_mode
            if stat.S_ISLNK(mode):
                problems.append(f"{_rel(full, root)}: a symlink (a toolkit holds real "
                                "files and folders only)")
            elif not stat.S_ISREG(mode):
                problems.append(f"{_rel(full, root)}: not a regular file")
            else:
                found.append(full)
    return sorted(found), problems


def _inside(root: Path, base: Path, relative: str) -> Path | None:
    """base/relative, if it stays inside the toolkit; None if it leaves it."""
    target = (base / relative).resolve()
    return target if target.is_relative_to(root) else None


@cache
def _sha256_cached(path: str, size: int, mtime_ns: int) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256(path: Path) -> str:
    """A file's SHA-256, remembered while the file is unchanged (pages list toolkits
    often; vendored archives can be large)."""
    st = path.stat()
    return _sha256_cached(str(path), st.st_size, st.st_mtime_ns)


# --- the rules -------------------------------------------------------------------------


def _check_recipe(rid: str, doc: Mapping[str, Any], root: Path, allow_root: bool,
                  known_needs: set[str], base_tools: Iterable[str],
                  base_skills: Iterable[str] = ()) -> list[str]:
    where = f"tools/{rid}.toml"
    problems: list[str] = []
    if doc.get("kind") == "skill-dir" and rid in base_skills:
        problems.append(f"{where}: ~/.claude/skills/{rid} is the Ship's own skill; give "
                        "yours another id")
    if rid in base_tools:
        problems.append(f"{where}: '{rid}' is also a catalogue tool; give yours another id")
    if doc.get("default", False):
        problems.append(f"{where}: default = true is not allowed in a toolkit (a toolkit tool "
                        "lands only on Ships you add it to)")
    if doc.get("run_as") == "root" and not allow_root:
        problems.append(f"{where}: runs as root; allowed only with allow_root = true in "
                        "ships.toml [toolkit.<id>]")
    if doc.get("bake", False):
        problems.append(f"{where}: bake = true is not allowed in a toolkit (baked images "
                        "hold catalogue tools only)")
    kind = doc["kind"]
    if kind == "npm" and not EXACT_VERSION.match(doc["version"]):
        problems.append(f"{where}: version '{doc['version']}' is not one exact version")
    for spec in doc.get("also_npm", ()):
        version = spec.rsplit("@", 1)[1]
        if not EXACT_VERSION.match(version):
            problems.append(f"{where}: also_npm '{spec}' is not pinned to one exact version")
    apt = list(doc.get("apt", ()))
    if kind == "apt":
        apt.append(doc["package"])
    for package in apt:
        if not APT_PACKAGE.fullmatch(package):
            problems.append(f"{where}: apt '{package}' is not a package name (a toolkit "
                            "cannot add apt repositories or install .deb files, and a "
                            "trailing - would remove the package)")
    if kind == "script":
        target = _inside(root, root / "tools", doc["script"])
        if target is None:
            problems.append(f"{where}: script '{doc['script']}' is outside the toolkit")
        elif not target.is_file():
            problems.append(f"{where}: script '{doc['script']}' is not in tools/")
    if kind == "skill-dir":
        target = _inside(root, root, doc["skill_dir"])
        if target is None:
            problems.append(f"{where}: skill_dir '{doc['skill_dir']}' is outside the toolkit")
        elif not (target / "SKILL.md").is_file():
            problems.append(f"{where}: skill_dir '{doc['skill_dir']}' has no SKILL.md")
    unknown = [n for n in doc.get("needs", ()) if n not in known_needs]
    if unknown:
        problems.append(f"{where}: needs unknown sign-in {', '.join(unknown)} (known: "
                        f"{', '.join(sorted(known_needs))})")
    return problems


def _check_profile(kit_id: str, pid: str, doc: dict[str, Any], root: Path,
                   own_tools: Iterable[str], base_tools: Iterable[str],
                   base_profiles: Iterable[str]) -> list[str]:
    """Checks the profile; qualifies its own tools (doc["tools"]) in place."""
    where = f"profiles/{pid}.toml"
    problems: list[str] = []
    if pid in base_profiles:
        problems.append(f"{where}: '{pid}' is also a catalogue profile; give yours another id")
    own, base = set(own_tools), set(base_tools)
    tools = []
    for ref in doc["tools"]:
        kit, _, name = ref.rpartition("/")
        if kit and kit != kit_id:
            problems.append(f"{where}: '{ref}' is from another toolkit (a profile lists "
                            "catalogue tools and its own toolkit's)")
        elif name in own:
            tools.append(f"{kit_id}/{name}")
        elif not kit and name in base:
            tools.append(name)
        else:
            problems.append(f"{where}: unknown tool '{ref}'")
    doc["tools"] = list(dict.fromkeys(tools))
    if "setup" in doc:
        target = _inside(root, root / "profiles", doc["setup"])
        if target is None:
            problems.append(f"{where}: setup '{doc['setup']}' is outside the toolkit")
        elif not target.is_file():
            problems.append(f"{where}: setup '{doc['setup']}' is not in profiles/")
    return problems


def _load_signins(folder: Path, root: Path, registry: signinreg.Registry
                  ) -> tuple[signinreg.Registry | None, list[str]]:
    """The toolkit's sign-in files, checked as FleetLedger's own are (the sign-in
    contract, contracts/signin.schema.json, and its rules: concierge/providers.py)."""
    from fleetledger.concierge import providers

    problems: list[str] = []
    if not folder.is_dir():
        return None, problems
    files = sorted(folder.glob("*.toml"))
    for path in files:
        try:
            providers.parse(path)
        except ConfigError as exc:
            if isinstance(exc.__cause__, OSError):
                problems.append(f"{_rel(path, root)}: cannot be read "
                                f"({exc.__cause__.strerror})")
                continue
            problems.append(f"{_rel(path, root)}: {exc.message.replace(f'{path}: ', '')}")
        except tomllib.TOMLDecodeError as exc:
            problems.append(f"{_rel(path, root)}: not valid TOML: {exc}")
            continue
        except OSError as exc:
            problems.append(f"{_rel(path, root)}: cannot be read ({exc.strerror})")
            continue
        doc = _toml(path, [], root) or {}
        if "signin" not in doc:
            problems.append(f"{_rel(path, root)}: needs a [signin] table (label and order, "
                            "or part_of one of this toolkit's sign-ins)")
    if problems:
        return None, problems
    try:
        own = signinreg.build(folder)
    except ConfigError as exc:
        return None, [exc.message.replace(str(root) + "/", "")]
    for flow in own.provider_ids:
        if flow in registry.provider_ids:
            problems.append(f"signins/{flow}.toml: '{flow}' is already one of FleetLedger's "
                            "own sign-ins")
    for key in own.service_keys:
        if key in registry.service_keys:
            problems.append(f"signins: status key '{key}' is already one of FleetLedger's "
                            "own sign-ins'")
    return own, problems


def load(root: Path | str, *, expect_id: str | None = None, allow_root: bool = False,
         base: Any = None, contracts: Path | None = None,
         taken: Mapping[str, str] | None = None, config: Config | None = None,
         signins: signinreg.Registry | None = None) -> Toolkit:
    """Read and check the toolkit in root. Raises ToolkitError with every problem.

    base: the Catalogue it joins (its tools and profiles may not reuse their ids);
    taken: sign-in and status key ids other loaded toolkits use -> their toolkit;
    signins: the sign-ins known so far (FleetLedger's own and loaded toolkits'), which
    `needs` may name besides the toolkit's own (default: FleetLedger's own)."""
    root = Path(root).expanduser()
    shown = paths.tilde(root)
    if not root.is_dir():
        raise ToolkitError(shown, ["no such folder"])
    if root.is_symlink():
        raise ToolkitError(shown, ["the folder is a symlink: name the folder itself"])
    root = root.resolve()
    problems: list[str] = []
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        raise ToolkitError(shown, [f"no {MANIFEST} at the top"])
    files, walk_problems = _walk(root)
    problems += walk_problems
    manifest = _toml(manifest_path, problems, root)
    if manifest is None:
        raise ToolkitError(shown, problems)
    problems += [f"{MANIFEST}: {p}" for p in schemas.errors("toolkit", manifest, contracts)]
    if problems:
        raise ToolkitError(shown, problems)
    kit_id = manifest["id"]
    if expect_id is not None and kit_id != expect_id:
        problems.append(f"{MANIFEST}: id is '{kit_id}', but ships.toml calls it '{expect_id}'")

    base_tools = set(getattr(base, "recipes", {}) or ())
    base_profiles = set(getattr(base, "profiles", {}) or ())
    registry = signinreg.registry()

    # Sign-ins first: recipes may need them.
    own_signins, signin_problems = _load_signins(root / "signins", root, registry)
    problems += signin_problems
    own_services = set(own_signins.service_keys) if own_signins else set()
    for ident in sorted({*(own_signins.provider_ids if own_signins else ()), *own_services}):
        other = (taken or {}).get(ident)
        if other is not None and other != kit_id:
            problems.append(f"signins: '{ident}' is also a sign-in of toolkit {other} "
                            "(sign-in ids are shared by every toolkit)")

    recipes: dict[str, dict] = {}
    profiles: dict[str, dict] = {}
    for folder, contract, into in (("tools", "tool-recipe", recipes),
                                   ("profiles", "profile", profiles)):
        for path in sorted((root / folder).glob("*.toml")):
            doc = _toml(path, problems, root)
            if doc is None:
                continue
            where = _rel(path, root)
            errors = schemas.errors(contract, doc, contracts)
            if errors:
                problems += [f"{where}: {e}" for e in errors[:10]]
                continue
            if doc["id"] != path.stem:
                problems.append(f"{where}: id '{doc['id']}' does not match the file name")
                continue
            into[doc["id"]] = doc
    known_needs = {*(signins or registry).service_keys, *own_services}
    for rid, doc in recipes.items():
        problems += _check_recipe(rid, doc, root, allow_root, known_needs, base_tools,
                                  getattr(base, "skill_names", ()))
    for pid, doc in profiles.items():
        problems += _check_profile(kit_id, pid, doc, root, recipes, base_tools, base_profiles)

    skills = []
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        for entry in sorted(skills_dir.iterdir()):
            where = _rel(entry, root)
            if entry.name.startswith("."):
                continue  # .DS_Store, .gitkeep and the like: no skill
            if not entry.is_dir():
                problems.append(f"{where}: skills/ holds one folder per skill")
            elif not SKILL_NAME.match(entry.name):
                problems.append(f"{where}: a skill folder is named a-z, 0-9 and -")
            elif not (entry / "SKILL.md").is_file():
                problems.append(f"{where}: no SKILL.md")
            else:
                skills.append(entry.name)

    for rel in manifest.get("instructions", ()):
        target = _inside(root, root, rel)
        if target is None or not target.is_file():
            problems.append(f"{MANIFEST}: instructions '{rel}' is not a file in instructions/")

    pinned: dict[str, str] = dict(manifest.get("files", {}))
    for rel, digest in sorted(pinned.items()):
        target = _inside(root, root, rel)
        if target is None:
            problems.append(f"{MANIFEST}: files '{rel}' is outside the toolkit")
        elif not target.is_file():
            problems.append(f"{MANIFEST}: files '{rel}' is missing")
        else:
            try:
                matches = sha256(target) == digest
            except OSError as exc:
                problems.append(f"{rel}: cannot be read ({exc.strerror})")
                continue
            if not matches:
                problems.append(f"{rel}: its SHA-256 is not the one {MANIFEST} pins")
    vendored = root / "files"
    for f in files:
        if f.is_relative_to(vendored) and _rel(f, root) not in pinned:
            problems.append(f"{_rel(f, root)}: not pinned in {MANIFEST} [files] (add its "
                            "SHA-256 there)")

    if problems:
        raise ToolkitError(shown, problems)
    return Toolkit(
        id=kit_id,
        version=manifest["version"],
        description=manifest["description"],
        root=root,
        recipes=recipes,
        profiles=profiles,
        signins=own_signins.choices if own_signins else (),
        flows=own_signins.provider_ids if own_signins else (),
        services=own_signins.service_keys if own_signins else (),
        skills=tuple(skills),
        instructions=tuple(manifest.get("instructions", ())),
        files=pinned,
        config=config,
        registry=own_signins,
    )


def load_config(cfg: Config, *, base: Any = None, contracts: Path | None = None,
                taken: Mapping[str, str] | None = None,
                signins: signinreg.Registry | None = None) -> Toolkit:
    return load(cfg.path, expect_id=cfg.id, allow_root=cfg.allow_root, base=base,
                contracts=contracts, taken=taken, config=cfg, signins=signins)


def load_configured(inventory: Any, base: Any = None, contracts: Path | None = None
                    ) -> tuple[list[Toolkit], dict[str, str]]:
    """Every toolkit ships.toml names: (the ones that load, {id: why} for the rest).

    A toolkit that does not load is left out with its reason, so one broken folder
    never stops FleetLedger listing or using the catalogue and the other toolkits."""
    loaded: list[Toolkit] = []
    broken: dict[str, str] = {}
    taken: dict[str, str] = {}
    known = getattr(base, "signins", None) or signinreg.registry()
    for cfg in configured(inventory):
        try:
            kit = load_config(cfg, base=base, contracts=contracts, taken=taken, signins=known)
        except ConfigError as exc:
            broken[cfg.id] = exc.message
            continue
        except OSError as exc:  # anything the checks above did not turn into a problem
            broken[cfg.id] = f"{paths.tilde(cfg.path)} cannot be read: {exc}"
            continue
        for ident in (*kit.flows, *kit.services):
            taken[ident] = kit.id
        if kit.registry is not None:
            known = signinreg.merge(known, kit.registry, f"toolkit {kit.id}")
        loaded.append(kit)
    return loaded, broken


def taken_by(kits: Iterable[Toolkit], but: str | None = None) -> dict[str, str]:
    """Sign-in ids and status keys the given toolkits use -> their toolkit."""
    return {ident: kit.id for kit in kits if kit.id != but
            for ident in (*kit.flows, *kit.services)}
