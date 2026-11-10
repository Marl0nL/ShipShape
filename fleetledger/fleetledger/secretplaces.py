"""Where ships.toml says each secret lives: one row per secret, so a new secret (a
git token, say) is one more row rather than field names spelt out in every module.

A secret is kept in the keyring (the OS keyring, or FleetLedger's vault when it is
on) under an item, or in a 0600 file (overlay/secret_store.py). ships.toml records
which, in three fields of the secret's table: the store, the keyring item and the
file. Never the secret itself. SSH key passphrases are recorded differently
([fleet.managed_keys] store), so they are not a row here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleetledger import paths
from fleetledger.overlay import secret_store

ITEM_FIELD = "keyring_item"


@dataclass(frozen=True)
class Place:
    kind: str  # proxmox | oauth: what vaultcmd and migrate call it
    label: str  # for messages: "the Proxmox token"
    section: tuple[str, ...]  # its table in ships.toml
    id_field: str  # what names the secret (no id: no secret)
    store_field: str  # "keyring", or "file" (the default when absent)
    file_field: str
    file_names_store: bool  # the file store writes store_field = "file" (else leaves it out)
    item_for: Callable[[str], str]  # the keyring item for an id

    @property
    def fields(self) -> tuple[str, str, str]:
        return (self.store_field, ITEM_FIELD, self.file_field)

    def table(self, doc: Mapping[str, Any]) -> Any:
        """The secret's table in a ships.toml document, or None."""
        node: Any = doc
        for name in self.section:
            node = node.get(name) if isinstance(node, Mapping) else None
        return node

    def item(self, table: Mapping[str, Any]) -> str:
        """The keyring item this secret would have, by its id."""
        return self.item_for(str(table[self.id_field]))

    def location(self, table: Mapping[str, Any]) -> tuple[str, str] | None:
        """("keyring", item) or ("file", the file as written, ~ not expanded) where the
        table says the secret is; None when it names none."""
        if not table.get(self.id_field):
            return None
        if table.get(self.store_field, "file") == "keyring":
            return ("keyring", str(table.get(ITEM_FIELD, "")))
        if table.get(self.file_field):
            return ("file", str(table[self.file_field]))
        return None

    def clear(self, table: MutableMapping[str, Any]) -> None:
        """Remove the store fields (set() then adds them at the table's end)."""
        for name in self.fields:
            if name in table:
                del table[name]

    def set(self, table: MutableMapping[str, Any], store: str, *, item: str | None = None,
            file: Path | None = None) -> None:
        """Record that the secret is in store: keyring (under item) or file. A field
        already there keeps its place; the other store's field goes."""
        if store == "keyring":
            table[self.store_field] = "keyring"
            table[ITEM_FIELD] = item
            table.pop(self.file_field, None)
            return
        if self.file_names_store:
            table[self.store_field] = "file"
        else:
            table.pop(self.store_field, None)
        table[self.file_field] = paths.tilde(file)
        table.pop(ITEM_FIELD, None)


PROXMOX = Place("proxmox", "the Proxmox token", ("drivers", "proxmox"), "token_id",
                "token_store", "token_file", file_names_store=False,
                item_for=secret_store.proxmox_item)
OAUTH = Place("oauth", "the overlay's OAuth secret", ("overlay", "api"), "oauth_client_id",
              "secret_store", "oauth_secret_file", file_names_store=True,
              item_for=secret_store.oauth_item)
PLACES = (PROXMOX, OAUTH)
BY_KIND = {p.kind: p for p in PLACES}
