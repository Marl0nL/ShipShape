"""Move FleetLedger to another computer: `ship-fleet migrate` (docs/runbooks/move-fleetledger.md).

Everything FleetLedger needs to manage the fleet is on the computer it runs on: the
inventory, the fleet admin key, the certificate authority's two keys, the secrets
(key passphrases, the Proxmox token, the OAuth secret) and its history. migrate
moves all of it in one go, so the Ships keep working from the new computer with
nothing changed on them: the same admin key logs in, the same pins check their host
keys, the same CA and the same revocation list stay in force. No Ship is adopted
again.

- check: what would move, what stays and why, and anything that stops a move
  (another ship-fleet running here, the web UI say; a Ship not enrolled yet; a secret
  that cannot be read; a file FleetLedger does not know; uncommitted changes in the
  checkout). Read-only.
- push [USER@]HOST: check, then stream it all over SSH into `ship-fleet migrate
  receive` on HOST (its host key must already be known: StrictHostKeyChecking=yes),
  then hand over (handover.py): this copy retires, then HOST activates.
- send: the same stream to a pipe, for running the move from the new computer
  (`ssh old '~/.local/bin/ship-fleet migrate send' | ship-fleet migrate receive`, where that
  computer's keyring is unlocked). Never to a terminal or a file.
- receive: on the new computer. Refuses unless it has no FleetLedger yet (what its
  setup leaves before the move does not count: a service install, an empty unlocked
  vault, and an audit log of only SETUP_ACTIONS), looked at again once it holds the
  lock, and its checkout is at the same commit;
  checks every file against the stream's manifest before writing any; stores each
  secret in this computer's own store under names it works out itself (a
  passphrase's keyring item depends on the key's full path);
  rewrites only the key paths and secret stores in ships.toml (and checks nothing
  else in it changed); keeps the fleet on standby; then verify.
- verify: every key opens, doctor's checks, the Proxmox token through its pin, the
  OAuth client's scopes, and a read-only status of every Ship with its certificate
  and revocation-list check. Nothing on any Ship changes.
- activate, retire, unretire, wipe: the hand-over by hand, a rollback, and deleting
  a retired copy after its two weeks (handover.py).

Until the hand-over nothing on the old computer changes, and nothing is written to
a file at rest on the way. What never moves: this-computer.json (a fact about that computer), locks
(an older version's empty in-use.lock included: inuse.OLD_FILE),
known_hosts (rebuilt from ships.toml), runtime sockets, a part-finished Proxmox setup,
the inbox upload spool, Ubuntu's cloud image cache and, unless --with-images, baked
image files. A key of your own that FleetLedger made stays on your machines: only
its .pub moves (the new computer never attaches as you).
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import tomllib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from fleetledger import __version__, ca, handover, inuse, keys, paths, vault
from fleetledger.errors import ConfigError, FleetError

SCHEMA = "fleetledger.migrate/1"
CHECKED_PATHS = ("ship", "versions.toml", "contracts", "fleetledger")
STORES = ("keyring", "file")
AGENT_KEYS_DIR = "agent-keys"  # where the new computer keeps your keys' .pub files
KEY_ROLES = {"admin": "fleet admin key", "host-ca": "host CA key", "user-ca": "user CA key"}
MAX_MANIFEST = 4 * 1024 * 1024
MAX_SECRETS = 64 * 1024
MAX_SECRET = 512
CHUNK = 1024 * 1024
STAGING = ".migrate-"  # the receiving side's own folder in the data dir, while it works
# The audit entries a FleetLedger with nothing in it yet writes: the appliance seed's
# service install (and a restart), the vault made before the move (unlocked after a
# reboot, its passphrase or recovery code changed), and a self-update to the old
# computer's commit. None holds fleet state. An audit log of only these is no
# FleetLedger to receive refuses; any other entry is.
SETUP_ACTIONS = frozenset({"service-install", "service-restart", "vault-init", "vault-unlock",
                           "vault-lock", "vault-passphrase", "vault-recovery", "self-update"})
MAX_SETUP_LOG = 256 * 1024

HALF_MADE = "not enrolled yet"  # a Ship's check, when it was being made as the fleet left

# Exit codes: received and verified; nothing written; written but not verified; the
# old copy retired but the new one not made active (no copy acts on the fleet).
VERIFIED, REFUSED, UNVERIFIED, NOT_ACTIVATED = 0, 1, 5, 6

SEGMENT = re.compile(r"^[A-Za-z0-9_.@:+=,-]{1,200}$")
FILE_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.@+-]{0,199}$")  # never . or ..
# Files in ~/.ssh that OpenSSH itself reads: a key never lands on one of these names.
SSH_OWN = ("authorized_keys", "authorized_keys2", "config", "environment", "known_hosts",
           "known_hosts2", "rc")
SSH_FILES = {*SSH_OWN, *(f"{n}.pub" for n in SSH_OWN)}
AGENT_MEMBER = re.compile(r"^keys/agent-[0-9]{1,3}\.pub$")
LOGIN_RE = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,190}$")
MEMBER = re.compile(r"^(manifest\.json|secrets\.json|config/ships\.toml|"
                    r"keys/(admin|host-ca|user-ca)(\.pub)?|keys/agent-[0-9]{1,3}\.pub|"
                    r"data/[^\x00]{1,1000}|images/[^/\x00]{1,200})$")
# What may sit in the receiving computer's data dir (data/<top>/...).
DATA_TOPS = ("audit.log", "status", "history.sqlite3", "attempts.json", "net", "ca", "trust")
TOOLKIT_ARCHIVES = "toolkit-archives"  # toolkitpack.CACHE: packed toolkits, rebuilt
BACKUP_LAST = "backup-last.json"  # the last ship-fleet backup's result (backup.py)
# The remote web UI's paired browsers and pairing links (web/devices.py): they stay.
WEB_FILES = ("web/devices.json", "web/pairings.json", "web/.lock")
HOST_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]{0,63}@)?[A-Za-z0-9_][A-Za-z0-9_.:-]{0,252}$")


# --- the plan (this computer, the one FleetLedger moves from) -------------------------------


@dataclass
class Move:
    member: str  # its name in the stream
    source: Path  # the file on this computer
    what: str  # for the listing
    kind: str = "file"  # file | sqlite (copied through SQLite) | image (large; not copied first)


@dataclass
class Plan:
    inventory: Any
    text: str
    moves: list[Move] = field(default_factory=list)
    keys: list[dict[str, Any]] = field(default_factory=list)
    secrets: dict[str, Any] = field(default_factory=dict)  # the values: never printed
    secret_names: list[str] = field(default_factory=list)
    remote_ui: dict[str, Any] | None = None  # the remote web UI's port and allow-list
    left_out: list[tuple[str, str]] = field(default_factory=list)  # (path, why)
    blockers: list[tuple[str, str]] = field(default_factory=list)  # (what, why)
    notes: list[str] = field(default_factory=list)  # said on both computers
    source_notes: list[str] = field(default_factory=list)  # said here only
    commit: str = "unknown"
    krl_sha256: str | None = None

    lenient: bool = False  # a backup (backup.py): what only stops a move is left out
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (what, why), lenient

    def block(self, what: str, why: str) -> None:
        self.blockers.append((what, why))

    def skip(self, what: str, why: str) -> None:
        """Stops a move; a backup leaves it out instead (a half-made Ship, a seed, a
        file FleetLedger does not know): it is still a good backup without it."""
        if self.lenient:
            self.skipped.append((what, why))
            self.left_out.append((what, why))
        else:
            self.block(what, why)

    def leave(self, path: Path | str, why: str) -> None:
        self.left_out.append((paths.tilde(path) if isinstance(path, Path) else path, why))


def _files(base: Path) -> list[Path]:
    """Every file under base (not folders), sorted; symlinks count as files."""
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*") if p.is_file() or p.is_symlink())


def _temp(name: str) -> bool:
    """A leftover of an atomic write (paths.write_private) cut short."""
    return name.startswith(".") and (".tmp-" in name or ".new-" in name or ".adopt-" in name)


def summary(inv) -> str:
    """What the inventory holds, in one line: both sides print it, so they can be compared."""
    ms = ca.machines(inv)
    cfg = inv.doc.get("ca") or {}
    parts = [f"{len(inv.ships())} Ship(s)",
             f"{len(ms)} trusted machine(s) ({sum(1 for m in ms if m.revoked)} untrusted)",
             f"{sum(len(m.issued) for m in ms)} issued certificate(s) on record",
             f"{len(cfg.get('revoked_host_keys', ()))} revoked host key(s)"]
    if cfg:
        parts.append("the CA" + (f" (+{len(cfg['previous_user_keys'])} old user CA key(s))"
                                 if cfg.get("previous_user_keys") else ""))
    if inv.driver_config("proxmox"):
        parts.append("Proxmox")
    if inv.overlay:
        parts.append("the overlay")
    if inv.images:
        parts.append(f"{len(inv.images)} image(s)")
    return ", ".join(parts)


def build_plan(app, text: str, inv, *, with_images: bool = False,
               for_backup: bool = False) -> Plan:
    """Everything that moves, stays or stops the move. Reads only (secrets included:
    each is read now, so a locked keyring stops the move before anything is sent).
    for_backup: what only stops a move is left out instead (Plan.skip)."""
    plan = Plan(inv, text, lenient=for_backup)
    store = app.store_factory()
    plan.moves.append(Move("config/ships.toml", store.path, f"inventory: {summary(inv)}"))
    here = handover.marker(store)
    if here is not None:
        plan.block("this copy", handover.banner(here).message)
    moving: set[Path] = set()
    _plan_checkout(plan, app)
    for ship in inv.ships():
        if ship.enrolled is None:
            plan.skip(f"Ship {ship.name}", "is not enrolled yet, and its seed holds its private "
                       f"host key: finish it (ship-fleet status {ship.name} --wait) or destroy "
                       "it first")
    managed_names = _plan_keys(plan, inv, moving)
    secret_files = _plan_secrets(plan, inv)
    _plan_images(plan, inv, moving, with_images)
    _plan_config(plan, moving | secret_files, managed_names)
    _plan_data(plan, inv, moving)
    _plan_toolkits(plan, inv)
    cfg = ca.config(inv)
    if cfg is not None:
        krl = ca.cached_krl(inv)
        if krl is None:
            plan.notes.append("The revocation list kept here does not match the inventory (or "
                              "was never built), so the new computer builds it afresh: run "
                              "ship-fleet ca sign there once.")
        else:
            plan.krl_sha256 = hashlib.sha256(krl).hexdigest()
    return plan


def _plan_checkout(plan: Plan, app) -> None:
    try:
        repo = app.repo_factory()
    except FleetError as exc:
        plan.block("the ShipShape checkout", exc.message)
        return
    git = repo.git_state(*CHECKED_PATHS)
    if git.commit == "unknown":
        plan.block("the ShipShape checkout", f"{repo.root} is not a git checkout, so there is "
                   "no commit for the new computer to match")
    elif git.dirty:
        plan.block("the ShipShape checkout", f"{repo.root} has uncommitted changes (ship/, "
                   "versions.toml, contracts/ or fleetledger/): commit and push them first; the "
                   "new computer must run the same commit")
    else:
        plan.commit = git.commit
        if repo.on_remote(git.commit) is False:
            plan.source_notes.append(f"Commit {git.commit[:12]} is on no remote branch yet: "
                                     "push it, so the new computer can check it out.")


def _plan_keys(plan: Plan, inv, moving: set[Path]) -> set[str]:
    """The admin and CA keys (both halves), your keys' .pub files, and the passphrases of
    the managed ones. Returns the passphrase names (keys.stored_name) that move."""
    store, managed = keys.managed(inv)
    managed_set = {k.resolve() for k in managed}
    roles = [("admin", inv.admin_key)]
    cfg = ca.config(inv)
    if cfg is not None:
        roles += [("host-ca", cfg.host_key), ("user-ca", cfg.user_key)]
    names: set[str] = set()
    privates: set[Path] = set()
    for role, path in roles:
        label = KEY_ROLES[role]
        pub = path.with_name(path.name + ".pub")
        missing = [f for f in (path, pub) if not f.is_file()]
        for f in missing:
            plan.block(paths.tilde(f), f"the {label} file is missing here")
        if missing or not FILE_NAME.match(path.name):
            if not missing:
                plan.block(paths.tilde(path), "a key file name of letters, digits and _ . @ + - "
                           "only can move")
            continue
        is_managed = path.resolve() in managed_set
        plan.keys.append({"role": role, "source": str(path.resolve()), "name": path.name,
                          "managed": is_managed})
        plan.moves += [Move(f"keys/{role}", path, label),
                       Move(f"keys/{role}.pub", pub, f"{label} (public half)")]
        moving.update({path.resolve(), pub.resolve()})
        privates.add(path.resolve())
        if is_managed:
            try:
                passphrase = keys.read_passphrase(path, store)
                keys.check_opens(path, passphrase)
            except FleetError as exc:
                plan.block(f"the {label}'s passphrase",
                           exc.message + (f" {exc.hint}" if exc.hint else ""))
                continue
            plan.secrets.setdefault("passphrases", {})[role] = passphrase
            names.add(keys.stored_name(path))
        elif keys.needs_passphrase(path):
            plan.notes.append(f"{paths.tilde(path)} has a passphrase of your own: it moves "
                              "encrypted, and on the new computer you load it (ssh-add) before "
                              "FleetLedger can use it.")
    if plan.secrets.get("passphrases"):
        plan.secret_names.append(f"{len(plan.secrets['passphrases'])} key passphrase(s)")
    for i, pub in enumerate(inv.agent_key_paths):
        if not pub.is_file():
            plan.block(paths.tilde(pub), "one of your keys for Ships (fleet.agent_keys) is "
                       "missing here")
            continue
        if not FILE_NAME.match(pub.name):
            plan.block(paths.tilde(pub), "a key file name of letters, digits and _ . @ + - only "
                       "can move")
            continue
        plan.keys.append({"role": "agent", "member": f"keys/agent-{i}.pub",
                          "source": str(pub.resolve()), "name": pub.name})
        plan.moves.append(Move(f"keys/agent-{i}.pub", pub, "your key for Ships (public half)"))
        moving.add(pub.resolve())
    for path in managed:
        if path.resolve() not in privates:
            plan.leave(path, "your own key, made by FleetLedger: its private half and passphrase "
                       "stay on your machines (the new computer never attaches as you)")
    return names


def _plan_secrets(plan: Plan, inv) -> set[Path]:
    """The Proxmox token and the OAuth secret, read from wherever ships.toml says. Returns
    the secret files that ships.toml names (they move as secrets, not as files)."""
    from fleetledger.drivers import proxmox_api
    from fleetledger.overlay import secret_store

    named: set[Path] = set()
    proxmox = inv.driver_config("proxmox")
    if proxmox:
        if proxmox.get("token_file"):
            named.add(paths.expand(proxmox["token_file"]).resolve())
        try:
            plan.secrets["proxmox_token"] = proxmox_api.token_secret(proxmox)
            plan.secret_names.append("the Proxmox token")
        except FleetError as exc:
            plan.block("the Proxmox token", exc.message + (f" {exc.hint}" if exc.hint else ""))
    api = (inv.overlay or {}).get("api")
    if api:
        if api.get("oauth_secret_file"):
            named.add(paths.expand(api["oauth_secret_file"]).resolve())
        try:
            plan.secrets["oauth_secret"] = secret_store.load(api)
            plan.secret_names.append("the overlay's OAuth secret")
        except FleetError as exc:
            plan.block("the overlay's OAuth secret",
                       exc.message + (f" {exc.hint}" if exc.hint else ""))
    return named


def _plan_images(plan: Plan, inv, moving: set[Path], with_images: bool) -> None:
    for image_id, entry in sorted(inv.images.items()):
        if "file" not in entry:
            continue
        path = paths.expand(entry["file"])
        if not path.is_file():
            continue
        published = ", ".join(sorted(entry.get("published", {})))
        if not with_images:
            why = (f"baked image {image_id}, published to {published}, so new Ships still use "
                   "it" if published else f"baked image {image_id}, not published anywhere: the "
                   "new computer cannot publish it without this file")
            plan.leave(path, why + " (--with-images moves it)")
            continue
        if not FILE_NAME.match(path.name):
            plan.block(paths.tilde(path), "an image file name of letters, digits and _ . @ + - "
                       "only can move")
            continue
        plan.moves.append(Move(f"images/{path.name}", path, f"baked image {image_id}", "image"))
        moving.add(path.resolve())
        side = path.with_suffix(".json")
        if side.is_file() and FILE_NAME.match(side.name):
            plan.moves.append(Move(f"images/{side.name}", side, f"image {image_id}'s manifest"))
            moving.add(side.resolve())


def _toolkit_dirs(inv) -> dict[Path, str]:
    """ships.toml's toolkit folders (resolved) -> toolkit id."""
    from fleetledger import toolkits

    return {cfg.path.resolve(): cfg.id for cfg in toolkits.configured(inv)}


