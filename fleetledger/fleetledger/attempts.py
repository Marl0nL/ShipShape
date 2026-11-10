"""Failed `ship-fleet new` attempts, kept so they can be edited and tried again.

When `ship-fleet new` fails (from the CLI, the web UI's New Ship form or setup's
first Ship), cmd_new saves what it was asked for: the command's own options, its
error, how far it got and anything the driver made but could not remove. They go
into one file in FleetLedger's data folder (`attempts.json`, mode 0600): the newest
KEEP attempts, one per Ship name (a newer attempt at a name replaces the older).
An attempt goes once a Ship of that name is made, or when you discard it.

No secrets: `new` takes none (names, sizes, choices, a repo name). The first
prompt is the only free text; it is kept only when redaction would leave it as it
is, else it is left out and the retry form asks for it again.

How far it got (stage):
- "checks": before anything was written; nothing to clear.
- "create": the driver failed making the VM; cmd_new took the Ship out of the
  inventory again. If the driver could not remove what it had made (a VM, a seed
  volume), that is in `left`, and the next `new` of that name removes it first
  (clear_left), or refuses and says what to do.
- "enrol": the VM was made but the Ship did not finish enrolling; it stays in the
  inventory. A retry under that name waits until you finish enrolling it or
  destroy it (its page has both).
"""

from __future__ import annotations

import json
import shlex
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleetledger import paths
from fleetledger.errors import FleetError

KEEP = 20
STAGES = ("checks", "create", "enrol")
# The `ship-fleet new` options an attempt keeps (all of them: none is a secret).
OPTIONS = ("name", "tag", "cores", "memory_mb", "disk_gb", "signins", "github_scopes",
           "autostart",
           "autostart_prompt", "repo", "no_overlay", "driver", "image", "vmid", "host",
           "mode", "allow_bypass", "agent_sudo", "timezone", "tools", "no_default_tools",
           "profile", "no_wait")
LISTS = ("tag", "autostart", "host", "profile")
FLAGS = ("no_overlay", "allow_bypass", "agent_sudo", "no_default_tools", "no_wait")
NUMBERS = ("cores", "memory_mb", "disk_gb", "vmid")
LEFT_KINDS = ("vm", "volume")
_lock = threading.Lock()


def attempts_path() -> Path:
    return paths.data_dir() / "attempts.json"


@dataclass
class Attempt:
    name: str
    at: float
    options: dict[str, Any]
    error: str
    stage: str = "checks"
    driver: str = "manual"
    left: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # options left out (re-enter them)
    blocked: str | None = None  # not saved: why a retry under its name waits (current())

    def argv(self) -> list[str]:
        return to_argv(self.options)

    def command(self) -> str:
        return "ship-fleet " + shlex.join(self.argv())

    def describe_left(self) -> list[str]:
        return [describe(item) for item in self.left]

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "at": self.at, "options": self.options,
                "error": self.error, "stage": self.stage, "driver": self.driver,
                "left": self.left, "dropped": self.dropped}


def describe(item: dict[str, Any]) -> str:
    if item.get("kind") == "vm":
        return f"VM {item.get('vmid')} ({item.get('name')})"
    return f"the seed volume {item.get('volume')}"


# --- options <-> the command line ------------------------------------------------


def options_of(args: Any) -> tuple[dict[str, Any], list[str]]:
    """(options, dropped) from a parsed `new` command: only the known options, of the
    expected types, and the prompt only if nothing in it looks like a secret."""
    from fleetledger.concierge.redact import clean, redact

    out: dict[str, Any] = {}
    dropped: list[str] = []
    for key in OPTIONS:
        value = getattr(args, key, None)
        if key in LISTS:
            value = [str(v) for v in (value or [])]
            if value:
                out[key] = value
        elif key in FLAGS:
            if value:
                out[key] = True
        elif key in NUMBERS:
            if isinstance(value, int) and not isinstance(value, bool):
                out[key] = value
        elif isinstance(value, str) and value:
            out[key] = value
    prompt = out.get("autostart_prompt")
    if prompt is not None and redact(clean(prompt)) != prompt:
        del out["autostart_prompt"]  # might hold a token, an address or a link: not kept
        dropped.append("autostart_prompt")
    return out, dropped


def to_argv(options: dict[str, Any]) -> list[str]:
    """The `ship-fleet new` arguments these options were (cli.py's spelling)."""
    out = ["new", str(options["name"])]
    for key in OPTIONS[1:]:
        value = options.get(key)
        flag = "--" + key.replace("_", "-")
        if value is None or value is False or value == []:
            continue
        if key in LISTS:
            for item in value:
                out += [flag, str(item)]
        elif key in FLAGS:
            out.append(flag)
        else:
            out += [flag, str(value)]
    return out


# --- the file --------------------------------------------------------------------


