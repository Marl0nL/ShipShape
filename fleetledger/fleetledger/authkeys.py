"""The trusted machines' bundle keys in FleetLedger's own ~/.ssh/authorized_keys.

On FleetLedger's own VM (`[fleet] self` names this computer: firstrun.on_its_own_vm),
each trusted machine's key gets one line, inside one marked block that FleetLedger
alone writes:

    restrict,command="/home/fleetledger/.local/bin/ship-fleet machine-bundle laptop" \
        ssh-ed25519 AAAA... laptop          (one line; split here to fit)

`restrict` turns off everything a key line can allow (a terminal, port, agent and X11
forwarding, ~/.ssh/rc) and anything a later OpenSSH adds to that list; `command=` makes
sshd run that one command whatever the client asks for (a shell, scp, sftp, another
command). Nothing is turned back on. The command is the ship-fleet the VM's wrapper
runs (appliance.WRAPPER), by its full path, and the machine's own name, so the key can
fetch its own bundle (machinebundle.py) and nothing else.

What FleetLedger never does here:

- touch a line outside the block, or change the file's mode or owner (a line of yours
  stays byte for byte; a last line with no newline gets one before the block);
- add a line for a key that has a line of its own outside the block (in
  authorized_keys or authorized_keys2). sshd uses the first line that matches a key,
  so a restricted line above yours would take your key's shell away, and one below it
  would never be used: the key keeps exactly what your line gives it;
- remove a line inside the block it cannot tell is one of its own (a key no machine
  has): it refuses, and says to move that line out;
- add a line on a computer that is not FleetLedger's own VM (a desktop's
  authorized_keys is your own login's), or on a copy that is not the active one. It
  still removes lines there for machines no longer trusted, and a retired copy's
  block is emptied: granting less is always allowed.

It writes atomically (a temporary file beside it, then a rename), with ~/.ssh locked
against another ship-fleet, and gives up rather than write over a change made while
it worked. `ship-fleet trust --sync-keys` rewrites the block to match; `ship-fleet
doctor` checks it.
"""

from __future__ import annotations

import base64
import fcntl
import os
import re
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fleetledger.errors import FleetError

BEGIN = "# >>> FleetLedger: trusted machines' bundle keys (ship-fleet trust writes these) >>>"
END = "# <<< FleetLedger: trusted machines' bundle keys <<<"
OPTIONS = "restrict"
SYNC = "ship-fleet trust --sync-keys"
NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")  # trustcmd.MACHINE_RE, as fullmatch
BLOB_RE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
PATH_RE = re.compile(r"/[A-Za-z0-9._/-]+")
KEYTYPE_RE = re.compile(r"(ssh|ecdsa|sk)-[A-Za-z0-9@.-]+")
ED25519_PREFIX = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
OTHER_FILES = ("authorized_keys2",)  # sshd's default AuthorizedKeysFile reads it too
TRIES = 3  # another writer changed the file while we worked: read it again


def _wrapper_target() -> str:
    from fleetledger import appliance

    found = re.search(r'^exec (/\S+) "\$@"$', appliance.WRAPPER, re.M)
    if not found:  # a test of the seed's wrapper fails first
        raise FleetError("The appliance's ship-fleet wrapper names no ship-fleet to run.")
    return found.group(1)


# The ship-fleet a forced line runs: the one /usr/local/bin/ship-fleet runs on the VM.
SHIP_FLEET = _wrapper_target()
HOME: Path | None = None  # tests point this at a folder of their own


def ssh_dir() -> Path:
    return (HOME or Path.home()) / ".ssh"


def path() -> Path:
    return ssh_dir() / "authorized_keys"


# --- one line --------------------------------------------------------------------------------


def ed25519_blob(public_key: str) -> str | None:
    """The base64 of an ed25519 public key line, checked down to its bytes; None for
    anything else. Only this, never the rest of the line, goes into authorized_keys."""
    parts = str(public_key).split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519" or not BLOB_RE.fullmatch(parts[1]):
        return None
    try:
        raw = base64.b64decode(parts[1], validate=True)
    except ValueError:
        return None
    if len(raw) != len(ED25519_PREFIX) + 32 or not raw.startswith(ED25519_PREFIX):
        return None
    return parts[1]


