"""Where the Tailscale OAuth client secret (and the Proxmox API token, through
read_keyring) is kept: the OS keyring, or a 0600 file.

- keyring (what `ship-fleet overlay setup` offers first when it can): the Python
  keyring library, with a secure backend only (Secret Service on Linux, i.e.
  GNOME Keyring or KWallet; Keychain on macOS). It protects the secret at rest
  (encrypted, unlocked with your login), but an unlocked keyring is readable by
  any process running as you, agents on this computer included.
- file: a file holding only the secret, mode 0600 in a 0700 folder.

ships.toml records which store and where (overlay.api.secret_store with
keyring_item, or oauth_secret_file), never the secret. A read never falls back
from one store to the other: a locked or missing keyring is an error that says
how to unlock it or switch stores.

"The keyring" is this computer's: the OS keyring, or, once you turn it on,
FleetLedger's own vault (vault.py), which then takes every keyring item under the
same names and never falls back to the OS keyring. While the vault is on, no secret
goes to a file (refuse_file).
"""

from __future__ import annotations

import stat
from collections.abc import Callable, Mapping
from typing import Any

from fleetledger import paths
from fleetledger.errors import ConfigError, FleetError

# Stable names: people may store the secret by hand before running setup
# (service fleetledger, item tailscale-oauth:<client id>), and setup reuses it.
SERVICE = "fleetledger"
OAUTH_ITEM_PREFIX = "tailscale-oauth:"


PROXMOX_ITEM_PREFIX = "proxmox-token:"


def oauth_item(client_id: str) -> str:
    return OAUTH_ITEM_PREFIX + client_id


def proxmox_item(token_id: str) -> str:
    return PROXMOX_ITEM_PREFIX + token_id


# Backends that keep secrets encrypted by the OS. Anything else (the fail and null
# backends, plaintext or "encrypted file" keyrings from keyrings.alt, ...) is refused.
SECURE_BACKENDS = {
    "keyring.backends.SecretService.Keyring",
    "keyring.backends.libsecret.Keyring",
    "keyring.backends.kwallet.DBusKeyring",
    "keyring.backends.kwallet.DBusKeyringKWallet4",
    "keyring.backends.macOS.Keyring",
    "keyring.backends.Windows.WinVaultKeyring",
}

BACKEND_VAULT = "fleetledger.vault.Backend"

SWITCH_HINT = ("Or keep the secret in a 0600 file instead: ship-fleet overlay setup --force "
               "(choose the file store).")


def _default_get_keyring():
    import keyring

    return keyring.get_keyring()


# Replaced in tests with a fake backend; nothing here ever touches a real keyring
# unless FleetLedger is run for real.
get_keyring: Callable[[], Any] = _default_get_keyring


def _name(backend: Any) -> str:
    return f"{type(backend).__module__}.{type(backend).__name__}"


def secure_backend() -> tuple[Any | None, str]:
    """The keyring backend to use, or None and why not: FleetLedger's vault when it is
    on here (locked or not), else the OS keyring's."""
    from fleetledger import vault

    if vault.is_on():
        return vault.backend(), vault.BACKEND_NAME
    return os_backend()


def vault_on() -> bool:
    from fleetledger import vault

    return vault.is_on()


def keyring_label() -> str:
    """What "the keyring" is here, for messages."""
    return "FleetLedger's vault" if vault_on() else "your OS keyring"


def refuse_file(what: str) -> None:
    """While the vault is on, no secret goes to a 0600 file (the vault would then
    protect less than it says)."""
    if vault_on():
        raise ConfigError(f"FleetLedger's vault is on here, so {what} goes in it, never in "
                          "a file.", hint="Choose the keyring store (with the vault on, "
                          "that is the vault), or turn the vault off first: ship-fleet "
                          "vault off.")


def os_backend() -> tuple[Any | None, str]:
    """The OS keyring's backend, or None and why not (whether or not the vault is on)."""
    try:
        backend = get_keyring()
    except Exception as exc:  # no keyring library, no D-Bus, ...
        return None, f"no keyring is available ({type(exc).__name__})"
    candidates = list(getattr(backend, "backends", None) or [backend])  # a chainer
    for candidate in candidates:
        if _name(candidate) in SECURE_BACKENDS:
            return candidate, _name(candidate)
    names = ", ".join(_name(c) for c in candidates)
    return None, f"no secure keyring backend (found: {names})"


def keyring_usable() -> tuple[bool, str]:
    """Can the keyring store a secret right now (secure backend, unlocked)?"""
    backend, why = secure_backend()
    if backend is None:
        return False, why
    try:
        backend.get_password(SERVICE, "fleetledger-probe")  # an unlock check, nothing read
    except Exception as exc:
        if why == BACKEND_VAULT:
            return False, "FleetLedger's vault is locked (ship-fleet unlock)"
        return False, f"the keyring is not usable: {_describe(exc)}"
    return True, why


def _describe(exc: Exception) -> str:
    """The kind of keyring error, never its text: a backend's message could echo
    what it was handed (the secret)."""
    return type(exc).__name__


