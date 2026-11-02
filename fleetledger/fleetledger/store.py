"""Where the inventory is kept.

A small interface so the inventory can move from one TOML file to a database
later without touching the rest of FleetLedger. TomlFileStore is the only
implementation: it keeps your comments and layout (tomlkit), takes a lock for
every change, validates the whole document against the contract before writing,
and writes atomically with mode 0600.
"""

from __future__ import annotations

import fcntl
import os
import tomllib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

import tomlkit
from tomlkit.items import Table

from fleetledger import paths, schemas
from fleetledger.errors import ConfigError, FleetError, NotFound
from fleetledger.inventory import Inventory, Ship


class Store(Protocol):
    def exists(self) -> bool: ...

    def load(self) -> Inventory: ...

    def edit(self) -> Any:
        """Context manager yielding an InventoryEditor; saved on clean exit."""
        ...


def semantic_errors(doc: Mapping[str, Any]) -> list[str]:
    """Rules the schema cannot express."""
    problems: list[str] = []
    seen_names: dict[str, str] = {}
    seen_ids: dict[str, str] = {}
    seen_keys: dict[str, str] = {}
    for ship_name, entry in sorted(doc.get("ship", {}).items()):
        for host in entry.get("names", []):
            other = seen_names.setdefault(host.lower(), ship_name)
            if other != ship_name:
                problems.append(f"ship.{ship_name}.names: '{host}' is also used by {other}")
        other = seen_ids.setdefault(entry.get("id", ""), ship_name)
        if other != ship_name:
            problems.append(f"ship.{ship_name}.id: same id as {other}")
        key = " ".join(str(entry.get("host_key", "")).split()[:2])
        other = seen_keys.setdefault(key, ship_name)
        if other != ship_name:
            problems.append(f"ship.{ship_name}.host_key: same host key as {other}")
    return problems


def validate(doc: Mapping[str, Any], what: str = "ships.toml") -> None:
    schemas.check("ships.toml", doc, what)
    problems = semantic_errors(doc)
    if problems:
        listed = "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(f"{what} is inconsistent:\n{listed}")


class InventoryEditor:
    """Changes to an inventory document, keeping comments and layout."""

    def __init__(self, doc: tomlkit.TOMLDocument) -> None:
        self.doc = doc

    def snapshot(self) -> Inventory:
        return Inventory(self.doc.unwrap())

    def _ship_table(self, name: str) -> Table:
        ships = self.doc.get("ship")
        if ships is None or name not in ships:
            raise NotFound(f"No Ship named '{name}' in the inventory.")
        return ships[name]

    def add_ship(self, name: str, entry: Mapping[str, Any]) -> None:
        if "ship" not in self.doc:
            self.doc.add(tomlkit.nl())
            self.doc["ship"] = tomlkit.table(is_super_table=True)
        ships = self.doc["ship"]
        if name in ships:
            raise FleetError(f"A Ship named '{name}' is already in the inventory.")
        table = tomlkit.table()
        for key, value in entry.items():
            table[key] = _item(value)
        ships[name] = table

    def remove_ship(self, name: str) -> None:
        self._ship_table(name)
        del self.doc["ship"][name]

    def set_field(self, name: str, key: str, value: Any) -> None:
        """Set a top-level key of a Ship entry; None removes it."""
        table = self._ship_table(name)
        if value is None:
            if key in table:
                del table[key]
        else:
            table[key] = _item(value)

    def set_tags(self, name: str, tags: list[str]) -> None:
        self.set_field(name, "tags", sorted(dict.fromkeys(tags)))

    def set_nested(self, name: str, section: str, key: str, value: Any) -> None:
        """Set ship.<name>.<section>.<key> (claude, size, driver_ref); None removes it."""
        table = self._ship_table(name)
        current = dict(table.get(section, {}))
        if value is None:
            current.pop(key, None)
        else:
            current[key] = value
        self.set_field(name, section, current or None)

    def mark_enrolled(self, name: str, when: str) -> None:
        self.set_field(name, "enrolled", when)


