"""Keys FleetLedger makes and protects itself ("managed keys").

A new fleet needs SSH keys: the fleet admin key (FleetLedger logs in to Ships
with it), the host and user CA keys, and perhaps a key of your own for attaching
to Ships. Asking a newcomer to name, place and protect each one is the hard part
of a first run, so by default FleetLedger does it:

- each key is an ordinary OpenSSH ed25519 key, encrypted with a random 256-bit
  passphrase;
- the passphrase is kept in the OS keyring (service "fleetledger", item
  key-passphrase:<key file name>), or, where there is no usable keyring, in a
  0600 file under ~/.config/fleetledger/key-passphrases/ (the same choice as the
  overlay and Proxmox secrets, and never a quiet fallback from one to the other);
- FleetLedger types the passphrase itself, into ssh-keygen when it makes the key
  and into ssh-add when the key is needed, through a terminal it controls: it is
  never on a command line, in the environment, in a log or on screen;
- when a command needs the keys, FleetLedger loads them into ssh-agent (yours, or
  one of its own if none is running), so everything else stays plain OpenSSH.

What this protects: the key files alone (a backup, a copied ~/.ssh) are useless
without the keyring. What it does not: any program running as you while the
keyring is unlocked can ask for the passphrase, as it can use ssh-agent. The file
store is weaker still (two files only you can read), which is why the keyring is
offered first. Keys you bring yourself keep the passphrase you chose, unless you
ask FleetLedger to take one over (adopt), which re-encrypts it after a check.

ships.toml records [fleet.managed_keys] store and keys (the private key paths).
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path

from fleetledger import paths
from fleetledger.errors import ConfigError, FleetError
from fleetledger.overlay import secret_store

ITEM_PREFIX = "key-passphrase:"
PASSPHRASE_DIR = "key-passphrases"
AGENT_SOCKET = "agent.sock"
TIMEOUT = 30
SWITCH_HINT = ("Unlock your keyring (log in to your desktop session), or load the key yourself: "
               "ssh-add <key>.")
KEY_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
# What ssh-keygen and ssh-add print when a passphrase does not open a key. Only
# these: anything they echo back (a key's comment, say "me@badger") must not match.
FAILED = (r"(?i)(failed to load key|bad passphrase|incorrect passphrase|"
          r"load key \S+: invalid format)")
# FleetLedger's own agent (only when none answers) drops keys after this long;
# FleetLedger loads them again when it next needs them.
OWN_AGENT_LIFETIME = "8h"


def stored_name(key: Path) -> str:
    """The key's file name and a short hash of its full path: two keys with the same
    file name in different folders never share a passphrase."""
    import hashlib

    full = str(paths.expand(key).resolve())
    return f"{key.name}-{hashlib.sha256(full.encode('utf-8')).hexdigest()[:8]}"


def item(key: Path) -> str:
    return ITEM_PREFIX + stored_name(key)


def passphrase_file(key: Path) -> Path:
    return paths.config_dir() / PASSPHRASE_DIR / stored_name(key)


def _check_name(key: Path) -> None:
    if not KEY_NAME_RE.match(key.name):
        raise FleetError(f"{key.name}: a managed key's file name uses letters, digits, _ . "
                         "and - only.")


# --- the passphrase store -------------------------------------------------------------------


def default_store() -> str:
    """keyring when a secure, unlocked one is here; otherwise file."""
    usable, _why = secret_store.keyring_usable()
    return "keyring" if usable else "file"


def store_passphrase(key: Path, passphrase: str, store: str) -> None:
    if store == "keyring":
        secret_store.store_in_keyring(item(key), passphrase, SWITCH_HINT)
        return
    target = passphrase_file(key)
    paths.private_dir(target.parent)
    paths.write_private(target, (passphrase + "\n").encode("utf-8"))


def read_passphrase(key: Path, store: str) -> str:
    if store == "keyring":
        return secret_store.read_keyring(item(key), f"passphrase for {key.name}", SWITCH_HINT)
    return secret_store.read_file(str(passphrase_file(key)), f"the passphrase for {key.name}")


def forget_passphrase(key: Path, store: str) -> None:
    """Drop a stored passphrase (after a failed make, so nothing dangles)."""
    try:
        if store == "keyring":
            secret_store.delete_from_keyring(item(key))
        else:
            passphrase_file(key).unlink(missing_ok=True)
    except (FleetError, OSError, KeyError):
        pass


def new_passphrase() -> str:
    return secrets.token_urlsafe(32)  # 256 bits


# --- ssh-keygen and ssh-add, driven through a terminal ----------------------------------------


def _spawn(argv: list[str], env: Mapping[str, str] | None = None):
    import pexpect

    # No logfile: nothing the child prints or we type is recorded. The child's
    # terminal does not echo passphrase prompts.
    return pexpect.spawn(argv[0], argv[1:], env=dict(env or os.environ), timeout=TIMEOUT,
                         encoding="utf-8", codec_errors="replace", echo=False)


def _finish(child, what: str) -> None:
    import pexpect

    try:
        child.expect(pexpect.EOF)
    except pexpect.TIMEOUT:
        child.close(force=True)
        raise FleetError(f"{what} did not finish.") from None
    child.close()
    if child.exitstatus != 0:
        raise FleetError(f"{what} failed.")


def _keygen_new(path: Path, comment: str, passphrase: str) -> None:
    import pexpect

    child = _spawn(["ssh-keygen", "-q", "-t", "ed25519", "-C", comment, "-f", str(path)])
    for _ in range(2):  # "Enter passphrase", then "Enter same passphrase again"
        if child.expect([r"(?i)passphrase.*:", pexpect.EOF]) != 0:
            raise FleetError("ssh-keygen did not ask for a passphrase.")
        child.sendline(passphrase)
    _finish(child, "ssh-keygen")


def _keygen_rekey(path: Path, old: str, new: str) -> None:
    """ssh-keygen -p: re-encrypt path from old (maybe empty) to new."""
    import pexpect

    child = _spawn(["ssh-keygen", "-p", "-f", str(path)])
    sent_new = 0
    while True:
        index = child.expect([r"(?i)old passphrase.*:", r"(?i)new passphrase.*:",
                              r"(?i)same passphrase again.*:", FAILED,
                              pexpect.EOF])
        if index == 0:
            child.sendline(old)
        elif index in (1, 2):
            child.sendline(new)
            sent_new += 1
        elif index == 3:
            child.close(force=True)
            raise FleetError("That is not the key's passphrase; nothing was changed.")
        else:
            break
    child.close()
    if child.exitstatus != 0 or sent_new < 2:
        raise FleetError("ssh-keygen could not change the key's passphrase; nothing was "
                         "changed.")


def make(path: Path, comment: str, store: str) -> None:
    """A new managed ed25519 key at path (and path.pub). Never overwrites."""
    _check_name(path)
    if path.exists() or path.with_name(path.name + ".pub").exists():
        raise FleetError(f"{path} already exists; FleetLedger never overwrites a key.")
    paths.private_dir(path.parent)
    passphrase = new_passphrase()
    tmp = path.with_name(f".{path.name}.new-{os.getpid()}")
    for leftover in (tmp, tmp.with_name(tmp.name + ".pub")):
        leftover.unlink(missing_ok=True)
    try:
        _keygen_new(tmp, comment, passphrase)
        store_passphrase(path, passphrase, store)
    except BaseException:
        for leftover in (tmp, tmp.with_name(tmp.name + ".pub")):
            leftover.unlink(missing_ok=True)
        raise
    os.chmod(tmp, 0o600)
    tmp.with_name(tmp.name + ".pub").rename(path.with_name(path.name + ".pub"))
    tmp.rename(path)


def adopt(path: Path, current: str, store: str) -> None:
    """Put an existing key under FleetLedger's protection: re-encrypt it with a new
    random passphrase, kept in store. current is its passphrase now ("" if none).
    The key is changed on a copy, checked, and only then moved over the original,
    so a failure leaves the original exactly as it was."""
    _check_name(path)
    if not path.is_file():
        raise FleetError(f"{path} is not a key file.")
    expected = public_line(path)  # its .pub must be there
    check_opens(path, current)  # the passphrase given opens it, before anything changes
    new = new_passphrase()
    copy = path.with_name(f".{path.name}.adopt-{os.getpid()}")
    shutil.copy2(path, copy)
    os.chmod(copy, 0o600)
    try:
        _keygen_rekey(copy, current, new)
        if public_of(copy, new) != expected:
            raise FleetError("The re-encrypted key does not match the original; nothing was "
                             "changed.")
        store_passphrase(path, new, store)
    except BaseException:
        copy.unlink(missing_ok=True)
        raise
    copy.rename(path)


def adopt_all(store, chosen: list[tuple[Path, str, str]], managed: str) -> list[str]:
    """Hand several keys over: (path, its passphrase now, the path as ships.toml writes
    it). Every passphrase is checked first, so a wrong one changes nothing; then each
    key is taken over and recorded at once, so an interruption never leaves a key
    re-encrypted but unrecorded. A failure part-way names the keys already handed
    over and where their passphrases now are."""
    for path, current, _ in chosen:
        public_line(path)
        try:
            check_opens(path, current)
        except FleetError as exc:
            raise FleetError(f"{exc.message} Nothing was changed.") from None
    done: list[str] = []
    for path, current, written in chosen:
        try:
            adopt(path, current, managed)
            with store.edit() as ed:
                record(ed, managed, [written])
        except FleetError as exc:
            if done:
                raise FleetError(f"{exc.message} Already handed over: {', '.join(done)} "
                                 f"(their passphrases are now in {describe(managed)}).") \
                    from None
            raise
        done.append(path.name)
    return done


def public_line(path: Path) -> str:
    """The key's public half (from path.pub), as "type blob"."""
    pub = path.with_name(path.name + ".pub")
    try:
        text = pub.read_text(encoding="utf-8").split()
    except (OSError, UnicodeDecodeError):
        raise FleetError(f"{path.name}: its public half ({pub.name}) is missing or unreadable; "
                         "put it next to the key.") from None
    return " ".join(text[:2])


