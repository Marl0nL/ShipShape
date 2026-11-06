"""The file inbox: files a person sends to the Ship for its agents
(contracts/ship-ctl.md, `ship-ctl inbox`).

`~/inbox/` belongs to the agent user. FleetLedger streams each file to
`ship-ctl inbox put` over its usual SSH login; nothing else on the Ship listens for
files. ship-ctl runs as root and the agent controls its home, so every step goes
through HomeFS-style opens that never follow a symlink:

- the bytes go to a new file (O_EXCL|O_NOFOLLOW, mode 0600, owned by the agent) in
  `~/.local/state/shipshape/incoming/`, outside the inbox, so an agent never sees a
  half-written file;
- once every byte is there and synced, the file is linked into `~/inbox/` under its
  cleaned name, or under `name-2.ext`, `name-3.ext` ... if that name is taken
  (linkat never replaces a file, so nothing is overwritten, even in a race), and the
  staging name is removed;
- files never get an execute bit, and nothing here opens, runs or unpacks them.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import stat
import time
import unicodedata
from datetime import UTC, datetime
from typing import BinaryIO

from .context import Context
from .errors import CtlError
from .homefs import UnsafePath
from .notice import record, send_to_cores
from .shiptoml import ShipToml

INBOX = "inbox"  # ~/inbox
INCOMING = ".local/state/shipshape/incoming"  # staging: same filesystem, outside the inbox
MAX_BYTES = 256 * 1024 * 1024  # per file; FleetLedger checks the same limit first
KEEP_FREE = 1024 * 1024 * 1024  # never fill the disk: leave at least 1 GiB free
NAME_BYTES = 200  # a cleaned name's length, in UTF-8 bytes
LIST_MAX = 500
CHUNK = 1024 * 1024
STALE = 3600  # seconds: a staging file this old belongs to a transfer that died
_PART = re.compile(r"^\.[0-9a-f]{16}\.part$")
# Besides letters and digits (any script): characters that are harmless in a file
# name. Quotes, $, backticks, backslashes, ;|&<>*?!{}: and control or format
# characters (such as bidi overrides) become _ or go.
_KEEP = frozenset(" ._-+,@()[]#%=~")
_LEADING = " .-~#"  # no hidden files, no names that read as an option


def human(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            text = f"{size:.1f}".removesuffix(".0")
            return f"{int(size)} {unit}" if unit == "bytes" else f"{text} {unit}"
        size /= 1024
    return f"{n} bytes"  # pragma: no cover


def clean_name(raw: str) -> str:
    """A safe file name that still reads like the original: the last path part only,
    NFC, harmless characters, no leading dot or dash, at most NAME_BYTES."""
    name = unicodedata.normalize("NFC", raw).replace("\\", "/").rsplit("/", 1)[-1]
    # Characters, then NFC, twice: dropping a format character can let a pair compose
    # (a letter and its accent, or = and U+0338 into the symbol ≠, which the second
    # pass turns into _). After two rounds, cleaning a cleaned name changes nothing.
    name = _characters(_characters(name))
    name = re.sub(r" +", " ", name).lstrip(_LEADING).rstrip(" .")
    if len(name.encode("utf-8")) > NAME_BYTES:
        stem, ext = split_ext(name)
        room = NAME_BYTES - len(ext.encode("utf-8")) - (1 if ext else 0)
        stem = stem.encode("utf-8")[:room].decode("utf-8", "ignore").rstrip(" .")
        name = unicodedata.normalize("NFC", f"{stem}.{ext}" if ext else stem)
    return name or "file"


def _characters(name: str) -> str:
    """Keep harmless characters: letters, digits, marks and _KEEP; spaces of any kind
    as a space; drop control and format characters (bidi overrides); others become _.
    NFC-normalises what it returns."""
    out = []
    for ch in name:
        cat = unicodedata.category(ch)
        if cat[0] == "Z" or ch.isspace():
            out.append(" ")  # tabs, newlines and odd spaces read as one space
        elif cat[0] == "C":
            continue  # other control and format characters, surrogates
        elif ch.isalnum() or cat[0] == "M" or ch in _KEEP:
            out.append(ch)
        else:
            out.append("_")
    return unicodedata.normalize("NFC", "".join(out))


def split_ext(name: str) -> tuple[str, str]:
    """(stem, extension without its dot): the last dot's, if the extension is short
    (at most 16 bytes) and the stem not empty; a .tar.* pair stays together.
    "report v1.2.pdf" -> ("report v1.2", "pdf"); "a.tar.gz" -> ("a", "tar.gz")."""
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext.encode("utf-8")) > 16:
        return name, ""
    inner, dot, tar = stem.rpartition(".")
    if dot and inner and tar.lower() == "tar":
        return inner, f"{tar}.{ext}"
    return stem, ext


def numbered(name: str, n: int) -> str:
    """The nth name for a taken one, before its extension: report v1.2.pdf ->
    report v1.2-2.pdf; archive.tar.gz -> archive-2.tar.gz; README -> README-2."""
    stem, ext = split_ext(name)
    return f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"


def _inbox_path(ctx: Context) -> str:
    return ctx.paths.agent_path(ctx.paths.agent_home / INBOX)


def _open(ctx: Context, rel: str, create: bool) -> int | None:
    try:
        return ctx.home.open_dir(rel, create=create, mode=0o700)
    except UnsafePath as exc:
        raise CtlError("invalid-input", f"~/{rel} is a symlink or not a folder; nothing "
                       "was touched. Move it out of the way on the Ship.") from exc


def _free_bytes(ctx: Context) -> int | None:
    try:
        st = os.statvfs(ctx.paths.agent_home)
    except OSError:
        return None
    return st.f_bavail * st.f_frsize


def _when(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def listing(ctx: Context) -> dict:
    """What is in ~/inbox now: its plain files (no folder is entered, no link
    followed), newest first."""
    data: dict = {"path": _inbox_path(ctx), "exists": False, "files": [], "others": 0,
                  "truncated": False, "max_bytes": MAX_BYTES, "free_bytes": _free_bytes(ctx)}
    fd = _open(ctx, INBOX, create=False)
    if fd is None:
        return data
    data["exists"] = True
    try:
        files = []
        with os.scandir(fd) as entries:
            for entry in entries:
                try:
                    st = entry.stat(follow_symlinks=False)
                    entry.name.encode("utf-8")  # a name that is not UTF-8 cannot be shown
                except (OSError, UnicodeEncodeError):
                    data["others"] += 1
                    continue
                if not stat.S_ISREG(st.st_mode):
                    data["others"] += 1
                    continue
                files.append({"name": entry.name, "size": st.st_size,
                              "modified": _when(st.st_mtime), "_m": st.st_mtime})
    finally:
        os.close(fd)
    files.sort(key=lambda f: (-f["_m"], f["name"]))
    data["truncated"] = len(files) > LIST_MAX
    data["files"] = [{k: v for k, v in f.items() if k != "_m"} for f in files[:LIST_MAX]]
    return data


def _receive(stream: BinaryIO, fd: int, size: int) -> int:
    """Copy exactly `size` bytes from stream to fd; returns how many came."""
    got = 0
    while got < size:
        chunk = stream.read(min(CHUNK, size - got))
        if not chunk:
            break
        _write_all(fd, chunk)
        got += len(chunk)
    return got


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def sweep(staging: int, now: float | None = None) -> list[str]:
    """Remove staging files a transfer left behind (ship-ctl killed, the Ship rebooted
    mid-transfer): plain .part files not written to for STALE seconds. A transfer in
    progress writes all the time, and FleetLedger gives up on one long before."""
    now = time.time() if now is None else now
    gone = []
    with os.scandir(staging) as entries:
        for entry in entries:
            if not _PART.match(entry.name):
                continue
            try:
                st = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(st.st_mode) and now - st.st_mtime > STALE:
                    os.unlink(entry.name, dir_fd=staging)
                    gone.append(entry.name)
            except OSError:
                continue
    return gone


def put(ctx: Context, raw_name: str, size: int, stream: BinaryIO) -> tuple[dict, list, list]:
    """Receive one file of `size` bytes from stream into ~/inbox. Returns (data,
    changed, messages). Refused before a byte is read if it is too big or would
    leave the disk nearly full."""
    name = clean_name(raw_name)
    if size < 0:
        raise CtlError("usage", "--size must be 0 or more")
    if size > MAX_BYTES:
        raise CtlError("invalid-input", f"{name} is {human(size)}; the inbox takes files of "
                       f"up to {human(MAX_BYTES)} each. Copy bigger files another way "
                       "(scp as agent, say).")
    owner = ctx.agent_owner
    staging = _open(ctx, INCOMING, create=True)
    # Sweep first: a transfer that died may have left what fills the disk.
    sweep(staging)
    free = _free_bytes(ctx)
    if free is not None and free - size < KEEP_FREE:
        os.close(staging)
        raise CtlError("invalid-input", f"Not enough disk space for {name} ({human(size)}): "
                       f"{human(max(free, 0))} free, and the Ship keeps {human(KEEP_FREE)} "
                       "free. Clear the inbox or grow the disk.")
    tmp = f".{secrets.token_hex(8)}.part"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=staging)
        try:
            os.fchmod(fd, 0o600)
            if owner:
                os.fchown(fd, *owner)
            got = _receive(stream, fd, size)
            extra = stream.read(1) if got == size else b""
            os.fsync(fd)
        finally:
            os.close(fd)
        if got < size:
            raise CtlError("invalid-input", f"The transfer of {name} was cut short "
                           f"({human(got)} of {human(size)}); nothing was kept.")
        if extra:
            raise CtlError("invalid-input", f"More than --size bytes came for {name}; "
                           "nothing was kept.")
        inbox = _open(ctx, INBOX, create=True)
        try:
            stored = _link_unique(staging, tmp, inbox, name)
            os.fsync(inbox)
        finally:
            os.close(inbox)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=staging)
        os.close(staging)
    path = f"{_inbox_path(ctx)}/{stored}"
    data = {"name": stored, "given": raw_name[:300], "size": size, "path": path,
            "renamed": stored != raw_name}
    note = f"Received {stored} ({human(size)}) in {_inbox_path(ctx)}."
    if stored != name:
        note += f" {name} was taken, so it is {stored}."
    elif stored != raw_name:
        note += " (Its name was cleaned up.)"
    record(ctx, f"{stored} ({size} bytes)", "inbox")
    return data, [path], [note]


def _link_unique(src_dir: int, src: str, dst_dir: int, name: str) -> str:
    """Hard-link src under name, or name-2, name-3 ...: linkat never replaces a file."""
    for n in range(1, 1000):
        candidate = name if n == 1 else numbered(name, n)
        try:
            os.link(src, candidate, src_dir_fd=src_dir, dst_dir_fd=dst_dir,
                    follow_symlinks=False)
            return candidate
        except FileExistsError:
            continue
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise CtlError("internal", "~/inbox is on another filesystem than ~/.local/"
                               "state/shipshape; cannot place the file") from exc
            raise
    raise CtlError("invalid-input", f"Too many files called {name} in the inbox already.")


def remove(ctx: Context, names: list[str]) -> tuple[dict, list, list]:
    """Remove these plain files from ~/inbox (exact names, as `list` gives them)."""
    data: dict = {"removed": [], "missing": [], "refused": []}
    for name in names:
        if not name or "/" in name or name in (".", "..") or "\0" in name:
            raise CtlError("usage", f"not a file name in the inbox: {name!r}")
    fd = _open(ctx, INBOX, create=False)
    if fd is None:
        data["missing"] = list(names)
        return data, [], ["The inbox is empty."]
    try:
        for name in names:
            try:
                st = os.stat(name, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                data["missing"].append(name)
                continue
            if not stat.S_ISREG(st.st_mode):
                data["refused"].append(name)
                continue
            os.unlink(name, dir_fd=fd)
            data["removed"].append(name)
    finally:
        os.close(fd)
    messages = []
    if data["removed"]:
        messages.append("Removed " + ", ".join(data["removed"]) + ".")
        record(ctx, "removed " + ", ".join(data["removed"]), "inbox")
    if data["missing"]:
        messages.append("Not in the inbox (moved already?): " + ", ".join(data["missing"]) + ".")
    if data["refused"]:
        messages.append("Not a plain file, left alone: " + ", ".join(data["refused"]) + ".")
    changed = [f"{_inbox_path(ctx)}/{n}" for n in data["removed"]]
    return data, changed, messages


def notice_text(count: int) -> str:
    """The one line a core agent gets: a count and where, never a file name (a
    name is the sender's text, and this line is typed into the agent's prompt)."""
    files = "1 new file" if count == 1 else f"{count} new files"
    return (f"ShipShape: {files} in your inbox (~/inbox), sent by the person. "
            "Handle them as the ship skill says when your work needs them.")


def notify(ctx: Context, ship: ShipToml, count: int) -> tuple[dict, list[str]]:
    """Tell the core ([[autostart]]) agents that files arrived, as notify-signin does."""
    if count < 1 or count > 1000:
        raise CtlError("usage", "--count must be between 1 and 1000")
    data: dict = {"count": count, "sent": [], "skipped": []}
    note = send_to_cores(ctx, ship, notice_text(count), data)
    if note:
        record(ctx, note, "inbox-notify")
        return data, [note]
    messages = [f"Inbox notice sent to: {', '.join(data['sent']) or 'none'}."]
    messages += [f"{s['name']}: not sent ({s['reason']})." for s in data["skipped"]]
    record(ctx, f"{count} file(s): " + " ".join(messages), "inbox-notify")
    return data, messages
