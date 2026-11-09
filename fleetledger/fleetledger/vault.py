"""FleetLedger's vault: every secret it keeps, in one encrypted file, unlocked once
after each boot with one passphrase (docs/vault.md).

Off unless you turn it on (`ship-fleet vault init`). On, it is this computer's
keyring for FleetLedger: everything that would go to the OS keyring (each managed
key's passphrase, the Proxmox token, the Tailscale OAuth secret) goes here instead,
under the same item names, so ships.toml still says `keyring` and every reader and
writer is unchanged (overlay/secret_store.py hands out this backend). It never falls
back to the OS keyring.

What is encrypted with what:

- the **data key**: 32 random bytes (a Fernet key) made once, at `vault init`;
- the **secrets**: one JSON document, Fernet-encrypted with the data key
  (AES-128-CBC with a random IV, then HMAC-SHA256 over it all: authenticated
  encryption from the `cryptography` package, nothing hand-made);
- the **slots**: the data key, Fernet-encrypted with a key derived from your
  passphrase by scrypt (N=2^17, r=8, p=1: 128 MiB and about a second per try,
  a fresh random salt per slot), and once more with a key derived the same way
  from a **recovery code** shown once at `vault init`.

vault.json (0600, beside ships.toml) holds the slots and the encrypted secrets:
nothing in it, or anywhere on disk, opens without the passphrase or the recovery
code. Unlocking derives the slot key, opens the data key and writes it to
`<runtime>/vault/key` (0600 in a 0700 folder), which must be on a RAM file system
(tmpfs): it is gone at power-off, so every boot starts locked, and a restart of
FleetLedger (an update) keeps it. Every ship-fleet process on this computer, the
service and commands over ssh alike, reads the data key from there; locking
deletes it.

Wrong passphrases are counted (in the same RAM folder): after FREE_TRIES, each try
waits longer (BACKOFF), up to MAX_WAIT, on top of scrypt's own second.
"""

from __future__ import annotations

import base64
import fcntl
import json
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fleetledger import paths
from fleetledger.errors import ConfigError, FleetError

SCHEMA = "fleetledger.vault/1"
FILE = "vault.json"
RUNTIME = "vault"
KEY_FILE = "key"
TRIES_FILE = "tries.json"
BACKEND_NAME = "fleetledger.vault.Backend"
# scrypt's cost; the file says what it was made with, within these bounds.
SCRYPT_N = 2 ** 17
SCRYPT_R = 8
SCRYPT_P = 1
MAX_N, MAX_R, MAX_P = 2 ** 20, 32, 16
MIN_PASSPHRASE = 12
MAX_PASSPHRASE = 1024
MAX_ITEMS = 2000
MAX_SECRET = 64 * 1024
MAX_FILE = 16 * 1024 * 1024
FREE_TRIES = 3
BACKOFF = 10.0  # seconds, doubling with each wrong try after FREE_TRIES
MAX_WAIT = 15 * 60.0
RAM_FILESYSTEMS = ("tmpfs", "ramfs")
SLOTS = ("passphrase", "recovery")
RUNBOOK = "docs/vault.md"
UNLOCK_HINT = ("Unlock it once (it locks at every boot): ship-fleet unlock, over ssh is fine, "
               "or Unlock in the web UI.")


class Locked(ConfigError):
    """The vault is on and locked: no secret can be read or written until it is
    unlocked."""

    def __init__(self, what: str = "") -> None:
        super().__init__("FleetLedger's vault is locked" + (f", so {what} cannot be read"
                                                           if what else "") + ".",
                         hint=UNLOCK_HINT)


class WrongSecret(FleetError):
    """The passphrase (or recovery code) does not open the vault."""


# --- where ------------------------------------------------------------------------------


def vault_path() -> Path:
    return paths.config_dir() / FILE


def runtime_folder() -> Path:
    return paths.runtime_dir() / RUNTIME


def key_path() -> Path:
    return runtime_folder() / KEY_FILE


