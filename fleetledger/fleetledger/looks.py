"""The fleet's Desk looks: one looks folder FleetLedger keeps for every computer of yours
(`ship-fleet looks`, lookscmd.py; contracts/ship-fleet.md, "ship-fleet looks").

The folder is the Desk's own format (contracts/desk-looks.md): looks.json, a Herdr theme per
palette it lists in palettes/looks-<id>.json, and the logos its presets name in logos/. It is
data and only data: nothing here runs it, imports it, or puts any of it in a command or a
path. Every rule the Desk enforces (herdr/mods/desk/lookfolder.js) is enforced again here, in
Python, and in a few corners more strictly (a field named twice in one object, a field given
as null, text that is not valid Unicode); a folder with one thing out of place is refused
whole, and what was stored before stays.

Two ways in, both read in full and checked before anything is written:

- a folder on this computer: only the files looks.json names are read, by name (never by
  listing the folder), each opened without following a link, each a regular file read with a
  hard limit;
- a tar stream on stdin (`tar -cf - -C ~/.config/herdr-app/desk-looks . | ssh ... ship-fleet
  looks publish -`): at most STREAM_BYTES are read, then parsed in memory. Every member must
  be a folder of the format (., palettes, logos) or a regular file with a name the format
  could give it; a link, a device, a sparse file, an absolute or `..` path, a name twice, or
  more members or bytes than the format's limits allow refuses the whole stream. Nothing in
  it is ever extracted. A palette or logo looks.json does not name is dropped unchecked, as
  the Desk never reads it either; nothing unchecked is stored.

What is stored is the checked folder, rewritten from its checked values (the JSON files; the
logos as they are, being checked pictures), as one file written atomically (a temporary file
beside it, then a rename): STORE under FleetLedger's data folder, mode 0600. Reading it back
(load) checks everything again, so a hand edit or a Desk that no longer offers what a preset
names shows as a problem, never as looks.
"""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import stat
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from fleetledger import paths
from fleetledger.errors import FleetError

SCHEMA = "shipshape.desk-looks/1"
SHOW_SCHEMA = "shipshape.fleetledger-looks/1"
FOLDER = "looks"  # under FleetLedger's data folder (migrate moves it; wipe deletes it)
STORE = "published.json"
STORE_VERSION = 1

# The contract's limits (contracts/desk-looks.md; lookfolder.js LIMITS).
LOOKS_BYTES = 65536
PALETTE_BYTES = 16384
LOGO_BYTES = 65536
TOTAL_BYTES = 524288
MAX_PALETTES = 16
MAX_PRESETS = 32
MAX_TAG_LOOKS = 64
MAX_LOGOS = 16
# A tar stream of a folder at those limits, with room for its headers and padding: more is
# refused before it is parsed. At most 3 folders and 1 + 16 + 16 files in it.
STREAM_BYTES = 1024 * 1024
STREAM_MEMBERS = 3 + 1 + MAX_PALETTES + MAX_LOGOS
# The stored file: the folder in base64 and a little about it.
STORE_BYTES = 1024 * 1024

# What the Desk itself offers (herdr/mods/desk/looks.js builtinParts(); a test keeps these the
# same): its palettes (id: is it dark), presets, finishes, types, densities and brand fonts.
DESK_PALETTES = {"graphite": True, "ink": True, "moss": True, "desk-slate": True,
                 "desk-ember": True, "paper": False, "desk-linen": False}
DESK_PRESETS = ("graphite", "slate", "ember", "linen", "moss")
FINISHES = ("soft", "outline", "ledger", "index", "glow")
TYPES = ("geist", "geist-mono", "ubuntu", "lato", "noto", "mono", "urbanist")
DENSITIES = ("cozy", "compact")
FONTS = ("Geist", "Geist Mono", "Ubuntu Sans", "Ubuntu Sans Mono", "Lato", "Noto Serif",
         "Noto Sans", "JetBrains Mono", "Urbanist")