def _in_toolkit(f: Path, dirs: dict[Path, str]) -> str | None:
    where = f.resolve()
    return next((kid for d, kid in dirs.items() if where.is_relative_to(d)), None)


def _plan_toolkits(plan: Plan, inv) -> None:
    """Toolkit folders are yours, wherever they are: migrate names each one (said on
    both computers) and copies none."""
    from fleetledger import toolkits

    for cfg in toolkits.configured(inv):
        plan.notes.append(
            f"Toolkit {cfg.id} is read from {paths.tilde(cfg.path)}, a folder of yours that "
            "migrate does not copy: bring this folder to the new computer (to the same "
            f"place, or change [toolkit.{cfg.id}] path there), then check it with "
            f"ship-fleet toolkit check {cfg.id}.")


def _plan_config(plan: Plan, moving: set[Path], managed_names: set[str]) -> None:
    from fleetledger import proxmox_setup, trustcmd
    from fleetledger.web import remote

    base = paths.config_dir()
    kits = _toolkit_dirs(plan.inventory)
    for f in _files(base):
        rel = f.relative_to(base).as_posix()
        if f.resolve() in moving or rel == "ships.toml":
            continue
        if (kit := _in_toolkit(f, kits)) is not None:
            plan.leave(f, f"part of toolkit {kit}'s folder, which migrate does not copy")
        elif rel == "ships.toml.lock":
            plan.leave(f, "a lock")
        elif rel in (handover.STANDBY, handover.RETIRED):
            continue  # this copy's own marker: it stops the move ("this copy", above)
        elif rel == vault.FILE:
            plan.leave(f, "this computer's vault: the secrets in it move as secrets, stored "
                       "again in the new computer's own store")
        elif rel == "known_hosts":
            plan.leave(f, "FleetLedger's known_hosts: rebuilt from the inventory's pins")
        elif rel == trustcmd.THIS_COMPUTER:
            plan.leave(f, "which machine entry is this computer: a fact about this computer, "
                       "not the fleet")
        elif rel == remote.REMOTE_FILE:
            plan.remote_ui = _remote_ui(f)
            plan.leave(f, "the remote web UI setting: this computer's own name on the tailnet. "
                       "Turning it on is yours to do on the new computer" + (
                           ": it is told the command, with the same logins" if plan.remote_ui
                           else " (this file is not one FleetLedger wrote, so it is told "
                           "nothing)"))
        elif rel == proxmox_setup.PROGRESS_FILE:
            plan.leave(f, "a part-finished Proxmox setup (run setup again there if needed)")
        elif rel.startswith(keys.PASSPHRASE_DIR + "/"):
            if f.name in managed_names:
                continue  # moves as a secret, stored again there
            plan.leave(f, "a passphrase no moving key uses (your own key's stays with it)")
        elif rel.startswith(AGENT_KEYS_DIR + "/"):
            plan.leave(f, "not one of your keys for Ships (fleet.agent_keys)")
        elif rel in ("proxmox-token", "tailscale-oauth"):
            plan.leave(f, "a secret file ships.toml does not use")
        elif rel == inuse.OLD_FILE and (old := inuse.old_file(f)) is not None:
            if old == "held":
                plan.skip(paths.tilde(f), "an older ship-fleet is running and holds it (its "
                           "in-use lock): stop it first, then check again")
            else:
                plan.leave(f, "an empty in-use lock an older ship-fleet left: the lock is now "
                           "on the folder itself, so nothing uses it (delete it when you like)")
        elif _temp(f.name):
            plan.leave(f, "a leftover of a write that was cut short")
        else:
            plan.skip(paths.tilde(f), "FleetLedger does not know this file, so migrate will not "
                       "guess whether it matters: move it out of the folder first")


