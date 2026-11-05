"""Everything a command needs: paths, a command runner, pinned versions, a clock."""

from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property

from .homefs import HomeFS, UnsafePath
from .paths import Paths
from .runner import Runner


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Context:
    paths: Paths
    runner: Runner = field(init=False)
    quiet: bool = False

    def __post_init__(self) -> None:
        self.runner = Runner(self.paths)

    @classmethod
    def from_env(cls) -> Context:
        return cls(Paths.from_env())

    @cached_property
    def versions(self) -> dict:
        try:
            with self.paths.versions_toml.open("rb") as fh:
                return tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError):
            return {}

    def log(self, message: str) -> None:
        """Diagnostics: stderr, which systemd sends to the journal. Never stdout."""
        if not self.quiet:
            print(f"ship-ctl: {message}", file=sys.stderr, flush=True)

    @property
    def agent_owner(self) -> tuple[int, int] | None:
        return self.runner.agent_ids()

    @property
    def home(self) -> HomeFS:
        """The agent's home, safe against symlinks the agent planted (homefs.py)."""
        return HomeFS(self.paths.agent_home, self.agent_owner)

    def read_home_json(self, path, default=None):
        """A JSON file under the agent's home; `default` if missing, unsafe or invalid."""
        try:
            raw = self.home.read(self.paths.home_rel(path))
            return default if raw is None else json.loads(raw)
        except (UnsafePath, OSError, ValueError):
            return default
