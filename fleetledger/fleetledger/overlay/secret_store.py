"""Where the Tailscale OAuth client secret is kept: the OS keyring, or a 0600 file.

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
"""

from __future__ import annotations

import stat
from collections.abc import Callable, Mapping
from typing import Any

from fleetledger import paths
from fleetledger.errors import ConfigError

SERVICE = "fleetledger"

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
    """The keyring backend to use, or None and why not."""
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
        return False, f"the keyring is not usable: {_describe(exc)}"
    return True, why


def _describe(exc: Exception) -> str:
    """The kind of keyring error, never its text: a backend's message could echo
    what it was handed (the secret)."""
    return type(exc).__name__


def store_in_keyring(item: str, secret: str) -> str:
    backend, why = secure_backend()
    if backend is None:
        raise ConfigError(f"Cannot use the keyring: {why}.", hint=SWITCH_HINT)
    try:
        backend.set_password(SERVICE, item, secret)
    except Exception as exc:
        raise ConfigError(f"Could not store the secret in the keyring: {_describe(exc)}.",
                          hint=SWITCH_HINT) from None
    return why


def read_file(path_value: str) -> str:
    path = paths.expand(path_value)
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise ConfigError(f"{path} does not exist.") from exc
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError(f"{path} is readable by others; chmod 600 it.")
    secret = path.read_text(encoding="utf-8").strip()
    if not secret or "\n" in secret:
        raise ConfigError(f"{path} must hold only the OAuth client secret.")
    return secret


def where(api: Mapping[str, Any]) -> str:
    if api.get("secret_store", "file") == "keyring":
        return f"the keyring (service {SERVICE}, item {api.get('keyring_item')})"
    return str(api.get("oauth_secret_file"))


def load(api: Mapping[str, Any]) -> str:
    """The OAuth client secret, from the store ships.toml names. Never falls back."""
    if api.get("secret_store", "file") != "keyring":
        return read_file(api["oauth_secret_file"])
    backend, why = secure_backend()
    if backend is None:
        raise ConfigError(f"The OAuth secret is in the keyring, but {why}.",
                          hint="Log in to a desktop session with GNOME Keyring or KWallet "
                          "(Keychain on macOS). " + SWITCH_HINT)
    try:
        secret = backend.get_password(SERVICE, api["keyring_item"])
    except Exception as exc:
        raise ConfigError(
            f"The keyring holding the OAuth secret is locked or unavailable: {_describe(exc)}.",
            hint="Unlock it (log in to your desktop session, or open your keyring app and "
            "unlock the login keyring), then try again. " + SWITCH_HINT) from None
    if not secret:
        raise ConfigError(f"The OAuth secret is not in {where(api)}.",
                          hint="Run ship-fleet overlay setup --force to store it again.")
    return secret.strip()
