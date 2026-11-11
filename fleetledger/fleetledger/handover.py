"""Which copy of FleetLedger acts on the fleet: active, standby or retired.

Only one FleetLedger may act on a fleet at a time. Two would undo each other's
changes: an older checkout's `update` rolls Ships back, and a stale inventory's
revocation list un-revokes machines. So `ship-fleet migrate` hands over in steps:

- receive leaves the new copy on **standby**: it has the whole fleet, but refuses to
  act until it is activated;
- at the cut-over (push, once the new copy has checked every Ship) the old copy
  **retires** itself first, then the new copy **activates** with the next epoch.
  Retiring first means a cut-over cut short leaves no copy active, never two.

A copy on standby or retired refuses every command but migrate's own, doctor and
service (which only manages the systemd unit) and its vault's (unlock, vault), the
web UI included, saying why and what to do. The **epoch** ([fleet] epoch in
ships.toml, 0 when absent) goes up at every activation. A retired copy remembers
the epoch it handed over to, so a later receive refuses a fleet older than one this
computer already handed on (an old backup, a forgotten copy).

Rollback within the window: `migrate retire` on the new copy, `migrate unretire` on
the old one. After about two weeks, `migrate wipe` deletes a retired copy's keys,
secrets and state; only its marker stays (no secrets), so an older fleet is still
refused here.

The markers sit beside ships.toml (standby.json, retired.json): facts about this
copy, never moved with the fleet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fleetledger import paths, vault
from fleetledger.errors import FleetError

STANDBY, RETIRED = "standby.json", "retired.json"
SCHEMA = "fleetledger.copy/1"
WIPE_AFTER = timedelta(days=14)  # keep a retired copy this long, for a rollback
# What a copy on standby or retired may still run (each checks for itself). service
# only installs, shows or removes the systemd unit (the UI it runs refuses as ever);
# unlock and vault act on no fleet, and a copy whose vault locked at a reboot must be
# able to open it to be activated, unretired or wiped.
ALLOWED = ("migrate", "doctor", "service", "unlock", "vault")


@dataclass(frozen=True)
class Marker:
    role: str  # standby | retired | unreadable (a marker file that cannot be read)
    when: str  # received / retired (RFC 3339)
    other: str | None  # the computer it came from (standby) or went to (retired)
    epoch: int  # standby: the epoch it activates with; retired: the epoch handed over to
    wiped: str | None = None  # retired: when its keys, secrets and state were deleted
    path: str = field(default="", compare=False)  # the file it was read from

    @property
    def age(self) -> timedelta:
        return datetime.now(UTC) - _parse(self.when)

    def doc(self) -> dict[str, Any]:
        out = {"schema": SCHEMA, "role": self.role, "when": self.when, "other": self.other,
               "epoch": self.epoch}
        if self.wiped:
            out["wiped"] = self.wiped
        return out


def _parse(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch(inventory) -> int:
    return int((inventory.doc.get("fleet") or {}).get("epoch", 0))


# --- the markers ------------------------------------------------------------------------------


def _path(store, name: str) -> Path:
    return store.path.with_name(name)


def marker(store) -> Marker | None:
    """This copy's marker (standby or retired), or None: an active copy, or none at all.
    A marker that cannot be read, or holds anything but what FleetLedger writes, is
    "unreadable": never active, and nothing acts on what it seems to say."""
    for name, role in ((RETIRED, "retired"), (STANDBY, "standby")):
        path = _path(store, name)
        if not (path.exists() or path.is_symlink()):
            continue
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            m = Marker(role, doc["when"], doc.get("other"), doc["epoch"], doc.get("wiped"),
                       str(path))
            ok = (doc.get("schema") == SCHEMA and doc.get("role") == role
                  and isinstance(m.when, str) and _parse(m.when)
                  and (m.other is None or isinstance(m.other, str))
                  and isinstance(m.epoch, int) and not isinstance(m.epoch, bool)
                  and 0 <= m.epoch < 1 << 31
                  and (m.wiped is None or (role == "retired" and isinstance(m.wiped, str)
                                           and _parse(m.wiped))))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            ok = False
        if not ok:
            return Marker("unreadable", "", None, 0, None, str(path))
        return m
    return None


def write(store, m: Marker) -> None:
    paths.private_dir(store.path.parent)
    paths.write_private(_path(store, RETIRED if m.role == "retired" else STANDBY),
                        (json.dumps(m.doc(), indent=2) + "\n").encode("utf-8"))


def clear(store, role: str) -> None:
    _path(store, RETIRED if role == "retired" else STANDBY).unlink(missing_ok=True)


def banner(m: Marker) -> FleetError:
    if m.role == "unreadable":
        return FleetError(
            f"{paths.tilde(m.path)} cannot be read as FleetLedger wrote it, so this copy "
            "does not act on the fleet: it may have been handed on (ship-fleet migrate).",
            hint="Fix or delete that file once you know which copy is the active one (two "
            "active copies undo each other's changes); docs/runbooks/move-fleetledger.md.")
    day = m.when[:10]
    if m.role == "standby":
        return FleetError(
            f"This FleetLedger is on standby: it has the fleet from "
            f"{m.other or 'another computer'} (received {day}) but does not act on it until "
            "it is made active.",
            hint="Its old computer's ship-fleet migrate push makes it active. Run by hand: "
            "ship-fleet migrate retire there, then ship-fleet migrate activate here.")
    if m.wiped:
        return FleetError(f"This FleetLedger was retired on {day} and wiped on {m.wiped[:10]}: "
                          f"the fleet is managed at {m.other or 'another computer'}.",
                          hint="To manage the fleet here again, move it back (ship-fleet "
                          "migrate push from there). To start another fleet here instead, "
                          f"delete {paths.tilde(m.path)} first.")
    return FleetError(
        f"This FleetLedger was retired on {day}, when the fleet moved to "
        f"{m.other or 'another computer'} (epoch {m.epoch}): it no longer acts on the fleet. "
        "Use the FleetLedger there.",
        hint="To roll back: ship-fleet migrate retire there, then ship-fleet migrate unretire "
        "here. After two weeks, ship-fleet migrate wipe deletes this copy's keys and secrets.")


def refusal(store) -> FleetError | None:
    """Why this copy may not act on the fleet (its marker's banner), or None: it may.
    Any marker at all says no, one that cannot be read included. The CLI's gate and the
    web UI (every request and every Ship poll, web/copyguard.py) both ask here."""
    m = marker(store)
    return None if m is None else banner(m)


def gate(command: str, store) -> None:
    """Refuse every command but ALLOWED on a copy on standby or retired."""
    if command in ALLOWED:
        return
    why = refusal(store)
    if why is not None:
        raise why


def confirm(app, question: str, word: str, yes: bool) -> None:
    """A typed yes before handing a fleet over (--yes, or a word typed in a terminal)."""
    if yes:
        return
    if not app.stdin_isatty():
        raise FleetError(f"{question} Run it in a terminal to answer, or add --yes.")
    if app.confirm(f"{question} Type {word} to go on: ").strip().lower() != word:
        raise FleetError("Nothing done.")


# --- the steps ------------------------------------------------------------------------------


def retire(store, to: str | None, handed_to: int) -> Marker:
    """This copy stops acting on the fleet (its files stay, for a rollback)."""
    m = Marker("retired", now(), to, handed_to)
    write(store, m)
    return m


def activate(store) -> int:
    """A copy on standby becomes the active one, with the epoch it was received for."""
    m = marker(store)
    if m is None or m.role != "standby":
        raise FleetError("This FleetLedger is not on standby: there is nothing to activate.")
    with store.edit() as ed:
        ed.doc["fleet"]["epoch"] = m.epoch
    clear(store, "standby")
    return m.epoch


def unretire(store) -> int:
    """A retired copy acts on the fleet again, with an epoch above any it handed over."""
    m = marker(store)
    if m is not None and m.role == "unreadable":
        raise banner(m)
    if m is None or m.role != "retired":
        raise FleetError("This FleetLedger is not retired.")
    if m.wiped:
        raise banner(m)
    new = max(epoch(store.load()), m.epoch) + 1
    with store.edit() as ed:
        ed.doc["fleet"]["epoch"] = new
    clear(store, "retired")
    return new


# --- wipe ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gone:
    what: str
    where: str  # a path, or a keyring item
    keyring: bool = False


def wipe_plan(store) -> tuple[list[Gone], list[tuple[str, str]]]:
    """What migrate wipe deletes, and what it keeps and why. Reads only."""
    from fleetledger import ca, keys
    from fleetledger.migrate import _files

    inv = store.load()
    kind, managed = keys.managed(inv)
    managed_set = {k.resolve() for k in managed}
    gone: list[Gone] = []
    kept: list[tuple[str, str]] = []
    roles = [("fleet admin key", inv.admin_key)]
    cfg = ca.config(inv)
    if cfg is not None:
        roles += [("host CA key", cfg.host_key), ("user CA key", cfg.user_key)]
    role_paths = {p.resolve() for _l, p in roles}
    for label, path in roles:
        if path.resolve() not in managed_set:
            if path.exists():
                kept.append((paths.tilde(path), f"the {label}, which you look after yourself: "
                             "delete it and its .pub unless you use it elsewhere"))
            continue
        for f in (path, path.with_name(path.name + ".pub")):
            if f.exists():
                gone.append(Gone(label, str(f)))
        if kind == "keyring":
            gone.append(Gone(f"the {label}'s passphrase", keys.item(path), keyring=True))
        elif keys.passphrase_file(path).exists():
            gone.append(Gone(f"the {label}'s passphrase", str(keys.passphrase_file(path))))
    staying = set()
    for path in managed:
        if path.resolve() not in role_paths:
            kept.append((paths.tilde(path), "your own key, made by FleetLedger: this computer "
                         "still attaches to Ships with it (its passphrase stays too)"))
            staying.add(keys.passphrase_file(path).resolve())
    proxmox = inv.driver_config("proxmox") or {}
    api = (inv.overlay or {}).get("api") or {}
    for label, table, store_key, item_key, file_key in (
            ("the Proxmox token", proxmox, "token_store", "keyring_item", "token_file"),
            ("the overlay's OAuth secret", api, "secret_store", "keyring_item",
             "oauth_secret_file")):
        if table.get(store_key) == "keyring":
            gone.append(Gone(label, str(table[item_key]), keyring=True))
        elif table.get(file_key) and paths.expand(table[file_key]).exists():
            gone.append(Gone(label, str(paths.expand(table[file_key]))))
    listed = {Path(g.where).resolve() for g in gone if not g.keyring}
    if staying and kind == "keyring" and vault.is_on():
        # The vault keeps your own key's passphrase: only the fleet's items leave it.
        staying.add(vault.vault_path().resolve())
        kept.append((paths.tilde(vault.vault_path()), "FleetLedger's vault: it still holds "
                     "your own key's passphrase (the fleet's secrets are deleted from it); "
                     "ship-fleet vault off moves that out, then delete it"))
    from fleetledger.migrate import _in_toolkit, _toolkit_dirs

    kits = _toolkit_dirs(inv)
    for kit_dir, kit_id in sorted(kits.items()):
        if any(kit_dir.is_relative_to(b.resolve()) for b in (paths.config_dir(),
                                                              paths.data_dir())):
            kept.append((paths.tilde(kit_dir), f"toolkit {kit_id}'s folder: yours, so wipe "
                         "leaves it; delete it yourself if you no longer need it"))
    for base in (paths.config_dir(), paths.data_dir()):
        for f in _files(base):
            if f.name in (STANDBY, RETIRED) and f.parent == store.path.parent:
                continue
            if f.resolve() in staying or f.resolve() in listed:
                continue
            if _in_toolkit(f, kits) is not None:
                continue  # a toolkit folder of yours (kept, above)
            gone.append(Gone("the vault (every secret in it)" if f == vault.vault_path()
                             else "FleetLedger's own file", str(f)))
    return gone, kept


def wipe(store, gone: list[Gone]) -> None:
    """Delete what wipe_plan listed: keyring items first (a locked keyring stops it
    before any file goes), then the files, then emptied folders."""
    from fleetledger.overlay import secret_store

    if any(g.keyring for g in gone):
        usable, why = secret_store.keyring_usable()
        if not usable:
            raise FleetError(f"Cannot reach the keyring ({why}); nothing was deleted.",
                             hint="Unlock it (log in to your desktop session), then try again.")
    for g in gone:
        if g.keyring and secret_store.existing_in_keyring(g.where) is not None:
            secret_store.delete_from_keyring(g.where)
    from fleetledger.migrate import _in_toolkit, _toolkit_dirs

    try:  # read before ships.toml goes: its toolkit folders stay whole, empty ones too
        kits = _toolkit_dirs(store.load())
    except FleetError:
        kits = {}
    for g in dict.fromkeys(g.where for g in gone if not g.keyring):
        Path(g).unlink(missing_ok=True)
    if not vault.is_on():
        vault.lock()  # its unlocked key has nothing left to open
    for base in (paths.config_dir(), paths.data_dir()):
        if base.is_dir():
            folders = sorted((d for d in base.rglob("*") if d.is_dir()), reverse=True)
            for folder in [*folders, *([base] if base != store.path.parent else [])]:
                if _in_toolkit(folder, kits) is not None or any(
                        d.is_relative_to(folder.resolve()) for d in kits):
                    continue  # a toolkit folder of yours, or a folder holding one
                try:
                    folder.rmdir()
                except OSError:
                    pass  # not empty: something kept
