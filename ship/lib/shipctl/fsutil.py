"""Small file helpers: atomic writes that report whether anything changed."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path


def read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None


def read_json(path: Path, default: object = None) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_atomic(
    path: Path,
    data: bytes | str,
    mode: int = 0o644,
    owner: tuple[int, int] | None = None,
    dry_run: bool = False,
) -> bool:
    """Write `data` to `path` atomically. Returns True if content, mode or owner changed."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    try:
        st = path.stat()
        same = (
            path.read_bytes() == data
            and (st.st_mode & 0o7777) == mode
            and (owner is None or (st.st_uid, st.st_gid) == owner)
        )
    except FileNotFoundError:
        same = False
    if same:
        return False
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        if owner is not None:
            os.chown(tmp, *owner)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return True


def remove(path: Path, dry_run: bool = False) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if not dry_run:
        path.unlink()
    return True


def ensure_dir(path: Path, mode: int = 0o755, owner: tuple[int, int] | None = None) -> None:
    """mkdir -p, giving every folder it creates `mode` and `owner`."""
    missing = []
    p = path
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(mode=mode)
        os.chmod(d, mode)
        if owner is not None:
            os.chown(d, *owner)