def _clean(raw: Any) -> Attempt | None:
    """One saved attempt, or None if it is not one (a hand-edited or older file)."""
    from fleetledger import ops

    if not isinstance(raw, dict):
        return None
    options = raw.get("options")
    name = raw.get("name")
    if not (isinstance(name, str) and ops.NAME_RE.match(name) and isinstance(options, dict)
            and options.get("name") == name):
        return None
    kept: dict[str, Any] = {}
    for key in OPTIONS:
        value = options.get(key)
        if key in LISTS and isinstance(value, list) and all(isinstance(v, str) for v in value):
            kept[key] = value
        elif key in FLAGS and value is True:
            kept[key] = True
        elif key in NUMBERS and isinstance(value, int) and not isinstance(value, bool):
            kept[key] = value
        elif key not in LISTS + FLAGS + NUMBERS and isinstance(value, str):
            kept[key] = value
    left = [dict(i) for i in raw.get("left") or [] if isinstance(i, dict)
            and i.get("kind") in LEFT_KINDS]
    at = raw.get("at")
    return Attempt(
        name=name, at=float(at) if isinstance(at, (int, float)) else 0.0, options=kept,
        error=str(raw.get("error") or ""),
        stage=raw.get("stage") if raw.get("stage") in STAGES else "checks",
        driver=str(raw.get("driver") or "manual"), left=left,
        dropped=[d for d in raw.get("dropped") or [] if d in OPTIONS])


def _read() -> list[Attempt]:
    items = (paths.read_json(attempts_path()) or {}).get("attempts")
    return [a for a in (_clean(i) for i in items or []) if a is not None]


def _write(items: list[Attempt]) -> None:
    path = attempts_path()
    paths.private_dir(path.parent)
    items = sorted(items, key=lambda a: a.at, reverse=True)[:KEEP]
    doc = {"schema": 1, "attempts": [a.to_json() for a in items]}
    paths.write_private(path, (json.dumps(doc, indent=1) + "\n").encode())


def load() -> list[Attempt]:
    """Every saved attempt, newest first."""
    with _lock:
        return sorted(_read(), key=lambda a: a.at, reverse=True)


def get(name: str) -> Attempt | None:
    return next((a for a in load() if a.name == name), None)


def save(attempt: Attempt) -> None:
    with _lock:
        _write([a for a in _read() if a.name != attempt.name] + [attempt])


def drop(name: str) -> bool:
    """Forget the attempt at name; True if there was one."""
    with _lock:
        items = _read()
        kept = [a for a in items if a.name != name]
        if len(kept) == len(items):
            return False
        _write(kept)
        return True


def record(args: Any, *, error: str, stage: str, driver: str,
           left: list[dict[str, Any]] | None = None) -> Attempt:
    """Save a failed `new` (best effort: a data folder that cannot be written never
    hides the error itself)."""
    options, dropped = options_of(args)
    try:
        before = get(options["name"])
    except OSError:
        before = None
    # What an earlier attempt left and nobody removed yet is still there (clear_left
    # saves the attempt without it once it is gone).
    kept = [i for i in (before.left if before else []) if i not in (left or [])]
    attempt = Attempt(name=options["name"], at=time.time(), options=options,
                      error=error, stage=stage, driver=before.driver if kept else driver,
                      left=kept + list(left or []), dropped=dropped)
    try:
        save(attempt)
    except OSError:
        pass
    return attempt


def current(inv: Any) -> list[Attempt]:
    """The attempts still worth offering, each with why a retry under its name waits
    (blocked), if it does. One whose Ship has since been made and enrolled (Finish
    enrolling, or made another way) is dropped, unless it still names something it
    left on the hypervisor: that stays the only record of it."""
    out = []
    for attempt in load():
        ship = inv.ship(attempt.name) if inv is not None and inv.has_ship(attempt.name) else None
        if ship is not None and ship.enrolled is not None and not attempt.left:
            try:
                drop(attempt.name)
            except OSError:
                pass
            continue
        attempt.blocked = blocker(inv, attempt)
        out.append(attempt)
    return out


def saved_at(name: str) -> float | None:
    """When the saved attempt at name was made, or None if there is none."""
    try:
        attempt = get(name)
    except OSError:
        return None
    return attempt.at if attempt is not None else None


def blocker(inv: Any, attempt: Attempt, name: str | None = None) -> str | None:
    """Why a retry of attempt as name (default its own) cannot start yet, in words."""
    name = name or attempt.name
    if inv is not None and inv.has_ship(name):
        # Made, but never enrolled: this attempt's half-made Ship (also when FleetLedger
        # stopped while it was being made, so the attempt still says an earlier stage).
        if name == attempt.name and inv.ship(name).enrolled is None:
            return (f"{name} is still in the inventory from the failed attempt: its VM was "
                    "made, but it did not finish enrolling. On its page, Finish enrolling "
                    "it, or destroy it to start again; or choose another name.")
        return f"{name} is already in the inventory: choose another name."
    return None


# --- what a failed attempt left -------------------------------------------------


def clear_left(app: Any, inv: Any, attempt: Attempt, say: Any) -> None:
    """Remove what attempt's driver made but could not remove (checked by name, so
    nothing else is touched), then save the attempt without it. Raises FleetError
    saying exactly what is still there and how to clear it."""
    if not attempt.left:
        return
    what = ", ".join(attempt.describe_left())
    say(f"Removing what the failed attempt at {attempt.name} left: {what}.")
    try:
        driver = app.driver_factory(inv, attempt.driver)
        messages = driver.remove_left(attempt.name, attempt.left)
    except FleetError as exc:
        raise FleetError(
            f"The failed attempt at {attempt.name} left {what}, and FleetLedger cannot "
            f"remove it: {exc.message}",
            hint="Delete it on the hypervisor by hand (or fix what stops FleetLedger), then "
            "try again: what is gone already is skipped. To give up on that attempt "
            "instead, Discard it on the New Ship page (ship-fleet ui).") from exc
    for message in messages:
        say(message)
    attempt.left = []
    try:
        save(attempt)
    except OSError:
        pass
