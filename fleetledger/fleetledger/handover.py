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
refused here. This computer's vault, if it has one, stays too, with the copy's
secrets taken out of it: it is the computer's keyring, not the copy's.

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


def gate(command: str, store) -> None:
    """Refuse every command but ALLOWED on a copy on standby or retired."""
    if command in ALLOWED:
        return
    m = marker(store)
    if m is not None:
        raise banner(m)


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
    os_keyring: bool = False  # the OS keyring's item even while the vault is on (an old copy)


def wipe_plan(store) -> tuple[list[Gone], list[tuple[str, str]]]:
    """What migrate wipe deletes, and what it keeps and why. Reads only.

    Only what this copy's ships.toml shows to be its own: its keys, its secrets (by the
    items and files it names) and its state. Never this computer's vault (its keyring,
    which a later receive stores into), nor anything in it the copy does not name."""
    from fleetledger import ca, keys, secretplaces
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
    for place in secretplaces.PLACES:
        found = place.location(place.table(inv.doc) or {})
        if found is None:
            continue
        store_kind, where = found
        if store_kind == "keyring":
            if where:
                gone.append(Gone(place.label, where, keyring=True))
        elif paths.expand(where).exists():
            gone.append(Gone(place.label, str(paths.expand(where))))
    listed = {Path(g.where).resolve() for g in gone if not g.keyring}
    if vault.is_on():
        # The vault is this computer's keyring, not the copy's: only the fleet's items
        # leave it. Those include a copy of a secret ships.toml still reads from a file
        # (a vault init cut short), so none of the fleet's secrets stays behind in it.
        from fleetledger import vaultcmd
        from fleetledger.overlay import secret_store

        def in_vault(item: str) -> str | None:  # FleetError: locked or unreadable
            value = vault.backend().get_password(secret_store.SERVICE, item)
            return value.strip() if value is not None else None

        items = {g.where for g in gone if g.keyring}
        os_ring, _why = secret_store.os_backend()
        for placed in vaultcmd.named(inv):
            key = placed.secret.key
            if key is not None and key.resolve() not in role_paths:
                continue  # your own key's passphrase: it stays, with the key
            item, label = placed.secret.item, placed.secret.label
            if not item:
                continue
            try:  # the copy's secret, where ships.toml reads it: to tell its copies by
                value = in_vault(item) if placed.store == "keyring" else \
                    secret_store.read_file(str(placed.file), label)
            except FleetError:
                value = None
            if placed.store != "keyring" and item not in items:
                # A copy in the vault while ships.toml reads a file (a vault init cut
                # short): it goes when it holds the same secret.
                try:
                    copy, readable = in_vault(item), True
                except FleetError:
                    copy, readable = None, False
                if not readable or (copy is not None and copy == value):
                    items.add(item)  # unreadable: listed, and the wipe stops on the vault
                    gone.append(Gone(f"{label} (a copy in the vault)", item, keyring=True))
                elif copy is not None:
                    kept.append((f"vault item {item}", f"{label}: not what ships.toml reads, "
                                 "so it may not be this copy's; left as it is"))
            if os_ring is None:
                continue
            # An old copy in the OS keyring (a vault init cut short) goes too, but only
            # one holding the very secret: another FleetLedger may use that name.
            try:
                there = os_ring.get_password(secret_store.SERVICE, item)
            except Exception:  # noqa: BLE001 - a locked OS keyring: its kind only
                kept.append((f"OS keyring item {item}", f"could not be checked for an old "
                             f"copy of {label} (locked?): look, and delete it if it is one"))
                continue
            if there is None:
                continue
            if value is not None and there.strip() == value:
                gone.append(Gone(f"{label} (an old copy in your OS keyring)", item,
                                 keyring=True, os_keyring=True))
            else:
                kept.append((f"OS keyring item {item}", f"{label}: not the fleet's secret (or "
                             "that could not be read), so it may not be this copy's; left "
                             "as it is"))
        if staying and kind == "keyring":
            why = ("FleetLedger's vault: it still holds your own key's passphrase (the "
                   "fleet's secrets are deleted from it). Keep it while you use that key: "
                   "ship-fleet vault off here would delete that passphrase with it")
        else:
            why = ("FleetLedger's vault, this computer's keyring: the fleet's secrets are "
                   "deleted from it, and the vault stays with anything else in it "
                   "(ship-fleet vault status); ship-fleet vault off deletes it")
        kept.append((paths.tilde(vault.vault_path()), why))
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
            if f == vault.vault_path() or f.resolve() == vault.vault_path().resolve():
                continue  # this computer's vault: never deleted (kept, above)
            if f.resolve() in staying or f.resolve() in listed:
                continue
            if _in_toolkit(f, kits) is not None:
                continue  # a toolkit folder of yours (kept, above)
            gone.append(Gone("FleetLedger's own file", str(f)))
    # A keyring item of your own key's never goes, whatever ships.toml says of others.
    own = {keys.item(p) for p in managed if p.resolve() not in role_paths}
    gone = [g for g in gone if not (g.keyring and g.where in own)
            and not (not g.keyring and Path(g.where).resolve() in staying)]
    return gone, kept


def wipe(store, gone: list[Gone]) -> None:
    """Delete what wipe_plan listed: keyring items first (a locked keyring or vault
    stops it before any file goes), then the files, ships.toml last (so a wipe cut
    short can run again), then emptied folders."""
    from fleetledger.overlay import secret_store

    ring = [g for g in gone if g.keyring and not g.os_keyring]
    if ring and vault.is_on():
        try:  # what the vault holds, read once: locked or unreadable stops it here
            held = set(vault.backend().item_names(secret_store.SERVICE))
        except FleetError as exc:
            raise FleetError(f"{exc.message} Nothing was deleted.", hint=exc.hint) from None
        for g in ring:
            if g.where in held:
                secret_store.delete_from_keyring(g.where)
    elif ring:
        usable, why = secret_store.keyring_usable()
        if not usable:
            raise FleetError(f"Cannot reach the keyring ({why}); nothing was deleted.",
                             hint="Unlock it (log in to your desktop session), then try again.")
        for g in ring:
            if secret_store.existing_in_keyring(g.where) is not None:
                secret_store.delete_from_keyring(g.where)
    os_items = [g for g in gone if g.os_keyring]
    if os_items:
        os_ring, why = secret_store.os_backend()
        for g in os_items:
            try:
                if os_ring is not None and os_ring.get_password(secret_store.SERVICE,
                                                                g.where) is not None:
                    os_ring.delete_password(secret_store.SERVICE, g.where)
            except Exception as exc:  # noqa: BLE001 - its kind only: never a secret
                raise FleetError(f"Could not remove {g.where} from your OS keyring "
                                 f"({type(exc).__name__}); no file was deleted yet.",
                                 hint="Unlock it (log in to your desktop session), then run "
                                 "the wipe again.") from None
    from fleetledger.migrate import _in_toolkit, _toolkit_dirs

    try:  # read before ships.toml goes: its toolkit folders stay whole, empty ones too
        kits = _toolkit_dirs(store.load())
    except FleetError:
        kits = {}
    files = list(dict.fromkeys(g.where for g in gone if not g.keyring))
    files.sort(key=lambda f: Path(f) == store.path)  # ships.toml last
    for f in files:
        try:
            Path(f).unlink(missing_ok=True)
        except OSError as exc:
            raise FleetError(f"Could not delete {paths.tilde(Path(f))} ({exc.strerror}); "
                             "the wipe stopped there.",
                             hint="Fix that, then run ship-fleet migrate wipe again: it "
                             "finishes what is left.") from None
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
