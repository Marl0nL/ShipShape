"""Safe file access inside the agent's home, for code that runs as root.

The agent user owns /home/agent and can put a symlink anywhere in it. A root
process that opened `~/.claude/settings.json` by path would follow such a link
and read or overwrite any file on the Ship. So every path here is resolved one
component at a time from an open directory, with O_NOFOLLOW at each step:

- a symlink (or a non-directory where a directory should be) anywhere in the
  path raises UnsafePath; nothing is read or written through it;
- writes go to a new temporary file in the same directory (O_EXCL|O_NOFOLLOW),
  get their mode and owner on the open descriptor, then replace the target with
  renameat(), which never follows a link;
- there is no window between checking a path and using it, because the checks
  are the opens themselves.

Paths are given relative to the home, e.g. ".claude/settings.json".
"""

from __future__ import annotations

import contextlib
import errno
import os
import secrets
import shutil
import stat
from pathlib import Path, PurePosixPath

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafePath(Exception):
    """A symlink or special file is in the way."""

    def __init__(self, rel: str):
        super().__init__(f"~/{rel}: a symlink or special file is in the way; left alone")
        self.rel = rel


def _parts(rel: str) -> list[str]:
    parts = [p for p in PurePosixPath(rel).parts if p not in ("", ".")]
    if not parts or ".." in parts or PurePosixPath(rel).is_absolute():
        raise ValueError(f"bad path under the home: {rel!r}")
    return parts


