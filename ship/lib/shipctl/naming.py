"""Claude session names: the one place a Ship decides what its sessions are called.

Every Claude session a Ship starts carries a readable, deterministic name,
<session prefix>-<agent name> (the prefix defaults to the Ship's name), so the
Claude app lists `ship-01-main` instead of an auto-generated title.

`launch_args` turns the name into what the Claude launch needs: `--name <name>`.
Phase 0 showed names set this way appear as titles in the Claude app and survive
`claude --resume` across a reboot, so herdr's own resume keeps them and the
resume shim needs no part in it. The flag is passed only when the installed
Claude Code lists it in `claude --help`, since the pinned minimum version
predates that check.
"""

from __future__ import annotations

import re

from .context import Context
from .shiptoml import ShipToml

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def session_name(ship: ShipToml, agent: str) -> str:
    prefix = ship.claude["session_prefix"]
    return _UNSAFE.sub("-", f"{prefix}-{agent}")[:64]


def name_flag_supported(ctx: Context) -> bool:
    r = ctx.runner.run(["claude", "--help"], timeout=15, agent=True)
    return r.ok and re.search(r"(^|\s)--name\b", r.out) is not None


def launch_args(name: str, supported: bool) -> list[str]:
    """Extra `claude` arguments that give a new session `name`."""
    return ["--name", name] if supported else []
