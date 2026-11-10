"""The fleet inventory: ships.toml (contracts/ships.toml.schema.json).

One local file is the source of truth for every Ship's names, pinned host key,
driver, tags and settings, plus the accounts each tag expects. Reading goes
through Inventory (an immutable view); changes go through store.Store.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fleetledger import paths, signinreg
from fleetledger.errors import NotFound

SIZE_DEFAULTS = {"cores": 4, "memory_mb": 8192, "disk_gb": 40}

STOCK_IMAGE = "stock"
"""The driver's own template, made from Ubuntu's cloud image (contracts/image.md)."""


def now_timestamp() -> str:
    """UTC, RFC 3339, Z suffix, whole seconds (the contracts' timestamp format)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_ship_id() -> str:
    return str(uuid.uuid4())


@dataclass(frozen=True)
class Ship:
    """One [ship.<name>] entry."""

    name: str
    id: str
    driver: str
    names: tuple[str, ...]
    host_key: str
    tags: tuple[str, ...] | None = None  # None: absent, so defaults.tags apply
    driver_ref: Mapping[str, str | int] = field(default_factory=dict)
    created: str | None = None
    enrolled: str | None = None
    size: Mapping[str, int] = field(default_factory=dict)
    claude: Mapping[str, Any] = field(default_factory=dict)
    tools: tuple[str, ...] | None = None
    profiles: tuple[str, ...] | None = None
    notes: str | None = None
    image: str | None = None  # the base image it was made from (None: before images)
    overlay_node: str | None = None  # overlay node id recorded at join (phase 5)
    signins: tuple[str, ...] | None = None  # the sign-ins it uses; None: all of them
    github_scopes: tuple[str, ...] = ()  # extra GitHub scopes it signs in with (ghscopes)

    @classmethod
    def from_entry(cls, name: str, entry: Mapping[str, Any]) -> Ship:
        return cls(
            name=name,
            id=entry["id"],
            driver=entry["driver"],
            names=tuple(entry["names"]),
            host_key=entry["host_key"],
            tags=tuple(entry["tags"]) if "tags" in entry else None,
            driver_ref=dict(entry.get("driver_ref", {})),
            created=entry.get("created"),
            enrolled=entry.get("enrolled"),
            size=dict(entry.get("size", {})),
            claude=dict(entry.get("claude", {})),
            tools=tuple(entry["tools"]) if "tools" in entry else None,
            profiles=tuple(entry["profiles"]) if "profiles" in entry else None,
            notes=entry.get("notes"),
            image=entry.get("image"),
            overlay_node=entry.get("overlay_node"),
            signins=tuple(entry["signins"]) if "signins" in entry else None,
            github_scopes=tuple(entry.get("github_scopes", ())),
        )

    @property
    def host_key_type_and_blob(self) -> str:
        """The host key without its comment: '<type> <base64>'."""
        return " ".join(self.host_key.split()[:2])


@dataclass(frozen=True)
class ExpectedAccount:
    """What a Ship's tags say one service should be signed in as."""

    service: str
    accounts: frozenset[str]  # empty: no expectation; >1: the tags disagree

    @property
    def conflicting(self) -> bool:
        return len(self.accounts) > 1

    def matches(self, account: str | None) -> bool | None:
        """True or False, or None when there is nothing to compare."""
        if not self.accounts or account is None:
            return None
        if self.conflicting:
            return False
        (expected,) = self.accounts
        return account.strip().casefold() == expected.strip().casefold()


class Inventory:
    """A read-only view of a validated ships.toml document."""

    def __init__(self, doc: Mapping[str, Any], path: Path | None = None) -> None:
        self.doc = doc
        self.path = path

    # --- fleet -----------------------------------------------------------
    @property
    def admin_key(self) -> Path:
        return paths.expand(self.doc["fleet"]["admin_key"])

    @property
    def admin_public_key_path(self) -> Path:
        return self.admin_key.with_name(self.admin_key.name + ".pub")

    @property
    def owner(self) -> str:
        """Your own login name on Ships (fleet.owner; default human)."""
        return str(self.doc["fleet"].get("owner", "human"))

    @property
    def agent_key_paths(self) -> list[Path]:
        return [paths.expand(p) for p in self.doc["fleet"]["agent_keys"]]

    @property
    def known_hosts(self) -> Path:
        value = self.doc["fleet"].get("known_hosts")
        return paths.expand(value) if value else paths.default_known_hosts()

    # --- defaults, tags, drivers ------------------------------------------
    @property
    def defaults(self) -> Mapping[str, Any]:
        return self.doc.get("defaults", {})

    @property
    def tag_accounts(self) -> Mapping[str, Mapping[str, str]]:
        return self.doc.get("tag", {})

    def profile_options(self, profile: str) -> Mapping[str, str]:
        return self.doc.get("profile_options", {}).get(profile, {})

    def driver_config(self, driver: str) -> Mapping[str, Any] | None:
        return self.doc.get("drivers", {}).get(driver)

    # --- images and the overlay (phases 4 and 5) ---------------------------
    @property
    def images(self) -> Mapping[str, Mapping[str, Any]]:
        """[image.<id>] entries (contracts/image.md)."""
        return self.doc.get("image", {})

    def image(self, image_id: str) -> Mapping[str, Any]:
        entry = self.images.get(image_id)
        if entry is None:
            known = ", ".join(sorted(self.images)) or "none yet"
            raise NotFound(f"No image '{image_id}' in the inventory (known: {known}).")
        return entry

    @property
    def pinned_image(self) -> str:
        """The image new Ships are made from: an image id, or "stock"."""
        return self.defaults.get("image", STOCK_IMAGE)

    @property
    def overlay(self) -> Mapping[str, Any] | None:
        return self.doc.get("overlay")

    # --- ships ------------------------------------------------------------
    def ships(self, tag: str | None = None) -> list[Ship]:
        found = [Ship.from_entry(n, e) for n, e in sorted(self.doc.get("ship", {}).items())]
        if tag is not None:
            found = [s for s in found if tag in self.effective_tags(s)]
        return found

    def has_ship(self, name: str) -> bool:
        return name in self.doc.get("ship", {})

    def ship(self, name: str) -> Ship:
        entry = self.doc.get("ship", {}).get(name)
        if entry is None:
            known = ", ".join(sorted(self.doc.get("ship", {}))) or "none yet"
            raise NotFound(f"No Ship named '{name}' in the inventory (known: {known}).")
        return Ship.from_entry(name, entry)

    def select(self, names: Iterable[str] = (), tag: str | None = None) -> list[Ship]:
        """Ships by name, or every Ship with a tag; at least one must match."""
        names = list(names)
        if names:
            chosen = [self.ship(n) for n in names]
            if tag is not None:
                chosen = [s for s in chosen if tag in self.effective_tags(s)]
        else:
            chosen = self.ships(tag)
        if not chosen:
            raise NotFound(f"No Ships with tag '{tag}'." if tag else "No Ships in the inventory.")
        return chosen

    def all_tags(self) -> list[str]:
        tags = set(self.tag_accounts)
        for ship in self.ships():
            tags.update(self.effective_tags(ship))
        return sorted(tags)

    def effective_tags(self, ship: Ship) -> tuple[str, ...]:
        """The Ship's own tags, or defaults.tags if it has no tags entry ([] means none)."""
        if ship.tags is not None:
            return ship.tags
        return tuple(self.defaults.get("tags", ()))

    def expected_account(self, ship: Ship, service: str) -> ExpectedAccount:
        """The account the Ship's tags expect for a service (claude, gcloud, github).

        A status key follows its sign-in's account (gcloud_adc follows gcloud). A Ship
        with no tags, or tags that name no account for the service, has no
        expectation. Tags follow effective_tags.
        """
        key = signinreg.registry().signin_of_service(service)
        accounts = {
            self.tag_accounts[t][key]
            for t in self.effective_tags(ship)
            if t in self.tag_accounts and key in self.tag_accounts[t]
        }
        return ExpectedAccount(service, frozenset(accounts))

    def effective_claude(self, ship: Ship) -> dict[str, Any]:
        """defaults.claude overlaid with the Ship's own claude settings."""
        merged = dict(self.defaults.get("claude", {}))
        merged.update(ship.claude)
        return merged

    def effective_size(self, ship: Ship | None = None) -> dict[str, int]:
        size = dict(SIZE_DEFAULTS)
        size.update(self.defaults.get("size", {}))
        if ship is not None:
            size.update(ship.size)
        return size
