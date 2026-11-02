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

import os
import threading
from pathlib import Path


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


def private_dir(path: Path) -> Path:
    """Create a folder only you can read (0700) and return it."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


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