def forced_command(name: str) -> str:
    return f"{SHIP_FLEET} machine-bundle {name}"


def line_for(name: str, public_key: str) -> str | None:
    """The one line for a machine, or None when its name or key is not one FleetLedger
    puts in authorized_keys (a name with anything but a-z, 0-9 and -; not ed25519)."""
    blob = ed25519_blob(public_key)
    if blob is None or not NAME_RE.fullmatch(name) or not PATH_RE.fullmatch(SHIP_FLEET):
        return None
    return f'{OPTIONS},command="{forced_command(name)}" ssh-ed25519 {blob} {name}'


def key_of(line: str) -> str | None:
    """The key (base64) a line of authorized_keys is for, read as sshd reads it: a key
    type first, or else options (skipped as sshd skips them, quotes and \\" included)
    and then the key. None: a comment, a blank line, or no key."""
    s = line.strip(" \t\r")
    if not s or s.startswith("#"):
        return None
    words = s.split()
    if KEYTYPE_RE.fullmatch(words[0]):
        return words[1] if len(words) > 1 else None
    i, quoted = 0, False
    while i < len(s) and (quoted or s[i] not in " \t"):
        if s[i] == "\\" and s[i + 1:i + 2] == '"':
            i += 1
        elif s[i] == '"':
            quoted = not quoted
        i += 1
    rest = s[i:].split()
    return rest[1] if len(rest) > 1 and KEYTYPE_RE.fullmatch(rest[0]) else None


# --- the file ---------------------------------------------------------------------------------


@dataclass
class File:
    path: Path
    raw: bytes | None  # None: there is no file
    lines: list[str]  # split on \n only, as sshd reads it
    begin: int | None = None  # index of the BEGIN line
    end: int | None = None
    broken: str | None = None  # the markers are not one BEGIN, then one END
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None
    symlink: bool = False
    outside: set[str] = field(default_factory=set)  # keys with a line of their own

    @property
    def block(self) -> list[tuple[int, str]]:
        if self.begin is None or self.end is None:
            return []
        return [(i, self.lines[i]) for i in range(self.begin + 1, self.end)]


def _is(line: str, marker: str) -> bool:
    return line.rstrip(" \t\r") == marker


def read() -> File:
    p = path()
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        f = File(p, None, [])
    except OSError as exc:
        raise FleetError(f"Cannot look at {p}: {exc.strerror}.") from exc
    else:
        if not os.path.isfile(p) or os.path.islink(p):
            f = File(p, None, [], symlink=os.path.islink(p), mode=st.st_mode, uid=st.st_uid)
            f.broken = f"{p} is not a plain file"
            return f
        try:
            raw = p.read_bytes()
        except OSError as exc:
            raise FleetError(f"Cannot read {p}: {exc.strerror}.") from exc
        f = File(p, raw, raw.decode("utf-8", "surrogateescape").split("\n"),
                 mode=st.st_mode & 0o7777, uid=st.st_uid, gid=st.st_gid)
    begins = [i for i, x in enumerate(f.lines) if _is(x, BEGIN)]
    ends = [i for i, x in enumerate(f.lines) if _is(x, END)]
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
            f.broken = (f"{p} has {len(begins)} start and {len(ends)} end marker line(s) of "
                        "FleetLedger's block, not one of each in that order")
        else:
            f.begin, f.end = begins[0], ends[0]
    inside = {i for i, _ in f.block}
    f.outside = {k for i, x in enumerate(f.lines)
                 if i not in inside and not _is(x, BEGIN) and not _is(x, END)
                 and (k := key_of(x))}
    for name in OTHER_FILES:
        try:
            text = (p.parent / name).read_text(encoding="utf-8", errors="surrogateescape")
        except FileNotFoundError:
            continue
        except OSError as exc:  # cannot tell what it allows: add nothing for anyone
            raise FleetError(f"Cannot read {p.parent / name}: {exc.strerror}.") from exc
        f.outside |= {k for x in text.split("\n") if (k := key_of(x))}
    return f


# --- what the block should hold ---------------------------------------------------------------


