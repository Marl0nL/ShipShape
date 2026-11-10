"""`ship-fleet unlock` and `ship-fleet vault` (vault.py; docs/vault.md).

- vault init: make the vault, with one passphrase you choose, and move every secret
  ships.toml names into it (from the OS keyring or 0600 files); then delete the old
  copies. Shows a recovery code once.
- unlock: once after each boot (or after vault lock), over ssh or in the web UI.
- vault status | lock | passphrase | recovery | off.

Moving secrets in or out is one shape: read every secret ships.toml names from where
it is now, write each to the new store, point ships.toml at it, read each back
through FleetLedger's own readers, and only then delete the old copies. A secret
that cannot be read stops it before anything changes.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleetledger import keys, paths, prompts, vault
from fleetledger.errors import ConfigError, FleetError
from fleetledger.overlay import secret_store

RUNBOOK = vault.RUNBOOK


# --- the secrets ships.toml names ------------------------------------------------------------


@dataclass(frozen=True)
class Secret:
    label: str  # for messages: "the fleet admin key's passphrase"
    kind: str  # passphrase | proxmox | oauth
    item: str  # its name in a keyring (and in the vault)
    key: Path | None = None  # passphrase: the key it opens

    def default_file(self) -> Path:
        from fleetledger import overlay_setup, proxmox_setup

        if self.kind == "passphrase":
            return keys.passphrase_file(self.key)
        return paths.config_dir() / (proxmox_setup.SECRET_FILE if self.kind == "proxmox"
                                     else overlay_setup.SECRET_FILE)


@dataclass(frozen=True)
class Placed:
    secret: Secret
    store: str  # keyring | file (keyring: the OS keyring, or the vault when it is on)
    file: Path | None  # where the file store has it


def named(inv) -> list[Placed]:
    """Every secret ships.toml names, and where it is now."""
    from fleetledger import ca

    out: list[Placed] = []
    store, managed = keys.managed(inv)
    roles = {inv.admin_key.resolve(): "the fleet admin key"}
    cfg = ca.config(inv)
    if cfg is not None:
        roles[cfg.host_key.resolve()] = "the host CA key"
        roles[cfg.user_key.resolve()] = "the user CA key"
    for path in managed:
        label = roles.get(path.resolve(), f"your key {path.name}")
        secret = Secret(f"{label}'s passphrase", "passphrase", keys.item(path), path)
        out.append(Placed(secret, store or "file",
                          keys.passphrase_file(path) if store == "file" else None))
    proxmox = inv.driver_config("proxmox") or {}
    if proxmox.get("token_id"):
        secret = Secret("the Proxmox token", "proxmox",
                        secret_store.proxmox_item(proxmox["token_id"]))
        if proxmox.get("token_store", "file") == "keyring":
            out.append(Placed(Secret(secret.label, "proxmox", str(proxmox["keyring_item"])),
                              "keyring", None))
        elif proxmox.get("token_file"):
            out.append(Placed(secret, "file", paths.expand(proxmox["token_file"])))
    api = (inv.overlay or {}).get("api") or {}
    if api.get("oauth_client_id"):
        if api.get("secret_store", "file") == "keyring":
            out.append(Placed(Secret("the overlay's OAuth secret", "oauth",
                                     str(api["keyring_item"])), "keyring", None))
        elif api.get("oauth_secret_file"):
            item = secret_store.oauth_item(str(api["oauth_client_id"]))
            out.append(Placed(Secret("the overlay's OAuth secret", "oauth", item), "file",
                              paths.expand(api["oauth_secret_file"])))
    return out


def read_from(placed: Placed, source: Any) -> str:
    """A secret's value from where it is now. source: the keyring backend to read a
    keyring item from (the OS keyring's, or the vault's)."""
    if placed.store == "file":
        return secret_store.read_file(str(placed.file), placed.secret.label)
    try:
        value = source.get_password(secret_store.SERVICE, placed.secret.item)
    except vault.Locked:
        raise
    except Exception as exc:  # noqa: BLE001 - a backend's own error: its kind only
        raise ConfigError(f"Cannot read {placed.secret.label} from the keyring "
                          f"({type(exc).__name__}).",
                          hint="Unlock your keyring (log in to your desktop session), then "
                               "try again.") from None
    if not value or not value.strip():
        raise ConfigError(f"{placed.secret.label} is not in the keyring (item "
                          f"{placed.secret.item}).")
    return value.strip()


def _names_files(store) -> bool:
    """Does ships.toml, as it is now, read any secret from a file? (Unreadable: say yes,
    so files are kept rather than lost.)"""
    try:
        return any(p.store == "file" for p in named(store.load()))
    except Exception:  # noqa: BLE001 - when unsure, keep the files
        return True


def _holds(path: Path, value: str) -> bool:
    try:
        return secret_store.read_file(str(path), "the secret") == value
    except FleetError:
        return False


def gather(inv, source: Any, skip: set[str] = frozenset()) -> list[tuple[Placed, str]]:
    """Every secret ships.toml names, with its value; refuses (naming each) if any
    cannot be read, before anything changes. skip: keyring items to leave out (those
    already in the vault, when ships.toml reads them from the keyring, i.e. the vault)."""
    found, problems = [], []
    for placed in named(inv):
        if placed.store == "keyring" and placed.secret.item in skip:
            continue
        try:
            found.append((placed, read_from(placed, source)))
        except FleetError as exc:
            problems.append(f"{placed.secret.label}: {exc.message}")
    if problems:
        raise FleetError("Cannot read every secret, so nothing was changed:\n  "
                         + "\n  ".join(problems))
    return found


def point_at(editor, moved: list[tuple[Placed, str]], store_kind: str) -> dict[str, Path]:
    """ships.toml's store fields for each moved secret, now in store_kind (keyring or
    file). Returns where the file store keeps each (by item)."""
    files: dict[str, Path] = {}
    doc = editor.doc
    for placed, _value in moved:
        secret = placed.secret
        target = secret.default_file() if store_kind == "file" else None
        if target is not None:
            files[secret.item] = target
        if secret.kind == "passphrase":
            doc["fleet"]["managed_keys"]["store"] = store_kind
        elif secret.kind == "proxmox":
            table = doc["drivers"]["proxmox"]
            for name in ("token_store", "keyring_item", "token_file"):
                if name in table:
                    del table[name]
            if store_kind == "keyring":
                table["token_store"] = "keyring"
                table["keyring_item"] = secret.item
            else:
                table["token_file"] = paths.tilde(target)
        else:
            table = doc["overlay"]["api"]
            for name in ("secret_store", "keyring_item", "oauth_secret_file"):
                if name in table:
                    del table[name]
            table["secret_store"] = store_kind
            if store_kind == "keyring":
                table["keyring_item"] = secret.item
            else:
                table["oauth_secret_file"] = paths.tilde(target)
    return files


# --- asking -----------------------------------------------------------------------------------


def _ask_secret(app, prompt: str) -> str:
    return prompts.secret(app, prompt, strip=False)  # a passphrase, exactly as typed


def _need_terminal(app, what: str) -> None:
    if getattr(app, "ask_secret", None) is None and not app.stdin_isatty():
        raise FleetError(f"{what} asks for the vault passphrase: run it in a terminal "
                         "(ssh -t ... is fine).")


def _new_passphrase(app) -> str:
    for _ in range(3):
        first = _ask_secret(app, "New vault passphrase (not shown): ")
        try:
            vault.check_passphrase(first)
        except FleetError as exc:
            app.note(exc.message)
            continue
        if _ask_secret(app, "The same again: ") == first:
            return first
        app.note("They differ; again.")
    raise FleetError("No passphrase set; nothing was changed.")


def _show_recovery(app, code: str) -> None:
    app.say("")
    app.say("Recovery code (shown once; it opens the vault if the passphrase is lost):")
    app.say(f"  {code}")
    app.say("Keep it somewhere safe and apart from the passphrase (a password manager, or "
            "on paper in a drawer). Anyone with it and a copy of this computer's disk has "
            "the fleet.")
    if not app.stdin_isatty():
        return
    last = code.rsplit("-", 1)[-1]
    for _ in range(3):
        typed = app.confirm("Type its last four characters to show you have it: ")
        if vault.clean_recovery_code(typed) == last:
            return
        app.note("That is not it; look again.")
    app.note("Not confirmed. The code is above; a new one any time: ship-fleet vault "
             "recovery.")


# --- moving secrets in and out ----------------------------------------------------------------


@dataclass
class Progress:
    """How far move_in got. Pointing is the point of no return: from then on ships.toml
    may name the vault, and every old copy is deleted only after it, so the vault may be
    the only copy of a secret. A rollback never comes after it."""

    pointing: bool = False  # ships.toml's write is about to happen, or has (it may land)


def move_in(app, store, progress: Progress | None = None) -> list[str]:
    """Every secret ships.toml names into the (unlocked) vault, then the old copies out.
    Returns the labels moved. Nothing to move without a fleet. progress: filled in as it
    goes (vault init decides from it whether taking the vault back is safe).

    It also finishes one an interrupt cut short: a secret ships.toml already reads from
    the vault is left as it is, and old copies left behind are deleted (leftovers)."""
    progress = progress if progress is not None else Progress()
    if not store.exists():
        return []
    inv = store.load()
    os_keyring, _why = secret_store.os_backend()
    names = set(vault.backend().item_names(secret_store.SERVICE))
    pending = gather(inv, _Source(os_keyring), skip=names)
    if pending:
        target = vault.backend()
        for placed, value in pending:
            target.set_password(secret_store.SERVICE, placed.secret.item, value)
        with store.edit() as ed:
            point_at(ed, pending, "keyring")
            # ships.toml is written as this block ends: from here it may land, so an
            # interrupt in the write itself already counts as pointed.
            progress.pointing = True
        _check_back(store)
        for placed, _value in pending:
            _delete_old(placed, os_keyring)
    for left in leftovers(store.load(), os_keyring):
        if left.same:  # a stale copy of what the vault holds: it goes
            left.delete()
    return [p.secret.label for p, _ in pending]


@dataclass
class Leftover:
    """An old copy of a secret the vault holds and ships.toml reads from it: a 0600
    file at its default place, or an OS keyring item of the same name."""

    secret: Secret
    where: str  # for messages
    same: bool  # holds exactly what the vault holds
    delete: Any  # () -> None


def leftovers(inv, os_keyring: Any) -> list[Leftover]:
    found: list[Leftover] = []
    try:
        backend = vault.backend()
        names = set(backend.item_names(secret_store.SERVICE))
    except FleetError:
        return []
    for placed in named(inv):
        secret = placed.secret
        if placed.store != "keyring" or secret.item not in names:
            continue
        value = backend.get_password(secret_store.SERVICE, secret.item)
        path = secret.default_file()
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                text = None
            found.append(Leftover(secret, paths.tilde(path), text == value,
                                  lambda path=path: path.unlink(missing_ok=True)))
        if os_keyring is not None:
            try:
                there = os_keyring.get_password(secret_store.SERVICE, secret.item)
            except Exception:  # noqa: BLE001 - a locked or gone keyring: nothing to say
                there = None
            if there is not None:
                found.append(Leftover(
                    secret, f"your OS keyring ({secret.item})", there.strip() == value,
                    lambda item=secret.item: _delete_old(Placed(Secret("", "", item),
                                                                "keyring", None),
                                                         os_keyring)))
    return found


class _Source:
    """The OS keyring backend for reading, or a refusal when there is none."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend

    def get_password(self, service: str, item: str) -> str | None:
        if self.backend is None:
            raise ConfigError("There is no usable OS keyring here.")
        return self.backend.get_password(service, item)


