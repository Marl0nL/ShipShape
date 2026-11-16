"""The keys that may log in to this computer as FleetLedger's user: `ship-fleet keys`.

On FleetLedger's own VM you work FleetLedger over ssh as its user, so the lines of that
user's ~/.ssh/authorized_keys (and authorized_keys2, which sshd also reads by default)
decide who has a shell on the computer holding the fleet's keys. This lists them, names
one (its comment, nothing else) and revokes one (its line, nothing else); and it tells
the audit log which key ran a command (actor).

`ls` only reads, anywhere. `name` and `revoke` change the file only on FleetLedger's own
VM: elsewhere authorized_keys is your own login's. Both read lines with authkeys.parse
(the reading sshd does, which FleetLedger's bundle-key block uses too) and write with
authkeys' discipline: ~/.ssh locked against another ship-fleet, a temporary file renamed
over the old one with its mode and group, nothing written over a change made meanwhile,
and every other line kept byte for byte. They never touch a line in FleetLedger's block
(a trusted machine's bundle-only key: `ship-fleet untrust NAME` removes it) or in
authorized_keys2 (edit that by hand).

Which key a session logged in with is known only from sshd itself: with `ExposeAuthInfo
yes`, sshd writes the key the session was let in with to a file named by SSH_USER_AUTH,
readable only by the user. Without it nothing here guesses: the audit names no key, and
`revoke` over ssh cannot tell whether it would cut off this very session, so it refuses
until you say --allow-lockout. Everyone with a shell here can change the audit log and
this file anyway; the actor is what sshd said, not a proof against that user.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass

from fleetledger import authkeys, prompts
from fleetledger.errors import FleetError

SCHEMA = "shipshape.fleetledger-key/1"
MAIN = "authorized_keys"
OTHER = "authorized_keys2"  # authkeys.OTHER_FILES: sshd's default reads it too
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+-]{0,63}")
FINGERPRINT = "SHA256:"
FORCE = "--allow-lockout"
MAX_AUTH_INFO = 64 * 1024
BUNDLE_RE = re.compile(rf"\S+ machine-bundle ({authkeys.NAME_RE.pattern})")
CERT_SUFFIX = "-cert-v01@openssh.com"
ACCESS = ("shell", "command", "bundle", "cert-authority")


# --- the lines ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Key:
    file: str  # MAIN or OTHER
    index: int  # in that file's lines (split on \n)
    text: str  # the line as it is
    parsed: authkeys.KeyLine
    fingerprint: str | None  # None: a key sshd could not decode (it lets nobody in)
    access: str  # ACCESS
    command: str | None  # access command: the command it forces
    machine: str | None  # access bundle: the trusted machine
    in_block: bool  # inside FleetLedger's marked block

    @property
    def number(self) -> int:
        return self.index + 1

    @property
    def name(self) -> str | None:
        return self.parsed.comment.rstrip(" \t\r") or None


def fingerprint(key: str) -> str | None:
    from fleetledger import sshkeys

    try:
        return sshkeys.checked_fingerprint("x " + key)
    except FleetError:
        return None


def options(text: str) -> list[tuple[str, str | None]]:
    """A line's options as (lower-case name, value or None), split on commas outside
    quotes, as sshd splits them; a quoted value unquoted (\\" is a quote)."""
    parts, cur, quoted, i = [], "", False, 0
    while i < len(text):
        c = text[i]
        if c == "\\" and text[i + 1:i + 2] == '"':
            cur, i = cur + '\\"', i + 2
            continue
        if c == '"':
            quoted = not quoted
        elif c == "," and not quoted:
            parts.append(cur)
            cur, i = "", i + 1
            continue
        cur, i = cur + c, i + 1
    if cur:
        parts.append(cur)
    out = []
    for part in parts:
        name, eq, value = part.partition("=")
        if eq and len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1].replace('\\"', '"')
        out.append((name.strip().lower(), value if eq else None))
    return out


