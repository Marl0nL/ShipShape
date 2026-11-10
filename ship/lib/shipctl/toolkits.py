"""Your own toolkits on this Ship: `ship-ctl toolkit apply|remove|list` (contracts/toolkit.md).

FleetLedger packs each toolkit on its own (a gzip tar with `toolkit.json` at the top:
id, version, content hash, created_at) and sends it only to Ships that may have it.
`apply` checks the archive and unpacks it, root-owned, into
/opt/shipshape/toolkits/<id>/<version>-<hash12>/, switches that toolkit's `current`
link, and records the release in ship.toml `[toolkits]`.

That table is the allow-list: ship-ctl reads recipes, profiles and sign-ins from a
toolkit only through it (`resolve`, `root_of`), from the release folder it names, so a
toolkit's files lying on disk without an entry there are never run. `run_as = "root"`
recipes run only when FleetLedger said `--allow-root` when that release was applied
(/var/lib/shipshape/toolkits.json).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath

from . import schema, shiptoml
from .context import Context, utc_now
from .errors import CtlError
from .fsutil import ensure_dir, read_json_dict, write_json
from .paths import Paths

MANIFEST = "toolkit.json"
MANIFEST_SCHEMA = "shipshape.toolkit/1"
MAX_ARCHIVE = 100 * 1024 * 1024  # bytes, as sent; FleetLedger refuses to pack more
MAX_UNPACKED = 512 * 1024 * 1024
MAX_MEMBERS = 20000
KEEP_RELEASES = 3
ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?"
RELEASE = re.compile(rf"^({VERSION})-([0-9a-f]{{12}})$")
# What a toolkit recipe's apt entries must be, checked again here in case FleetLedger
# was bypassed (fleetledger/toolkits.py has the same rule).
# A Debian package name, whole: no .deb file, path, repository or version. Never a
# trailing "-", which apt-get install reads as "remove this package".
APT_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]*[a-z0-9+]")
_BAD_NAME = re.compile(r"[\x00-\x1f\x7f\\]")


# --- where things are --------------------------------------------------------------------
def base(paths: Paths) -> Path:
    return paths.opt / "toolkits"


def record_path(paths: Paths) -> Path:
    return paths.state / "toolkits.json"


def allowed(ship: shiptoml.ShipToml) -> dict[str, str]:
    """ship.toml [toolkits]: toolkit id -> release ("<version>-<hash12>")."""
    return dict(ship.doc.get("toolkits", {}))


def version_of(release: str) -> str:
    m = RELEASE.match(release)
    return m.group(1) if m else release


def _record(paths: Paths) -> dict:
    return read_json_dict(record_path(paths))


def root_allowed(paths: Paths, kit: str, release: str) -> bool:
    """Did FleetLedger allow root recipes when it applied this very release?"""
    rec = _record(paths).get(kit)
    return isinstance(rec, dict) and rec.get("release") == release and \
        rec.get("allow_root") is True


def _trusted(paths: Paths, path: Path) -> bool:
    """A folder ship-ctl may read code from: a real folder (no symlink), and on a real
    Ship owned by root and writable by root alone (an agent cannot change it)."""
    try:
        st = path.lstat()
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if paths.sandbox:
        return True
    return st.st_uid == 0 and not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def root_of(paths: Paths, ship: shiptoml.ShipToml, kit: str) -> Path:
    """The release folder the allow-list names for `kit`. Raises tool-failed when the
    toolkit is not in ship.toml [toolkits], or its folder is missing or not root's."""
    release = allowed(ship).get(kit)
    if release is None:
        raise CtlError("tool-failed", f"toolkit {kit} is not on this Ship (not in ship.toml "
                                      f"[toolkits]; FleetLedger sends it: ship-fleet toolkit "
                                      f"push {kit})")
    folder = base(paths) / kit
    root = folder / release
    if not (_trusted(paths, base(paths)) and _trusted(paths, folder) and _trusted(paths, root)):
        raise CtlError("tool-failed", f"toolkit {kit} {release}: its folder is missing or not "
                                      "root's (send it again: ship-fleet toolkit push)")
    return root


