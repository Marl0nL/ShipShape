"""The ShipShape checkout FleetLedger runs from.

FleetLedger reads the contracts, versions.toml and the Ship catalogue from the
repo, and packs ship/ into tooling bundles (contracts/seed.md). install.sh
installs it editable, so the package always sits inside its checkout. Set
SHIPSHAPE_REPO to point at another checkout.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from fleetledger.errors import ConfigError


def _looks_like_repo(path: Path) -> bool:
    return (path / "contracts").is_dir() and (path / "versions.toml").is_file()


def find_root(start: Path | None = None) -> Path:
    env = os.environ.get("SHIPSHAPE_REPO")
    if env:
        root = Path(env).expanduser().resolve()
        if not _looks_like_repo(root):
            raise ConfigError(f"SHIPSHAPE_REPO={root} is not a ShipShape checkout.")
        return root
    here = (start or Path(__file__)).resolve()
    for candidate in [here, *here.parents]:
        if _looks_like_repo(candidate):
            return candidate
    raise ConfigError(
        "Cannot find the ShipShape checkout (contracts/ and versions.toml).",
        hint="Install with fleetledger/install.sh from a checkout, or set SHIPSHAPE_REPO.",
    )


@dataclass(frozen=True)
class GitState:
    commit: str  # full hex, or "unknown"
    dirty: bool


@dataclass(frozen=True)
class Repo:
    root: Path

    @classmethod
    def locate(cls) -> Repo:
        return cls(find_root())

    @property
    def contracts(self) -> Path:
        return self.root / "contracts"

    @property
    def ship_dir(self) -> Path:
        return self.root / "ship"

    @property
    def tools_dir(self) -> Path:
        return self.ship_dir / "tools"

    @property
    def profiles_dir(self) -> Path:
        return self.ship_dir / "profiles"

    @property
    def versions_path(self) -> Path:
        return self.root / "versions.toml"

    @cached_property
    def versions(self) -> dict:
        with self.versions_path.open("rb") as fh:
            return tomllib.load(fh)

    @property
    def tooling_version(self) -> str:
        return self.versions["shipshape"]["tooling"]

    def git_state(self, *paths: str) -> GitState:
        """Commit of HEAD, and whether the given paths have uncommitted changes."""
        try:
            commit = _git(self.root, "rev-parse", "HEAD").strip()
            status = _git(self.root, "status", "--porcelain", "--", *(paths or (".",)))
        except (OSError, subprocess.CalledProcessError):
            return GitState("unknown", False)
        return GitState(commit, bool(status.strip()))

    def git_files(self, subdir: str) -> list[str] | None:
        """Tracked and untracked-but-not-ignored files under subdir, or None outside git."""
        try:
            out = _git(
                self.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                subdir,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        return sorted({f for f in out.split("\0") if f and (self.root / f).is_file()})


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