def check_opens(path: Path, passphrase: str) -> None:
    """Does passphrase open this key, and does it match its .pub? (Nothing changes.)"""
    if public_of(path, passphrase) != public_line(path):
        raise FleetError(f"{path.name} does not match its .pub file.")


def public_of(path: Path, passphrase: str) -> str:
    """The public half derived from the private key (proves the passphrase opens it)."""
    import pexpect

    child = _spawn(["ssh-keygen", "-y", "-f", str(path)])
    index = child.expect([r"(?i)passphrase.*:", r"ssh-ed25519 \S+", pexpect.EOF])
    if index == 0:
        child.sendline(passphrase)
        index = child.expect([r"ssh-ed25519 \S+", FAILED, pexpect.EOF])
        if index != 0:
            child.close(force=True)
            raise FleetError(f"The passphrase does not open {path.name}.")
    elif index != 1:
        raise FleetError(f"ssh-keygen could not read {path.name}.")
    line = child.after.strip()
    _finish(child, "ssh-keygen -y")
    return line


def needs_passphrase(path: Path) -> bool:
    """Is the key encrypted? (ssh-keygen -y with an empty passphrase fails.)"""
    proc = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(path)],
                          capture_output=True, text=True, timeout=10, check=False)
    return proc.returncode != 0


