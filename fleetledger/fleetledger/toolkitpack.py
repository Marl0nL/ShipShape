"""Pack one of your toolkits into the archive FleetLedger sends to a Ship
(contracts/toolkit.md, "On a Ship").

The archive is a gzip tar: `toolkit.json` (contracts/toolkit-manifest.schema.json: id,
version, content hash, created_at) at the top, then the toolkit's own files, as
`toolkits.load` checked them. Like the tooling bundle (seed/bundle.py) it is built
reproducibly: sorted entries, owner root, fixed modes and times, no symlinks. Each
toolkit is packed on its own, so a Ship that may not have a toolkit never gets its files.

`ship-ctl toolkit apply` checks the content hash against the files it unpacked, and
names the release <version>-<first 12 of the hash>.

Packed archives are kept in FleetLedger's data folder (toolkit-archives/), so an
unchanged toolkit is sent as the same bytes each time without packing it again;
`ship-fleet migrate` leaves that folder (it is rebuilt when next needed).
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import stat
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from fleetledger import paths, schemas
from fleetledger.errors import FleetError
from fleetledger.inventory import now_timestamp
from fleetledger.seed.bundle import EXCLUDED_DIRS, EXCLUDED_NAMES, EXCLUDED_SUFFIXES

MANIFEST = "toolkit.json"
SCHEMA = "shipshape.toolkit/1"
MAX_BYTES = 100 * 1024 * 1024  # as sent; the Ship refuses more (shipctl/toolkits.py)
CACHE = "toolkit-archives"
_BAD_NAME = tuple(chr(c) for c in [*range(0x20), 0x7F]) + ("\\",)


@dataclass(frozen=True)
class Archive:
    data: bytes
    manifest: dict[str, Any]

    @property
    def id(self) -> str:
        return self.manifest["id"]

    @property
    def version(self) -> str:
        return self.manifest["version"]

    @property
    def release(self) -> str:
        """The release folder name on the Ship: <version>-<content hash 12>."""
        return f"{self.version}-{self.manifest['content_sha256'][:12]}"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _excluded(rel: PurePosixPath) -> bool:
    if any(part in EXCLUDED_DIRS for part in rel.parts):
        return True
    return rel.name in EXCLUDED_NAMES or rel.name.endswith(EXCLUDED_SUFFIXES)


def contents(kit: Any) -> list[tuple[str, bytes, int]]:
    """(path, bytes, mode) of every file to pack, sorted by path. The vendored files are
    checked against their pins again here, as read: what is sent is what was checked."""
    from fleetledger import toolkits

    root: Path = kit.root
    found, problems = toolkits._walk(root)
    if problems:
        raise FleetError(f"Toolkit {kit.id} changed since it was checked: {problems[0]}.")
    out = []
    for path in found:
        rel = PurePosixPath(path.relative_to(root).as_posix())
        if _excluded(rel):
            continue
        name = rel.as_posix()
        if name == MANIFEST:
            raise FleetError(f"Toolkit {kit.id}: {MANIFEST} at its top is reserved for the "
                             "archive's manifest; rename that file.")
        if any(c in name for c in _BAD_NAME):
            raise FleetError(f"Toolkit {kit.id}: a file name with a control character or "
                             f"backslash ({name!r}) cannot be sent.")
        data = path.read_bytes()
        pinned = kit.files.get(name)
        if pinned is not None and hashlib.sha256(data).hexdigest() != pinned:
            raise FleetError(f"Toolkit {kit.id}: {name} no longer matches its SHA-256 in "
                             "toolkit.toml.", hint=f"ship-fleet toolkit check {kit.id}")
        mode = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
        out.append((name, data, mode))
    return sorted(out, key=lambda e: e[0].encode("utf-8"))


def content_hash(files: list[tuple[str, bytes, int]]) -> str:
    """contracts/toolkit.md: one line per file, sorted by path (UTF-8 bytes),
    "<sha256> <755|644> <path>\\n"; the SHA-256 of those lines."""
    lines = "".join(f"{hashlib.sha256(data).hexdigest()} {mode:o} {name}\n"
                    for name, data, mode in sorted(files, key=lambda e: e[0].encode("utf-8")))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _info(name: str, mode: int, size: int, mtime: int, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.size = size
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


def pack(kit: Any, created_at: str | None = None, contracts: Path | None = None,
         files: list[tuple[str, bytes, int]] | None = None) -> Archive:
    """Pack a loaded toolkit (toolkits.Toolkit). The same files and created_at always
    give the same bytes."""
    files = contents(kit) if files is None else files
    created_at = created_at or now_timestamp()
    manifest = {"schema": SCHEMA, "id": kit.id, "version": kit.version,
                "content_sha256": content_hash(files), "created_at": created_at}
    schemas.check("toolkit-manifest", manifest, MANIFEST, contracts)
    mtime = int(datetime.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S")
                .replace(tzinfo=UTC).timestamp())
    entries: list[tuple[str, bytes | None, int]] = [
        (MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode(), 0o644)]
    dirs: set[str] = set()
    for name, data, mode in files:
        for parent in list(PurePosixPath(name).parents)[:-1]:
            dirs.add(str(parent))
        entries.append((name, data, mode))
    entries += [(d, None, 0o755) for d in dirs]
    entries.sort(key=lambda e: e[0].encode("utf-8"))

    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=6) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name, data, mode in entries:
                if data is None:
                    tar.addfile(_info(name, mode, 0, mtime, tarfile.DIRTYPE))
                else:
                    tar.addfile(_info(name, mode, len(data), mtime, tarfile.REGTYPE),
                                io.BytesIO(data))
                if raw.tell() > MAX_BYTES:
                    break
    data = raw.getvalue()
    if len(data) > MAX_BYTES:
        raise FleetError(
            f"Toolkit {kit.id} packs to more than {MAX_BYTES // (1024 * 1024)} MiB, the limit "
            "for a toolkit archive.",
            hint="Keep large files out of the toolkit folder, or split it into two toolkits.")
    return Archive(data=data, manifest=manifest)


def cache_dir() -> Path:
    return paths.data_dir() / CACHE


def archive(kit: Any, contracts: Path | None = None) -> Archive:
    """The archive to send for a loaded toolkit: the one kept from last time while the
    toolkit's files are unchanged (same bytes), else packed now and kept."""
    files = contents(kit)
    digest = content_hash(files)
    folder = cache_dir()
    kept = folder / f"{kit.id}.tar.gz"
    with contextlib.suppress(OSError, ValueError, tarfile.TarError, KeyError):
        data = kept.read_bytes()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            member = tar.extractfile(MANIFEST)
            manifest = json.loads(member.read()) if member else {}
        if manifest.get("content_sha256") == digest and manifest.get("id") == kit.id and \
                manifest.get("version") == kit.version and \
                not schemas.errors("toolkit-manifest", manifest, contracts):
            return Archive(data=data, manifest=manifest)
    packed = pack(kit, contracts=contracts, files=files)
    with contextlib.suppress(OSError):
        paths.private_dir(folder)
        paths.write_private(kept, packed.data)
    return packed


def drop_cached(kit_id: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(cache_dir() / f"{kit_id}.tar.gz")