def _plan_data(plan: Plan, inv, moving: set[Path]) -> None:
    base = paths.data_dir()
    listed = {path for path, _ in plan.left_out}
    kits = _toolkit_dirs(inv)
    for f in _files(base):
        rel = f.relative_to(base).as_posix()
        top = rel.split("/", 1)[0]
        if f.resolve() in moving:
            continue
        if (kit := _in_toolkit(f, kits)) is not None:
            plan.leave(f, f"part of toolkit {kit}'s folder, which migrate does not copy")
        elif top == "seeds" and f.name.startswith("appliance-"):
            plan.skip(paths.tilde(f), "FleetLedger's own VM's seed holds that VM's private "
                       "host key: delete it once the VM has booted (and from Proxmox's storage)")
        elif top == "seeds":
            plan.skip(paths.tilde(f), "a seed holds a Ship's private host key: finish enrolling "
                       "that Ship, or remove the seed of one that is gone")
        elif paths.tilde(f) in listed:
            continue  # said already (a baked image's file)
        elif top == "images":
            plan.leave(f, "Ubuntu's cloud image, fetched and checked again when needed"
                       if rel.startswith("images/base/") else "an image file not moving "
                       "(--with-images moves the inventory's images)")
        elif top == "inbox-spool":
            plan.leave(f, "a file half-way to a Ship's inbox")
        elif top == TOOLKIT_ARCHIVES:
            plan.leave(f, "a packed toolkit archive: rebuilt from the toolkit's folder when "
                       "it is next sent")
        elif rel == BACKUP_LAST:
            plan.leave(f, "this computer's last backup result (ship-fleet backup)")
        elif rel in WEB_FILES:
            plan.leave(f, "the remote web UI's paired browsers and pairing links: bound to "
                       "this computer's address; pair each device again with the new computer "
                       "(ship-fleet ui link)")
        elif top.startswith(STAGING):
            plan.leave(f, "a leftover of a migrate that was cut short")
        elif rel.startswith("history.sqlite3-"):
            plan.leave(f, "part of the history database, which is copied whole")
        elif _temp(f.name):
            plan.leave(f, "a leftover of a write that was cut short")
        elif top not in DATA_TOPS or (top == "ca" and rel not in (
                "ca/revoked-keys.krl", "ca/revoked-keys.spec-sha256")) or f.is_symlink():
            plan.skip(paths.tilde(f), "FleetLedger does not know this file, so migrate will not "
                       "guess whether it matters: move it out of the folder first")
        elif not all(SEGMENT.match(s) and s not in (".", "..") for s in rel.split("/")):
            plan.block(paths.tilde(f), "an unexpected file name")
        else:
            kind = "sqlite" if rel == "history.sqlite3" else "file"
            plan.moves.append(Move(f"data/{rel}", f, _data_label(rel), kind))


