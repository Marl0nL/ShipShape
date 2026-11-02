"""Claude session names: the one place a Ship decides what its sessions are called.

Every Claude session a Ship starts carries a readable, deterministic name,
<session prefix>-<agent name> (the prefix defaults to the Ship's name), so the
Claude app lists `ship-01-main` instead of an auto-generated title.

`launch_args` turns the name into what the Claude launch needs. How Claude Code
takes a session name (and whether it survives `claude --resume`) is being
checked in phase 0; until then it returns nothing and the name is only recorded.
"""

from __future__ import annotations

import re

from .shiptoml import ShipToml

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def session_name(ship: ShipToml, agent: str) -> str:
    prefix = ship.claude["session_prefix"]
    return _UNSAFE.sub("-", f"{prefix}-{agent}")[:64]


def launch_args(name: str) -> list[str]:
    """Extra `claude` arguments that give a new session `name` (none known yet)."""
    return []