# --- ssh-agent -------------------------------------------------------------------------------


def _agent_answers(env: Mapping[str, str]) -> bool:
    if not env.get("SSH_AUTH_SOCK"):
        return False
    try:
        proc = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, timeout=10,
                              env=dict(env), check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode in (0, 1)  # 1: no keys yet; 2: cannot reach it


# The agent of the shell FleetLedger was started from, before ensure_agent may point
# SSH_AUTH_SOCK at FleetLedger's own: plain ssh in your shells uses this one.
USERS_AGENT = os.environ.get("SSH_AUTH_SOCK")


def users_agent_answers() -> bool:
    """Is your own ssh-agent (the one your shells use) running and in use here?"""
    return bool(USERS_AGENT) and os.environ.get("SSH_AUTH_SOCK") == USERS_AGENT and \
        _agent_answers(os.environ)


def ensure_agent() -> str:
    """An ssh-agent that answers: yours (SSH_AUTH_SOCK), or else FleetLedger's own,
    started on a socket in its private runtime folder. Sets SSH_AUTH_SOCK for this
    process (so every ssh FleetLedger runs uses it) and returns the socket."""
    if _agent_answers(os.environ):
        return os.environ["SSH_AUTH_SOCK"]
    sock = _agent_socket()
    env = {**os.environ, "SSH_AUTH_SOCK": str(sock)}
    if not _agent_answers(env):
        sock.unlink(missing_ok=True)
        try:
            proc = subprocess.run(["ssh-agent", "-a", str(sock), "-t", OWN_AGENT_LIFETIME],
                                  capture_output=True,
                                  text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise FleetError("Could not start ssh-agent.",
                             hint="Start one yourself: eval \"$(ssh-agent)\"") from exc
        if proc.returncode != 0:
            why = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise FleetError("Could not start ssh-agent" + (f": {why[-1]}" if why else "."),
                             hint="Start one yourself: eval \"$(ssh-agent)\"")
        if not _agent_answers(env):
            raise FleetError("Started ssh-agent, but it does not answer.")
    os.environ["SSH_AUTH_SOCK"] = str(sock)
    return str(sock)


SOCKET_PATH_MAX = 100  # Unix socket paths are limited to about 108 bytes


def _agent_socket() -> Path:
    """FleetLedger's own agent socket: in its private runtime folder, or, when that
    path is too long for a Unix socket, in a short private folder of yours under
    /tmp (refused if anyone else owns it or can open it)."""
    sock = paths.runtime_dir() / AGENT_SOCKET
    if len(str(sock)) <= SOCKET_PATH_MAX:
        paths.private_dir(sock.parent)
        return sock
    folder = Path("/tmp") / f"fleetledger-{os.getuid()}"
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = folder.lstat()
    import stat as _stat

    if (not _stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
            or _stat.S_IMODE(st.st_mode) & 0o077):
        raise FleetError(f"{folder} is not a private folder of yours; not starting ssh-agent "
                         "there.", hint="Start one yourself: eval \"$(ssh-agent)\"")
    return folder / AGENT_SOCKET


def loaded_fingerprints() -> set[str]:
    try:
        proc = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, timeout=10,
                              check=False)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {line.split()[1] for line in proc.stdout.splitlines() if len(line.split()) > 1}