def _remote_ui(path: Path) -> dict[str, Any] | None:
    """The remote web UI's port and allow-list (web/remote.py), to tell the new computer
    how to turn it on again; None when the file is not one FleetLedger wrote."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    port, logins = doc.get("port"), doc.get("logins", [])
    if not (isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535) \
            or not isinstance(logins, list) or len(logins) > 20 \
            or not all(isinstance(x, str) and LOGIN_RE.match(x) for x in logins):
        return None
    return {"port": port, "logins": logins}


def _data_label(rel: str) -> str:
    top = rel.split("/", 1)[0]
    return {"audit.log": "audit log", "status": "last status", "history.sqlite3":
            "charts and Activity history", "attempts.json": "failed Ship attempts (and their "
            "leftovers)", "net": "network summaries", "ca": "revocation list",
            "trust": "trust bundle"}.get(top, top)


def print_plan(say: Callable[[str], None], plan: Plan) -> None:
    from fleetledger.commands import table

    rows = [[m.what, paths.tilde(m.source)] for m in plan.moves]
    if plan.secret_names:
        rows.append(["secrets, stored again there", ", ".join(plan.secret_names)])
    say("Moves to the new computer:")
    say(table(rows, ["WHAT", "FROM"]))
    if plan.left_out:
        say("\nStays here:")
        say(table([list(r) for r in plan.left_out], ["FILE", "WHY"]))
    for note in [*plan.notes, *plan.source_notes]:
        say(f"\nNote: {note}")
    if plan.blockers:
        say("\nStops the move:")
        for what, why in plan.blockers:
            say(f"  {what}: {why}")


# --- the stream ------------------------------------------------------------------------------


def _sha256_file(path: Path) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _copy_sqlite(src: Path, dst: Path) -> None:
    """A consistent copy of an SQLite file, through SQLite's own backup."""
    source = sqlite3.connect(src, timeout=5)
    try:
        target = sqlite3.connect(dst)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _tarinfo(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = size, 0o600, 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def manifest_of(plan: Plan, files: list[dict[str, Any]], with_images: bool) -> dict[str, Any]:
    from fleetledger.seed.shiptoml import local_timezone

    return {
        "schema": SCHEMA,
        "source": {
            "host": socket.gethostname().split(".")[0][:63],
            "fleetledger": __version__,
            "commit": plan.commit,
            "home": str(Path.home().resolve()),
            "config_dir": str(paths.config_dir().resolve()),
            "data_dir": str(paths.data_dir().resolve()),
            "timezone": local_timezone(),
        },
        "summary": summary(plan.inventory),
        "epoch": handover.epoch(plan.inventory),
        "keys": plan.keys,
        "secrets": plan.secret_names,
        "files": files,
        "left_out": [list(r) for r in plan.left_out],
        "notes": plan.notes,
        "krl_sha256": plan.krl_sha256,
        "remote_ui": plan.remote_ui,
        "with_images": with_images,
    }


def write_stream(plan: Plan, out: BinaryIO, *, with_images: bool = False) -> None:
    """The whole move as one tar stream: the manifest (every member's size and SHA-256)
    first, then the files, then the secrets. Files are copied aside first, so what is
    sent is what was hashed; secrets stay in memory. The copies go in FleetLedger's
    own private runtime folder (memory, on most systems), never a shared /tmp."""
    if plan.blockers:
        raise FleetError("Not moving: see what stops the move above.")
    runtime = paths.private_dir(paths.runtime_dir())
    with tempfile.TemporaryDirectory(prefix="migrate-send-", dir=runtime) as tmp:
        sending: list[tuple[str, Path]] = []
        files: list[dict[str, Any]] = []
        for n, move in enumerate(plan.moves):
            if move.member == "config/ships.toml":
                path = Path(tmp) / f"{n:04d}"
                path.write_text(plan.text, encoding="utf-8")
            elif move.kind == "image":
                path = move.source  # large: hashed in place, checked again on the other side
            else:
                path = Path(tmp) / f"{n:04d}"
                if move.kind == "sqlite":
                    _copy_sqlite(move.source, path)
                else:
                    shutil.copyfile(move.source, path)
            digest, size = _sha256_file(path)
            files.append({"member": move.member, "size": size, "sha256": digest,
                          "what": move.what})
            sending.append((move.member, path))
        secrets = json.dumps(plan.secrets).encode("utf-8")
        files.append({"member": "secrets.json", "size": len(secrets),
                      "sha256": hashlib.sha256(secrets).hexdigest(), "what": "secrets"})
        manifest = json.dumps(manifest_of(plan, files, with_images), indent=1).encode("utf-8")
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
            tar.addfile(_tarinfo("manifest.json", len(manifest)), io.BytesIO(manifest))
            for member, path in sending:
                with path.open("rb") as fh:
                    tar.addfile(_tarinfo(member, path.stat().st_size), fh)
            tar.addfile(_tarinfo("secrets.json", len(secrets)), io.BytesIO(secrets))
    out.flush()


def _pipe_only(out: BinaryIO) -> None:
    """send writes every secret: only into a pipe (ssh, or receive), never a terminal or
    a file, which would keep them unencrypted at rest."""
    try:
        fd = out.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        return  # an in-memory stream (tests)
    if os.isatty(fd):
        raise FleetError("send writes the whole fleet, secrets included: pipe it into "
                         "ship-fleet migrate receive (through ssh), never to a terminal.")
    mode = os.fstat(fd).st_mode
    if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
        raise FleetError("send writes the whole fleet, secrets included: pipe it into "
                         "ship-fleet migrate receive (through ssh), never into a file, which "
                         "would keep every secret unencrypted.")


def check(app, *, with_images: bool = False) -> Plan:
    """The plan, printed. Read-only (the UI check takes the lock for a moment)."""
    store = app.store_factory()
    if not store.exists():
        raise ConfigError(f"No inventory at {store.path}: there is no FleetLedger here to move.")
    busy = None
    try:
        with inuse.exclusive(store):
            pass
    except FleetError as exc:
        busy = exc
    with store.held() as text:
        plan = build_plan(app, text, store.load(), with_images=with_images)
    if busy is not None:
        plan.block("another ship-fleet", busy.message + " " + (busy.hint or ""))
    return plan


def send(app, out: BinaryIO, *, with_images: bool = False) -> Plan:
    """Check, then write the stream to out, holding the UI lock and the inventory's lock
    throughout so nothing changes here while it is copied. Says everything on stderr:
    stdout is the stream."""
    store = app.store_factory()
    if not store.exists():
        raise ConfigError(f"No inventory at {store.path}: there is no FleetLedger here to move.")
    with inuse.exclusive(store), store.held() as text:
        plan = build_plan(app, text, store.load(), with_images=with_images)
        if plan.blockers:
            print_plan(app.note, plan)
            raise FleetError("Not moving: see what stops the move above.")
        write_stream(plan, out, with_images=with_images)
    return plan


# --- push: send over SSH into receive -----------------------------------------------------

SSH_OPTIONS = ("StrictHostKeyChecking=yes", "ForwardAgent=no", "ForwardX11=no",
               "ClearAllForwardings=yes", "PermitLocalCommand=no", "RequestTTY=no")


# Where install.sh's pipx puts ship-fleet. ssh runs a command with no login shell, and
# even sh -l only has it on PATH if a profile adds it (Ubuntu's ~/.profile does; a home
# without one, or another system's, may not).
PIPX_BIN = "$HOME/.local/bin"


def remote_command(ship_fleet: str, argv: Sequence[str]) -> str:
    """What runs on the new computer: ship-fleet with argv, in a login shell. The default
    is ship-fleet on that PATH, else pipx's ~/.local/bin/ship-fleet; a path given is run
    as it is."""
    if ship_fleet == "ship-fleet":
        program = f'PATH="$PATH:{PIPX_BIN}"; exec ship-fleet'
    elif ship_fleet.startswith("~/"):
        program = 'exec "$HOME"/' + shlex.quote(ship_fleet[2:])
    else:
        program = "exec " + shlex.quote(ship_fleet)
    return shlex.join(["sh", "-lc", program + " " + shlex.join(argv)])


def ssh_argv(host: str, remote: str, options: Iterable[str] = ()) -> list[str]:
    """ssh to the new computer. Our options come first, so none can be weakened by one
    given later (ssh keeps the first value it reads): its host key must be known."""
    if not HOST_RE.match(host):
        raise FleetError(f"'{host}' is not [USER@]HOST.")
    argv = ["ssh", "-T"]
    for opt in (*SSH_OPTIONS, *options):
        argv += ["-o", opt]
    return [*argv, "--", host, remote]


def _relay(stream, say: Callable[[str], None], prefix: str) -> threading.Thread:
    def run() -> None:
        for raw in iter(stream.readline, b""):
            say(prefix + raw.decode("utf-8", "replace").rstrip("\n"))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _session(app, host: str, argv: list[str],
             feed: Callable[[BinaryIO], None] | None = None) -> tuple[int, bool]:
    """Run ssh, relaying what the other side says; feed writes its input. Returns the
    exit code, and whether it stopped reading before the input was all sent."""
    popen = getattr(app, "popen", None) or subprocess.Popen
    try:
        proc = popen(argv, stdin=subprocess.PIPE if feed else subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise FleetError("ssh not found; install OpenSSH.") from exc
    relays = [_relay(proc.stdout, app.say, f"{host}: "),
              _relay(proc.stderr, app.note, f"{host}: ")]
    cut_short = False
    if feed is not None:
        try:
            feed(proc.stdin)
        except (BrokenPipeError, ConnectionResetError, ValueError):
            cut_short = True  # it stopped reading: its own output says why
        except BaseException:
            proc.kill()  # a cut-short stream: the other side writes nothing
            raise
        finally:
            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
    code = proc.wait()
    for relay in relays:
        relay.join(timeout=10)
    if code == 255:
        raise FleetError(f"ssh could not log in to {host} (or its host key is not known here, "
                         "or it changed).",
                         hint=f"Log in once with plain ssh {host} and compare its host key's "
                         "fingerprint first.")
    if code == 127:
        raise FleetError(f"{host} has no ship-fleet on PATH, nor in ~/.local/bin.",
                         hint="Install FleetLedger there (fleetledger/install.sh), or say "
                         "where it is: --ship-fleet /path/to/ship-fleet.")
    return code, cut_short


def push(app, host: str, *, store_kind: str | None = None, with_images: bool = False,
         options: Iterable[str] = (), ship_fleet: str = "ship-fleet",
         cutover: bool = True) -> int:
    """Send everything over SSH to `ship-fleet migrate receive` on host, which keeps
    it on standby and checks every Ship. Then, unless cutover is False, hand over:
    this copy retires first, then host activates with the next epoch (so a hand-over
    cut short leaves no copy active, never two). Returns 0 once host is the active
    FleetLedger (or, without the cut-over, on standby and checked)."""
    from fleetledger import appliance

    receive_argv = ["migrate", "receive", "--pushed"] + (["--store", store_kind]
                                                         if store_kind else [])
    ssh_argv(host, "", options)  # refuses a host that is not one, before anything else
    store = app.store_factory()
    if not store.exists():
        raise ConfigError(f"No inventory at {store.path}: there is no FleetLedger here to move.")
    with inuse.exclusive(store), store.held() as text:
        inv = store.load()
        # FleetLedger's own VM ([fleet] self): its host key pinned when its seed was made,
        # checked here before anything else may set where ssh looks for it.
        options = [*appliance.push_options(inv, host), *options]
        argv = ssh_argv(host, remote_command(ship_fleet, receive_argv), options)
        plan = build_plan(app, text, inv, with_images=with_images)
        print_plan(app.say, plan)
        if plan.blockers:
            raise FleetError("Not moving: see what stops the move above.")
        app.say(f"\nSending to {host} (nothing on this computer changes until it has checked "
                "every Ship)...")
        code, cut_short = _session(app, host, argv,
                                   lambda out: write_stream(plan, out, with_images=with_images))
        if code == VERIFIED and cutover:
            return _cut_over(app, store, host, handover.epoch(inv) + 1, options, ship_fleet)
    if code == VERIFIED:
        app.say(f"\n{host} has the fleet on standby and its checks passed (any WARN is above); "
                "this copy is still the active one. To move for real, push again (it "
                "replaces the copy on standby).")
    elif code == UNVERIFIED:
        app.note(f"\n{host} has the fleet on standby, but could not check all of it (see "
                 "above), so this copy is still the active one. Fix that there, then push "
                 "again (it replaces the copy on standby).")
    else:
        app.note(f"\n{host} did not take the fleet" + (" (it stopped reading)" if cut_short
                                                       else "") +
                 f" (exit {code}). Nothing on this computer changed.")
    return code


def _cut_over(app, store, host: str, handed: int, options: Iterable[str],
              ship_fleet: str) -> int:
    handover.retire(store, host, handed)  # first: never two active copies
    app.say(f"\nThis copy is now retired: it no longer acts on the fleet. Making {host} the "
            "active FleetLedger...")
    argv = ssh_argv(host, remote_command(ship_fleet, ["migrate", "activate", "--yes"]), options)
    try:
        code, _ = _session(app, host, argv)
    except Exception as exc:  # noqa: BLE001 - this copy has retired: say what to do, always
        app.note(exc.message if isinstance(exc, FleetError) else type(exc).__name__)
        code = REFUSED
    if code == 0:
        app.say(f"\n{host} is now the active FleetLedger (epoch {handed}), and nothing on any "
                "Ship changed. This copy refuses to act on the fleet and keeps its files for a "
                f"rollback (ship-fleet migrate retire on {host}, then ship-fleet migrate unretire "
                "here). After two weeks, delete its keys and secrets: ship-fleet migrate wipe.")
        return VERIFIED
    app.note(f"\nThis copy is retired, but {host} did not become active (exit {code}), so no "
             f"copy acts on the fleet now. Fix that there, then run ship-fleet migrate activate "
             f"on {host}; or, to keep using this copy instead, ship-fleet migrate unretire here.")
    return NOT_ACTIVATED


# --- receive (the new computer) ------------------------------------------------------------


def setup_only_log(path: Path) -> bool:
    """The audit log holds only SETUP_ACTIONS entries, each about no Ship: what the
    appliance's seed and a vault init before the move leave. Anything else in it (an
    entry of any other kind, a line that is not one) is a FleetLedger that was used."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_SETUP_LOG:
            return False
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    for line in filter(str.strip, lines):
        try:
            entry = json.loads(line)
        except ValueError:
            return False
        if not isinstance(entry, dict) or entry.get("ship") != "-" \
                or entry.get("action") not in SETUP_ACTIONS:
            return False
    return True


def existing_state(store) -> list[Path]:
    """FleetLedger files already on this computer (locks, the marker a wiped copy
    leaves, an earlier receive's leftover folder and an audit log of setup alone do
    not count): receive never writes over a FleetLedger."""
    found = []
    # This computer's own vault is its keyring, not a FleetLedger: receive stores into it
    # (receive refuses one that holds secrets already: used_vault).
    markers = {store.path.with_name(handover.STANDBY), store.path.with_name(handover.RETIRED),
               vault.vault_path()}
    log = paths.audit_log_path()
    for base in (paths.config_dir(), paths.data_dir(), store.path.parent):
        for f in _files(base):
            rel = f.relative_to(base).as_posix()
            if rel == "ships.toml.lock" or rel.startswith(STAGING) or f in markers:
                continue
            if f == log and setup_only_log(f):
                continue
            if f not in found:
                found.append(f)
    if store.exists() and store.path not in found:
        found.append(store.path)
    return found


def refuse_existing(store, here: handover.Marker | None) -> None:
    """receive's refusal of a FleetLedger here: its files (not for a copy on standby,
    which is replaced) and its vault (not after a wipe, which keeps your own key's
    passphrase in it)."""
    found = [] if here is not None and here.role == "standby" else existing_state(store)
    if found:
        listed = ", ".join(paths.tilde(f) for f in found[:5]) + (" ..." if len(found) > 5 else "")
        hint = ("Move those files aside first (or point FLEETLEDGER_CONFIG_DIR and "
                "FLEETLEDGER_DATA_DIR at empty folders).")
        log = paths.audit_log_path()
        if found == [log] and (what := _not_setup(log)):
            hint = (f"Its audit log records more than setting this computer up ({what}). If "
                    f"this computer never held a fleet, move just {paths.tilde(log)} aside "
                    "(not the vault); otherwise move FleetLedger's folders aside.")
        raise FleetError(f"This computer already has a FleetLedger ({listed}); migrate never "
                         "writes over one. Nothing was written.", hint=hint)
    if here is None and (refusal := used_vault()) is not None:
        raise refusal


def _not_setup(log: Path) -> str | None:
    """The first entry in log that is not setup, said briefly (none when it cannot be
    read as entries)."""
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    for line in filter(str.strip, lines):
        try:
            entry = json.loads(line)
        except ValueError:
            return "a line that is not an entry"
        if not isinstance(entry, dict):
            return "a line that is not an entry"
        if entry.get("ship") != "-" or entry.get("action") not in SETUP_ACTIONS:
            return f"{entry.get('action', '?')} on {entry.get('ship', '?')}"
    return None


def used_vault() -> FleetError | None:
    """The refusal for this computer's vault, if it is on and is not empty (another
    fleet's secrets, or the vault of a FleetLedger moved aside) or is locked (it cannot
    be seen to be empty, nor take the secrets)."""
    if vault.state() == "off":
        return None
    if vault.state() == "locked":
        return FleetError("FleetLedger's vault here is locked, so receive cannot see that it "
                          "is empty, nor store the fleet's secrets in it. Nothing was written.",
                          hint=vault.UNLOCK_HINT)
    held = vault.backend().count()
    if not held:
        return None
    return FleetError(f"FleetLedger's vault here already holds {held} secret(s), so this "
                      "computer has had a FleetLedger; migrate never writes over one. Nothing "
                      "was written.", hint="If that FleetLedger is not needed, move "
                      f"{paths.tilde(vault.vault_path())} aside (with anything else of it), "
                      "then ship-fleet vault init again.")


def choose_store(store_kind: str | None) -> str:
    """keyring or file for every secret here. Asked for and unusable: refused, never a
    quiet switch to the other."""
    from fleetledger.overlay import secret_store

    if store_kind is None:
        return keys.default_store()
    if store_kind not in STORES:
        raise FleetError(f"--store: keyring or file, not {store_kind!r}.")
    if store_kind == "file":
        secret_store.refuse_file("every secret")
    if store_kind == "keyring":
        usable, why = secret_store.keyring_usable()
        if not usable:
            raise ConfigError(f"Cannot keep the secrets in the keyring here: {why}.",
                              hint="Run receive where your keyring is unlocked (a terminal in "
                              "your desktop session), or choose --store file.")
    return store_kind


def _read_json(tar: tarfile.TarFile, member: tarfile.TarInfo, limit: int) -> Any:
    if not member.isfile() or member.size > limit:
        raise FleetError(f"{member.name} in the stream is not what FleetLedger sends.")
    data = tar.extractfile(member).read(limit + 1)
    try:
        return json.loads(data), data
    except ValueError:
        raise FleetError(f"{member.name} in the stream is not valid JSON.") from None


def check_manifest(manifest: Any) -> dict[str, Any]:
    """The manifest's shape, before anything in it is used."""
    def bad(why: str) -> FleetError:
        return FleetError(f"The stream's manifest is not one FleetLedger understands ({why}); "
                          "nothing was written.", hint="Run the same FleetLedger commit on "
                          "both computers.")

    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise bad(f"schema {manifest.get('schema') if isinstance(manifest, dict) else '?'}")
    for name in ("source", "keys", "files", "left_out", "notes"):
        if name not in manifest:
            raise bad(f"no {name}")
    if not isinstance(manifest["source"], dict) or not re.fullmatch(
            r"[0-9a-f]{40}", str(manifest["source"].get("commit", ""))):
        raise bad("no commit")
    epoch = manifest.setdefault("epoch", 0)
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise bad("epoch")
    seen = set()
    for f in manifest["files"]:
        member = f.get("member") if isinstance(f, dict) else None
        if (not isinstance(member, str) or not MEMBER.match(member) or member in seen
                or not isinstance(f.get("size"), int) or f["size"] < 0
                or not re.fullmatch(r"[0-9a-f]{64}", str(f.get("sha256", "")))):
            raise bad(f"file entry {member!r}")
        if member.startswith("data/"):
            parts = member[5:].split("/")
            if parts[0] not in DATA_TOPS or not all(
                    SEGMENT.match(p) and p not in (".", "..") for p in parts):
                raise bad(f"file entry {member!r}")
        if member.startswith("images/") and not FILE_NAME.match(member[7:]):
            raise bad(f"file entry {member!r}")
        seen.add(member)
    for required in ("config/ships.toml", "keys/admin", "keys/admin.pub", "secrets.json"):
        if required not in seen:
            raise bad(f"no {required}")
    claimed: set[str] = set()
    for k in manifest["keys"]:
        if not isinstance(k, dict) or k.get("role") not in (*KEY_ROLES, "agent") or \
                not FILE_NAME.match(str(k.get("name", ""))) or \
                not str(k.get("source", "")).startswith("/") or \
                not FILE_NAME.match(Path(str(k["source"])).name) or \
                {str(k["name"]), Path(str(k["source"])).name} & SSH_FILES:
            raise bad("key entry")  # placement uses the source's name: a file's, checked
        if k["role"] == "agent":
            member = k.get("member")
            if not isinstance(member, str) or not AGENT_MEMBER.match(member):
                raise bad("key entry")
            halves = {member}
        else:
            member = f"keys/{k['role']}"
            halves = {member, f"{member}.pub"}
        if not halves <= seen:
            raise bad(f"key {member} is not in the stream")
        if halves & claimed:
            raise bad(f"key {member} is listed twice")
        claimed |= halves
    unclaimed = sorted(m for m in seen if m.startswith("keys/") and m not in claimed)
    if unclaimed:  # every key file has exactly one place to go
        raise bad(f"{unclaimed[0]} is in the stream, but no key entry says what it is")
    remote_ui = manifest.get("remote_ui")
    if remote_ui is not None and (
            not isinstance(remote_ui, dict) or set(remote_ui) != {"port", "logins"}
            or not isinstance(remote_ui["port"], int) or isinstance(remote_ui["port"], bool)
            or not 1024 <= remote_ui["port"] <= 65535
            or not isinstance(remote_ui["logins"], list) or len(remote_ui["logins"]) > 20
            or not all(isinstance(x, str) and LOGIN_RE.match(x) for x in remote_ui["logins"])):
        raise bad("remote_ui")
    return manifest