def is_on() -> bool:
    """Is the vault this computer's keyring for FleetLedger? (vault.json is there.)"""
    path = vault_path()
    return path.exists() or path.is_symlink()


def mount_of(path: Path, mountinfo: str | None = None) -> tuple[str, str] | None:
    """(mount point, file system type) of the mount holding path, from
    /proc/self/mountinfo; None where that cannot be told (no /proc, say)."""
    if mountinfo is None:
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError:
            return None
    target = str(path)
    best: tuple[str, str] | None = None
    for line in mountinfo.splitlines():
        left, sep, right = line.partition(" - ")
        fields = left.split()
        if not sep or len(fields) < 5 or not right.split():
            continue
        point = fields[4].replace("\\040", " ").replace("\\011", "\t").replace(
            "\\012", "\n").replace("\\134", "\\")
        inside = target == point or target.startswith(point.rstrip("/") + "/")
        if inside and (best is None or len(point) >= len(best[0])):
            best = (point, right.split()[0])
    return best


def in_ram(path: Path) -> bool:
    """Is path (or the nearest folder of it that exists) on a RAM file system?"""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    found = mount_of(probe.resolve())
    return found is not None and found[1] in RAM_FILESYSTEMS


def swap_on(swaps: str | None = None) -> bool:
    """Is any swap in use here? (An unlocked data key in RAM could be written to it.)"""
    if swaps is None:
        try:
            swaps = Path("/proc/swaps").read_text(encoding="utf-8")
        except OSError:
            return False
    return len([line for line in swaps.splitlines() if line.strip()]) > 1


# --- the cryptography ---------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: Any, size: int | None = None) -> bytes:
    if not isinstance(text, str):
        raise ValueError("not text")
    data = base64.b64decode(text.encode("ascii"), validate=True)
    if size is not None and len(data) != size:
        raise ValueError("wrong size")
    return data


