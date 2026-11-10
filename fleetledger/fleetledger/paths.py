"""Where FleetLedger keeps its files on your computer.

Nothing here lives in the repo: the inventory and keys name your accounts and
machines (docs/hygiene.md).

- config (FLEETLEDGER_CONFIG_DIR, else $XDG_CONFIG_HOME/fleetledger, else
  ~/.config/fleetledger): ships.toml, known_hosts, the Proxmox token file.
- data (FLEETLEDGER_DATA_DIR, else $XDG_DATA_HOME/fleetledger, else
  ~/.local/share/fleetledger): seeds, the audit log, cached status.
- runtime (FLEETLEDGER_RUNTIME_DIR, else $XDG_RUNTIME_DIR/fleetledger, else
  ~/.cache/fleetledger/run): SSH connection-sharing sockets.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


def _base(env: str, xdg: str, fallback: str, leaf: str = "fleetledger") -> Path:
    value = os.environ.get(env)
    if value:
        return Path(value).expanduser()
    xdg_value = os.environ.get(xdg)
    if xdg_value:
        return Path(xdg_value).expanduser() / leaf
    return Path.home() / fallback / leaf


def config_dir() -> Path:
    return _base("FLEETLEDGER_CONFIG_DIR", "XDG_CONFIG_HOME", ".config")


def data_dir() -> Path:
    return _base("FLEETLEDGER_DATA_DIR", "XDG_DATA_HOME", ".local/share")


def runtime_dir() -> Path:
    value = os.environ.get("FLEETLEDGER_RUNTIME_DIR")
    if value:
        return Path(value).expanduser()
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / "fleetledger"
    return Path.home() / ".cache" / "fleetledger" / "run"


def inventory_path() -> Path:
    return config_dir() / "ships.toml"


def default_known_hosts() -> Path:
    return config_dir() / "known_hosts"


def seeds_dir() -> Path:
    return data_dir() / "seeds"


def audit_log_path() -> Path:
    return data_dir() / "audit.log"


def status_cache_dir() -> Path:
    return data_dir() / "status"


def expand(path: str | os.PathLike[str]) -> Path:
    """Expand a leading ~/ as the contracts allow in path values."""
    return Path(os.fspath(path)).expanduser()


def tilde(path: str | os.PathLike[str]) -> str:
    """path as ~/... when it is under your home folder (for messages), else as given."""
    try:
        return "~/" + str(Path(path).resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return str(path)


def private_dir(path: Path) -> Path:
    """Create a folder only you can read (0700) and return it."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


SOCKET_PATH_MAX = 100  # Unix socket paths are limited to about 108 bytes


def runtime_socket(name: str, hint: str, *, per_home: bool = False) -> Path:
    """Where FleetLedger keeps one of its Unix sockets: in its private runtime folder,
    or, when that path is too long for a socket, in a short private folder of yours
    under /tmp (refused if anyone else owns it or can open it). hint: what to do
    instead, for that refusal. per_home: in that short folder, name it after this
    runtime folder too, so two FleetLedger homes of yours never share it."""
    import hashlib
    import stat

    from fleetledger.errors import FleetError

    sock = runtime_dir() / name
    if len(str(sock)) <= SOCKET_PATH_MAX:
        private_dir(sock.parent)
        return sock
    folder = Path("/tmp") / f"fleetledger-{os.getuid()}"
    try:
        folder.mkdir(mode=0o700)
    except FileExistsError:
        pass
    st = folder.lstat()
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) & 0o077):
        raise FleetError(f"{folder} is not a private folder of yours; not using a socket "
                         "there.", hint=hint)
    if per_home:
        home = hashlib.sha256(str(runtime_dir()).encode("utf-8")).hexdigest()[:12]
        return folder / f"{home}-{name}"
    return folder / name


def read_json(path: Path) -> dict[str, Any] | None:
    """A JSON object kept in a state file, or None when there is none: missing,
    unreadable, not UTF-8, not JSON, or JSON that is not an object."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError: JSONDecodeError and UnicodeDecodeError
        return None
    return doc if isinstance(doc, dict) else None


def write_private(path: Path, data: bytes) -> None:
    """Write a file atomically with mode 0600 (the folder must already exist)."""
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)