def _in_vault(item: str) -> bool:
    try:
        return vault.backend().get_password(secret_store.SERVICE, item) is not None
    except FleetError:
        return False


def _delete_old(placed: Placed, os_keyring: Any) -> None:
    if placed.store == "file" and placed.file is not None:
        placed.file.unlink(missing_ok=True)
    elif placed.store == "keyring" and os_keyring is not None:
        try:
            os_keyring.delete_password(secret_store.SERVICE, placed.secret.item)
        except Exception:  # noqa: BLE001 - gone already, or the keyring said no: said below
            pass


def _check_back(store) -> None:
    """Read every secret through FleetLedger's own readers, as ships.toml now says."""
    from fleetledger.drivers import proxmox_api

    inv = store.load()
    kind, managed = keys.managed(inv)
    for path in managed:
        keys.read_passphrase(path, kind)
    proxmox = inv.driver_config("proxmox") or {}
    if proxmox.get("token_id"):
        proxmox_api.token_secret(proxmox)
    api = (inv.overlay or {}).get("api")
    if api:
        secret_store.load(api)


def move_out(app, store, store_kind: str) -> list[str]:
    """Every secret ships.toml names out of the (unlocked) vault, into the OS keyring
    (same item names) or 0600 files; ships.toml pointed there; each read back from
    there. The vault itself is deleted by the caller, after."""
    if not store.exists():
        return []
    inv = store.load()
    moving = gather(inv, vault.backend())
    os_keyring, why = secret_store.os_backend()
    if store_kind == "keyring":
        if os_keyring is None:
            raise ConfigError(f"Cannot keep the secrets in the OS keyring here: {why}.",
                              hint="Choose --store file, or run it in your desktop session.")
        for placed, value in moving:
            os_keyring.set_password(secret_store.SERVICE, placed.secret.item, value)
            if (os_keyring.get_password(secret_store.SERVICE, placed.secret.item) or "") \
                    .strip() != value:
                raise ConfigError(f"The OS keyring did not keep {placed.secret.label}; the "
                                  "vault is unchanged.")
    if store_kind != "file":
        with store.edit() as ed:
            point_at(ed, moving, store_kind)
        return [p.secret.label for p, _ in moving]
    # Written and read back first, then pointed at; anything failing on the way takes
    # the files back, so no secret is left in a file while the vault, unchanged, still
    # holds them all. Once ships.toml may name the files, they stay (it reads them).
    # A file already holding the very value (an earlier vault off cut short) is kept
    # as it is, so vault off can always run again.
    written: list[Path] = []
    pointed = False
    try:
        for placed, value in moving:
            target = placed.secret.default_file()
            if target.exists():
                if _holds(target, value):
                    continue
                raise ConfigError(f"{paths.tilde(target)} is there already, holding something "
                                  "else; not writing over it. Nothing was changed.")
            # Not secret_store.write_file: the vault is still on here, and turning it
            # off is the one time secrets leave it for files (write_file would refuse).
            paths.private_dir(target.parent)
            paths.write_private(target, (value + "\n").encode("utf-8"))
            written.append(target)
            if secret_store.read_file(str(target), placed.secret.label) != value:
                raise ConfigError(f"{target} did not keep {placed.secret.label}.")
        with store.edit() as ed:
            point_at(ed, moving, store_kind)
            pointed = True  # ships.toml is written as this block ends: it may land
    except BaseException as exc:
        # ships.toml's write is atomic: it named the files, or it did not. Only if it
        # did must they stay.
        if not (pointed and _names_files(store)):
            for target in written:
                target.unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise ConfigError(f"Could not write the secrets to files ({exc.strerror}); the "
                              "vault is unchanged and still holds them.") from None
        raise
    return [p.secret.label for p, _ in moving]