class HomeFS:
    def __init__(self, home: Path, owner: tuple[int, int] | None = None):
        self.home = home
        self.owner = owner  # (uid, gid) given to everything created; None in sandbox mode

    # --- walking ------------------------------------------------------------------
    def _open_dir(self, fd: int, name: str, rel: str, create: bool, mode: int) -> int | None:
        try:
            return os.open(name, _DIR_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            if not create:
                return None
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise UnsafePath(rel) from exc
            raise
        os.mkdir(name, mode, dir_fd=fd)
        new = os.open(name, _DIR_FLAGS, dir_fd=fd)
        os.fchmod(new, mode)
        if self.owner:
            os.fchown(new, *self.owner)
        return new

    def _parent(self, rel: str, create: bool, dir_mode: int) -> tuple[int | None, str]:
        """An fd for the folder holding `rel` (caller closes it), and the last name."""
        parts = _parts(rel)
        fd = os.open(self.home, _DIR_FLAGS)
        done: list[str] = []
        try:
            for name in parts[:-1]:
                done.append(name)
                nxt = self._open_dir(fd, name, "/".join(done), create, dir_mode)
                os.close(fd)
                fd = nxt
                if fd is None:
                    return None, parts[-1]
            return fd, parts[-1]
        except BaseException:
            if fd is not None:
                os.close(fd)
            raise

    @staticmethod
    def _open_file(dfd: int, name: str, rel: str) -> int | None:
        try:
            fd = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
            )
        except FileNotFoundError:
            return None
        except OSError as exc:
            # ENXIO: a Unix socket. Anything that is not a plain file is left alone.
            if exc.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO):
                raise UnsafePath(rel) from exc
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise UnsafePath(rel)
        return fd

    # --- reading ---------------------------------------------------------------------
    def read(self, rel: str) -> bytes | None:
        """The file's bytes, or None if it (or a folder on the way) does not exist."""
        dfd, name = self._parent(rel, create=False, dir_mode=0o755)
        if dfd is None:
            return None
        try:
            fd = self._open_file(dfd, name, rel)
            if fd is None:
                return None
            with os.fdopen(fd, "rb") as fh:
                return fh.read()
        finally:
            os.close(dfd)

    # --- writing ---------------------------------------------------------------------
    def _write_at(
        self,
        dfd: int,
        name: str,
        rel: str,
        data: bytes,
        mode: int,
        dry_run: bool,
        replace_links: bool,
        keep_mode: bool = False,
    ) -> bool:
        try:
            fd = self._open_file(dfd, name, rel)
        except UnsafePath:
            if not replace_links:
                raise
            st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
            if stat.S_ISDIR(st.st_mode):
                raise
            if dry_run:
                return True
            os.unlink(name, dir_fd=dfd)  # removes the link itself, never its target
            fd = None
        if fd is not None:
            with os.fdopen(fd, "rb") as fh:
                st = os.fstat(fh.fileno())
                if keep_mode:
                    mode = stat.S_IMODE(st.st_mode)
                same = (
                    fh.read() == data
                    and stat.S_IMODE(st.st_mode) == mode
                    and (self.owner is None or (st.st_uid, st.st_gid) == self.owner)
                )
            if same:
                return False
        if dry_run:
            return True
        tmp = f".{name}.{secrets.token_hex(4)}"
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=dfd,
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fchmod(fh.fileno(), mode)
                if self.owner:
                    os.fchown(fh.fileno(), *self.owner)
                os.fsync(fh.fileno())
            os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
        return True

    def write(
        self,
        rel: str,
        data: bytes | str,
        mode: int = 0o644,
        *,
        dry_run: bool = False,
        dir_mode: int = 0o755,
        keep_mode: bool = False,
    ) -> bool:
        """Write atomically; True if content, mode or owner changed (or would, in a dry run).

        Missing folders are created with `dir_mode`. With `keep_mode`, an existing
        file keeps its mode (`mode` is for a new file). Raises UnsafePath if a
        symlink is anywhere on the way, including the file itself.
        """
        if isinstance(data, str):
            data = data.encode("utf-8")
        dfd, name = self._parent(rel, create=not dry_run, dir_mode=dir_mode)
        if dfd is None:  # dry run, folder missing
            return True
        try:
            return self._write_at(dfd, name, rel, data, mode, dry_run, False, keep_mode)
        finally:
            os.close(dfd)

    def open_dir(self, rel: str, *, create: bool, mode: int = 0o700) -> int | None:
        """An fd for the folder ~/rel (the caller closes it), reached without following
        a symlink; None if it is missing and not `create`. Folders it creates get `mode`."""
        dfd, name = self._parent(rel, create=create, dir_mode=mode)
        if dfd is None:
            return None
        try:
            return self._open_dir(dfd, name, rel, create=create, mode=mode)
        finally:
            os.close(dfd)

    def mkdirs(self, rel: str, mode: int = 0o755) -> None:
        dfd, name = self._parent(rel, create=True, dir_mode=mode)
        try:
            fd = self._open_dir(dfd, name, rel, create=True, mode=mode)
            os.close(fd)
        finally:
            os.close(dfd)

    def sync_dir(self, src: Path, rel: str) -> bool:
        """Make ~/rel an exact copy of the trusted folder `src` (files 0644, folders 0755).

        Symlinks found inside ~/rel are removed (not followed) and replaced.
        Returns True if anything changed.
        """
        dfd, name = self._parent(rel, create=True, dir_mode=0o755)
        try:
            fd = self._open_dir(dfd, name, rel, create=True, mode=0o755)
        finally:
            os.close(dfd)
        try:
            return self._sync(src, fd, rel)
        finally:
            os.close(fd)

    def _sync(self, src: Path, dfd: int, rel: str) -> bool:
        changed = False
        wanted = {e.name: e for e in os.scandir(src)}
        for entry in os.scandir(dfd):
            want = wanted.get(entry.name)
            is_dir = entry.is_dir(follow_symlinks=False)
            if want is not None and want.is_dir() == is_dir and not entry.is_symlink():
                continue
            if is_dir:
                shutil.rmtree(entry.name, dir_fd=dfd)
            else:
                os.unlink(entry.name, dir_fd=dfd)
            changed = True
        for name, entry in sorted(wanted.items()):
            sub = f"{rel}/{name}"
            if entry.is_dir(follow_symlinks=False):
                fd = self._open_dir(dfd, name, sub, create=True, mode=0o755)
                try:
                    changed |= self._sync(Path(entry.path), fd, sub)
                finally:
                    os.close(fd)
            elif entry.is_file(follow_symlinks=False):
                data = Path(entry.path).read_bytes()
                changed |= self._write_at(dfd, name, sub, data, 0o644, False, replace_links=True)
        return changed