def split(item_id: str) -> tuple[str | None, str]:
    """'kit/x' -> ('kit', 'x'); 'x' -> (None, 'x')."""
    kit, _, own = item_id.rpartition("/")
    return (kit or None), own


def profile_path(paths: Paths, ship: shiptoml.ShipToml, pid: str) -> tuple[Path, str | None]:
    """A profile's file and its toolkit (None: the catalogue's)."""
    kit, own = split(pid)
    if kit is None:
        return paths.profiles / f"{own}.toml", None
    return root_of(paths, ship, kit) / "profiles" / f"{own}.toml", kit


def profile_tools(paths: Paths, ship: shiptoml.ShipToml, pid: str, doc: dict) -> list[str]:
    """A profile's tools as ship.toml names them. In a toolkit's profile a bare id is the
    toolkit's own tool if it has one (as FleetLedger reads it), else a catalogue tool."""
    kit, _ = split(pid)
    tools = [str(t) for t in doc.get("tools", [])]
    if kit is None:
        return tools
    root = root_of(paths, ship, kit)
    out = []
    for ref in tools:
        other, name = split(ref)
        if other is not None and other != kit:
            raise CtlError("tool-failed", f"profile {pid}: {ref} is from another toolkit")
        if other == kit or (root / "tools" / f"{name}.toml").is_file():
            out.append(f"{kit}/{name}")
        else:
            out.append(name)
    return out


def available(paths: Paths, ship: shiptoml.ShipToml) -> list[str]:
    """kit/tool for every recipe of every toolkit in the allow-list."""
    out = []
    for kit in sorted(allowed(ship)):
        try:
            root = root_of(paths, ship, kit)
        except CtlError:
            continue
        out += [f"{kit}/{p.stem}" for p in sorted((root / "tools").glob("*.toml"))]
    return out


def signin_folders(paths: Paths) -> list[Path]:
    """The sign-in folders of the toolkits in the allow-list (signins.registry)."""
    try:
        ship = shiptoml.load(paths)
    except CtlError:
        return []
    folders = []
    for kit in sorted(allowed(ship)):
        try:
            folder = root_of(paths, ship, kit) / "signins"
        except CtlError:
            continue
        if folder.is_dir():
            folders.append(folder)
    return folders


# --- versions ----------------------------------------------------------------------------
def _semver_key(version: str) -> tuple:
    """Precedence as semver.org says: numbers, then a pre-release sorts before the
    release; build metadata (+...) is ignored."""
    core, _, _build = version.partition("+")
    core, _, pre = core.partition("-")
    nums = tuple(int(x) for x in core.split("."))
    if not pre:
        return (nums, 1, ())
    parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split("."))
    return (nums, 0, parts)


def older(a: str, b: str) -> bool:
    """Is version a lower than version b?"""
    return _semver_key(a) < _semver_key(b)