def _target_key_path(source: str, src: dict[str, Any]) -> Path:
    """Where a key goes here: the same place relative to this computer's FleetLedger
    folders or ~/.ssh as it had there; from anywhere else, ~/.ssh/<its name>. Never
    elsewhere in your home folder, whatever the stream says."""
    path = Path(source)
    home = src.get("home")
    for base, here in ((src.get("config_dir"), paths.config_dir()),
                       (src.get("data_dir"), paths.data_dir()),
                       (home and str(Path(home) / ".ssh"), Path.home() / ".ssh")):
        if not base:
            continue
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        if ".." in rel.parts or not rel.parts:
            break
        return here / rel
    return Path.home() / ".ssh" / path.name


def placement(manifest: dict[str, Any], store) -> dict[str, Path]:
    """Where each member goes on this computer (members not in it: secrets.json)."""
    src = manifest["source"]
    out: dict[str, Path] = {"config/ships.toml": store.path}
    taken: set[str] = set()
    for k in manifest["keys"]:
        if k["role"] == "agent":
            name, n = k["name"], 2
            while name in taken:
                stem, dot, ext = k["name"].partition(".")
                name, n = f"{stem}-{n}{dot}{ext}", n + 1
            taken.add(name)
            out[k["member"]] = paths.config_dir() / AGENT_KEYS_DIR / name
        else:
            target = _target_key_path(k["source"], src)
            out[f"keys/{k['role']}"] = target
            out[f"keys/{k['role']}.pub"] = target.with_name(target.name + ".pub")
    for f in manifest["files"]:
        member = f["member"]
        if member.startswith("data/"):
            out[member] = paths.data_dir() / member[5:]
        elif member.startswith("images/"):
            out[member] = paths.data_dir() / "images" / member[7:]
    return out


def clashes(manifest: dict[str, Any], where: dict[str, Path],
            merged: frozenset[Path] = frozenset()) -> tuple[list[str], set[str]]:
    """Members whose place here is taken by a different file (refused), and members
    already here byte for byte (a key you copied before: left as it is). merged: places
    whose file here is kept beside the one that comes (a setup-only audit log)."""
    sha = {f["member"]: f["sha256"] for f in manifest["files"]}
    refused, same = [], set()
    by_target: dict[Path, str] = {}
    for member, target in where.items():
        other = by_target.setdefault(target, member)
        if other != member and sha.get(other) != sha.get(member):
            refused.append(f"{paths.tilde(target)} would receive two different files")
        if target in merged:
            continue
        if target.exists() or target.is_symlink():
            if target.is_file() and not target.is_symlink() and \
                    _sha256_file(target)[0] == sha.get(member):
                same.add(member)
            else:
                refused.append(f"{paths.tilde(target)} is already here, and different")
    return refused, same


def _invariant(doc: dict[str, Any]) -> dict[str, Any]:
    """The inventory without what receive rewrites: every Ship, pin, machine, issued
    certificate, revocation and setting must come through unchanged."""
    doc = copy.deepcopy(doc)
    fleet = doc.get("fleet", {})
    for key in ("admin_key", "agent_keys", "known_hosts", "managed_keys"):
        fleet.pop(key, None)
    for key in ("host_key", "user_key"):
        doc.get("ca", {}).pop(key, None)
    for key in ("token_store", "keyring_item", "token_file"):
        doc.get("drivers", {}).get("proxmox", {}).pop(key, None)
    for key in ("secret_store", "keyring_item", "oauth_secret_file"):
        doc.get("overlay", {}).get("api", {}).pop(key, None)
    for entry in doc.get("image", {}).values():
        entry.pop("file", None)
    return doc


def _set(table: Any, key: str, value: Any) -> None:
    if value is None:
        if key in table:
            del table[key]
    else:
        table[key] = value


def rewrite(text: str, manifest: dict[str, Any], where: dict[str, Path], store_kind: str,
            secrets: dict[str, Any]) -> tuple[str, list[tuple[str, str, str]]]:
    """ships.toml for this computer: the key paths, secret stores and image files.
    Returns the text and the secrets to store: (kind, item or path, value)."""
    import tomlkit

    from fleetledger import overlay_setup, proxmox_setup
    from fleetledger import store as store_mod
    from fleetledger.overlay import secret_store

    doc = tomlkit.parse(text)
    fleet = doc["fleet"]
    roles = {k["role"]: k for k in manifest["keys"] if k["role"] != "agent"}
    fleet["admin_key"] = paths.tilde(where["keys/admin"])
    fleet["agent_keys"] = [paths.tilde(where[k["member"]]) for k in manifest["keys"]
                           if k["role"] == "agent"]
    _set(fleet, "known_hosts", None)  # rebuilt from the pins, in this computer's folder
    to_store: list[tuple[str, str, str]] = []
    passphrases = secrets.get("passphrases") or {}
    managed = [role for role, k in roles.items() if k.get("managed")]
    if managed:
        table = tomlkit.table()
        table["store"] = store_kind
        written = []
        for role in managed:
            target = where[f"keys/{role}"]
            if paths.tilde(target) not in written:
                written.append(paths.tilde(target))
                value = passphrases.get(role)
                if not isinstance(value, str):
                    raise FleetError(f"The stream has no passphrase for the {KEY_ROLES[role]}; "
                                     "nothing was written.")
                to_store.append(("passphrase", str(target), value))
        table["keys"] = written
        fleet["managed_keys"] = table
    else:
        _set(fleet, "managed_keys", None)
    if "ca" in doc:
        doc["ca"]["host_key"] = paths.tilde(where["keys/host-ca"])
        doc["ca"]["user_key"] = paths.tilde(where["keys/user-ca"])
    proxmox = doc.get("drivers", {}).get("proxmox")
    if proxmox is not None:
        value = secrets.get("proxmox_token")
        if not isinstance(value, str):
            raise FleetError("The stream has no Proxmox token; nothing was written.")
        if store_kind == "keyring":
            item = secret_store.proxmox_item(str(proxmox["token_id"]))
            _set(proxmox, "token_store", "keyring")
            _set(proxmox, "keyring_item", item)
            _set(proxmox, "token_file", None)
            to_store.append(("keyring", item, value))
        else:
            path = paths.config_dir() / proxmox_setup.SECRET_FILE
            _set(proxmox, "token_store", None)
            _set(proxmox, "keyring_item", None)
            _set(proxmox, "token_file", paths.tilde(path))
            to_store.append(("file", str(path), value))
    api = doc.get("overlay", {}).get("api")
    if api is not None:
        value = secrets.get("oauth_secret")
        if not isinstance(value, str):
            raise FleetError("The stream has no OAuth secret; nothing was written.")
        if store_kind == "keyring":
            item = secret_store.oauth_item(str(api["oauth_client_id"]))
            _set(api, "secret_store", "keyring")
            _set(api, "keyring_item", item)
            _set(api, "oauth_secret_file", None)
            to_store.append(("keyring", item, value))
        else:
            path = paths.config_dir() / overlay_setup.SECRET_FILE
            _set(api, "secret_store", "file")
            _set(api, "keyring_item", None)
            _set(api, "oauth_secret_file", paths.tilde(path))
            to_store.append(("file", str(path), value))
    for entry in doc.get("image", {}).values():
        if "file" in entry:
            name = Path(str(entry["file"])).name
            member = f"images/{name}"
            if member in where:
                entry["file"] = paths.tilde(where[member])
            else:
                del entry["file"]  # it stayed on the other computer
    out = tomlkit.dumps(doc)
    plain = tomllib.loads(out)
    if _invariant(plain) != _invariant(tomllib.loads(text)):
        raise FleetError("Rewriting ships.toml for this computer changed more than its paths "
                         "and stores (a FleetLedger bug); nothing was written.")
    store_mod.validate(plain, "the received ships.toml")
    for _kind, _where, value in to_store:
        if not value or not value.isprintable() or len(value) > MAX_SECRET:
            raise FleetError("A secret in the stream is not one FleetLedger keeps; nothing was "
                             "written.")
    return out, to_store