@dataclass
class Review:
    file: File
    adds: bool  # may this copy add lines here (its own VM, the active copy)?
    wanted: dict[str, str] = field(default_factory=dict)  # machine -> its line
    own: list[str] = field(default_factory=list)  # their key has a line of its own
    unfit: list[str] = field(default_factory=list)  # a name or key no line is made for
    present: dict[str, list[str]] = field(default_factory=dict)  # machine -> its lines
    foreign: list[int] = field(default_factory=list)  # line numbers (1-based)

    @property
    def missing(self) -> list[str]:
        return [n for n, x in self.wanted.items() if x not in self.present.get(n, [])]

    @property
    def edited(self) -> list[str]:
        return [n for n in self.wanted if n in self.present
                and (len(self.present[n]) > 1 or self.present[n][0] != self.wanted[n])]

    @property
    def stale(self) -> list[str]:
        return [n for n in self.present if n not in self.wanted]

    @property
    def in_step(self) -> bool:
        return not (self.missing or self.edited or self.stale or self.foreign)

    def state_of(self, name: str) -> str | None:
        """ca show's word for one trusted machine: installed, own-line, missing, or
        None where FleetLedger keeps no lines (not its own VM, nothing here)."""
        if name in self.own:
            return "own-line"
        if name in self.wanted:
            return "installed" if self.wanted[name] in self.present.get(name, []) else "missing"
        return "missing" if self.adds and name in self.unfit else None


def _this_machine() -> str | None:
    from fleetledger import trustcmd

    here = trustcmd.this_computer()
    return here["machine"] if here else None


def adds_here(store, inv) -> bool:
    """FleetLedger adds lines only on its own VM, and only as the active copy."""
    from fleetledger import firstrun, handover

    return handover.marker(store) is None and firstrun.on_its_own_vm(inv)


def review(store, inv, f: File | None = None) -> Review:
    from fleetledger import ca

    f = f or read()
    r = Review(f, adds_here(store, inv))
    machines = ca.machines(inv)
    by_key = {}
    for m in machines:
        if (blob := ed25519_blob(m.public_key)) is not None:
            by_key.setdefault(blob, m.name)
    written = re.compile(rf'{OPTIONS},command="{re.escape(SHIP_FLEET)} machine-bundle '
                         rf'({NAME_RE.pattern})" ssh-ed25519 {BLOB_RE.pattern} \1')
    for i, x in f.block:
        key = key_of(x)
        if key is None:
            if x.strip(" \t\r") and not x.lstrip(" \t").startswith("#"):
                r.foreign.append(i + 1)  # not a comment, yet no key sshd would read
            continue
        if key in by_key:
            r.present.setdefault(by_key[key], []).append(x)
        elif found := written.fullmatch(x):
            # One FleetLedger wrote, for a key no record has now (edited in ships.toml):
            # removing it takes nothing away from anyone but that key.
            r.present.setdefault(found.group(1), []).append(x)
        else:
            r.foreign.append(i + 1)
    if not r.adds and handover_role(store) is not None:
        return r  # standby or retired: nothing is wanted here
    here = _this_machine()
    for m in machines:
        if m.revoked or m.name == here:
            continue
        line = line_for(m.name, m.public_key)
        if line is None:
            r.unfit.append(m.name)
        elif ed25519_blob(m.public_key) in f.outside:
            if r.adds:  # elsewhere no machine gets a line: nothing to say of yours
                r.own.append(m.name)
        elif r.adds or m.name in r.present:
            r.wanted[m.name] = line
    return r


def handover_role(store) -> str | None:
    from fleetledger import handover

    m = handover.marker(store)
    return m.role if m else None


# --- writing ----------------------------------------------------------------------------------


@contextmanager
def _locked() -> Iterator[None]:
    """~/.ssh locked against another ship-fleet doing this (the folder: the file itself
    is replaced). Made, 0700, only when it is missing."""
    d = ssh_dir()
    if not d.exists():
        d.mkdir(mode=0o700, parents=True)
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def compose(f: File, lines: list[str]) -> str:
    """The file with its block holding these lines (no block at all for none); every
    line outside it as it was."""
    if f.begin is not None:
        before, after = f.lines[:f.begin], f.lines[f.end + 1:]
        return "\n".join(before + ([BEGIN, *lines, END] if lines else []) + after)
    text = "\n".join(f.lines)
    if not lines:
        return text
    block = "\n".join([BEGIN, *lines, END]) + "\n"
    return text + block if not text or text.endswith("\n") else text + "\n" + block