ID = re.compile(r"[a-z0-9][a-z0-9-]{0,30}")  # a preset id or a tag
PALETTE_ID = re.compile(r"looks-[a-z0-9][a-z0-9-]{0,30}")
LOGO = re.compile(r"[a-z0-9][a-z0-9-]{0,30}\.(png|jpg)")
COLOR = re.compile(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?")
BAND_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
LABEL = re.compile(r"[^\x00-\x1f\x7f]{1,40}")
THEME_TOP = ("background", "foreground", "selection", "cursor")
THEME_COLORS = ("canvas", "chrome", "surface", "popover", "raised", "hover", "selected",
                "border", "input", "muted", "dim", "accent", "accent_foreground", "solid",
                "solid_foreground", "working", "blocked", "done", "idle")
MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8\xff"}

LOOKS_FILE = "looks.json"
# Every name a file of the format can have, relative to the folder (and its two folders).
MEMBER = re.compile(r"looks\.json|palettes/looks-[a-z0-9][a-z0-9-]{0,30}\.json"
                    r"|logos/[a-z0-9][a-z0-9-]{0,30}\.(png|jpg)")
MEMBER_DIRS = ("", "palettes", "logos")


class Refused(FleetError):
    """A folder (or a stored copy) with something out of place: why, in the Desk's words."""

    def __init__(self, why: str):
        super().__init__(why)
        self.why = why


def _bad(why: str):
    raise Refused(why)


def _say(x: Any) -> str:
    return json.dumps(str(x)[:40])


def _only(obj: Any, where: str, allowed: tuple[str, ...], required: tuple[str, ...] = ()) -> None:
    if not isinstance(obj, dict):
        _bad(f"{where} is not an object")
    for k in obj:
        if k not in allowed:
            _bad(f"{where} has an unknown field, {_say(k)}")
    for k in required:
        if k not in obj:
            _bad(f"{where} has no {k}")


def _list(x: Any, where: str, most: int) -> list:
    if not isinstance(x, list):
        _bad(f"{where} is not a list")
    if len(x) > most:
        _bad(f"{where} has more than {most} entries")
    return x


def _text(x: Any, where: str) -> str:
    if isinstance(x, str) and LABEL.fullmatch(x):
        try:
            x.encode("utf-8")
            return x
        except UnicodeEncodeError:
            pass
    _bad(f"{where} is not a short line of text")


def _int(x: Any, where: str, lo: int, hi: int) -> int:
    whole = isinstance(x, int) or (isinstance(x, float) and x.is_integer())
    if isinstance(x, bool) or not whole or not lo <= x <= hi:
        _bad(f"{where} is not a whole number from {lo} to {hi}")
    return int(x)


def _str_in(x: Any, choices) -> bool:
    return isinstance(x, str) and x in choices


def parse_json(raw: bytes, where: str) -> Any:
    """Strict JSON: UTF-8, no field twice in one object, no NaN or Infinity."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _bad(f"{where} is not UTF-8 text")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in items:
            if k in out:
                _bad(f"{where} names {_say(k)} twice in one object")
            out[k] = v
        return out

    def constant(name: str):
        _bad(f"{where} is not JSON")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        _bad(f"{where} is not JSON")


def check_palette(doc: Any, where: str = "a palette") -> dict[str, Any]:
    """A Herdr theme from palettes/<id>.json, checked; a fresh copy (lookfolder.checkPalette)."""
    _only(doc, where, (*THEME_TOP, "ansi", "colors"), (*THEME_TOP, "ansi"))
    out: dict[str, Any] = {}
    for k in THEME_TOP:
        if not (isinstance(doc[k], str) and COLOR.fullmatch(doc[k])):
            _bad(f"{where}: {k} is not a colour (#rrggbb)")
        out[k] = doc[k]
    if not isinstance(doc["ansi"], list) or len(doc["ansi"]) != 16:
        _bad(f"{where}: ansi is not 16 colours")
    for i, c in enumerate(doc["ansi"]):
        if not (isinstance(c, str) and COLOR.fullmatch(c)):
            _bad(f"{where}: ansi {i} is not a colour")
    out["ansi"] = list(doc["ansi"])
    if "colors" in doc:
        _only(doc["colors"], f"{where}: colors", THEME_COLORS)
        out["colors"] = {}
        for k, c in doc["colors"].items():
            if not (isinstance(c, str) and COLOR.fullmatch(c)):
                _bad(f"{where}: colors.{k} is not a colour")
            out["colors"][k] = c
    return out


@dataclass
class Looks:
    doc: dict[str, Any]  # looks.json again, rewritten from the checked values
    palettes: list[dict[str, Any]]  # {id, label, dark}
    presets: list[str]  # ids, in order
    tag_looks: list[tuple[str, str]]  # (tag, preset)
    logos: list[str]  # names, sorted


def check_looks(doc: Any) -> Looks:
    """looks.json, checked against the contract and against what the Desk offers
    (lookfolder.checkLooks with looks.js builtinParts())."""
    _only(doc, "looks.json", ("schema", "palettes", "presets", "tag_looks"), ("schema",))
    if doc["schema"] != SCHEMA:
        _bad(f"looks.json is not {SCHEMA}")
    dark = dict(DESK_PALETTES)
    palettes = []
    for i, p in enumerate(_list(doc.get("palettes", []), "palettes", MAX_PALETTES)):
        where = f"palettes {i}"
        _only(p, where, ("id", "label", "dark"), ("id", "label", "dark"))
        if not (isinstance(p["id"], str) and PALETTE_ID.fullmatch(p["id"])):
            _bad(f"{where}: id is not looks-<a-z, 0-9 and ->")
        if p["id"] in dark:
            _bad(f"{where}: {p['id']} is listed twice")
        if not isinstance(p["dark"], bool):
            _bad(f"{where}: dark is not true or false")
        dark[p["id"]] = p["dark"]
        palettes.append({"id": p["id"], "label": _text(p["label"], f"{where}: label"),
                         "dark": p["dark"]})
    presets = []
    ids = set(DESK_PRESETS)
    seen: set[str] = set()
    logos: set[str] = set()
    for i, p in enumerate(_list(doc.get("presets", []), "presets", MAX_PRESETS)):
        where = f"presets {i}"
        _only(p, where, ("id", "label", "palette", "light", "finish", "type", "density", "band",
                         "brand"),
              ("id", "label", "palette", "light", "finish", "type", "density"))
        if not (isinstance(p["id"], str) and ID.fullmatch(p["id"])):
            _bad(f"{where}: id is not a-z, 0-9 and -")
        if p["id"] in seen:
            _bad(f"{where}: {p['id']} is listed twice")
        seen.add(p["id"])
        out: dict[str, Any] = {"id": p["id"], "label": _text(p["label"], f"{where}: label")}
        if not (isinstance(p["palette"], str) and dark.get(p["palette"]) is True):
            _bad(f"{where}: palette {_say(p['palette'])} is not a dark palette here or in "
                 "palettes")
        if not (isinstance(p["light"], str) and dark.get(p["light"]) is False):
            _bad(f"{where}: light {_say(p['light'])} is not a light palette here or in palettes")
        out["palette"], out["light"] = p["palette"], p["light"]
        for k, table in (("finish", FINISHES), ("type", TYPES), ("density", DENSITIES)):
            if not _str_in(p[k], table):
                _bad(f"{where}: {k} {_say(p[k])} is not one the Desk has")
            out[k] = p[k]
        if "band" in p:
            band = p["band"]
            if not (isinstance(band, list) and len(band) == 2 and all(
                    isinstance(c, str) and BAND_COLOR.fullmatch(c) for c in band)):
                _bad(f"{where}: band is not two colours (#rrggbb)")
            out["band"] = list(band)
        if "brand" in p:
            b = p["brand"]
            _only(b, f"{where}: brand", ("name", "logo", "w", "h", "round", "font"),
                  ("name", "logo"))
            if not (isinstance(b["logo"], str) and LOGO.fullmatch(b["logo"])):
                _bad(f"{where}: brand.logo is not a file name in logos/ (a-z, 0-9 and -, then "
                     ".png or .jpg)")
            logos.add(b["logo"])
            brand: dict[str, Any] = {"name": _text(b["name"], f"{where}: brand.name"),
                                     "logo": b["logo"]}
            for k, lo, hi in (("w", 8, 64), ("h", 8, 64), ("round", 0, 32)):
                if k in b:
                    brand[k] = _int(b[k], f"{where}: brand.{k}", lo, hi)
            if "font" in b:
                if not _str_in(b["font"], FONTS):
                    _bad(f"{where}: brand.font {_say(b['font'])} is not a font the Desk has")
                brand["font"] = b["font"]
            out["brand"] = brand
        presets.append(out)
        ids.add(p["id"])
    if len(logos) > MAX_LOGOS:
        _bad(f"presets name more than {MAX_LOGOS} logos")
    tag_looks: list[dict[str, str]] = []
    tags: set[str] = set()
    for i, t in enumerate(_list(doc.get("tag_looks", []), "tag_looks", MAX_TAG_LOOKS)):
        where = f"tag_looks {i}"
        _only(t, where, ("tag", "preset"), ("tag", "preset"))
        if not (isinstance(t["tag"], str) and ID.fullmatch(t["tag"])):
            _bad(f"{where}: tag is not a-z, 0-9 and -")
        if t["tag"] in tags:
            _bad(f"{where}: tag {t['tag']} is listed twice")
        if not _str_in(t["preset"], ids):
            _bad(f"{where}: preset {_say(t['preset'])} is neither here nor in presets")
        tags.add(t["tag"])
        tag_looks.append({"tag": t["tag"], "preset": t["preset"]})
    canonical: dict[str, Any] = {"schema": SCHEMA}
    for k, v in (("palettes", palettes), ("presets", presets), ("tag_looks", tag_looks)):
        if k in doc:
            canonical[k] = v
    return Looks(doc=canonical, palettes=[dict(p) for p in palettes],
                 presets=[p["id"] for p in presets],
                 tag_looks=[(t["tag"], t["preset"]) for t in tag_looks], logos=sorted(logos))


def check_logo(name: str, data: bytes | None) -> int:
    """A logo's bytes, checked: the picture its name says, by its first bytes; not too big."""
    if data is None:
        _bad(f"logos/{name} is not there")
    if len(data) > LOGO_BYTES:
        _bad(f"logos/{name} is over {LOGO_BYTES // 1024} KiB")
    if not data.startswith(MAGIC[name.rsplit(".", 1)[1]]):
        _bad(f"logos/{name} is not a {'PNG' if name.endswith('.png') else 'JPEG'} picture")
    return len(data)


def _dump(doc: Any) -> bytes:
    """A checked document as stored: compact, so never longer than the text it came from."""
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


@dataclass
class Folder:
    """A checked looks folder: its files as stored, by name in the folder."""

    looks: Looks
    themes: dict[str, dict[str, Any]]
    logos: dict[str, bytes]
    files: dict[str, bytes] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.files:
            files = {LOOKS_FILE: _dump(self.looks.doc)}
            files.update({f"palettes/{pid}.json": _dump(t) for pid, t in self.themes.items()})
            files.update({f"logos/{n}": b for n, b in self.logos.items()})
            self.files = dict(sorted(files.items()))

    @property
    def size(self) -> int:
        return sum(len(b) for b in self.files.values())

    @property
    def sha256(self) -> str:
        """One digest of the whole folder: each file's name, length and bytes, by name."""
        h = hashlib.sha256()
        for name, data in sorted(self.files.items()):
            h.update(f"{name}\n{len(data)}\n".encode())
            h.update(data)
        return h.hexdigest()


Reader = Callable[[str, int], "bytes | None"]  # (name in the folder, limit) -> bytes or None


def read_folder(read: Reader) -> Folder:
    """The folder `read` gives, read by name from looks.json and checked whole (lookfolder
    readLookFolder). Refused when anything is out of place, looks.json missing included."""
    raw = read(LOOKS_FILE, LOOKS_BYTES)
    if raw is None:
        _bad("there is no looks.json")
    if len(raw) > LOOKS_BYTES:
        _bad(f"looks.json is over {LOOKS_BYTES // 1024} KiB")
    total = len(raw)
    looks = check_looks(parse_json(raw, LOOKS_FILE))
    themes = {}
    for p in looks.palettes:
        name = f"palettes/{p['id']}.json"
        t = read(name, PALETTE_BYTES)
        if t is None:
            _bad(f"{name} is not there")
        if len(t) > PALETTE_BYTES:
            _bad(f"{name} is over {PALETTE_BYTES // 1024} KiB")
        total += len(t)
        themes[p["id"]] = check_palette(parse_json(t, name), name)
    logos = {}
    for name in looks.logos:
        data = read(f"logos/{name}", LOGO_BYTES)
        total += check_logo(name, data)
        logos[name] = data
    if total > TOTAL_BYTES:
        _bad(f"the folder is over {TOTAL_BYTES // 1024} KiB")
    folder = Folder(looks=looks, themes=themes, logos=logos)
    if folder.size > TOTAL_BYTES:  # rewritten compactly it is never larger; never stored if so
        _bad(f"the folder is over {TOTAL_BYTES // 1024} KiB")
    return folder


# --- the two ways in ----------------------------------------------------------------------------


def _read_fd(fd: int, limit: int) -> bytes:
    """Up to limit + 1 bytes (one more than allowed says it is too big)."""
    chunks, have = [], 0
    while have <= limit:
        chunk = os.read(fd, limit + 1 - have)
        if not chunk:
            break
        chunks.append(chunk)
        have += len(chunk)
    return b"".join(chunks)


class FolderReader:
    """Files of a folder on this computer, by name: no link is followed below the folder you
    name, only regular files are read, each with a hard limit."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.root = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except FileNotFoundError:
            raise FleetError(f"{path} is not there.") from None
        except NotADirectoryError:
            raise FleetError(f"{path} is not a folder.") from None
        except OSError as exc:
            raise FleetError(f"Can't open {path}: {exc.strerror or exc}.") from None

    def close(self) -> None:
        os.close(self.root)

    def __call__(self, name: str, limit: int) -> bytes | None:
        parts = name.split("/")
        opened: list[int] = []
        try:
            at = self.root
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                if last:  # a FIFO or a device is never even opened
                    try:
                        st = os.stat(part, dir_fd=at, follow_symlinks=False)
                    except FileNotFoundError:
                        return None
                    if stat.S_ISLNK(st.st_mode):
                        _bad(f"{name} is not a regular file (a link is never followed)")
                    if not stat.S_ISREG(st.st_mode):
                        _bad(f"{name} is not a regular file")
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | (
                    os.O_NONBLOCK if last else os.O_DIRECTORY)
                try:
                    fd = os.open(part, flags, dir_fd=at)
                except FileNotFoundError:
                    return None
                except OSError:  # a link (ELOOP), or not a folder where one should be
                    _bad(f"{'/'.join(parts[:i + 1])} is not a "
                         f"{'regular file' if last else 'folder'} (a link is never followed)")
                opened.append(fd)
                at = fd
            if not stat.S_ISREG(os.fstat(at).st_mode):
                _bad(f"{name} is not a regular file")
            try:
                return _read_fd(at, limit)
            except OSError as exc:
                _bad(f"{name} could not be read ({exc.strerror or exc})")
        finally:
            for fd in opened:
                os.close(fd)


def read_stream(source: BinaryIO) -> bytes:
    """A tar stream from stdin, read with a hard limit before anything else looks at it."""
    data = source.read(STREAM_BYTES + 1)
    if len(data) > STREAM_BYTES:
        _bad(f"the tar stream is over {STREAM_BYTES // 1024} KiB")
    return data


def _member_name(raw: str) -> str:
    name = raw
    while name.startswith("./"):
        name = name[2:]
    return "" if name in (".", "") else name


def tar_files(data: bytes) -> dict[str, bytes]:
    """The files of a looks folder from a tar stream (`tar -cf - -C FOLDER .`), checked
    member by member and never extracted. Refused whole for any member a looks folder
    cannot hold, or any it cannot read; read_folder then takes only what looks.json names."""
    caps = {"looks.json": LOOKS_BYTES, "palettes": PALETTE_BYTES, "logos": LOGO_BYTES}
    files: dict[str, bytes] = {}
    counts = {"palettes": 0, "logos": 0}
    members = total = 0
    try:
        tar = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except Refused:
        raise
    except Exception:  # noqa: BLE001 - tarfile raises more than TarError on crafted headers
        _bad("the input is not a tar stream (make one with tar -cf - -C FOLDER .)")
    try:
        while (m := tar.next()) is not None:
            members += 1
            if members > STREAM_MEMBERS:
                _bad(f"the tar stream has more than {STREAM_MEMBERS} members")
            name = _member_name(m.name)
            shown = _say(m.name)
            if m.type == tarfile.DIRTYPE:
                if name not in MEMBER_DIRS:
                    _bad(f"the tar stream has {shown}, which is not part of a looks folder")
                continue
            if m.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or m.issparse():
                _bad(f"the tar stream has {shown}, which is not a regular file (a link, a "
                     "device or another special file)")
            if not MEMBER.fullmatch(name):
                _bad(f"the tar stream has {shown}, which is not part of a looks folder")
            if name in files:
                _bad(f"the tar stream has {shown} twice")
            kind = name.split("/", 1)[0]
            if kind in counts:
                counts[kind] += 1
                most = MAX_PALETTES if kind == "palettes" else MAX_LOGOS
                if counts[kind] > most:
                    _bad(f"the tar stream has more than {most} files in {kind}/")
            if m.size > caps[kind]:
                _bad(f"{name} is over {caps[kind] // 1024} KiB")
            total += m.size
            if total > TOTAL_BYTES:
                _bad(f"the tar stream holds over {TOTAL_BYTES // 1024} KiB of files")
            fh = tar.extractfile(m)
            body = fh.read(m.size + 1) if fh is not None else b""
            if len(body) != m.size:
                _bad(f"{name} is cut short in the tar stream")
            files[name] = body
    except Refused:
        raise
    except Exception:  # noqa: BLE001 - RecursionError, ValueError, ... from crafted headers
        _bad("the tar stream could not be read (cut short or damaged)")
    return files


def from_files(files: dict[str, bytes]) -> Reader:
    return lambda name, limit: files.get(name)


# --- the store ----------------------------------------------------------------------------------


def store_path() -> Path:
    return paths.data_dir() / FOLDER / STORE


@dataclass
class Stored:
    folder: Folder
    published_at: str
    source: dict[str, Any]


def save(folder: Folder, source: dict[str, Any], published_at: str) -> None:
    """Stores the checked folder in place of what was there, atomically."""
    source = check_source(source, "from")
    doc = {"version": STORE_VERSION, "published_at": published_at, "from": source,
           "sha256": folder.sha256,
           "files": {n: base64.b64encode(b).decode("ascii") for n, b in folder.files.items()}}
    path = store_path()
    paths.private_dir(path.parent)
    paths.write_private(path, (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode())


def clear() -> bool:
    """Forgets the stored folder. True when there was one."""
    try:
        store_path().unlink()
        return True
    except FileNotFoundError:
        return False


SOURCE_FIELDS = ("how", "folder", "address", "actor", "actor_key")
SOURCE_TEXT = re.compile(r"[^\x00-\x1f\x7f-\x9f\u200e\u200f\u202a-\u202e\u2066-\u2069]{1,200}")
TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
FINGERPRINT = re.compile(r"SHA256:[A-Za-z0-9+/]{43}")


def check_source(src: Any, where: str = f"{STORE}: from") -> dict[str, Any]:
    """Where a publish came from, as kept: each field null or one short line of plain text
    (no control or direction characters), and of its own shape."""
    _only(src, where, SOURCE_FIELDS, SOURCE_FIELDS)
    for k in SOURCE_FIELDS:
        v = src[k]
        if v is not None and not (isinstance(v, str) and SOURCE_TEXT.fullmatch(v)):
            _bad(f"{where}.{k} is not a short line of text")
    if src["how"] not in ("folder", "stdin"):
        _bad(f"{where}.how is neither folder nor stdin")
    if src["folder"] is not None and src["how"] != "folder":
        _bad(f"{where}.folder is given for a stream")
    if src["actor_key"] is not None and not FINGERPRINT.fullmatch(src["actor_key"]):
        _bad(f"{where}.actor_key is not a key's fingerprint")
    if src["address"] is not None:
        try:
            ipaddress.ip_address(src["address"])
        except ValueError:
            _bad(f"{where}.address is not an address")
    return {k: src[k] for k in SOURCE_FIELDS}


def load() -> Stored | None:
    """The stored folder, checked again from the start; None when nothing is stored.
    Refused (with why) for a store this code did not write or no longer accepts."""
    path = store_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        _bad(f"{STORE} is not a regular file")
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            _bad(f"{STORE} is not a regular file")
        raw = _read_fd(fd, STORE_BYTES)
    finally:
        os.close(fd)
    if len(raw) > STORE_BYTES:
        _bad(f"{STORE} is too big")
    doc = parse_json(raw, STORE)
    _only(doc, STORE, ("version", "published_at", "from", "sha256", "files"),
          ("version", "published_at", "from", "sha256", "files"))
    if type(doc["version"]) is not int or doc["version"] != STORE_VERSION:
        _bad(f"{STORE} is not a version this FleetLedger knows")
    if not (isinstance(doc["published_at"], str) and TIMESTAMP.fullmatch(doc["published_at"])):
        _bad(f"{STORE}: published_at is not a time")
    source = check_source(doc["from"])
    if not isinstance(doc["files"], dict):
        _bad(f"{STORE}: files is not an object")
    files = {}
    for name, b64 in doc["files"].items():
        if not MEMBER.fullmatch(name) or not isinstance(b64, str):
            _bad(f"{STORE} has a file it cannot hold, {_say(name)}")
        try:
            files[name] = base64.b64decode(b64, validate=True)
        except ValueError:
            _bad(f"{STORE}: {name} is not base64")
    folder = read_folder(from_files(files))
    if folder.files != dict(sorted(files.items())):
        _bad(f"{STORE} holds files other than the checked folder's")
    if doc["sha256"] != folder.sha256:
        _bad(f"{STORE}: its digest does not match its files")
    return Stored(folder=folder, published_at=doc["published_at"], source=source)