def _check_commit(app, manifest: dict[str, Any]) -> None:
    wanted = str(manifest["source"].get("commit", ""))
    repo = app.repo_factory()
    git = repo.git_state(*CHECKED_PATHS)
    if git.commit != wanted:
        raise FleetError(
            f"This computer's ShipShape checkout is at {git.commit[:12]}, the fleet comes from "
            f"{wanted[:12]}: both must run the same commit (update packs ship/ from it, so an "
            "older one would roll Ships back). Nothing was written.",
            hint=f"git -C {repo.root} fetch && git -C {repo.root} checkout {wanted[:12]}, "
            "then fleetledger/install.sh, then try again.")
    if git.dirty:
        raise FleetError(f"{repo.root} has uncommitted changes (ship/, versions.toml, contracts/ "
                         "or fleetledger/); nothing was written.",
                         hint="Commit or stash them, then try again.")


def _read_members(tar: tarfile.TarFile, manifest: dict[str, Any], staging: Path
                  ) -> tuple[dict[str, Path], dict[str, Any]]:
    expected = {f["member"]: f for f in manifest["files"]}
    staged: dict[str, Path] = {}
    secrets: dict[str, Any] | None = None
    n = 0
    while (member := tar.next()) is not None:
        n += 1
        entry = expected.get(member.name)
        if entry is None or member.name in staged or (member.name == "secrets.json"
                                                      and secrets is not None):
            raise FleetError(f"The stream holds {member.name!r}, which its manifest does not "
                             "list; nothing was written.")
        if not member.isfile() or member.size != entry["size"]:
            raise FleetError(f"{member.name} in the stream is not what its manifest says; "
                             "nothing was written.")
        fh = tar.extractfile(member)
        if member.name == "secrets.json":
            data = fh.read(MAX_SECRETS + 1)
            if len(data) > MAX_SECRETS or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise FleetError("The stream's secrets do not match its manifest; nothing was "
                                 "written.")
            try:
                secrets = json.loads(data)
            except ValueError:
                secrets = None
            if not isinstance(secrets, dict):
                raise FleetError("The stream's secrets are not what FleetLedger sends; nothing "
                                 "was written.")
            continue
        path = staging / f"{n:04d}"
        digest = hashlib.sha256()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            while chunk := fh.read(CHUNK):
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != entry["sha256"]:
            raise FleetError(f"{member.name} arrived different from what was sent (its SHA-256 "
                             "does not match); nothing was written.")
        staged[member.name] = path
    missing = sorted(set(expected) - set(staged) - {"secrets.json"})
    if missing or secrets is None:
        raise FleetError("The stream ended early (missing: " + ", ".join(
            missing or ["secrets.json"]) + "); nothing was written.",
                         hint="Run the move again.")
    return staged, secrets


def _store_secret(kind: str, where: str, value: str, store_kind: str,
                  undo: list[Callable[[], None]]) -> None:
    from fleetledger.overlay import secret_store

    def keyring(item: str, store: Callable[[], None]) -> None:
        had = secret_store.existing_in_keyring(item)
        store()
        if had is None:
            undo.append(lambda: secret_store.delete_from_keyring(item))
        elif had != value:  # an older item of the same name: put it back on failure
            undo.append(lambda: secret_store.store_in_keyring(item, had))

    if kind == "passphrase":
        target = Path(where)
        if store_kind == "keyring":
            keyring(keys.item(target), lambda: keys.store_passphrase(target, value, "keyring"))
        else:
            path = keys.passphrase_file(target)
            keys.store_passphrase(target, value, "file")
            undo.append(lambda: path.unlink(missing_ok=True))
    elif kind == "keyring":
        keyring(where, lambda: secret_store.store_in_keyring(where, value))
    else:
        path = Path(where)
        secret_store.write_file(path, value, kind)
        undo.append(lambda: path.unlink(missing_ok=True))


def _install(staged: dict[str, Path], where: dict[str, Path], same: set[str],
             text: str, to_store: list[tuple[str, str, str]], store_kind: str,
             store, standby: handover.Marker, kept: dict[Path, bytes] | None = None
             ) -> list[Path]:
    """Put everything in place: the standby marker before ships.toml (so the copy is
    never active by accident), ships.toml last. Anything going wrong takes back what
    this receive wrote, so the computer is left as it was. kept: files here whose bytes
    go after the incoming file's (clashes' merged)."""
    kept = kept or {}
    merge: list[tuple[Path, Path]] = []
    undo: list[Callable[[], None]] = []
    written: list[Path] = []
    placed: set[Path] = set()
    paths.private_dir(paths.config_dir())
    paths.private_dir(paths.data_dir())
    try:
        for member, path in staged.items():
            if member == "config/ships.toml" or member in same:
                continue
            target = where[member]
            if target in kept:
                merge.append((target, path))  # last but one: fewer failures to take back
                continue
            if target.exists() or target.is_symlink():
                if target in placed:
                    continue  # the same file again (two key roles sharing one key)
                raise FleetError(f"{paths.tilde(target)} appeared while receiving; nothing "
                                 "was written.")  # clashes() saw nothing there
            placed.add(target)
            if member.startswith("keys/") and target.parent != paths.config_dir() / AGENT_KEYS_DIR:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # e.g. ~/.ssh
            else:
                paths.private_dir(target.parent)
            if member.startswith("keys/"):
                paths.write_private(target, path.read_bytes())
                if member.endswith(".pub"):
                    os.chmod(target, 0o644)
            else:
                shutil.move(str(path), str(target))
                os.chmod(target, 0o600)
            undo.append(lambda t=target: t.unlink(missing_ok=True))
            written.append(target)
        for kind, place, value in to_store:
            _store_secret(kind, place, value, store_kind, undo)
        for target, path in merge:  # this computer's own setup entries, after the fleet's
            came = path.read_bytes()
            undo.append(lambda t=target: paths.write_private(t, kept[t]))
            paths.write_private(target, came + (b"" if came.endswith(b"\n") or not came
                                                else b"\n") + kept[target])
            written.append(target)
        paths.private_dir(store.path.parent)
        handover.write(store, standby)
        undo.append(lambda: handover.clear(store, "standby"))
        paths.write_private(store.path, text.encode("utf-8"))
        handover.clear(store, "retired")  # a wiped copy's marker: this one is newer
    except BaseException:
        for step in reversed(undo):
            try:
                step()
            except Exception:  # noqa: BLE001 - undo as much as possible
                pass
        raise
    return written


def receive(app, source: BinaryIO, *, store_kind: str | None = None,
            pushed: bool = False) -> int:
    """Take a fleet from the stream on source and keep it on standby (it does not act
    until activated). Returns VERIFIED, or UNVERIFIED when it was written but a check
    here failed (fix it, then ship-fleet migrate verify).

    Onto a computer with no FleetLedger, or with one on standby (replaced: it never
    acted), or with only the marker a wiped copy left, which refuses a fleet older
    than the one it handed on."""
    store = app.store_factory()
    here = handover.marker(store)
    if here is not None and here.role == "unreadable":
        raise handover.banner(here)
    if here is not None and here.role == "retired" and not here.wiped:
        raise FleetError(
            f"This computer has a retired FleetLedger (it handed the fleet to "
            f"{here.other or 'another computer'} on {here.when[:10]}); migrate never writes "
            "over one. Nothing was written.",
            hint="To bring the fleet back here with what changed since: ship-fleet migrate wipe "
            "here first. To roll back without those changes: ship-fleet migrate retire there, "
            "then ship-fleet migrate unretire here.")
    replacing = here is not None and here.role == "standby"
    refuse_existing(store, here)
    kind = choose_store(store_kind)
    with inuse.exclusive(store):
        refuse_existing(store, here)  # again: nothing can change it from here on
        for old in paths.data_dir().glob(STAGING + "*"):
            shutil.rmtree(old, ignore_errors=True)  # a receive cut short before
        try:
            tar = tarfile.open(fileobj=source, mode="r|")
            first = tar.next()
        except tarfile.TarError:
            raise FleetError("The input is not a FleetLedger move (ship-fleet migrate send); "
                             "nothing was written.") from None
        if first is None or first.name != "manifest.json":
            raise FleetError("The input is not a FleetLedger move (ship-fleet migrate send); "
                             "nothing was written.")
        try:
            manifest = check_manifest(_read_json(tar, first, MAX_MANIFEST)[0])
        except (tarfile.TarError, OSError) as exc:
            raise FleetError(f"The stream broke off ({type(exc).__name__}); nothing was "
                             "written.", hint="Run the move again.") from None
        if here is not None and here.role == "retired" and manifest["epoch"] < here.epoch:
            raise FleetError(
                f"This computer handed the fleet on at epoch {here.epoch}, and the fleet coming "
                f"in is from epoch {manifest['epoch']}: an older state. Nothing was written.",
                hint="Move the active FleetLedger's fleet here, not an older copy or backup.")
        _check_commit(app, manifest)
        if replacing:
            app.say(f"Replacing the copy on standby here (from {here.other or 'another computer'}"
                    f", received {here.when[:10]}; it never acted on the fleet).")
            handover.wipe(store, handover.wipe_plan(store)[0])
            handover.clear(store, "standby")
        where = placement(manifest, store)
        log = paths.audit_log_path()
        kept = {log: log.read_bytes()} if setup_only_log(log) else {}
        refused, same = clashes(manifest, where, frozenset(kept))
        if refused:
            raise FleetError("Not writing over files already here: " + "; ".join(refused) + ".",
                             hint="These belong to something else (another fleet?): move them "
                             "aside, then try again. Nothing was written.")
        src = manifest["source"]
        app.say(f"Receiving the fleet from {src.get('host', '?')} (commit "
                f"{str(src.get('commit'))[:12]}): {manifest.get('summary', '')}.")
        staging = paths.private_dir(paths.data_dir() / f"{STAGING}{os.getpid()}")
        try:
            try:
                staged, secrets = _read_members(tar, manifest, staging)
            except (tarfile.TarError, OSError) as exc:
                raise FleetError(f"The stream broke off ({type(exc).__name__}); nothing was "
                                 "written.", hint="Run the move again.") from None
            text = staged["config/ships.toml"].read_text(encoding="utf-8")
            new_text, to_store = rewrite(text, manifest, where, kind, secrets)
            del secrets
            standby = handover.Marker("standby", handover.now(), src.get("host"),
                                      manifest["epoch"] + 1)
            _install(staged, where, same, new_text, to_store, kind, store, standby, kept)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    inv = store.load()
    _say_received(app, manifest, where, kind, inv)
    try:
        checks = verify(app, source_timezone=src.get("timezone"),
                        krl_sha256=manifest.get("krl_sha256"))
    except Exception as exc:  # noqa: BLE001 - the fleet is written: say so, whatever happened
        checks = [Check("fail", "verify", f"stopped with {type(exc).__name__}")]
    ok = report(app, checks)
    me = socket.gethostname().split(".")[0]
    by_hand = ("" if pushed else " It acts on the fleet once made active: on the old "
               f"computer, ship-fleet migrate retire --to {me}; then here, ship-fleet migrate "
               "activate.")
    if ok:
        unfinished = [c.name for c in checks if c.state == "warn"
                      and c.detail.startswith(f"{HALF_MADE}, ")]  # _ship_checks' own
        reached = ("reached every enrolled Ship (not " + ", ".join(unfinished)
                   + ": still being made, WARN above)" if unfinished else "reached every Ship")
        app.say(f"\nThis computer has the fleet on standby and {reached}; nothing on any Ship "
                "changed." + by_hand)
        return VERIFIED
    app.say("\nThe fleet is here on standby, but not everything checks out yet (FAIL above). "
            "Fix that, then run ship-fleet migrate verify." + by_hand)
    return UNVERIFIED