# --- the commands -----------------------------------------------------------------------------


def unlock(secret: str, *, recovery: bool = False, account: str | None = None,
           wait: bool = True) -> str:
    """Unlock (ship-fleet unlock and the web UI's Unlock page both come here): unlocked
    or already; WrongSecret, a wait, or Locked's causes otherwise. Audited. wait=False
    for a terminal of FleetLedger's own user (vault.unlock)."""
    from fleetledger.concierge import audit

    try:
        done = vault.unlock(secret, recovery=recovery, wait=wait)
    except vault.WrongSecret:
        audit.record("-", "vault-unlock", "vault", "wrong", account=account)
        raise
    except vault.Waiting:
        audit.record("-", "vault-unlock", "vault", "waiting", account=account)
        raise
    if done == "unlocked":
        audit.record("-", "vault-unlock", "vault", "recovery" if recovery else "ok",
                     account=account)
    return done


def lock(store, account: str | None = None) -> bool:
    """Lock now (ship-fleet vault lock and the web UI's Lock both come here): the data
    key leaves RAM and the managed keys leave ssh-agent. True if it was unlocked."""
    from fleetledger.concierge import audit

    if not vault.is_on():
        raise FleetError("FleetLedger's vault is off here: there is nothing to lock.")
    was = vault.lock()
    if store.exists():
        try:
            keys.forget_loaded(store.load())
        except FleetError:
            pass
    audit.record("-", "vault-lock", "vault", "ok" if was else "already", account=account)
    return was