def fingerprint(path: Path) -> str | None:
    try:
        proc = subprocess.run(["ssh-keygen", "-lf", str(path.with_name(path.name + ".pub"))],
                              capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = proc.stdout.split()
    return parts[1] if proc.returncode == 0 and len(parts) > 1 else None


def add_to_agent(path: Path, passphrase: str) -> None:
    import pexpect

    child = _spawn(["ssh-add", str(path)])
    index = child.expect([r"(?i)passphrase.*:", pexpect.EOF])
    if index == 0:
        child.sendline(passphrase)
        if child.expect([r"(?i)identity added", FAILED,
                         pexpect.EOF]) == 1:
            child.close(force=True)
            raise FleetError(f"The stored passphrase does not open {path.name}.")
    _finish(child, "ssh-add")


# --- what ships.toml says ----------------------------------------------------------------------


def managed(inventory) -> tuple[str | None, list[Path]]:
    """(store, keys) from [fleet.managed_keys]; (None, []) without it."""
    table = (inventory.doc.get("fleet") or {}).get("managed_keys") or {}
    if not table:
        return None, []
    return str(table.get("store")), [paths.expand(k) for k in table.get("keys", [])]


# Keys this process put in an agent: (agent socket, fingerprint) -> when. An agent's
# own list cannot be trusted on its own: GNOME's agent (gcr-ssh-agent, and the older
# gnome-keyring one) lists every key in ~/.ssh that has a .pub, loaded or not, and
# when one is used it asks for the key's passphrase in a desktop prompt. For a
# managed key nobody knows that passphrase, so FleetLedger adds its keys itself
# rather than trusting the list, and only skips a key it added a short while ago
# that is still listed.
_ADDED: dict[tuple[str, str], float] = {}
ADDED_FRESH = 10 * 60  # seconds


def _agent_id(sock: str) -> str:
    try:
        return os.path.realpath(sock)
    except OSError:
        return sock


def desktop_agent_preloads() -> bool:
    """Is SSH_AUTH_SOCK GNOME's agent, which lists every key in ~/.ssh with a .pub
    whether or not it holds it (and prompts for the passphrase when one is used)?
    Told from the socket's place: <runtime dir>/keyring/ssh or <runtime dir>/gcr/ssh."""
    sock = os.environ.get("SSH_AUTH_SOCK")
    if not sock:
        return False
    real = Path(_agent_id(sock))
    return real.name == "ssh" and real.parent.name in ("keyring", "gcr")


def ensure_loaded(inventory, wanted: Iterable[Path] | None = None) -> list[str]:
    """Load the managed keys (or those of wanted that are managed) into ssh-agent,
    unless this process added them a short while ago and the agent still lists
    them. Returns the names of the keys it loaded."""
    import time

    store, keys = managed(inventory)
    if store is None:
        return []
    chosen = [k for k in keys if wanted is None or k in {Path(w) for w in wanted}]
    if not chosen:
        return []
    agent = _agent_id(ensure_agent())
    held = loaded_fingerprints()
    now = time.monotonic()
    loaded = []
    for key in chosen:
        fpr = fingerprint(key)
        added = _ADDED.get((agent, fpr or ""))
        if fpr is not None and fpr in held and added is not None and now - added < ADDED_FRESH:
            continue
        if not key.exists():
            raise ConfigError(f"The managed key {key} is gone.")
        add_to_agent(key, read_passphrase(key, store))
        if fpr is not None:
            _ADDED[(agent, fpr)] = now
        loaded.append(key.name)
    return loaded


def record(editor, store: str, new_keys: Iterable[str]) -> None:
    """Add keys (path strings as written in ships.toml) to [fleet.managed_keys]."""
    import tomlkit

    fleet = editor.doc["fleet"]
    table = fleet.get("managed_keys")
    if table is None:
        table = tomlkit.table()
        table["store"] = store
        table["keys"] = tomlkit.array()
        fleet["managed_keys"] = table
    elif table.get("store") != store:
        raise FleetError(f"Managed keys already keep their passphrases in the "
                         f"{table.get('store')}; use the same store.")
    for key in new_keys:
        if key not in table["keys"]:
            table["keys"].append(key)


def describe(store: str) -> str:
    if store == "keyring":
        return "your OS keyring"
    return f"0600 files in {paths.config_dir() / PASSPHRASE_DIR}"