def _say_received(app, manifest: dict[str, Any], where: dict[str, Path], kind: str,
                  inv) -> None:
    from fleetledger.commands import table

    rows = []
    for k in manifest["keys"]:
        member = k["member"] if k["role"] == "agent" else f"keys/{k['role']}"
        label = KEY_ROLES.get(k["role"], "your key for Ships (public half)")
        rows.append([label, paths.tilde(where[member])])
    if manifest.get("secrets"):
        where_kept = keys.describe(kind) if kind == "file" else \
            "your OS keyring (service fleetledger)"
        rows.append(["secrets", ", ".join(manifest["secrets"]) + " in " + where_kept])
    data = [f for f in manifest["files"] if f["member"].startswith(("data/", "images/"))]
    rows.append(["history and caches", f"{len(data)} file(s) in {paths.tilde(paths.data_dir())}"])
    rows.append(["inventory", f"{paths.tilde(where['config/ships.toml'])}: {summary(inv)}"])
    app.say(table(rows, ["WHAT", "NOW AT"]))
    if manifest["left_out"]:
        app.say(f"\nStayed on {manifest['source'].get('host', 'the other computer')}:")
        for path, why in manifest["left_out"]:
            app.say(f"  {path}: {why}")
    for note in manifest.get("notes") or []:
        app.say(f"Note: {note}")
    remote_ui = manifest.get("remote_ui")
    if remote_ui:
        logins = "".join(f" --login {shlex.quote(x)}" for x in remote_ui["logins"])
        app.say("Note: the web UI was open to your other devices there (Tailscale Serve). It "
                "stays off here until you turn it on: tailscale serve --bg "
                f"127.0.0.1:{remote_ui['port']}, then ship-fleet ui remote https://<this "
                f"computer's name> --port {remote_ui['port']}{logins} (docs/runbooks/"
                "remote-ui.md). Each device pairs again.")


# --- verify (the new computer) -----------------------------------------------------------


@dataclass(frozen=True)
class Check:
    state: str  # ok | warn | fail
    name: str
    detail: str = ""


def verify(app, *, source_timezone: str | None = None,
           krl_sha256: str | None = None) -> list[Check]:
    """Does the fleet work from here? Every key opens, doctor, the Proxmox token through
    its pin, the OAuth client's scopes, and a read-only status of every Ship with its
    certificate check. Changes nothing on any Ship."""
    from fleetledger import firstrun, status
    from fleetledger.seed.shiptoml import local_timezone

    store = app.store_factory()
    inv = store.load()
    checks: list[Check] = []
    for item in firstrun.everything(app, offline=True):
        if item.state in ("fail", "missing", "warn") and item.name != "this copy":
            state = "fail" if item.state == "fail" else "warn"
            checks.append(Check(state, f"doctor: {item.area} {item.name}",
                                item.detail + (f" (next: {item.next})" if item.next else "")))
    checks += _key_checks(inv)
    checks += _proxmox_check(app, inv)
    checks += _oauth_check(app, inv)
    cfg = ca.config(inv)
    krl = ca.cached_krl(inv) if cfg is not None else None
    if krl_sha256 is not None:
        same = krl is not None and hashlib.sha256(krl).hexdigest() == krl_sha256
        checks.append(Check("ok" if same else "fail", "revocation list",
                            "the same bytes as on the old computer" if same else
                            "differs from the old computer's (a FleetLedger bug)"))
    ships = inv.ships()
    if ships:
        try:
            ssh = app.ssh_factory(inv)
            views = status.poll(ssh, inv, ships)
        except FleetError as exc:
            checks.append(Check("fail", "reach the Ships", exc.message))
            views = []
        for view in views:
            checks += _ship_checks(cfg, view, krl)
    if source_timezone and source_timezone != local_timezone():
        checks.append(Check("warn", "time zone",
                            f"new Ships get this computer's ({local_timezone()}), not "
                            f"{source_timezone} as before (or ship-fleet new --timezone)"))
    if not any(c.state == "fail" for c in checks):
        checks.insert(0, Check("ok", "doctor", "no failures"))
    return checks


def _key_checks(inv) -> list[Check]:
    out = []
    store, managed = keys.managed(inv)
    managed_set = {k.resolve() for k in managed}
    named = [("fleet admin key", inv.admin_key)]
    cfg = ca.config(inv)
    if cfg is not None:
        named += [("host CA key", cfg.host_key), ("user CA key", cfg.user_key)]
    for label, path in named:
        try:
            if path.resolve() in managed_set:
                keys.check_opens(path, keys.read_passphrase(path, store))
                out.append(Check("ok", label, f"opens with its passphrase from "
                                 f"{keys.describe(store)}"))
            elif keys.needs_passphrase(path):
                out.append(Check("warn", label, "has a passphrase of your own: load it into "
                                 f"ssh-agent here (ssh-add {paths.tilde(path)})"))
            else:
                keys.check_opens(path, "")
                out.append(Check("ok", label, "opens (no passphrase)"))
        except FleetError as exc:
            out.append(Check("fail", label, exc.message))
    return out


def _proxmox_check(app, inv) -> list[Check]:
    config = inv.driver_config("proxmox")
    if not config:
        return []
    from fleetledger.drivers import proxmox_api

    try:
        proxmox_api.token_secret(config)
        factory = getattr(app, "proxmox_api", None)
        api = factory(config) if factory is not None else proxmox_api.ProxmoxAPI(config)
        version = api.get("/version") or {}
    except FleetError as exc:
        return [Check("fail", "Proxmox token", exc.message)]
    return [Check("ok", "Proxmox token", f"works through the pinned certificate (Proxmox VE "
                  f"{version.get('version', '?')})")]


def _oauth_check(app, inv) -> list[Check]:
    config = inv.overlay or {}
    if config.get("key_source") != "tailscale-api":
        return []
    from fleetledger import overlay_setup

    api_cfg = dict(config["api"])
    level = "policy" if api_cfg.get("manage_policy") else (
        "devices" if api_cfg.get("remove_devices") else "keys")
    try:
        api = overlay_setup._api(app, api_cfg)
        api.token()
    except FleetError as exc:
        return [Check("fail", "OAuth client", exc.message)]
    granted = getattr(api, "granted", None)
    if granted is None:
        return [Check("ok", "OAuth client", "signs in (its scopes are not listed)")]
    missing = overlay_setup.missing_scopes(overlay_setup.LEVELS[level][0], granted)
    if missing:
        return [Check("fail", "OAuth client", f"lacks the scopes: {', '.join(missing)}")]
    return [Check("ok", "OAuth client", f"signs in with the scopes the {level} level needs")]


def _ship_checks(cfg, view, krl: bytes | None) -> list[Check]:
    name = view.ship.name
    if view.status is None and view.unenrolled and view.problem in (None, "unreachable"):
        # Half-made when the fleet left (a backup takes it along): it may never have come
        # up, or still be provisioning, and the copy must still become active to finish
        # or destroy it. Anything else (another host key, another identity, an error) is
        # still a failure: it says something is wrong, not just unfinished.
        how = "not answering" if view.problem == "unreachable" else "still provisioning"
        return [Check("warn", name, f"{HALF_MADE}, {how} ({view.message or view.state}): "
                                    f"once this copy is active, finish it (ship-fleet status "
                                    f"{name} --wait) or destroy it")]
    if view.status is None:
        return [Check("fail", name, f"no status: {view.message or view.state}")]
    out = [Check("ok", name, f"answers on its pinned host key ({view.host})")]
    if cfg is None:
        return out
    try:
        state = ca.judge(cfg, view.ship, view.status, krl)
    except FleetError as exc:
        return [*out, Check("warn", f"{name} certificate", exc.message)]
    if not state.supported:
        return out
    if state.needs_cert:
        out.append(Check("warn", f"{name} certificate",
                         f"{state.reason}: ship-fleet ca sign {name}"))
    if state.trust_behind:
        out.append(Check("warn", f"{name} SSH trust",
                         "its user CA keys or revocation list differ from this copy's: "
                         f"ship-fleet ca sign {name}"))
    if not state.needs_cert and not state.trust_behind:
        out.append(Check("ok", f"{name} certificate", "signed by this CA; same revocation list"))
    return out


def report(app, checks: list[Check]) -> bool:
    from fleetledger.commands import table

    app.say("\nChecking it works from here (read-only: nothing changes on any Ship):")
    rows = [[{"ok": "ok", "warn": "WARN", "fail": "FAIL"}[c.state], c.name, c.detail]
            for c in checks]
    app.say(table(rows, ["", "CHECK", "DETAIL"]))
    return not any(c.state == "fail" for c in checks)


# --- the command --------------------------------------------------------------------------


ACTIONS = ("check", "push", "send", "receive", "verify", "activate", "retire", "unretire",
           "wipe", "import")