def cmd_unlock(args, app) -> int:
    state = vault.state()
    if state == "off":
        raise FleetError("FleetLedger's vault is off here: there is nothing to unlock.",
                         hint="ship-fleet vault init turns it on.")
    if state == "unlocked":
        app.say("The vault is unlocked already (until the next boot or ship-fleet vault lock).")
        return 0
    if args.stdin:
        secret = sys.stdin.readline().rstrip("\n")
    else:
        _need_terminal(app, "ship-fleet unlock")
        secret = _ask_secret(app, "Recovery code: " if args.recovery
                             else "Vault passphrase (not shown): ")
    # Your own terminal is not held to the wait wrong tries from the web build up.
    unlock(secret, recovery=args.recovery, wait=False)
    app.say("Unlocked, until the next boot (or ship-fleet vault lock). FleetLedger can act on "
            "the fleet again.")
    if args.recovery:
        app.say("Set a new passphrase now: ship-fleet vault passphrase --recovery")
    _warn_swap(app)
    return 0


def _warn_swap(app) -> None:
    if vault.swap_on():
        app.note("Note: swap is on here, so the unlocked key in RAM could be written to it. "
                 f"An always-on FleetLedger computer is best run without swap ({RUNBOOK}).")


def vault_init(args, app, store) -> int:
    from fleetledger.concierge import audit

    if vault.is_on():
        return _finish_init(app, store)
    vault.check_ram()
    if store.exists():  # every secret must be readable before anything changes
        gather(store.load(), _Source(secret_store.os_backend()[0]))
    _need_terminal(app, "ship-fleet vault init")
    app.say("FleetLedger's vault keeps every secret FleetLedger needs (its keys' passphrases, "
            "the Proxmox token, the OAuth secret) in one file, encrypted with a passphrase "
            "you choose. It locks at every boot; unlock it once with ship-fleet unlock or in "
            "the web UI. Until then FleetLedger is read-only; your Ships keep running.")
    passphrase = _new_passphrase(app)
    progress = Progress()
    try:
        # Inside the take-back: an interrupt after vault.json lands, before the move
        # begins, must not leave an empty vault that every reader now looks in.
        code = vault.create(passphrase)
        moved = move_in(app, store, progress)
    except BaseException as exc:
        if progress.pointing:
            # ships.toml may name the vault, and old copies may be gone: the vault may be
            # the only copy of some secret now, so it stays. It holds every secret (they
            # were all stored before pointing); vault status shows each one is in it.
            raise
        # Nothing names the vault yet, and every secret is still where it was: take the
        # new vault back, so vault init can simply run again.
        vault.vault_path().unlink(missing_ok=True)
        vault.lock()
        if isinstance(exc, FleetError | OSError):
            why = exc.message if isinstance(exc, FleetError) else exc.strerror
            raise FleetError(f"The vault could not take the secrets ({why}). It was taken "
                             "back: nothing changed, every secret is where it was.",
                             hint="Fix that, then ship-fleet vault init again.") from None
        raise
    audit.record("-", "vault-init", "vault", "ok")
    app.say(f"The vault is on: {paths.tilde(vault.vault_path())}, unlocked until the next boot.")
    if moved:
        app.say("Moved into it: " + ", ".join(moved) + ". Their old copies are deleted.")
    else:
        app.say("It is empty for now; secrets FleetLedger keeps from now on go in it.")
    _show_recovery(app, code)
    _warn_swap(app)
    return 0