def _refuse_unsafe(f: File) -> None:
    if f.broken:
        raise FleetError(f"Not changed: {f.broken}.", hint=hand_fix(f))
    if f.raw is not None and f.uid != os.geteuid():
        raise FleetError(f"Not changed: {f.path} belongs to another user.",
                         hint=f"It should be this user's own: chown {os.geteuid()} {f.path}")


def _write(f: File, text: str) -> None:
    data = text.encode("utf-8", "surrogateescape")
    fd = None
    tmp = f.path.with_name(f".{f.path.name}.fleetledger-{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        if f.raw is not None:
            os.fchmod(fd, f.mode)
            if f.gid is not None and os.fstat(fd).st_gid != f.gid:
                os.fchown(fd, -1, f.gid)
        with os.fdopen(fd, "wb") as fh:
            fd = None
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        now = f.path.read_bytes() if f.path.exists() else None
        if now != f.raw:
            raise _Changed
        os.replace(tmp, f.path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    d = os.open(f.path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(d)
    finally:
        os.close(d)


class _Changed(Exception):
    """authorized_keys changed while we worked (you, or ssh-copy-id): read it again."""


@dataclass
class Synced:
    review: Review
    wrote: bool
    installed: list[str]  # machines that had no line and have one now
    removed: list[str]
    fixed: list[str]


def sync(store, *, edit: bool = True) -> Synced:
    """Rewrite the block to what review says, if it differs. Refuses (FleetError) a
    file whose markers are broken, a line in the block that is not FleetLedger's, a
    file that is not a plain one of this user's. Then starts the idle clock
    (refresh_allowed) of each machine whose line is new and that never asked nor was
    refused for being idle (unless
    edit is False: the caller holds ships.toml's lock)."""
    from fleetledger import ca
    from fleetledger.inventory import now_timestamp

    for _ in range(TRIES):
        r = review(store, store.load())
        if r.in_step and not (r.file.broken and r.file.raw is not None):
            # Nothing to do (a symlinked file included, wherever no line is wanted): no
            # lock, and no ~/.ssh made.
            result = Synced(r, False, [], [], [])
            break
        with _locked():
            r = review(store, store.load())
            f = r.file
            _refuse_unsafe(f)
            if r.foreign:
                raise FleetError(
                    f"Not changed: line(s) {', '.join(map(str, r.foreign))} of {f.path}, "
                    "inside FleetLedger's block, are not for any machine FleetLedger "
                    "trusted, so it will not remove them.",
                    hint="Move them above the block's first marker line (or delete "
                    f"them), then run {SYNC} again.")
            result = Synced(r, False, [n for n in r.wanted if n not in r.present],
                            r.stale, r.edited)
            if r.in_step:
                break
            try:
                _write(f, compose(f, list(r.wanted.values())))
            except _Changed:
                continue
            result.wrote = True
            result.review = review(store, store.load())  # as it is now
            break
    else:
        raise FleetError(f"Not changed: {path()} kept changing while FleetLedger wrote it.",
                         hint=f"Run {SYNC} again.")
    if result.wrote:
        from fleetledger.concierge import audit

        audit.record("-", "authorized-keys", ",".join(
            sorted({*result.installed, *result.removed, *result.fixed})) or "-", "ok")
    if result.installed and edit:
        inv = store.load()
        # Never asked (and never refused for it): its 30 days start now, whatever an
        # older --allow-again said.
        start = [n for n in result.installed if (m := ca.machine(inv, n)) is not None
                 and not m.last_refreshed and not m.idle_refused]
        if start:
            now = now_timestamp()
            with store.edit() as ed:
                for n in start:
                    fresh = ca.machine(ed.snapshot(), n)
                    if fresh and not fresh.last_refreshed and not fresh.idle_refused:
                        ed.set_machine_field(n, "refresh_allowed", now)
    return result


# --- what is said -----------------------------------------------------------------------------


def hand_fix(f: File | None = None) -> str:
    return (f"Edit {f.path if f else path()} by hand: keep your own lines outside "
            "FleetLedger's block and delete the block's lines and markers (from "
            f"'{BEGIN[:24]}...' to '{END[:24]}...'); then {SYNC} writes it again")


def where_said() -> str:
    from fleetledger import paths

    return paths.tilde(path())


def own_line_said(name: str) -> str:
    return (f"{name}'s key has a line of its own in {where_said()} (one of your keys), so "
            "FleetLedger adds no bundle-only line for it: that key keeps what your line "
            f"allows, and fetches its bundle with ssh <this VM> ship-fleet machine-bundle "
            f"{name}.")


def synced_said(s: Synced) -> list[str]:
    out = []
    where = where_said()
    if s.installed:
        out.append(f"Bundle key added to {where} for {', '.join(s.installed)}: that key may "
                   "fetch its own bundle here and nothing else.")
    if s.fixed:
        out.append(f"Bundle key line(s) in {where} rewritten as FleetLedger writes them: "
                   f"{', '.join(s.fixed)}.")
    if s.removed:
        out.append(f"Bundle key line(s) removed from {where}: {', '.join(s.removed)}.")
    return out


def first_fetch_said(name: str, identity: str) -> str:
    """What works on the machine today, in one sentence (the machine's refresher, when
    it comes, replaces it)."""
    import getpass

    return (f"From {name}, ssh -i {identity} {getpass.getuser()}@<this VM> now returns its "
            "bundle as JSON (the command is forced, whatever you ask for); nothing on "
            f"{name} applies it yet, so install it as below.")


def check(store, inv) -> tuple[str, str, str] | None:
    """doctor's item: (state, detail, next), or None where there is nothing to check
    (not FleetLedger's own VM, and no block here)."""
    try:
        r = review(store, inv)
    except FleetError as exc:
        return "fail", exc.message, exc.hint or ""
    f = r.file
    where = where_said()
    if not r.adds and f.begin is None and not (f.broken and f.raw is not None):
        return None  # not its own VM, and no block of FleetLedger's (a symlink included)
    if f.broken:
        return "fail", f.broken, hand_fix(f)
    active = handover_role(store) is None
    fix = SYNC if active else hand_fix(f)
    problems = []
    if r.foreign:
        problems.append(f"line(s) {', '.join(map(str, r.foreign))} in FleetLedger's block are "
                        "for no machine it trusted")
        fix = (f"Move line(s) {', '.join(map(str, r.foreign))} of {where} above the "
               f"block, then {SYNC}" if active else hand_fix(f))
    if r.edited:
        problems.append(f"line(s) changed by hand: {', '.join(r.edited)}")
    if r.stale:
        problems.append("line(s) for machines no longer trusted here: " + ", ".join(r.stale))
    if f.raw is not None and f.mode is not None and f.mode & 0o022:
        problems.append(f"mode {f.mode:04o}: others may write it, so sshd may ignore it")
        fix = f"chmod 600 {where}" if not (r.foreign or r.edited or r.stale) else fix
    if f.raw is not None and f.uid != os.geteuid():
        problems.append("it belongs to another user")
    if problems:
        return "fail", f"{where}: " + "; ".join(problems), fix
    if r.missing:
        return "missing", (f"{where} has no bundle key for {', '.join(r.missing)}, so it "
                           "cannot fetch its bundle"), SYNC
    said = (f"{where}: {len(r.wanted)} trusted machine(s) may fetch their bundle and nothing "
            "else" if r.wanted else f"{where}: no bundle keys (no trusted machine needs one)")
    if r.own:
        said += f"; {', '.join(r.own)} use their own line"
    if r.unfit:
        said += f"; none for {', '.join(r.unfit)} (its name or key is not one FleetLedger puts "
        said += "there)"
    return "ok", said, ""


def states(store, inv) -> dict[str, str | None] | None:
    """Each machine's word for ca show (Review.state_of), or None: nothing to show."""
    try:
        r = review(store, inv)
    except FleetError:
        return None
    if not r.adds and r.file.begin is None:
        return None
    from fleetledger import ca

    return {m.name: r.state_of(m.name) for m in ca.machines(inv) if not m.revoked}
