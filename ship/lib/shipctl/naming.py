"""What a Ship's own Claude sessions carry: their names and their Remote Control.

Every Claude session a Ship starts carries a readable, deterministic name,
<session prefix>-<agent name> (the prefix defaults to the Ship's name), so the
Claude app lists `ship-01-main` instead of an auto-generated title.

`launch_args` turns the name into what the Claude launch needs: `--name <name>`.
Phase 0 showed names set this way appear as titles in the Claude app and survive
`claude --resume` across a reboot, so herdr's own resume keeps them and the
resume shim needs no part in it. The flag is passed only when the installed
Claude Code lists it in `claude --help`, since the pinned minimum version
predates that check.

`remote_control_args` is how Remote Control reaches only the agents the Ship
gives it (contracts/settings.md, "Remote Control"): with claude.remote_control
"on", the managed settings leave it off for every session, and a launch the Ship
makes (autostart, resume, crash restart, `agent restart`, a runtime switch)
carries `--remote-control` when the agent record says that agent gets it
(autostart agents do; an adopted one only once `ship-ctl agent remote-control`
turns it on). Claude Code documents that the flag turns it on for one session
even when remoteControlAtStartup is false; with `--resume` it reattaches the
session's saved Remote Control session.
"""

from __future__ import annotations

import re

from .context import Context
from .shiptoml import ShipToml

RC_OFF = ("off", "blocked")  # "blocked": an older name for "off"
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


def remote_control_args(ship: ShipToml, wanted: bool) -> list[str]:
    """Extra `claude` arguments for a session the Ship starts. wanted: the agent
    record gives this agent Remote Control. "on" follows it, "all" gives it to
    every session anyway, "off" to none. Last on the line: the flag takes an
    optional name, so nothing may follow it but another flag."""
    value = ship.claude["remote_control"]
    on = value == "all" or (value not in RC_OFF and wanted)
    return ["--remote-control"] if on else []
