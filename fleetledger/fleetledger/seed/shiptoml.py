"""Build a Ship's first ship.toml from the inventory (contracts/settings.md)."""

from __future__ import annotations

import os
import re
import zoneinfo
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from fleetledger import schemas
from fleetledger.catalogue import Catalogue
from fleetledger.inventory import Inventory, Ship

# ship.timezone (contracts/ship.toml.schema.json): an IANA zone name, never a path.
TIMEZONE_RE = re.compile(r"^[A-Z][A-Za-z0-9_+-]{0,40}(/[A-Za-z0-9_+-]{1,40}){0,2}$")


def local_timezone(env: Mapping[str, str] | None = None,
                   localtime: Path = Path("/etc/localtime"),
                   timezone_file: Path = Path("/etc/timezone"),
                   known: set[str] | None = None) -> str:
    """This computer's IANA time zone, for a new Ship's ship.timezone.

    TZ, else the zone /etc/localtime links to, else /etc/timezone, else UTC.
    Anything that is not a plain zone name is skipped, and so is a name this
    computer's tzdata does not know (a POSIX TZ string such as EST+5, say).
    """
    env = os.environ if env is None else env
    known = zoneinfo.available_timezones() if known is None else known
    candidates = []
    tz = env.get("TZ", "").lstrip(":")
    candidates.append(tz.split("zoneinfo/", 1)[-1])
    try:
        candidates.append(os.readlink(localtime).split("zoneinfo/", 1)[-1])
    except OSError:
        pass
    try:
        candidates.append(timezone_file.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        pass
    return next((c for c in candidates
                 if TIMEZONE_RE.match(c) and (not known or c in known)), "UTC")


def known_timezone(name: str) -> bool:
    """Does this computer's tzdata have `name`? True when it has no tzdata at all
    (the Ship checks again before using it)."""
    known = zoneinfo.available_timezones()
    return not known or name in known


@dataclass(frozen=True)
class Autostart:
    name: str
    cwd: str
    mode: str | None = None
    prompt: str | None = None
    repo: str | None = None  # a GitHub repo (owner/name) cloned into cwd first


@dataclass(frozen=True)
class SeedOptions:
    """Values that come only from `ship-fleet new` options (absent by default)."""

    autostart: Sequence[Autostart] = ()
    agent_sudo: bool | None = None
    lan_guard: bool | None = None
    lan_guard_allow: Sequence[str] = field(default_factory=tuple)
    timezone: str | None = None


def tools_for(inventory: Inventory, ship: Ship, catalogue: Catalogue) -> list[str]:
    """ship.tools, else defaults.tools, else the catalogue's default recipes."""
    if ship.tools is not None:
        return list(ship.tools)
    if "tools" in inventory.defaults:
        return list(inventory.defaults["tools"])
    return catalogue.default_tools()


def profiles_for(inventory: Inventory, ship: Ship) -> list[str]:
    if ship.profiles is not None:
        return list(ship.profiles)
    return list(inventory.defaults.get("profiles", []))


def build(
    inventory: Inventory,
    ship: Ship,
    catalogue: Catalogue,
    options: SeedOptions | None = None,
    contracts: Path | None = None,
) -> dict[str, Any]:
    """The ship.toml document as plain data, validated against the contract."""
    options = options or SeedOptions()
    doc: dict[str, Any] = {"schema": 1}
    doc["ship"] = {
        "name": ship.name,
        "id": ship.id,
        "tags": list(inventory.effective_tags(ship)),
    }
    if options.timezone:
        doc["ship"]["timezone"] = options.timezone

    claude = inventory.effective_claude(ship)
    claude.setdefault("session_prefix", ship.name)
    doc["claude"] = claude

    if options.agent_sudo is not None:
        doc["agent"] = {"sudo": options.agent_sudo}
    if options.lan_guard is not None or options.lan_guard_allow:
        network: dict[str, Any] = {}
        if options.lan_guard is not None:
            network["lan_guard"] = options.lan_guard
        if options.lan_guard_allow:
            network["lan_guard_allow"] = list(options.lan_guard_allow)
        doc["network"] = network

    if ship.github_scopes:
        doc["github"] = {"scopes": list(ship.github_scopes)}

    tools = tools_for(inventory, ship, catalogue)
    profiles = profiles_for(inventory, ship)
    catalogue.check_allowed(doc["ship"]["tags"], tools, profiles, ship.name)
    catalogue.check_tools(tools)
    catalogue.check_profiles(profiles)
    doc["tools"] = {"install": tools, "profiles": profiles}

    profile_options = {}
    for profile in profiles:
        values = catalogue.resolve_profile_options(profile, inventory.profile_options(profile))
        if values:
            profile_options[profile] = values
    if profile_options:
        doc["profile_options"] = profile_options

    from fleetledger.seed import cloudinit

    keys = [k for path in inventory.agent_key_paths
            for k in cloudinit.read_public_keys(path, "an agent key")]
    doc["owner"] = {"name": inventory.owner, "keys": keys}  # your own login, with sudo

    if options.autostart:
        entries = []
        for agent in options.autostart:
            entry: dict[str, Any] = {"name": agent.name, "cwd": agent.cwd}
            if agent.mode:
                entry["mode"] = agent.mode
            if agent.prompt:
                entry["prompt"] = agent.prompt
            if agent.repo:
                entry["repo"] = agent.repo
            entries.append(entry)
        doc["autostart"] = entries

    schemas.check("ship.toml", doc, f"ship.toml for {ship.name}", contracts)
    return doc


def render(doc: Mapping[str, Any]) -> str:
    """ship.toml text, in the layout of contracts/examples/ship.toml/."""
    out = tomlkit.document()
    out.add(tomlkit.comment("ShipShape Ship settings (contracts/ship.toml.schema.json)."))
    out.add(tomlkit.comment("Written by FleetLedger; change it with `ship-fleet set`."))
    out["schema"] = doc["schema"]
    for section in ("ship", "claude", "agent", "network", "github", "tools"):
        if section in doc:
            table = tomlkit.table()
            for key, value in doc[section].items():
                table[key] = value
            out[section] = table
    if "profile_options" in doc:
        outer = tomlkit.table(is_super_table=True)
        for profile, values in doc["profile_options"].items():
            inner = tomlkit.table()
            for key, value in values.items():
                inner[key] = value
            outer[profile] = inner
        out["profile_options"] = outer
    if "owner" in doc:
        out["owner"] = dict(doc["owner"])
    if "autostart" in doc:
        aot = tomlkit.aot()
        for entry in doc["autostart"]:
            table = tomlkit.table()
            for key, value in entry.items():
                table[key] = value
            aot.append(table)
        out["autostart"] = aot
    return tomlkit.dumps(out)