def _key(file: str, index: int, text: str, parsed: authkeys.KeyLine, in_block: bool) -> Key:
    opts = options(parsed.options)
    names = {n for n, _ in opts}
    command = next((v for n, v in opts if n == "command"), None)
    machine = None
    if "cert-authority" in names:
        access = "cert-authority"
    elif command is not None:
        found = BUNDLE_RE.fullmatch(command)
        if in_block and found and "restrict" in names:
            access, machine, command = "bundle", found.group(1), None
        else:
            access = "command"
    else:
        access = "shell"
    return Key(file, index, text, parsed, fingerprint(parsed.key), access, command, machine,
               in_block)


@dataclass
class Listing:
    file: authkeys.File  # authorized_keys, as authkeys reads it
    keys: list[Key]
    other_error: str | None = None  # authorized_keys2 could not be read


def listing() -> Listing:
    """Every line with a key, in the order sshd tries them: authorized_keys, then
    authorized_keys2."""
    f = authkeys.read()
    block = {i for i, _ in f.block}
    keys = [_key(MAIN, i, x, p, i in block) for i, x in enumerate(f.lines)
            if (p := authkeys.parse(x))]
    other_error = None
    try:
        text = (f.path.parent / OTHER).read_text(encoding="utf-8", errors="surrogateescape")
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        text, other_error = "", f"cannot read {f.path.parent / OTHER}: {exc.strerror}"
    keys += [_key(OTHER, i, x, p, False) for i, x in enumerate(text.split("\n"))
             if (p := authkeys.parse(x))]
    return Listing(f, keys, other_error)


# --- the session -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    over_ssh: bool  # SSH_CONNECTION (or SSH_CLIENT) says so
    keys: tuple[str, ...] | None  # the keys sshd let it in with; None: cannot tell


def session(environ: Mapping[str, str] | None = None) -> Session:
    """What sshd says this session logged in with (ExposeAuthInfo's SSH_USER_AUTH: a
    file of this user's, lines "publickey <type> <base64>"). None for the keys when
    there is no such file, it is not a plain file of this user's, or it names a
    certificate (whose line here cannot be told)."""
    env = os.environ if environ is None else environ
    over = bool(env.get("SSH_CONNECTION") or env.get("SSH_CLIENT"))
    where = env.get("SSH_USER_AUTH")
    if not where:
        return Session(over, None)
    try:
        fd = os.open(where, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return Session(True, None)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid()
                or st.st_size > MAX_AUTH_INFO):
            return Session(True, None)
        data = os.read(fd, MAX_AUTH_INFO + 1)
    except OSError:
        return Session(True, None)
    finally:
        os.close(fd)
    keys = []
    for line in data.decode("utf-8", "replace").split("\n"):
        words = line.split()
        if words[:1] != ["publickey"]:
            continue
        if len(words) < 3 or words[1].endswith(CERT_SUFFIX):
            return Session(True, None)
        keys.append(authkeys._same_key(words[2]))
    return Session(True, tuple(dict.fromkeys(keys)))


def actor(environ: Mapping[str, str] | None = None) -> dict:
    """Who runs this command, for the audit log: {"actor_key": its fingerprint, "actor":
    the first line for that key's name}, or less, or {} when sshd did not say (one key,
    not a certificate). Never fails: a command runs whatever this finds."""
    try:
        s = session(environ)
        if not s.keys or len(s.keys) != 1:
            return {}
        fp = fingerprint(s.keys[0])
        if fp is None:
            return {}
        out = {"actor_key": fp}
        first = next((k for k in listing().keys if k.parsed.key == s.keys[0]), None)
        if first is not None and first.name:
            out["actor"] = first.name[:200]
        return out
    except Exception:  # noqa: BLE001 - the audit's helper never stops a command
        return {}


def this_session(k: Key, s: Session) -> bool | None:
    return None if s.keys is None else k.parsed.key in s.keys


# --- what may change ---------------------------------------------------------------------------


def on_appliance(store) -> bool:
    from fleetledger import firstrun

    try:
        inv = store.load()
    except FleetError:
        return False
    return firstrun.on_its_own_vm(inv)


NOT_HERE = ("Not changed: this is not FleetLedger's own VM, so ~/.ssh/authorized_keys here "
            "is your own login's, not FleetLedger's to manage. ship-fleet keys ls shows it; "
            "change it yourself.")


def changeable(k: Key, f: authkeys.File, appliance: bool) -> bool:
    return appliance and k.file == MAIN and not k.in_block and not f.broken


