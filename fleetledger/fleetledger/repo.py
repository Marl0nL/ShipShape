"""The ShipShape checkout FleetLedger runs from.

FleetLedger reads the contracts, versions.toml and the Ship catalogue from the
repo, and packs ship/ into tooling bundles (contracts/seed.md). install.sh
installs it editable, so the package always sits inside its checkout. Set
SHIPSHAPE_REPO to point at another checkout.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from fleetledger.errors import ConfigError

COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")  # a commit, full or abbreviated


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

    def on_remote(self, commit: str) -> bool | None:
        """Is commit on a remote-tracking branch, so another checkout can fetch it?
        None outside git."""
        try:
            out = _git(self.root, "branch", "-r", "--contains", commit)
        except (OSError, subprocess.CalledProcessError):
            return None
        return bool(out.strip())

    def has_commit(self, commit: str) -> bool:
        """Is commit (hex, full or abbreviated) a commit in this checkout? False outside
        git."""
        if not COMMIT_RE.match(commit):
            return False
        try:
            _git(self.root, "cat-file", "-e", f"{commit}^{{commit}}")
        except (OSError, subprocess.CalledProcessError):
            return False
        return True

    def is_shallow(self) -> bool:
        """A shallow clone (git clone --depth): it cannot see commits past its depth."""
        try:
            return _git(self.root, "rev-parse", "--is-shallow-repository").strip() == "true"
        except (OSError, subprocess.CalledProcessError):
            return False

    def is_ancestor(self, older: str, newer: str) -> bool | None:
        """Is older the same commit as newer, or one in its history? None when git
        cannot say (outside git, or a commit this checkout does not have)."""
        if not (COMMIT_RE.match(older) and COMMIT_RE.match(newer)):
            return None
        try:
            proc = git(self.root, "merge-base", "--is-ancestor", older, newer)
        except OSError:
            return None
        return {0: True, 1: False}.get(proc.returncode)

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


def git(root: Path | str, *args: str, timeout: float | None = None,
        check: bool = False) -> subprocess.CompletedProcess:
    """git -C root args, its output captured as text. The one way FleetLedger runs git
    in a checkout; each caller keeps its own timeout and turns failures into its own
    errors. check=True raises CalledProcessError on a non-zero exit; OSError (no git)
    and TimeoutExpired come through as subprocess.run raises them."""
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          timeout=timeout, check=check)


def _git(root: Path, *args: str) -> str:
    return git(root, *args, check=True).stdout