def derive(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    """A Fernet key (as Fernet takes it) from a passphrase or recovery code: scrypt."""
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

    raw = Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(secret.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def seal_slot(kind: str, data_key: bytes, secret: str) -> dict[str, Any]:
    """The data key, encrypted with a key scrypt derives from secret (a fresh salt)."""
    from cryptography.fernet import Fernet

    salt = secrets.token_bytes(16)
    token = Fernet(derive(secret, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)).encrypt(data_key)
    return {"kind": kind, "kdf": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
            "salt": _b64(salt), "key": token.decode("ascii")}


def open_slot(slot: dict[str, Any], secret: str) -> bytes | None:
    """The data key, if secret opens this slot; else None."""
    from cryptography.fernet import Fernet, InvalidToken

    try:
        key = derive(secret, _unb64(slot["salt"], 16), slot["n"], slot["r"], slot["p"])
        data_key = Fernet(key).decrypt(slot["key"].encode("ascii"))
    except (InvalidToken, ValueError, UnicodeError):
        return None
    return data_key if _is_data_key(data_key) else None


def _is_data_key(data_key: bytes) -> bool:
    try:
        return len(base64.urlsafe_b64decode(data_key)) == 32
    except (ValueError, TypeError):
        return False


def encrypt_items(data_key: bytes, items: dict[str, dict[str, str]]) -> str:
    from cryptography.fernet import Fernet

    text = json.dumps({"items": items}, separators=(",", ":"), sort_keys=True)
    return Fernet(data_key).encrypt(text.encode("utf-8")).decode("ascii")


def decrypt_items(data_key: bytes, token: str) -> dict[str, dict[str, str]]:
    """The secrets; ConfigError if the data key does not open them (another vault's
    key, or a file changed by hand: never guessed at)."""
    from cryptography.fernet import Fernet, InvalidToken

    try:
        doc = json.loads(Fernet(data_key).decrypt(token.encode("ascii")))
    except (InvalidToken, ValueError, UnicodeError):
        raise ConfigError(f"The vault's data key does not open {paths.tilde(vault_path())}.",
                          hint="Lock and unlock it again (ship-fleet vault lock, then "
                               "ship-fleet unlock).") from None
    items = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(items, dict) or not all(
            isinstance(service, str) and isinstance(entries, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in entries.items())
            for service, entries in items.items()):
        raise ConfigError(f"{paths.tilde(vault_path())} holds something FleetLedger did not "
                          "write.")
    return items


# --- the file ---------------------------------------------------------------------------


def _broken(why: str) -> ConfigError:
    return ConfigError(f"{paths.tilde(vault_path())} is not a vault FleetLedger wrote "
                       f"({why}); nothing in it is used.",
                       hint=f"Restore it from a backup ({RUNBOOK}, Recovery).")


def load_file() -> dict[str, Any]:
    """vault.json, checked: exactly what this module writes, scrypt's cost within
    bounds (a file changed to ask for a huge one is refused, not run)."""
    path = vault_path()
    try:
        if path.is_symlink():
            raise _broken("a symbolic link")
        if path.stat().st_size > MAX_FILE:
            raise _broken("too large")
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("FleetLedger's vault is off here (no vault.json).",
                          hint="ship-fleet vault init turns it on.") from None
    except (OSError, ValueError, UnicodeError) as exc:
        raise _broken(type(exc).__name__) from None
    if not isinstance(doc, dict) or set(doc) != {"schema", "slots", "data"} \
            or doc["schema"] != SCHEMA or not isinstance(doc["data"], str) \
            or not isinstance(doc["slots"], list):
        raise _broken("not its layout")
    kinds = []
    for slot in doc["slots"]:
        if not isinstance(slot, dict) or set(slot) != {"kind", "kdf", "n", "r", "p", "salt",
                                                       "key"}:
            raise _broken("a slot is not its layout")
        n, r, p = slot["n"], slot["r"], slot["p"]
        if (slot["kind"] not in SLOTS or slot["kdf"] != "scrypt"
                or not all(isinstance(v, int) and not isinstance(v, bool) for v in (n, r, p))
                or not (2 ** 10 <= n <= MAX_N and n & (n - 1) == 0)
                or not 1 <= r <= MAX_R or not 1 <= p <= MAX_P
                or not isinstance(slot["key"], str)):
            raise _broken("a slot's cost or kind is out of bounds")
        try:
            _unb64(slot["salt"], 16)
        except ValueError:
            raise _broken("a slot's salt") from None
        kinds.append(slot["kind"])
    if "passphrase" not in kinds or len(kinds) != len(set(kinds)):
        raise _broken("its slots")
    return doc


def write_file(doc: dict[str, Any]) -> None:
    folder = paths.config_dir()
    paths.private_dir(folder)
    paths.write_private(vault_path(), (json.dumps(doc, indent=1) + "\n").encode("utf-8"))


@contextmanager
def _locked_file() -> Iterator[None]:
    """One writer at a time (a lock file in the RAM folder: every process shares it)."""
    folder = paths.private_dir(runtime_folder())
    with open(folder / ".lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# --- making one ---------------------------------------------------------------------------


def check_passphrase(passphrase: str) -> None:
    if len(passphrase) < MIN_PASSPHRASE:
        raise FleetError(f"A vault passphrase has at least {MIN_PASSPHRASE} characters (a few "
                         "words is easiest): it is all that stands between a copy of this "
                         "computer's disk and your fleet.")
    if len(passphrase) > MAX_PASSPHRASE:
        raise FleetError(f"A vault passphrase has at most {MAX_PASSPHRASE} characters.")


def new_recovery_code() -> str:
    """160 random bits as 8 groups of 4 letters and digits (base32): ABCD-EFGH-..."""
    text = base64.b32encode(secrets.token_bytes(20)).decode("ascii")
    return "-".join(text[i:i + 4] for i in range(0, 32, 4))


def clean_recovery_code(text: str) -> str:
    raw = "".join(ch for ch in text.upper() if ch.isalnum())
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def create(passphrase: str, items: dict[str, dict[str, str]] | None = None) -> str:
    """A new vault holding items (none: empty), unlocked; returns its recovery code.
    Refuses where one is already, and where the unlocked key could not be kept in RAM."""
    from cryptography.fernet import Fernet

    check_passphrase(passphrase)
    if is_on():
        raise FleetError(f"A vault is on here already ({paths.tilde(vault_path())}).",
                         hint="ship-fleet vault status")
    check_ram()
    data_key = Fernet.generate_key()
    code = new_recovery_code()
    doc = {"schema": SCHEMA,
           "slots": [seal_slot("passphrase", data_key, passphrase),
                     seal_slot("recovery", data_key, code)],
           "data": encrypt_items(data_key, items or {})}
    write_file(doc)
    _keep(data_key)
    return code


# --- unlocking ----------------------------------------------------------------------------


def check_ram() -> None:
    folder = runtime_folder()
    if not in_ram(folder):
        raise ConfigError(
            f"FleetLedger's runtime folder ({folder}) is not in RAM (tmpfs), so the unlocked "
            "vault's key would be written to disk.",
            hint="Log in so XDG_RUNTIME_DIR is set (it is /run/user/<id>, in RAM), or point "
                 "FLEETLEDGER_RUNTIME_DIR at a folder on a tmpfs.")


def _keep(data_key: bytes) -> None:
    folder = paths.private_dir(runtime_folder())
    paths.write_private(folder / KEY_FILE, data_key + b"\n")


def state() -> str:
    """off | locked | unlocked."""
    if not is_on():
        return "off"
    return "unlocked" if key_path().is_file() else "locked"


def data_key() -> bytes:
    """The unlocked data key; Locked when there is none."""
    try:
        key = key_path().read_bytes().strip()
    except FileNotFoundError:
        raise Locked() from None
    except OSError as exc:
        raise ConfigError(f"Cannot read the unlocked vault's key: {exc.strerror}.") from None
    if not _is_data_key(key):
        raise Locked()
    return key


@dataclass(frozen=True)
class Tries:
    count: int
    at: float  # wall clock of the last wrong try

    def wait(self, now: float) -> float:
        """Seconds until the next try may be made."""
        if self.count < FREE_TRIES:
            return 0.0
        delay = min(BACKOFF * 2 ** (self.count - FREE_TRIES), MAX_WAIT)
        return max(0.0, self.at + delay - now)


def tries() -> Tries:
    try:
        doc = json.loads((runtime_folder() / TRIES_FILE).read_text(encoding="utf-8"))
        count, at = int(doc["count"]), float(doc["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return Tries(0, 0.0)
    return Tries(max(0, count), at)


def _note_try(ok: bool, now: float) -> None:
    folder = paths.private_dir(runtime_folder())
    if ok:
        (folder / TRIES_FILE).unlink(missing_ok=True)
        return
    last = tries()
    paths.write_private(folder / TRIES_FILE,
                        json.dumps({"count": last.count + 1, "at": now}).encode("utf-8"))


class Waiting(FleetError):
    """Too many wrong tries lately: the next must wait."""


def unlock(secret: str, *, recovery: bool = False, wait: bool = True,
           wall: Callable[[], float] = time.time) -> str:
    """Unlock with the passphrase (or, recovery, the recovery code): keep the data key
    in RAM until the next boot or lock. Returns "unlocked" or "already".

    One try at a time, across threads and processes (the vault's lock), so a burst of
    tries can neither get past the wait nor run scrypt (128 MiB each) side by side.
    wait=False: not held to the wait after wrong tries (a terminal of FleetLedger's own
    user, who could delete the count anyway); the tries are still counted."""
    check_ram()
    with _locked_file():
        if state() == "unlocked":
            return "already"
        return _unlock_locked(secret, recovery=recovery, wait=wait, wall=wall)


def _unlock_locked(secret: str, *, recovery: bool, wait: bool,
                   wall: Callable[[], float]) -> str:
    waiting = tries().wait(wall()) if wait else 0
    if waiting > 0:
        raise Waiting(f"Too many wrong tries: wait {int(waiting) + 1} seconds before the "
                      "next.", hint="The audit log says who tried (vault-unlock, wrong, with "
                      "the device's login): ship-fleet ui revoke signs a device out. Over "
                      "ssh, ship-fleet unlock is not held to this wait.")
    doc = load_file()
    kind = "recovery" if recovery else "passphrase"
    slot = next((s for s in doc["slots"] if s["kind"] == kind), None)
    if slot is None:
        raise FleetError("This vault has no recovery code.")
    key = open_slot(slot, clean_recovery_code(secret) if recovery else secret)
    if key is None:
        _note_try(False, wall())
        raise WrongSecret("That " + ("recovery code" if recovery else "passphrase")
                          + " does not open the vault.")
    decrypt_items(key, doc["data"])  # the key opens the secrets too, before keeping it
    _keep(key)
    _note_try(True, wall())
    return "unlocked"


def lock() -> bool:
    """Forget the unlocked data key here (every FleetLedger process on this computer
    stops reading secrets at once). True if it was unlocked."""
    try:
        key_path().unlink()
    except FileNotFoundError:
        return False
    return True


# --- rotation -----------------------------------------------------------------------------


def _reseal(secret: str, *, recovery: bool, kind: str, new: str) -> None:
    """Open the data key with secret (the passphrase, or recovery code), then seal slot
    kind again with new. The data key and the secrets stay as they are."""
    with _locked_file():
        doc = load_file()
        slot_kind = "recovery" if recovery else "passphrase"
        slot = next((s for s in doc["slots"] if s["kind"] == slot_kind), None)
        key = open_slot(slot, clean_recovery_code(secret) if recovery else secret) \
            if slot else None
        if key is None:
            raise WrongSecret("That " + ("recovery code" if recovery else "passphrase")
                              + " does not open the vault; nothing was changed.")
        doc["slots"] = [s for s in doc["slots"] if s["kind"] != kind] + \
            [seal_slot(kind, key, new)]
        doc["slots"].sort(key=lambda s: SLOTS.index(s["kind"]))
        write_file(doc)


def change_passphrase(secret: str, new: str, *, recovery: bool = False) -> None:
    check_passphrase(new)
    _reseal(secret, recovery=recovery, kind="passphrase", new=new)


def new_recovery(passphrase: str) -> str:
    code = new_recovery_code()
    _reseal(passphrase, recovery=False, kind="recovery", new=code)
    return code


# --- the keyring backend ------------------------------------------------------------------


class Backend:
    """The vault as a keyring backend (get_password, set_password, delete_password),
    which overlay/secret_store.py hands out when the vault is on. Every call reads the
    file and the unlocked key afresh, so a lock elsewhere takes effect at once."""

    def _items(self) -> dict[str, dict[str, str]]:
        key = data_key()
        return decrypt_items(key, load_file()["data"])

    def get_password(self, service: str, item: str) -> str | None:
        return self._items().get(service, {}).get(item)

    def item_names(self, service: str) -> list[str]:
        return sorted(self._items().get(service, {}))

    def set_password(self, service: str, item: str, secret: str) -> None:
        if not isinstance(secret, str) or len(secret.encode("utf-8")) > MAX_SECRET:
            raise FleetError("That secret is too large for the vault.")
        self._change(lambda items: items.setdefault(service, {}).__setitem__(item, secret))

    def delete_password(self, service: str, item: str) -> None:
        def drop(items: dict[str, dict[str, str]]) -> None:
            if item not in items.get(service, {}):
                raise KeyError(item)
            del items[service][item]
            if not items[service]:
                del items[service]

        self._change(drop)

    def _change(self, change: Callable[[dict[str, dict[str, str]]], None]) -> None:
        with _locked_file():
            key = data_key()
            doc = load_file()
            items = decrypt_items(key, doc["data"])
            change(items)
            if sum(len(v) for v in items.values()) > MAX_ITEMS:
                raise FleetError("The vault is full.")
            doc["data"] = encrypt_items(key, items)
            write_file(doc)


def backend() -> Backend:
    return Backend()
