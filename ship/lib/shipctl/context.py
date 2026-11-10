"""Everything a command needs: paths, a command runner, pinned versions, a clock."""

from __future__ import annotations

import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property

from .homefs import HomeFS, UnsafePath
from .paths import Paths
from .runner import Runner

STAMP = "%Y-%m-%dT%H:%M:%SZ"  # the Ship's one UTC timestamp format


def stamp(when: datetime | float) -> str:
    """A UTC timestamp, from epoch seconds or a datetime."""
    if isinstance(when, datetime):
        return when.astimezone(UTC).strftime(STAMP)
    return datetime.fromtimestamp(when, UTC).strftime(STAMP)


def parse_stamp(text: str) -> datetime:
    """The UTC datetime a stamp() names; ValueError if it is not one."""
    return datetime.strptime(text, STAMP).replace(tzinfo=UTC)


def utc_now() -> str:
    return stamp(datetime.now(UTC))


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
            try:
                print(f"ship-ctl: {message}", file=sys.stderr, flush=True)
            except (OSError, ValueError):
                # stderr is gone (the SSH session that ran ship-ctl dropped): what was
                # being done must still finish (a runtime switch's rollback, say).
                sys.stderr = open(os.devnull, "w")  # noqa: SIM115 - for the process's life

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
