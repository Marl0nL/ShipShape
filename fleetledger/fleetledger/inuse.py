"""Is this FleetLedger in use? A lock on the folder that holds the inventory says so.

Every ship-fleet command (the web UI included, for as long as it serves) holds it
shared while it runs, so any number can run at once. `ship-fleet migrate` takes it
exclusively while it copies the fleet to or from another computer: it refuses while
anything else runs here (a job, a sign-in or an edit in it could change the fleet
mid-copy), and nothing else can start until the copy is done.

The lock is flock(2) on the folder itself, so taking it writes no file (doctor
stays read-only), and it goes away with its process: one that crashed never leaves
it behind.

An older ship-fleet kept an empty file, OLD_FILE, beside ships.toml for this lock.
Nothing uses it now, but an update leaves it behind: old_file tells that leftover
apart from a lock a still-running older ship-fleet holds, and from anything else of
that name.
"""

from __future__ import annotations

import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fleetledger import paths
from fleetledger.errors import FleetError

OLD_FILE = "in-use.lock"


def lock_path(store) -> Path:
    return store.path.parent


@contextmanager
def _held(store, mode: int, busy: FleetError) -> Iterator[None]:
    path = lock_path(store)
    try:
        if not path.is_dir():
            paths.private_dir(path)
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise FleetError(f"Cannot open {path} to lock it: {exc.strerror}.") from None
    try:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except BlockingIOError:
            raise busy from None
        except OSError as exc:  # a folder that cannot be locked (some network file systems)
            raise FleetError(f"Cannot lock {path} ({exc.strerror}): FleetLedger's folders "
                             "must be on a local disk.") from None
        yield
    finally:
        os.close(fd)


def using(store):
    """Held by every ship-fleet while it runs (shared: others may run too)."""
    return _held(store, fcntl.LOCK_SH, FleetError(
        "FleetLedger is being moved to or from another computer (ship-fleet migrate) right "
        "now; try again when that has finished."))


def exclusive(store):
    """Held while ship-fleet migrate copies this FleetLedger (nothing else may run)."""
    return _held(store, fcntl.LOCK_EX, FleetError(
        "Another ship-fleet is running on this computer (the web UI, or a command in another "
        "terminal): a job, sign-in or change in it could alter the fleet while it is copied.",
        hint="Stop it first (Ctrl-C in its terminal; the service: systemctl --user stop "
             "fleetledger), then try again."))


def old_file(path: Path) -> str | None:
    """What OLD_FILE at path is: "left" for an empty file no process holds a lock on
    (an older version's leftover, safe to ignore), "held" when one does, flock or a
    POSIX record lock (an older ship-fleet still running), or None for anything else,
    which is not that leftover: a file with something in it, a link, not a file, or
    one that cannot be checked. Writes nothing; the test locks it takes go as soon as
    they are taken (it is opened for writing only because a POSIX write lock needs
    that, and only a write lock is refused by every other lock)."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size != 0:
            return None
        for take in (fcntl.flock, fcntl.lockf):
            try:
                take(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EAGAIN, errno.EACCES):
                    return "held"
                return None
        return "left"
    finally:
        os.close(fd)
