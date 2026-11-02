#!/usr/bin/python3 -I
"""Resume shim: installed as /opt/shipshape/shim/claude, first on herdr's PATH.

Every launch goes straight to the real launcher, ~/.local/bin/claude, which is
never replaced, so Claude Code's self-updates keep working. The one exception
(contracts/settings.md): `--resume <id>` for a session the mode ledger lists as
bypassPermissions, on a Ship whose ship.toml has allow_bypass = true, and with no
mode flag already given. Then it adds --dangerously-skip-permissions, because
Claude Code does not restore bypass on resume by itself.
"""

from __future__ import annotations

import json
import os
import sys
import time
import tomllib

MODE_FLAGS = ("--dangerously-skip-permissions", "--permission-mode")


def resume_id(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a in ("--resume", "-r") and i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
        if a.startswith("--resume="):
            return a.split("=", 1)[1] or None
    return None


def has_mode_flag(args: list[str]) -> bool:
    return any(a in MODE_FLAGS or a.startswith("--permission-mode=") for a in args)


def ledger_mode(ledger: str, session_id: str) -> str | None:
    try:
        with open(ledger) as fh:
            entry = json.load(fh)["sessions"][session_id]
        return entry["mode"] if isinstance(entry, dict) else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def bypass_allowed(ship_toml: str) -> bool:
    try:
        with open(ship_toml, "rb") as fh:
            return tomllib.load(fh).get("claude", {}).get("allow_bypass") is True
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return False


def decide(args: list[str], ledger: str, ship_toml: str) -> tuple[list[str], str | None]:
    """Returns (arguments for the real claude, a log line or None)."""
    sid = resume_id(args)
    if sid is None:
        return args, None
    mode = ledger_mode(ledger, sid)
    if mode == "bypassPermissions" and not has_mode_flag(args) and bypass_allowed(ship_toml):
        return ["--dangerously-skip-permissions", *args], f"resume {sid}: bypass restored"
    return args, f"resume {sid}: ledger={mode or 'none'}, passed through"


def main() -> None:
    home = os.path.expanduser("~")
    real = os.path.join(home, ".local", "bin", "claude")
    state = os.path.join(home, ".local", "state", "shipshape")
    root = os.environ.get("SHIPSHAPE_ROOT", "/")
    ship_toml = os.path.join(root, "etc", "shipshape", "ship.toml")
    args, note = decide(sys.argv[1:], os.path.join(state, "modes.json"), ship_toml)
    if note:
        try:
            os.makedirs(state, mode=0o700, exist_ok=True)
            with open(os.path.join(state, "shim.log"), "a") as fh:
                fh.write(time.strftime("%Y-%m-%dT%H:%M:%SZ ", time.gmtime()) + note + "\n")
        except OSError:
            pass
    try:
        os.execv(real, [real, *args])
    except OSError as exc:
        print(f"claude (ShipShape shim): cannot run {real}: {exc.strerror}", file=sys.stderr)
        sys.exit(127)


if __name__ == "__main__":
    main()