def _finish_init(app, store) -> int:
    """vault init on a vault that is on already: finish what an interrupted one left
    (secrets not moved in yet, old copies left behind), or say nothing is left."""
    if vault.state() == "locked":
        raise FleetError("A vault is on here already, and locked.", hint=vault.UNLOCK_HINT)
    before = leftovers(store.load(), secret_store.os_backend()[0]) if store.exists() else []
    moved = move_in(app, store)
    after = leftovers(store.load(), secret_store.os_backend()[0]) if store.exists() else []
    gone = len(before) - len(after)
    if moved or gone:
        from fleetledger.concierge import audit

        audit.record("-", "vault-init", "vault", "finished")
    if not moved and not gone and not after:
        app.say(f"The vault is on already ({paths.tilde(vault.vault_path())}), and every "
                "secret ships.toml names is in it; nothing was left behind.")
        return 0
    app.say("The vault is on already; finished what an interrupted vault init left:"
            + (f" moved in {', '.join(moved)};" if moved else "")
            + (f" deleted {gone} old cop{'y' if gone == 1 else 'ies'} left behind." if gone
               else ""))
    for left in after:
        app.say(f"  {left.secret.label}: an old copy at {left.where} differs from the vault's; "
                "left as it is: delete it once you know which is right.")
    return 0