# --- the archive ---------------------------------------------------------------------------
def content_hash(root: Path) -> str:
    """The content hash (contracts/toolkit.md): one line per file but toolkit.json,
    sorted by path, "<sha256> <755|644> <path>\\n"; the SHA-256 of those lines."""
    lines = []
    for top, dirs, files in os.walk(root):
        dirs.sort()
        here = Path(top)
        for name in files:
            full = here / name
            rel = full.relative_to(root).as_posix()
            if rel == MANIFEST:
                continue
            st = full.lstat()
            if not stat.S_ISREG(st.st_mode):
                raise CtlError("invalid-toolkit", f"{rel}: not a regular file")
            digest = hashlib.sha256()
            with full.open("rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(block)
            mode = "755" if st.st_mode & stat.S_IXUSR else "644"
            lines.append((rel.encode("utf-8"), f"{digest.hexdigest()} {mode} {rel}\n"))
    lines.sort()
    return hashlib.sha256("".join(line for _, line in lines).encode("utf-8")).hexdigest()


def _verify_sidecar(path: Path) -> None:
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.exists():
        return
    try:
        expected = sidecar.read_text(encoding="utf-8").split()[0].lower()
    except (OSError, IndexError) as exc:
        raise CtlError("invalid-toolkit", f"cannot read {sidecar.name}") from exc
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise CtlError("invalid-toolkit", "the archive does not match its .sha256 file")


def _extract(path: Path, dest: Path) -> None:
    """Unpack the archive into dest: relative paths, regular files and folders only."""
    try:
        tar = tarfile.open(path, "r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise CtlError("invalid-toolkit", "not a gzip-compressed tar archive") from exc
    with tar:
        members: list[tarfile.TarInfo] = []
        try:
            for m in tar:  # counted as read: a flood of entries stops early
                members.append(m)
                if len(members) > MAX_MEMBERS:
                    raise CtlError("invalid-toolkit",
                                   f"the archive has more than {MAX_MEMBERS} entries")
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise CtlError("invalid-toolkit", "the archive is damaged") from exc
        total = 0
        seen: set[str] = set()
        for m in members:
            name = PurePosixPath(m.name)
            try:
                m.name.encode("utf-8")
            except UnicodeEncodeError:
                raise CtlError("invalid-toolkit", "a file name in the archive is not "
                                                  "UTF-8") from None
            if (name.is_absolute() or not name.parts or ".." in name.parts
                    or _BAD_NAME.search(m.name)):
                raise CtlError("invalid-toolkit", f"unsafe path in the archive: {m.name[:200]!r}")
            if m.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) \
                    or m.sparse is not None:
                raise CtlError("invalid-toolkit", "links and devices are not allowed: "
                                                  f"{m.name[:200]!r}")
            key = name.as_posix()
            if key in seen:
                raise CtlError("invalid-toolkit", f"{key[:200]!r} is in the archive twice")
            seen.add(key)
            total += m.size
        if total > MAX_UNPACKED:
            raise CtlError("invalid-toolkit", "the archive unpacks to more than "
                                              f"{MAX_UNPACKED // (1024 * 1024)} MB")
        try:
            tar.extractall(dest, members=members, filter="data")
        except (OSError, tarfile.TarError, EOFError) as exc:
            raise CtlError("invalid-toolkit", f"the archive does not unpack: {exc}") from exc


def _settle(paths: Paths, root: Path) -> None:
    """Root's, folders 0755 and files 0644 (0755 if executable): nothing the agent can
    change, no setuid bit."""
    for top, dirs, files in os.walk(root):
        for name in [*dirs, *files]:
            full = Path(top) / name
            st = full.lstat()
            if stat.S_ISDIR(st.st_mode):
                mode = 0o755
            elif stat.S_ISREG(st.st_mode):
                mode = 0o755 if st.st_mode & stat.S_IXUSR else 0o644
            else:
                raise CtlError("invalid-toolkit", f"{name}: not a file or folder")
            if not paths.sandbox:
                os.chown(full, 0, 0, follow_symlinks=False)
            os.chmod(full, mode)
    if not paths.sandbox:
        os.chown(root, 0, 0)
    os.chmod(root, 0o755)


def _manifest(ctx: Context, root: Path) -> dict:
    try:
        doc = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CtlError("invalid-toolkit", f"no {MANIFEST} at the top of the archive") from None
    except (OSError, ValueError) as exc:
        raise CtlError("invalid-toolkit", f"{MANIFEST} is not valid JSON") from exc
    errors = schema.load(ctx.paths.schema("toolkit-manifest")).errors(doc)
    if errors:
        raise CtlError("invalid-toolkit", f"{MANIFEST}: " + "; ".join(errors[:3]))
    try:
        with (root / "toolkit.toml").open("rb") as fh:
            toml = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CtlError("invalid-toolkit", "no readable toolkit.toml in the archive") from exc
    if not ID.fullmatch(doc["id"]) or not re.fullmatch(VERSION, doc["version"]):
        raise CtlError("invalid-toolkit", f"{MANIFEST}: id or version is not one")
    if toml.get("id") != doc["id"] or toml.get("version") != doc["version"]:
        raise CtlError("invalid-toolkit", f"{MANIFEST} says {doc['id']} {doc['version']}, "
                                          "toolkit.toml does not")
    return doc


def _check_contents(ctx: Context, ship: shiptoml.ShipToml, kit: str, root: Path) -> None:
    """Recipes and profiles follow their schemas; the sign-in files load together with
    the bundle's and the other toolkits' (as converge checks a bundle's)."""
    for folder, contract in (("tools", "tool-recipe"), ("profiles", "profile")):
        validator = schema.load(ctx.paths.schema(contract))
        for path in sorted((root / folder).glob("*.toml")):
            try:
                with path.open("rb") as fh:
                    doc = tomllib.load(fh)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise CtlError("invalid-toolkit", f"{folder}/{path.name}: not valid TOML") \
                    from exc
            errors = validator.errors(doc)
            if errors or doc.get("id") != path.stem:
                raise CtlError("invalid-toolkit", f"{folder}/{path.name}: "
                                                  f"{'; '.join(errors[:3]) or 'id'}")
    if (root / "signins").is_dir():
        from . import signins

        others = []
        for other in sorted(allowed(ship)):
            if other == kit:
                continue
            with contextlib.suppress(CtlError):
                folder = root_of(ctx.paths, ship, other) / "signins"
                if folder.is_dir():
                    others.append(folder)
        try:
            signins.build([ctx.paths.signins, *others, root / "signins"],
                          ctx.paths.schema("signin"))
        except CtlError as exc:
            raise CtlError("invalid-toolkit", exc.message) from None


def _clear_leftovers(top: Path, older_than: float = 3600) -> None:
    """Remove what an apply cut short left (.unpack-*), once it is an hour old."""
    import time

    for d in top.glob(".unpack-*"):
        with contextlib.suppress(OSError):
            if time.time() - d.lstat().st_mtime > older_than:
                shutil.rmtree(d)


def _point_current(folder: Path, release: str) -> None:
    tmp = folder / f".current-{secrets.token_hex(4)}"
    os.symlink(release, tmp)
    os.replace(tmp, folder / "current")


def _prune(folder: Path, keep: str) -> None:
    dirs = sorted((d for d in folder.iterdir()
                   if d.is_dir() and not d.is_symlink() and not d.name.startswith(".")),
                  key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[KEEP_RELEASES:]:
        if d.name != keep:
            shutil.rmtree(d, ignore_errors=True)


def _save_allow_list(ctx: Context, ship: shiptoml.ShipToml, kit: str,
                     release: str | None) -> shiptoml.ShipToml:
    doc = json.loads(json.dumps(ship.doc))
    table = doc.setdefault("toolkits", {})
    if release is None:
        table.pop(kit, None)
    else:
        table[kit] = release
    if not table:
        doc.pop("toolkits")
    errors = shiptoml.check(doc, ctx.paths)
    if errors:
        raise CtlError("invalid-ship-toml", "; ".join(errors[:5]))
    shiptoml.save(doc, ctx.paths.ship_toml)
    return shiptoml.ShipToml(doc)


def _write_record(paths: Paths, kit: str, entry: dict | None) -> None:
    doc = _record(paths)
    if entry is None:
        doc.pop(kit, None)
    else:
        doc[kit] = entry
    write_json(record_path(paths), doc, 0o644, indent=2)


def own_items(paths: Paths, ship: shiptoml.ShipToml, kit: str) -> tuple[list[str], list[str]]:
    """The tools and profiles ship.toml asks for from `kit`, its profiles' tools included."""
    profiles = [p for p in ship.profiles if split(p)[0] == kit]
    tools = [t for t in ship.tools if split(t)[0] == kit]
    for pid in ship.profiles:
        try:
            path, _ = profile_path(paths, ship, pid)
            with path.open("rb") as fh:
                listed = profile_tools(paths, ship, pid, tomllib.load(fh))
        except (CtlError, OSError, tomllib.TOMLDecodeError):
            continue
        tools += [t for t in listed if split(t)[0] == kit and t not in tools]
    return tools, profiles


def apply(ctx: Context, ship: shiptoml.ShipToml, archive: str, *, force: bool = False,
          allow_root: bool = False) -> tuple[dict, list[str], list[str]]:
    """`toolkit apply --archive PATH`. Returns (data, changed, messages)."""
    path = Path(archive)
    try:
        st = path.lstat()
    except OSError:
        raise CtlError("invalid-toolkit", f"no archive at {archive}") from None
    if not stat.S_ISREG(st.st_mode):
        raise CtlError("invalid-toolkit", f"{archive} is not a regular file")
    if st.st_size > MAX_ARCHIVE:
        raise CtlError("invalid-toolkit", f"the archive is {st.st_size // (1024 * 1024)} MB; "
                                          f"a toolkit archive may be "
                                          f"{MAX_ARCHIVE // (1024 * 1024)} MB at most")
    _verify_sidecar(path)

    top = base(ctx.paths)
    ensure_dir(top, 0o755)
    os.chmod(top, 0o755)
    _clear_leftovers(top)
    tmp = Path(tempfile.mkdtemp(prefix=".unpack-", dir=top))
    try:
        _extract(path, tmp)
        manifest = _manifest(ctx, tmp)
        if content_hash(tmp) != manifest["content_sha256"]:
            raise CtlError("invalid-toolkit", "the files do not match the content hash in "
                                              f"{MANIFEST} (changed on the way?)")
        kit, version = manifest["id"], manifest["version"]
        release = f"{version}-{manifest['content_sha256'][:12]}"
        before = allowed(ship).get(kit)
        if before is not None and older(version, version_of(before)) and not force:
            raise CtlError("toolkit-rollback",
                           f"{kit} {version} is older than the {version_of(before)} on this "
                           "Ship; not rolled back (apply it with --force to roll back on "
                           "purpose)")
        _check_contents(ctx, ship, kit, tmp)
        _settle(ctx.paths, tmp)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    folder = top / kit
    dest = folder / release
    aside = None
    new_folder = not folder.exists()
    try:
        ensure_dir(folder, 0o755)
        if not ctx.paths.sandbox:
            os.chown(folder, 0, 0)
        os.chmod(folder, 0o755)
        if dest.exists() or dest.is_symlink():
            aside = folder / f".old-{release}-{secrets.token_hex(4)}"
            os.rename(dest, aside)
        os.rename(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        if aside is not None and not dest.exists():
            os.rename(aside, dest)
        if new_folder:
            shutil.rmtree(folder, ignore_errors=True)
        raise
    previous = os.readlink(folder / "current") if (folder / "current").is_symlink() else None
    old_record = _record(ctx.paths).get(kit)
    try:
        _point_current(folder, release)
        # The record first: if ship.toml then fails, the allow-list still names the old
        # release, which the record no longer allows root for (refused, never widened).
        _write_record(ctx.paths, kit, {"release": release, "version": version,
                                       "allow_root": bool(allow_root), "applied_at": utc_now()})
        ship = _save_allow_list(ctx, ship, kit, release)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        if aside is not None:
            os.rename(aside, dest)
            aside = None
        if previous is not None:
            _point_current(folder, previous)
        elif (folder / "current").is_symlink():
            (folder / "current").unlink()
        with contextlib.suppress(OSError):
            _write_record(ctx.paths, kit, old_record if isinstance(old_record, dict) else None)
        raise
    finally:
        if aside is not None:
            shutil.rmtree(aside, ignore_errors=True)
    with contextlib.suppress(OSError):
        _prune(folder, release)

    changed = [ctx.paths.agent_path(dest), ctx.paths.agent_path(ctx.paths.ship_toml)]
    rolled = before is not None and older(version, version_of(before))
    what = "rolled back to" if rolled else ("now on" if before != release else "already on")
    messages = [f"Toolkit {kit}: {what} {version} ({release})."]
    if before is not None and before != release:
        messages.append(f"It was on {before}.")
    data = {"toolkit": {"id": kit, "version": version, "release": release,
                        "previous": before, "allow_root": bool(allow_root)}}

    # Its tools and profiles this Ship asks for: install them from the new release.
    tools, profiles = own_items(ctx.paths, ship, kit)
    if tools or profiles:
        from . import tools as tools_mod

        failed: list[str] = []
        if tools:
            try:
                ran, did, notes = tools_mod.run(ctx, ship, tools)
            except CtlError as exc:
                ran, did, notes = exc.data or {}, exc.changed, exc.messages
                failed.append(exc.message)
            data["installed"] = ran.get("installed", {})
            changed += did
            messages += notes
        if profiles:
            states, notes = tools_mod.run_profiles(ctx, ship, profiles)
            data["profiles"] = states
            messages += notes
            failed += [f"profile {p} failed" for p, s in states.items() if s == "failed"]
        if failed:
            raise CtlError("tool-failed", f"toolkit {kit} {version} is applied, but its tools "
                                          f"did not all install: {'; '.join(failed)}"
                           ).with_data(data, changed, messages)
    return data, changed, messages


def remove(ctx: Context, ship: shiptoml.ShipToml, kit: str) -> tuple[dict, list[str], list[str]]:
    """`toolkit remove ID`: uninstall its tools, drop them and the toolkit from
    ship.toml, then delete its files."""
    if not ID.fullmatch(kit):
        raise CtlError("usage", f"{kit!r} is not a toolkit id")
    folder = base(ctx.paths) / kit
    listed = kit in allowed(ship)
    if not listed and not (folder.exists() or folder.is_symlink()):
        raise CtlError("usage", f"there is no toolkit {kit} on this Ship")
    from . import tools as tools_mod

    messages: list[str] = []
    changed: list[str] = []
    tools, profiles = own_items(ctx.paths, ship, kit)
    recorded = read_json_dict(ctx.paths.state / "tools.json")
    tools += [t for t in recorded if split(t)[0] == kit and t not in tools]
    uninstalled: list[str] = []
    if listed:
        uninstalled, notes = tools_mod.uninstall(ctx, ship, tools)
        messages += notes

    doc = json.loads(json.dumps(ship.doc))
    section = doc.get("tools", {})
    if section:
        section["install"] = [t for t in section.get("install", []) if split(t)[0] != kit]
        section["profiles"] = [p for p in section.get("profiles", []) if split(p)[0] != kit]
    for pid in list(doc.get("profile_options", {})):
        if split(pid)[0] == kit:
            del doc["profile_options"][pid]
    if "profile_options" in doc and not doc["profile_options"]:
        del doc["profile_options"]
    doc.get("toolkits", {}).pop(kit, None)
    if "toolkits" in doc and not doc["toolkits"]:
        del doc["toolkits"]
    errors = shiptoml.check(doc, ctx.paths)
    if errors:
        raise CtlError("invalid-ship-toml", "; ".join(errors[:5]))
    if shiptoml.save(doc, ctx.paths.ship_toml):
        changed.append(ctx.paths.agent_path(ctx.paths.ship_toml))
    ship = shiptoml.ShipToml(doc)
    tools_mod.forget(ctx, ship, [*tools, *profiles])
    _write_record(ctx.paths, kit, None)
    if folder.exists() or folder.is_symlink():
        if folder.is_symlink() or not folder.is_dir():
            folder.unlink()
        else:
            shutil.rmtree(folder)
        changed.append(ctx.paths.agent_path(folder))
    messages.insert(0, f"Removed toolkit {kit}" + (f" and its tools ({', '.join(tools)})"
                                                   if tools else "") + ".")
    return {"removed": kit, "tools": tools, "uninstalled": uninstalled,
            "profiles": profiles}, changed, messages


def listing(ctx: Context, ship: shiptoml.ShipToml) -> list[dict]:
    """Each toolkit in the allow-list: id, version, release, root allowed, its tools,
    and whether its folder is there."""
    out = []
    for kit, release in sorted(allowed(ship).items()):
        entry = {"id": kit, "version": version_of(release), "release": release,
                 "allow_root": root_allowed(ctx.paths, kit, release), "state": "ok",
                 "tools": []}
        try:
            root = root_of(ctx.paths, ship, kit)
            entry["tools"] = [p.stem for p in sorted((root / "tools").glob("*.toml"))]
        except CtlError:
            entry["state"] = "missing"
        out.append(entry)
    return out


def status_section(ctx: Context, ship: shiptoml.ShipToml) -> list[dict]:
    """The status document's `toolkits`: id, version, release, state, and whether its
    root recipes may run (FleetLedger sends it again when that is not what it wants)."""
    return [{k: e[k] for k in ("id", "version", "release", "state", "allow_root")}
            for e in listing(ctx, ship)]
