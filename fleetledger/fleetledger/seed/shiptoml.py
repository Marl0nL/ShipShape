"""Build a Ship's first ship.toml from the inventory (contracts/settings.md)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

from fleetledger import schemas
from fleetledger.catalogue import Catalogue
from fleetledger.inventory import Inventory, Ship


@dataclass(frozen=True)
class Autostart:
    name: str
    cwd: str
    mode: str | None = None
    prompt: str | None = None


@dataclass(frozen=True)
class SeedOptions:
    """Values that come only from `ship-fleet new` options (absent by default)."""

    autostart: Sequence[Autostart] = ()
    agent_sudo: bool | None = None
    lan_guard: bool | None = None
    lan_guard_allow: Sequence[str] = field(default_factory=tuple)


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

    tools = tools_for(inventory, ship, catalogue)
    profiles = profiles_for(inventory, ship)
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

    if options.autostart:
        entries = []
        for agent in options.autostart:
            entry: dict[str, Any] = {"name": agent.name, "cwd": agent.cwd}
            if agent.mode:
                entry["mode"] = agent.mode
            if agent.prompt:
                entry["prompt"] = agent.prompt
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
    for section in ("ship", "claude", "agent", "network", "tools"):
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
    if "autostart" in doc:
        aot = tomlkit.aot()
        for entry in doc["autostart"]:
            table = tomlkit.table()
            for key, value in entry.items():
                table[key] = value
            aot.append(table)
        out["autostart"] = aot
    return tomlkit.dumps(out)