def existing_in_keyring(item: str) -> str | None:
    """A secret already in a secure, usable keyring under item; None otherwise
    (no keyring, locked, insecure, or no such item)."""
    backend, _ = secure_backend()
    if backend is None:
        return None
    try:
        value = backend.get_password(SERVICE, item)
    except Exception:
        return None
    return value.strip() if value and value.strip() else None


def stored_names(prefix: str) -> list[str] | None:
    """What is already in the keyring under FleetLedger's service with this prefix (the
    part after it: token ids, client ids), so setup can offer it instead of asking again.

    Read from the items' attributes only: no secret is read. None where that cannot be
    told (no usable keyring, or a backend with no way to list, such as macOS Keychain).
    """
    backend, _ = secure_backend()
    if backend is None:
        return None
    try:
        names = _item_names(backend)
    except Exception:  # locked, D-Bus gone, ...: unknown, not "none"
        return None
    if names is None:
        return None
    return sorted({n[len(prefix):] for n in names
                   if isinstance(n, str) and n.startswith(prefix) and len(n) > len(prefix)})


def _item_names(backend: Any) -> list[str] | None:
    kind = _name(backend)
    if kind == BACKEND_VAULT:
        return backend.item_names(SERVICE)
    if kind == "keyring.backends.SecretService.Keyring":
        username = backend.schemes[backend.scheme]["username"]
        collection = backend.get_preferred_collection()
        try:
            return [item.get_attributes().get(username)
                    for item in collection.search_items(backend._query(SERVICE))]
        finally:
            close = getattr(getattr(collection, "connection", None), "close", None)
            if close is not None:
                close()
    if kind == "keyring.backends.libsecret.Keyring":
        from gi.repository import Secret

        items = Secret.password_search_sync(backend.schema, backend._query(SERVICE),
                                            Secret.SearchFlags.ALL, None)
        return [item.get_attributes().get("username") for item in items]
    return None


def store_in_keyring(item: str, secret: str, switch_hint: str = SWITCH_HINT) -> str:
    backend, why = secure_backend()
    if backend is None:
        raise ConfigError(f"Cannot use the keyring: {why}.", hint=switch_hint)
    try:
        backend.set_password(SERVICE, item, secret)
    except FleetError:  # the vault's own (locked, full ...): it says what to do
        raise
    except Exception as exc:
        raise ConfigError(f"Could not store the secret in the keyring: {_describe(exc)}.",
                          hint=switch_hint) from None
    return why


def delete_from_keyring(item: str) -> None:
    backend, why = secure_backend()
    if backend is None:
        raise ConfigError(f"Cannot use the keyring: {why}.")
    try:
        backend.delete_password(SERVICE, item)
    except FleetError:
        raise
    except Exception as exc:
        raise ConfigError(f"Could not remove {item} from the keyring: {_describe(exc)}.") \
            from None


def read_file(path_value: str, what: str = "the OAuth client secret") -> str:
    path = paths.expand(path_value)
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise ConfigError(f"{path} does not exist.") from exc
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError(f"{path} is readable by others; chmod 600 it.")
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:  # a folder, unreadable, not text
        raise ConfigError(f"Cannot read {path} ({type(exc).__name__}).") from None
    if not secret or "\n" in secret:
        raise ConfigError(f"{path} must hold only {what}.")
    return secret


def where(api: Mapping[str, Any]) -> str:
    if api.get("secret_store", "file") == "keyring":
        if vault_on():
            return f"FleetLedger's vault (item {api.get('keyring_item')})"
        return f"the keyring (service {SERVICE}, item {api.get('keyring_item')})"
    return str(api.get("oauth_secret_file"))


def read_keyring(item: str, what: str, switch_hint: str) -> str:
    """A secret from the keyring, with errors that say how to unlock it or switch
    to the file store. Never falls back to anything else."""
    backend, why = secure_backend()
    if backend is None:
        raise ConfigError(f"The {what} is in the keyring, but {why}.",
                          hint="Log in to a desktop session with GNOME Keyring or KWallet "
                          "(Keychain on macOS). " + switch_hint)
    try:
        secret = backend.get_password(SERVICE, item)
    except FleetError as exc:  # the vault: locked, or its file unreadable (says which)
        if why == BACKEND_VAULT:
            from fleetledger import vault

            if isinstance(exc, vault.Locked):
                raise vault.Locked(f"the {what}") from None
        raise
    except Exception as exc:
        raise ConfigError(
            f"The keyring holding the {what} is locked or unavailable: {_describe(exc)}.",
            hint="Unlock it (log in to your desktop session, or open your keyring app and "
            "unlock the login keyring), then try again. " + switch_hint) from None
    if not secret or not secret.strip():
        where = ("FleetLedger's vault" if why == BACKEND_VAULT
                 else f"the keyring (service {SERVICE}")
        raise ConfigError(f"The {what} is not in {where}"
                          + (f" (item {item})." if why == BACKEND_VAULT else f", item {item})."),
                          hint=switch_hint)
    return secret.strip()


def load(api: Mapping[str, Any]) -> str:
    """The OAuth client secret, from the store ships.toml names. Never falls back."""
    if api.get("secret_store", "file") != "keyring":
        return read_file(api["oauth_secret_file"])
    return read_keyring(api["keyring_item"], "OAuth secret", SWITCH_HINT)