def add_parsers(sub, add) -> None:
    import argparse

    p = add(sub, "migrate", "move FleetLedger to another computer, with every key, secret and Ship",
            "Move this FleetLedger, with everything it needs, to another computer: the\n"
            "inventory (every Ship and pin, trusted machine, issued certificate and\n"
            "revocation), the fleet admin key and the CA keys, the secrets (stored again\n"
            "in that computer's own keyring or files) and the history. The Ships keep\n"
            "working from there with nothing changed on them.\n\n"
            "Only one copy acts on the fleet: the new one waits on standby until it has\n"
            "checked every Ship; then this one retires (it refuses to act, and keeps its\n"
            "files for a rollback) and the new one takes over with the next epoch.\n\n"
            "  ship-fleet migrate check                 what would move, and what stops it\n"
            "  ship-fleet migrate push you@new-computer move it there over SSH, then hand over\n"
            "  ssh old-computer '~/.local/bin/ship-fleet migrate send' | "
            "ship-fleet migrate receive\n"
            "                                           the same, run on the new computer\n"
            "  ship-fleet migrate verify                on the new computer: check it works\n"
            "  ship-fleet migrate retire, activate      hand over by hand: old, then new\n"
            "  ship-fleet migrate unretire              roll back: this copy acts again\n"
            "  ship-fleet migrate wipe                  after two weeks: delete a retired copy\n\n"
            "See docs/runbooks/move-fleetledger.md.")
    p.add_argument("action", choices=ACTIONS)
    p.add_argument("host", nargs="?", metavar="[USER@]HOST|FILE",
                   help="push: the new computer; import: the backup file (ship-fleet backup)")
    p.add_argument("--recovery", action="store_true",
                   help="import: open the backup with the vault's recovery code")
    p.add_argument("--store", choices=STORES,
                   help="push/receive: where the new computer keeps the secrets (default: its "
                   "keyring when one is unlocked there, else 0600 files)")
    p.add_argument("--with-images", action="store_true",
                   help="also move baked image files (large)")
    p.add_argument("-o", "--ssh-option", action="append", default=[], metavar="OPTION",
                   help="push: an ssh -o option, e.g. Port=2222 (repeatable)")
    p.add_argument("--ship-fleet", default="ship-fleet", metavar="PATH",
                   help="push: ship-fleet on the new computer (default: on its PATH, "
                   "else ~/.local/bin)")
    p.add_argument("--no-cutover", action="store_true",
                   help="push: leave the new copy on standby; this one stays the active one")
    p.add_argument("--to", metavar="HOST",
                   help="retire: where the fleet is managed from now on (said to anyone who "
                   "runs this copy)")
    p.add_argument("--now", action="store_true",
                   help="wipe: do not wait the two weeks a retired copy is kept for a rollback")
    p.add_argument("--yes", action="store_true",
                   help="activate, retire, unretire, wipe: do not ask first")
    p.add_argument("--pushed", action="store_true", help=argparse.SUPPRESS)  # push runs receive


def cmd_migrate(args, app) -> int:
    action = args.action
    if action == "import":
        return import_backup(app, args.host, store_kind=args.store, recovery=args.recovery)
    if action != "push" and args.host:
        raise FleetError(f"migrate {action} takes no host.")
    if action == "check":
        plan = check(app, with_images=args.with_images)
        print_plan(app.say, plan)
        if plan.blockers:
            return 1
        app.say("\nReady to move: ship-fleet migrate push [USER@]HOST.")
        return 0
    if action == "push":
        if not args.host:
            raise FleetError("migrate push needs the new computer: ship-fleet migrate push "
                             "[USER@]HOST.")
        return push(app, args.host, store_kind=args.store, with_images=args.with_images,
                    options=args.ssh_option, ship_fleet=args.ship_fleet,
                    cutover=not args.no_cutover)
    if action == "send":
        out = getattr(app, "stdout_bytes", None) or sys.stdout.buffer
        _pipe_only(out)
        try:
            plan = send(app, out, with_images=args.with_images)
        except (BrokenPipeError, ConnectionResetError):
            app.note("The other side stopped reading (its own output says why). Nothing on "
                     "this computer changed.")
            return 1
        app.note(f"Sent: {summary(plan.inventory)}. Nothing on this computer changed.")
        return 0
    if action == "receive":
        source = getattr(app, "stdin_bytes", None)
        if source is None:
            if app.stdin_isatty():
                raise FleetError("receive reads the fleet from its input: run it at the end of "
                                 "a pipe (ssh OLD '~/.local/bin/ship-fleet migrate send' | "
                                 "ship-fleet migrate receive), or use migrate push from the old "
                                 "computer.")
            source = sys.stdin.buffer
        return receive(app, source, store_kind=args.store, pushed=args.pushed)
    if action == "verify":
        return 0 if report(app, verify(app)) else 1
    return {"activate": _activate, "retire": _retire, "unretire": _unretire,
            "wipe": _wipe}[action](args, app)


def import_backup(app, file: str | None, *, store_kind: str | None = None,
                  recovery: bool = False) -> int:
    """Restore a backup (ship-fleet backup): open it with the vault's passphrase or
    recovery code as they were when it was made, then take the fleet in as receive
    does (on standby, every Ship checked; migrate activate makes it the active copy)."""
    import getpass

    from fleetledger import backup

    if not file:
        raise FleetError("migrate import needs the backup file: ship-fleet migrate import FILE.")
    path = paths.expand(file)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise FleetError(f"Cannot read {path}: {exc.strerror}.") from None
    ask = getattr(app, "ask_secret", None)
    if ask is None:
        if not app.stdin_isatty():
            raise FleetError("migrate import asks for the vault's passphrase: run it in a "
                             "terminal.")
        ask = lambda prompt: getpass.getpass(prompt, stream=sys.stderr)  # noqa: E731
    secret = ask("The backup's recovery code: " if recovery
                 else "The vault passphrase the backup was made with (not shown): ")
    stream, header = backup.open_backup(data, secret, recovery=recovery)
    app.say(f"Opened the backup of {header.get('fleet', 'a fleet')} made {header.get('made')} "
            f"(epoch {header.get('epoch')}, commit {str(header.get('commit'))[:12]}).")
    code = receive(app, io.BytesIO(stream), store_kind=store_kind)
    from fleetledger.overlay import secret_store

    store = app.store_factory()
    if store.exists() and not secret_store.vault_on() and \
            keys.managed(store.load())[0] == "file":
        app.note("Note: the secrets went to 0600 files here. On an always-on computer, turn "
                 "the vault on (ship-fleet vault init moves them into it), as before the "
                 "restore would have been best (docs/runbooks/fleetledger-vm.md).")
    return code


# --- handing over: activate, retire, unretire, wipe -------------------------------------------


def _activate(args, app) -> int:
    store = app.store_factory()
    with inuse.exclusive(store):
        m = handover.marker(store)
        if m is not None and m.role == "unreadable":
            raise handover.banner(m)
        if m is None or m.role != "standby":
            raise FleetError("This FleetLedger is not on standby: there is nothing to activate.")
        handover.confirm(app, f"Make this copy the active FleetLedger (epoch {m.epoch})? Only "
                         f"once the copy on {m.other or 'the old computer'} is retired "
                         "(ship-fleet migrate retire there): two active copies undo each "
                         "other's changes.", "yes", args.yes)
        if not report(app, verify(app)):
            raise FleetError("Not made active: a check failed (FAIL above). Fix it, then try "
                             "again.")
        epoch = handover.activate(store)
    app.say(f"\nThis FleetLedger is now the active one (epoch {epoch}).")
    return 0


def _retire(args, app) -> int:
    if args.to and not HOST_RE.match(args.to):
        raise FleetError(f"--to: '{args.to}' is not [USER@]HOST.")
    store = app.store_factory()
    with inuse.exclusive(store):
        m = handover.marker(store)
        if m is not None and m.role == "standby":
            raise FleetError("This copy is on standby: it never acted on the fleet, so there is "
                             "nothing to retire.", hint="To drop it: ship-fleet migrate wipe.")
        if m is not None:
            raise handover.banner(m)
        inv = store.load()
        handover.confirm(app, "This FleetLedger stops acting on the fleet" + (
            f", which is managed at {args.to} from now on" if args.to else "") +
            "; its files stay, for a rollback.", "retire", args.yes)
        handover.retire(store, args.to, handover.epoch(inv) + 1)
    app.say("This FleetLedger is retired: it refuses to act on the fleet. To act again: "
            "ship-fleet migrate unretire. After two weeks, delete its keys and secrets: "
            "ship-fleet migrate wipe.")
    return 0


def _unretire(args, app) -> int:
    store = app.store_factory()
    with inuse.exclusive(store):
        m = handover.marker(store)
        if m is not None and m.role == "unreadable":
            raise handover.banner(m)
        if m is None or m.role != "retired":
            raise FleetError("This FleetLedger is not retired.")
        if m.wiped:
            raise handover.banner(m)
        handover.confirm(
            app, f"This copy acts on the fleet again. Retire the copy at "
            f"{m.other or 'the other computer'} first (ship-fleet migrate retire there): two "
            "active copies undo each other's changes. What it changed since does not come back "
            "here; to bring that too, wipe this copy and move the fleet back from there "
            "instead.", "unretire", args.yes)
        epoch = handover.unretire(store)
    app.say(f"This FleetLedger acts on the fleet again (epoch {epoch}).")
    return 0


def _wipe(args, app) -> int:
    import dataclasses

    from fleetledger.commands import table

    store = app.store_factory()
    with inuse.exclusive(store):
        m = handover.marker(store)
        if m is not None and m.role == "unreadable":
            raise handover.banner(m)
        if m is None:
            raise FleetError("This FleetLedger is the active one: wipe deletes only a retired "
                             "copy (or one on standby).",
                             hint="To stop using it, move the fleet first (ship-fleet migrate "
                             "push), or ship-fleet migrate retire.")
        if m.wiped:
            raise FleetError(f"This copy was wiped already, on {m.wiped[:10]}.")
        if m.role == "retired" and m.age < handover.WIPE_AFTER and not args.now:
            left = (handover.WIPE_AFTER - m.age).days + 1
            raise FleetError(f"This copy retired on {m.when[:10]} and is kept two weeks for a "
                             f"rollback ({left} more day(s)).",
                             hint=f"Check the fleet works from {m.other or 'its new computer'}, "
                             "then wipe it now with --now.")
        gone, kept = handover.wipe_plan(store)
        app.say("Deletes:")
        app.say(table([[g.what, ("keyring item " if g.keyring else "") + (
            g.where if g.keyring else paths.tilde(g.where))] for g in gone], ["WHAT", "WHERE"]))
        if kept:
            app.say("\nKeeps:")
            app.say(table([list(k) for k in kept], ["FILE", "WHY"]))
        handover.confirm(app, "Delete all of that from this computer? It cannot be undone.",
                         "wipe", args.yes)
        handover.wipe(store, gone)
        if m.role == "retired":
            handover.write(store, dataclasses.replace(m, wiped=handover.now()))
        else:
            handover.clear(store, "standby")
    app.say("\nWiped. " + ("Only a small marker stays, so this computer refuses an older copy "
                           "of the fleet." if m.role == "retired" else ""))
    return 0
