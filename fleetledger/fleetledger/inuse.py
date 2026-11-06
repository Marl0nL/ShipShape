"""Is this FleetLedger in use? A lock on the folder that holds the inventory says so.

Every ship-fleet command (the web UI included, for as long as it serves) holds it
shared while it runs, so any number can run at once. `ship-fleet migrate` takes it
exclusively while it copies the fleet to or from another computer: it refuses while
anything else runs here (a job, a sign-in or an edit in it could change the fleet
mid-copy), and nothing else can start until the copy is done.

The lock is flock(2) on the folder itself, so taking it writes no file (doctor
stays read-only), and it goes away with its process: one that crashed never leaves
it behind.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fleetledger import paths
from fleetledger.errors import FleetError


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
        hint="Stop it first (Ctrl-C in its terminal), then try again."))