def select(keys: list[Key], selector: str) -> list[Key]:
    """The lines a name (exactly as written) or a SHA256: fingerprint names."""
    if selector.startswith(FINGERPRINT):
        return [k for k in keys if k.fingerprint == selector]
    return [k for k in keys if k.name == selector]


def _where(k: Key) -> str:
    return f"{k.file} line {k.number}"


def _one(lst: Listing, selector: str) -> Key:
    found = select(lst.keys, selector)
    if not found:
        raise FleetError(f"No key here is named or has the fingerprint '{shown(selector)}'.",
                         hint="ship-fleet keys ls lists them.")
    if len(found) > 1:
        raise FleetError(f"{len(found)} lines match '{shown(selector)}' ("
                         + "; ".join(f"{_where(k)}, {k.fingerprint}" for k in found) + ").",
                         hint="Give the fingerprint of the one you mean, or name them apart "
                         "first (ship-fleet keys name SHA256:... NEW-NAME); lines with one "
                         "key twice you edit by hand.")
    return found[0]


def _changeable_or_refuse(k: Key, lst: Listing, verb: str) -> None:
    if k.in_block:
        who = k.machine or k.name or "a trusted machine"
        raise FleetError(f"Not changed: that is the bundle-only line FleetLedger keeps for "
                         f"{shown(who)} ({_where(k)}).",
                         hint=f"ship-fleet untrust {shown(who)} removes it; FleetLedger names "
                         "it after its machine." if verb == "revoke" else
                         "FleetLedger names it after its machine.")
    if k.file != MAIN:
        raise FleetError(f"Not changed: that line is in ~/.ssh/{OTHER} ({_where(k)}), which "
                         f"ship-fleet keys does not write.", hint=f"Edit ~/.ssh/{OTHER} by hand.")
    if lst.file.broken:
        raise FleetError(f"Not changed: {lst.file.broken}.", hint=authkeys.hand_fix(lst.file))


def lockout_reasons(lst: Listing, k: Key, s: Session) -> list[str]:
    """Why removing k could shut you out of this computer (refused without FORCE)."""
    why = []
    if s.keys is not None and k.parsed.key in s.keys:
        why.append("it is the key this session logged in with")
    elif s.keys is None and s.over_ssh:
        why.append("FleetLedger cannot tell which key this ssh session logged in with "
                   "(sshd's ExposeAuthInfo is off here), so it may be this one")
    if k.access == "shell" and not any(o.parsed.key != k.parsed.key and surely_a_shell(lst, o)
                                       for o in lst.keys):
        why.append("it is the last key here that surely gets a shell")
    return why


# Options that leave a line able to log in, from anywhere, at any time, to a shell.
HARMLESS = {"restrict", "no-pty", "pty", "no-port-forwarding", "port-forwarding",
            "no-agent-forwarding", "agent-forwarding", "no-x11-forwarding", "x11-forwarding",
            "no-user-rc", "user-rc", "no-touch-required", "verify-required"}


def surely_a_shell(lst: Listing, o: Key, check=None) -> bool:
    """Would sshd surely give o's key a shell through o? Only a line FleetLedger can be
    sure of counts as the key you will log in with after a revoke: in authorized_keys
    (an sshd_config may leave authorized_keys2 out), outside FleetLedger's block, the
    first line for its key (sshd uses that one), with no option that could keep it out
    (from=, expiry-time=, anything unknown), and a key ssh-keygen itself accepts."""
    if o.access != "shell" or o.file != MAIN or o.in_block or o.fingerprint is None:
        return False
    if next(x for x in lst.keys if x.parsed.key == o.parsed.key) is not o:
        return False
    if any(name not in HARMLESS for name, _ in options(o.parsed.options)):
        return False
    return (check or _keygen_accepts)(f"{o.parsed.keytype} {o.parsed.key}")