def vault_status(args, app, store) -> int:
    state = vault.state()
    if state == "off":
        app.say("Vault: off. Secrets are kept in "
                + ("your OS keyring" if secret_store.os_backend()[0] is not None
                   else "0600 files") + " (as ships.toml says). Turn it on: ship-fleet vault "
                "init.")
        return 0
    app.say(f"Vault: {state} ({paths.tilde(vault.vault_path())}).")
    if state == "locked":
        app.say(f"FleetLedger is read-only until it is unlocked. {vault.UNLOCK_HINT}")
        waiting = vault.tries().wait(time.time())
        if waiting > 0:
            app.say(f"After wrong tries, the next may be made in {int(waiting) + 1} seconds.")
        return 0
    try:
        names = vault.backend().item_names(secret_store.SERVICE)
    except FleetError as exc:
        app.say(f"Cannot read it: {exc.message}")
        return 1
    app.say(f"It holds {len(names)} secret(s).")
    if store.exists():
        inv = store.load()
        for placed in named(inv):
            held = placed.secret.item in names
            if placed.store == "keyring":
                said = "in the vault" if held else "NOT in the vault"
            elif held:  # an interrupted vault init: stored, not yet pointed at
                said = (f"in the vault, but read from its {placed.store} still (ship-fleet "
                        "vault init finishes the move)")
            else:
                said = f"NOT in the vault ({placed.store})"
            app.say(f"  {placed.secret.label}: {said}")
        for left in leftovers(inv, secret_store.os_backend()[0]):
            app.say(f"  {left.secret.label}: an old copy is left behind at {left.where}"
                    + (" (ship-fleet vault init deletes it)" if left.same
                       else " and differs from the vault's: delete it once you know which "
                            "is right"))
    _warn_swap(app)
    return 0


def vault_lock(args, app, store) -> int:
    was = lock(store)
    app.say("Locked: FleetLedger is read-only until it is unlocked again (ship-fleet unlock). "
            "Your Ships keep running." if was else "The vault was locked already.")
    return 0


def vault_passphrase(args, app, store) -> int:
    from fleetledger.concierge import audit

    vault.load_file()
    _need_terminal(app, "ship-fleet vault passphrase")
    current = _ask_secret(app, "Recovery code: " if args.recovery
                          else "Current vault passphrase (not shown): ")
    new = _new_passphrase(app)
    vault.change_passphrase(current, new, recovery=args.recovery)
    audit.record("-", "vault-passphrase", "vault", "recovery" if args.recovery else "ok")
    app.say("The vault's passphrase is changed. The recovery code is as it was. Backups taken "
            "before still open with the old passphrase.")
    return 0


def vault_recovery(args, app, store) -> int:
    from fleetledger.concierge import audit

    vault.load_file()
    _need_terminal(app, "ship-fleet vault recovery")
    passphrase = _ask_secret(app, "Vault passphrase (not shown): ")
    code = vault.new_recovery(passphrase)
    audit.record("-", "vault-recovery", "vault", "ok")
    app.say("A new recovery code; the old one no longer opens this vault (a backup taken "
            "before still opens with it).")
    _show_recovery(app, code)
    return 0