def _item(value: Any) -> Any:
    """Small mappings become inline tables, which read best in ships.toml."""
    if isinstance(value, Mapping):
        inline = tomlkit.inline_table()
        for k, v in value.items():
            inline[k] = v
        return inline
    if isinstance(value, tuple):
        return list(value)
    return value


class TomlFileStore:
    """ships.toml on disk (default: ~/.config/fleetledger/ships.toml)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or paths.inventory_path()

    def exists(self) -> bool:
        return self.path.is_file()

    def _read(self) -> tomlkit.TOMLDocument:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(
                f"No inventory at {self.path}.", hint="Run `ship-fleet init` first."
            ) from exc
        try:
            return tomlkit.parse(text)
        except Exception as exc:  # tomlkit raises several parse error types
            raise ConfigError(f"{self.path} is not valid TOML: {exc}") from exc

    def load(self) -> Inventory:
        doc = self._read().unwrap()
        validate(doc, str(self.path))
        return Inventory(doc, self.path)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        paths.private_dir(self.path.parent)
        lock_path = self.path.with_name(self.path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def edit(self) -> Iterator[InventoryEditor]:
        with self._locked():
            editor = InventoryEditor(self._read())
            yield editor
            self._write(editor.doc)

    def create(self, doc: tomlkit.TOMLDocument) -> None:
        with self._locked():
            if self.exists():
                raise FleetError(f"{self.path} already exists; not overwriting it.")
            self._write(doc)

    def _write(self, doc: tomlkit.TOMLDocument) -> None:
        text = tomlkit.dumps(doc)
        # Re-parse what we are about to write, so the file on disk is what we validated.
        plain = tomllib.loads(text)
        validate(plain, str(self.path))
        paths.write_private(self.path, text.encode("utf-8"))


def new_inventory(admin_key: str, agent_keys: list[str]) -> tomlkit.TOMLDocument:
    """A fresh ships.toml with comments explaining each section."""
    doc = tomlkit.document()
    doc.add(tomlkit.comment("FleetLedger inventory (contracts/ships.toml.schema.json)."))
    doc.add(tomlkit.comment("Local only: it names your accounts and machines. Never commit it."))
    doc["schema"] = 1
    fleet = tomlkit.table()
    fleet["admin_key"] = admin_key
    fleet["agent_keys"] = agent_keys
    doc["fleet"] = fleet
    defaults = tomlkit.table()
    defaults["driver"] = "manual"
    defaults.add(tomlkit.comment('tags = ["work"]        # tags for new Ships'))
    defaults.add(tomlkit.comment('claude = { remote_control = "on", default_mode = "auto" }'))
    doc["defaults"] = defaults
    doc.add(tomlkit.nl())
    doc.add(tomlkit.comment("Expected accounts per tag, checked after every sign-in:"))
    doc.add(tomlkit.comment("[tag.work]"))
    doc.add(tomlkit.comment('claude = "work@example.com"'))
    doc.add(tomlkit.comment('gcloud = "work@example.com"'))
    doc.add(tomlkit.comment('github = "example-user"'))
    return doc


def ship_entry(ship: Ship) -> dict[str, Any]:
    """The ships.toml entry for a Ship (inverse of Ship.from_entry)."""
    entry: dict[str, Any] = {"id": ship.id}
    if ship.tags:
        entry["tags"] = list(ship.tags)
    entry["driver"] = ship.driver
    if ship.driver_ref:
        entry["driver_ref"] = dict(ship.driver_ref)
    entry["names"] = list(ship.names)
    entry["host_key"] = ship.host_key
    for key in ("created", "enrolled", "notes"):
        value = getattr(ship, key)
        if value is not None:
            entry[key] = value
    for key in ("size", "claude"):
        value = getattr(ship, key)
        if value:
            entry[key] = dict(value)
    for key in ("tools", "profiles"):
        value = getattr(ship, key)
        if value is not None:
            entry[key] = list(value)
    return entry