def _keygen_accepts(line: str) -> bool:
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".pub") as fh:
        fh.write(line + "\n")
        fh.flush()
        try:
            got = subprocess.run(["ssh-keygen", "-lf", fh.name], capture_output=True,
                                 text=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
    return got.returncode == 0


# --- the changes -------------------------------------------------------------------------------


def renamed(line: str, name: str) -> str:
    """The line with its comment set to name: everything up to the end of its key kept
    as it is (and a closing \\r). FleetError if the result is not that line again
    with that comment, read as sshd reads it."""
    if not NAME_RE.fullmatch(name):
        raise FleetError(f"'{shown(name)}' is not a name FleetLedger writes: letters, digits "
                         "and . _ @ + - only (no spaces), starting with a letter or digit, "
                         "up to 64 characters.")
    cr = "\r" if line.endswith("\r") else ""
    base = line[:-1] if cr else line
    old = authkeys.parse(base)
    if old is None:
        raise FleetError("Not changed: FleetLedger cannot read that line's key.")
    new = base[:old.end] + " " + name + cr
    got = authkeys.parse(new)
    if (got is None or "\n" in new or (got.options, got.keytype, got.key)
            != (old.options, old.keytype, old.key) or got.comment != name + cr):
        raise FleetError("Not changed: the renamed line would not read as the same key.")
    return new


def _apply(expect: str, change) -> tuple[Key, Listing]:
    """Under ~/.ssh's lock: find the line again (the one line of authorized_keys whose
    text is what was checked), let change(listing, key) check it again and return the
    file's new lines, and write them. Read again when another writer changed the file
    meanwhile."""
    for _ in range(authkeys.TRIES):
        with authkeys._locked():
            lst = listing()
            found = [k for k in lst.keys if k.file == MAIN and k.text == expect]
            if len(found) != 1:
                raise FleetError("Not changed: that line changed while FleetLedger worked "
                                 "(or is in the file twice).",
                                 hint="ship-fleet keys ls, then run it again.")
            k = found[0]
            new_lines = change(lst, k)
            authkeys._refuse_unsafe(lst.file)
            try:
                authkeys._write(lst.file, "\n".join(new_lines))
            except authkeys._Changed:
                continue
            except OSError as exc:
                raise FleetError(f"Cannot write {lst.file.path}: {exc.strerror or exc}.") \
                    from exc
            return k, lst
    raise FleetError(f"Not changed: {authkeys.path()} kept changing while FleetLedger wrote it.",
                     hint="Run it again.")


def name_key(store, selector: str, new_name: str) -> tuple[Key, bool]:
    """Set one line's comment to new_name; (the line as it was, whether it changed)."""
    if not on_appliance(store):
        raise FleetError(NOT_HERE)
    renamed("ssh-ed25519 AAAA", new_name)  # the name alone, before anything is read
    lst = listing()
    k = _one(lst, selector)
    _changeable_or_refuse(k, lst, "name")
    if k.name == new_name:
        return k, False
    taken = [o for o in lst.keys if o.name == new_name]
    if taken:
        raise FleetError(f"Not changed: {_where(taken[0])} is already named {new_name}.",
                         hint="Names here are told apart: choose another, or rename that one.")

    def change(lst2: Listing, k2: Key) -> list[str]:
        _changeable_or_refuse(k2, lst2, "name")
        if any(o.name == new_name for o in lst2.keys if o.index != k2.index or o.file != MAIN):
            raise FleetError(f"Not changed: another line is named {new_name} now.")
        lines = list(lst2.file.lines)
        lines[k2.index] = renamed(k2.text, new_name)
        return lines

    return _apply(k.text, change)[0], True


def plan_revoke(lst: Listing, selector: str, s: Session, allow_lockout: bool) -> Key:
    """The one line `revoke` would remove, or FleetError saying why not."""
    k = _one(lst, selector)
    _changeable_or_refuse(k, lst, "revoke")
    same = [o for o in lst.keys if o.parsed.key == k.parsed.key and o is not k]
    if same:
        raise FleetError(f"Not changed: that key has {len(same) + 1} lines ("
                         + "; ".join(_where(o) for o in [k, *same]) + "), so removing one "
                         "would leave it able to log in.",
                         hint="Remove them by hand (or untrust its machine, for a "
                         "bundle-only one).")
    why = lockout_reasons(lst, k, s)
    if why and not allow_lockout:
        raise FleetError("Not revoked: " + "; and ".join(why) + ", so removing it could shut "
                         "you out of this computer.",
                         hint="Check with ship-fleet keys ls that you can still log in with "
                         f"another key, then add {FORCE}.")
    return k


def revoke(store, selector: str, s: Session, *, allow_lockout: bool, confirm) -> Key | None:
    """Remove one line (after confirm(key) says yes); the line removed, or None."""
    if not on_appliance(store):
        raise FleetError(NOT_HERE)
    k = plan_revoke(listing(), selector, s, allow_lockout)
    if not confirm(k):
        return None

    def change(lst2: Listing, k2: Key) -> list[str]:
        if plan_revoke(lst2, k2.fingerprint or selector, s, allow_lockout).text != k2.text:
            raise FleetError("Not changed: that line changed while FleetLedger worked.")
        lines = list(lst2.file.lines)
        del lines[k2.index]
        if k2.index == len(lines) and lines:  # it was the last line, with no newline:
            lines.append("")  # the line before keeps the newline it had
        return lines

    return _apply(k.text, change)[0]


# --- what is said ------------------------------------------------------------------------------


def shown(text: str, width: int | None = None) -> str:
    """Text from the file, safe for a terminal: control characters as '?'."""
    out = "".join("?" if unicodedata.category(c)[0] == "C" else c for c in text)
    if width and len(out) > width:
        out = out[:width - 1] + "…"
    return out


def can_said(k: Key) -> str:
    if k.access == "bundle":
        return f"bundle only, machine {k.machine} (ship-fleet untrust {k.machine})"
    if k.access == "command":
        return "only the command " + shown(k.command or "", 60)
    if k.access == "cert-authority":
        return "any certificate it signs"
    return "shell"


def as_json(k: Key, f: authkeys.File, s: Session, appliance: bool) -> dict:
    return {"schema": SCHEMA, "file": k.file, "line": k.number, "name": k.name,
            "type": k.parsed.keytype, "fingerprint": k.fingerprint, "access": k.access,
            "machine": k.machine, "command": k.command, "options": k.parsed.options,
            "this_session": this_session(k, s), "changeable": changeable(k, f, appliance)}


def _list(args, app) -> int:
    lst = listing()
    s = session()
    appliance = on_appliance(app.store_factory())
    if args.json:
        app.say(json.dumps([as_json(k, lst.file, s, appliance) for k in lst.keys], indent=2))
        for problem in filter(None, [lst.file.broken, lst.other_error]):
            app.note(f"ship-fleet: {problem}.")
        return 0
    user = getpass.getuser()
    if lst.keys:
        app.say(f"Keys that may log in to this computer as {user} "
                f"({authkeys.where_said()}, then {OTHER}):")
        rows = [("NAME", "TYPE", "FINGERPRINT", "CAN")] + [
            (shown(k.name or "-", 32), shown(k.parsed.keytype, 24), k.fingerprint or "unreadable",
             can_said(k) + (" (this session)" if this_session(k, s) else "")
             + (f" [{OTHER}]" if k.file == OTHER else "")) for k in lst.keys]
        widths = [max(len(r[i]) for r in rows) for i in range(3)]
        for r in rows:
            cells = "  ".join(c.ljust(w) for c, w in zip(r[:3], widths, strict=True))
            app.say(f"  {cells}  {r[3]}")
    else:
        app.say(f"No key may log in to this computer as {user} through "
                f"{authkeys.where_said()} or {OTHER}.")
    for problem in filter(None, [lst.file.broken, lst.other_error]):
        app.say(f"Note: {problem}.")
    if not appliance:
        app.say("This is not FleetLedger's own VM: these are your own login's keys, and "
                "ship-fleet keys name and revoke change nothing here.")
    elif s.keys is None and s.over_ssh:
        app.say("Which key this session logged in with is not known: sshd's ExposeAuthInfo "
                "is off here (docs/runbooks/fleetledger-vm.md), so audit lines name no key "
                f"and revoke asks for {FORCE}.")
    return 0


def _name(args, app) -> int:
    if not args.key or not args.new_name:
        raise FleetError("Say which key and its new name: ship-fleet keys name KEY NEW-NAME "
                         "(KEY: its name, or its SHA256: fingerprint).")
    from fleetledger.concierge import audit

    k, changed = name_key(app.store_factory(), args.key, args.new_name)
    if not changed:
        app.say(f"{_where(k)} is already named {args.new_name}.")
        return 0
    audit.record("-", "key-name", k.fingerprint or "-", "ok")
    app.say(f"Named {args.new_name}: {_where(k)} ({k.fingerprint}); only its comment changed.")
    return 0


def _revoke(args, app) -> int:
    if not args.key or args.new_name:
        raise FleetError("Say which key: ship-fleet keys revoke KEY (its name, or its SHA256: "
                         "fingerprint).")
    from fleetledger import ca
    from fleetledger.concierge import audit

    store = app.store_factory()
    user = getpass.getuser()

    def confirm(k: Key) -> bool:
        if args.yes:
            return True
        said = (f"This removes {_where(k)} ({shown(k.name or 'no name')}, {k.fingerprint}, "
                f"{can_said(k)}): that key can no longer log in to this computer as {user}.")
        word = k.name if k.name and k.name == shown(k.name) else "yes"
        prompts.typed(app, f"{said} Type {word} to revoke it: ", word, exact=word != "yes",
                      no_terminal=f"Nothing revoked. {said} Run it in a terminal to confirm, "
                      "or add --yes.", refused="Nothing revoked.")
        return True

    k = revoke(store, args.key, session(), allow_lockout=args.allow_lockout, confirm=confirm)
    if k is None:
        return 0
    audit.record("-", "key-revoke", k.fingerprint or "-", "ok")
    app.say(f"Revoked: {shown(k.name or 'the key')} ({k.fingerprint}) can no longer log in to "
            f"this computer as {user}.")
    try:
        inv = store.load()
        for m in ca.machines(inv):
            blob = authkeys.ed25519_blob(m.public_key)
            if not m.revoked and blob and authkeys._same_key(blob) == k.parsed.key:
                app.say(f"Its machine {m.name} is still trusted, so ship-fleet trust "
                        f"--sync-keys gives that key a bundle-only line again; ship-fleet "
                        f"untrust {m.name} ends that too.")
    except FleetError:
        pass
    return 0


def cmd_keys(args, app) -> int:
    return {"ls": _list, "name": _name, "revoke": _revoke}[args.action](args, app)


def add_parsers(sub, add) -> None:
    p = add(sub, "keys", "the ssh keys that may log in to this computer as FleetLedger's "
            "user: list, name, revoke",
            "The keys in this user's ~/.ssh/authorized_keys (and authorized_keys2): who may\n"
            "log in to this computer, and so run FleetLedger, over ssh.\n\n"
            "  ship-fleet keys ls [--json]          each key: name, type, fingerprint, and\n"
            "                                       what it may do (a shell, one command,\n"
            "                                       a trusted machine's bundle only)\n"
            "  ship-fleet keys name KEY NEW-NAME    set one key's comment (only that)\n"
            "  ship-fleet keys revoke KEY           remove one key's line (asks first)\n\n"
            "KEY is a key's name or its SHA256: fingerprint (ssh-keygen -lf prints it).\n"
            "name and revoke change the file only on FleetLedger's own VM, never a line\n"
            "in FleetLedger's block (ship-fleet untrust NAME removes a machine's) or in\n"
            "authorized_keys2. revoke refuses to remove the last key that surely gets a shell,\n"
            "or the key this session logged in with (or, when sshd does not say which\n"
            f"that is, any key over ssh), unless {FORCE} is given.\n\n"
            "Audit lines of commands run over ssh name the key that logged in, when sshd\n"
            "says which (ExposeAuthInfo yes; docs/runbooks/fleetledger-vm.md).")
    p.add_argument("action", choices=("ls", "name", "revoke"))
    p.add_argument("key", nargs="?", metavar="KEY",
                   help="name, revoke: the key's name or SHA256: fingerprint")
    p.add_argument("new_name", nargs="?", metavar="NEW-NAME", help="name: its new name")
    p.add_argument("--json", action="store_true",
                   help="ls: JSON (contracts/fleetledger-key.schema.json)")
    p.add_argument("--yes", action="store_true", help="revoke: go on without asking")
    p.add_argument(FORCE, dest="allow_lockout", action="store_true",
                   help="revoke: even the last key that surely gets a shell, or the key this "
                   "session may have logged in with (you can be shut out of this computer)")