def vault_off(args, app, store) -> int:
    from fleetledger.concierge import audit

    if not vault.is_on():
        app.say("FleetLedger's vault is off here already.")
        return 0
    vault.data_key()  # unlocked, or Locked
    kind = args.store or ("keyring" if secret_store.os_backend()[0] is not None else "file")
    if not args.yes:
        into = "your OS keyring" if kind == "keyring" else "0600 files"
        prompts.typed(app, f"Move every secret out of the vault into {into} and delete the "
                      "vault? Type off to go on: ", "off",
                      no_terminal=f"Turning the vault off puts every secret back in {into}. "
                      "Run it in a terminal to confirm, or add --yes.",
                      refused="Nothing done.")
    moved = move_out(app, store, kind)
    path = vault.vault_path()
    try:
        path.unlink()
    finally:
        if not path.exists():  # gone: its RAM key opens nothing now. Still there (cut
            vault.lock()       # short before the delete): it stays unlocked, to run again
    if store.exists():
        _check_back(store)  # now through the OS keyring or the files
    audit.record("-", "vault-off", "vault", kind)
    app.say(f"The vault is off: deleted {paths.tilde(path)}. "
            + (f"Moved out: {', '.join(moved)}, into "
               + ("your OS keyring." if kind == "keyring" else "0600 files.") if moved else ""))
    return 0


VERBS = ("init", "status", "lock", "passphrase", "recovery", "off")


def add_parsers(sub, add) -> None:
    import argparse

    u = add(sub, "unlock", "unlock FleetLedger's vault, once after each boot",
            "Unlock FleetLedger's vault with its passphrase: once after each boot, for\n"
            "every secret and every FleetLedger process on this computer (the service,\n"
            "the web UI, commands over ssh). Fine over ssh: ssh -t HOST ship-fleet unlock.\n"
            f"See {RUNBOOK}.")
    u.add_argument("--recovery", action="store_true",
                   help="unlock with the recovery code instead of the passphrase")
    u.add_argument("--stdin", action="store_true",
                   help="read the passphrase (or code) from one line of standard input")
    p = add(sub, "vault", "FleetLedger's vault: every secret, unlocked once per boot",
            "FleetLedger's vault: one encrypted file holding every secret FleetLedger\n"
            "needs (its keys' passphrases, the Proxmox token, the OAuth secret), unlocked\n"
            "once after each boot with one passphrase (ship-fleet unlock, or the web UI).\n"
            "Locked, FleetLedger is read-only; your Ships keep running. Off unless you\n"
            f"turn it on. See {RUNBOOK}.\n\n"
            "  ship-fleet vault init            make it, move every secret in\n"
            "  ship-fleet vault status          off, locked or unlocked, and what it holds\n"
            "  ship-fleet vault lock            lock it now\n"
            "  ship-fleet vault passphrase      change the passphrase\n"
            "  ship-fleet vault recovery        a new recovery code\n"
            "  ship-fleet vault off             move every secret back out")
    verbs = p.add_subparsers(dest="vault_command", metavar="<action>", title="actions")
    verbs.add_parser("init", help="make the vault and move every secret into it")
    verbs.add_parser("status", help="off, locked or unlocked, and what it holds")
    verbs.add_parser("lock", help="lock it now (FleetLedger is read-only until unlocked)")
    pw = verbs.add_parser("passphrase", help="change the passphrase",
                          formatter_class=argparse.RawDescriptionHelpFormatter)
    pw.add_argument("--recovery", action="store_true",
                    help="prove it with the recovery code (a lost passphrase)")
    verbs.add_parser("recovery", help="a new recovery code (the old one stops working)")
    off = verbs.add_parser("off", help="move every secret back out and delete the vault")
    off.add_argument("--store", choices=("keyring", "file"),
                     help="where the secrets go (default: the OS keyring where there is one)")
    off.add_argument("--yes", action="store_true", help="do not ask")


def cmd_vault(args, app) -> int:
    verb = getattr(args, "vault_command", None) or "status"
    return globals()[f"vault_{verb}"](args, app, app.store_factory())
