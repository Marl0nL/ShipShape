"""Pack the repo's ship/ tree into a tooling bundle (contracts/seed.md).

The bundle is a gzip tar with relative paths only: bundle.json and versions.toml
at the root, then everything under ship/ (minus tests, node_modules and editor
or cache files). It is built reproducibly: sorted entries, owner root, fixed
modes and times, so the same tree and timestamp always give the same bytes.
"""

from __future__ import annotations

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

from fleetledger import schemas
from fleetledger.errors import FleetError
from fleetledger.inventory import now_timestamp
from fleetledger.paths import write_private
from fleetledger.repo import Repo

MAX_BYTES = 5 * 1024 * 1024
REQUIRED_EXECUTABLES = ("provision/ship-provision", "bin/ship-ctl")
EXCLUDED_DIRS = {"tests", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".git"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", "~", ".orig", ".rej")
EXCLUDED_NAMES = {".DS_Store", "Thumbs.db"}


@dataclass(frozen=True)
class Bundle:
    data: bytes
    manifest: dict

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    @property
    def release(self) -> str:
        """The release folder name on the Ship: <tooling_version>-<commit12>."""
        return f"{self.manifest['tooling_version']}-{self.manifest['commit'][:12]}"

    @property
    def checksum_line(self) -> str:
        """Contents of bundle.sha256, as `sha256sum -c` reads it."""
        return f"{self.sha256}  bundle.tar.gz\n"


def _excluded(rel: PurePosixPath) -> bool:
    if rel.parts and rel.parts[0] == "tests":  # ship/tests/
        return True
    if any(part in EXCLUDED_DIRS - {"tests"} for part in rel.parts):
        return True
    return rel.name in EXCLUDED_NAMES or rel.name.endswith(EXCLUDED_SUFFIXES)


def ship_files(repo: Repo) -> list[PurePosixPath]:
    """Files to pack, relative to ship/."""
    listed = repo.git_files("ship")
    if listed is None:
        listed = [
            str(p.relative_to(repo.root))
            for p in sorted(repo.ship_dir.rglob("*"))
            if p.is_file() or p.is_symlink()
        ]
    files = []
    for name in listed:
        rel = PurePosixPath(name).relative_to("ship")
        if _excluded(rel):
            continue
        if (repo.ship_dir / rel).is_symlink():
            raise FleetError(f"ship/{rel} is a symlink; bundles carry regular files only.")
        files.append(rel)
    return sorted(files)


def _info(name: str, mode: int, size: int, mtime: int, kind: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.size = size
    info.mtime = mtime
    info.uid = info.gid = 0
    info.uname = info.gname = "root"
    return info


def pack(repo: Repo, created_at: str | None = None) -> Bundle:
    """Pack ship/ from the checkout into a bundle."""
    files = ship_files(repo)
    present = {str(f) for f in files}
    for required in REQUIRED_EXECUTABLES:
        path = repo.ship_dir / required
        if required not in present or not os.access(path, os.X_OK):
            raise FleetError(
                f"ship/{required} is missing or not executable, so no Ship can be provisioned "
                "from this checkout.",
                hint="It comes from the Ship track (docs/tracks.md); update your checkout.",
            )

    git = repo.git_state("ship", "versions.toml")
    created_at = created_at or now_timestamp()
    manifest = {
        "schema": "shipshape.bundle/1",
        "tooling_version": repo.tooling_version,
        "commit": git.commit,
        "dirty": git.dirty,
        "created_at": created_at,
    }
    schemas.check("bundle-manifest", manifest, "bundle.json", repo.contracts)
    mtime = int(
        datetime.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC).timestamp()
    )

    entries: list[tuple[str, bytes | None, int]] = [
        ("bundle.json", (json.dumps(manifest, indent=2) + "\n").encode(), 0o644),
        ("versions.toml", repo.versions_path.read_bytes(), 0o644),
    ]
    dirs: set[str] = set()
    for rel in files:
        for parent in list(rel.parents)[:-1]:
            dirs.add(str(parent))
        source = repo.ship_dir / rel
        executable = bool(source.stat().st_mode & stat.S_IXUSR)
        entries.append((str(rel), source.read_bytes(), 0o755 if executable else 0o644))
    if present & {"bundle.json", "versions.toml"} or dirs & {"bundle.json", "versions.toml"}:
        raise FleetError("ship/ must not contain bundle.json or versions.toml at its root.")
    entries += [(d, None, 0o755) for d in dirs]
    entries.sort(key=lambda e: e[0])

    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for name, data, mode in entries:
                if data is None:
                    tar.addfile(_info(name, mode, 0, mtime, tarfile.DIRTYPE))
                else:
                    tar.addfile(_info(name, mode, len(data), mtime, tarfile.REGTYPE),
                                io.BytesIO(data))
    data = raw.getvalue()
    if len(data) > MAX_BYTES:
        raise FleetError(
            f"The tooling bundle is {len(data) // 1024} KiB; the contract limit is 5 MiB.",
            hint="Keep large or generated files out of ship/ (contracts/seed.md).",
        )
    return Bundle(data=data, manifest=manifest)


def write(bundle: Bundle, path: Path) -> None:
    write_private(path, bundle.data)
