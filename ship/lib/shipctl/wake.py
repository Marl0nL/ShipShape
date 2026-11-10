"""Wake the Ship's primary agent some minutes after a boot (`ship-ctl wake`;
contracts/settings.md, "Waking the primary agent after a boot").

After a reboot the agent record resumes every agent's conversation, but nothing
prompts them: whatever they ran in the background (monitors, servers, loops) stays
down until someone writes to them. With ship.toml `wake.after_boot` on,
ship-wake.timer runs `ship-ctl wake` `wake.delay_minutes` after each boot, and it
types one line (`wake.text`) into the primary agent (record.primary).

- **Once per boot:** the outcome is kept in /var/lib/shipshape/wake.json with the
  boot id, so running it again in the same boot (a restart of the unit, by hand)
  sends nothing. Two at once: the second finds the lock taken and does nothing.
- **Notice rules:** sent the way notify-signin sends (notice.send_one), through
  the runtime: never into an agent at a permission prompt or in a state the
  runtime cannot tell, and not before this boot's resume is done. Such an agent,
  or one not running yet, is tried again every RETRY_S seconds, TRIES times in
  all; then it gives up, and says so in the journal.
- **Not late:** a timer enabled or moved later in a boot can fire at once; a run
  more than LATE_S after the time it was meant for sends nothing, so the line
  never says "rebooted 10 minutes ago" hours after a boot.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Iterator

from . import notice
from . import record as agent_record
from .context import Context, utc_now
from .errors import CtlError
from .fsutil import ensure_dir, read_json, remove, write_atomic
from .runtime import for_ship, wait_budget
from .shiptoml import ShipToml
from .status import boot_id

FILE = "wake.json"
LOCK = "wake.lock"
TIMER = "ship-wake.timer"
DROP_IN = f"/etc/systemd/system/{TIMER}.d/50-shipshape.conf"
TRIES = 6  # tries in all, RETRY_S apart: about five minutes
RETRY_S = 60
LATE_S = 30 * 60  # after the time it was meant for, it is no longer sent
WHAT = "wake"  # the journal's prefix (ship-ctl logs system)


def delay_text(minutes: int) -> str:
    if minutes % 60 == 0:
        hours = minutes // 60
        return "1 hour" if hours == 1 else f"{hours} hours"
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def line(ship: ShipToml) -> str:
    """The line typed into the agent: wake.text with {delay} filled in, on one line."""
    w = ship.wake
    text = str(w["text"]).replace("{delay}", delay_text(int(w["delay_minutes"])))
    return " ".join(text.split())


def drop_in(ship: ShipToml) -> str:
    """ship-wake.timer's delay, written by ship-ctl apply."""
    return ("# Written by ship-ctl apply from /etc/shipshape/ship.toml [wake].\n"
            "[Timer]\nOnBootSec=\n"
            f"OnBootSec={int(ship.wake['delay_minutes'])}min\n")


@contextlib.contextmanager
def _locked(ctx: Context) -> Iterator[bool]:
    ensure_dir(ctx.paths.state, 0o755)
    with open(ctx.paths.state / LOCK, "a") as fh:
        os.chmod(fh.name, 0o600)
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _done(ctx: Context, boot: str) -> dict | None:
    """This boot's outcome, if this boot already had its run."""
    doc = read_json(ctx.paths.state / FILE, None)
    return doc if isinstance(doc, dict) and boot and doc.get("boot_id") == boot else None


def _finish(ctx: Context, boot: str, data: dict, message: str) -> tuple[dict, list[str]]:
    keep = {"boot_id": boot, "at": utc_now(), **{k: data[k] for k in
                                                 ("outcome", "agent", "tries", "reason")}}
    write_atomic(ctx.paths.state / FILE, json.dumps(keep, indent=2) + "\n", 0o644)
    notice.record(ctx, message, WHAT)
    return data, [message]


def _try(ctx: Context, ship: ShipToml, text: str) -> tuple[str | None, str | None]:
    """One try: (the primary agent's name or None, why it was not sent or None)."""
    if agent_record.resume_pending(ctx):
        return None, "resume-pending"
    doc = agent_record.load(ctx)
    name, _ = agent_record.primary(doc, ship)
    if name is None:
        return None, "no-primary"
    rt = for_ship(ctx, ship)  # herdr's server or herdr-core (ship.toml runtime)
    _, live, _ = rt.agents()
    if live is None:
        return name, "runtime-not-answering"
    return name, notice.send_one(ctx, rt, name, live, doc, text)


def run(ctx: Context, ship: ShipToml) -> tuple[dict, list[str]]:
    """Send this boot's line to the primary agent, at most once. (data, messages)"""
    data: dict = {"outcome": "off", "agent": None, "tries": 0, "reason": None}
    w = ship.wake
    if not w["after_boot"]:
        return data, ["wake.after_boot is off; nothing sent."]
    boot = boot_id(ctx)
    if not boot:  # without the boot id, once per boot cannot be kept
        data["outcome"] = "no-boot-id"
        return data, ["This boot's id cannot be read; nothing sent."]
    done = _done(ctx, boot)
    if done is not None:
        data.update(outcome="already-done", agent=done.get("agent"))
        return data, [f"This boot's wake already ran ({done.get('outcome')}); nothing sent."]
    due = int(w["delay_minutes"]) * 60
    up = agent_record.uptime(ctx)
    if up is not None and up < due - 60:
        data["outcome"] = "too-early"
        return data, [f"Not {delay_text(int(w['delay_minutes']))} after the boot yet; "
                      "nothing sent."]
    with _locked(ctx) as got:
        if not got:
            data["outcome"] = "busy"
            return data, ["Another ship-ctl wake is running; nothing sent."]
        if _done(ctx, boot) is not None:  # it finished while this one waited
            data["outcome"] = "already-done"
            return data, ["This boot's wake already ran; nothing sent."]
        if up is not None and up > due + LATE_S:
            data.update(outcome="late", reason="late")
            return _finish(ctx, boot, data, "the boot was too long ago "
                           f"({int(up // 60)} minutes); nothing sent.")
        text = line(ship)
        for attempt in range(1, TRIES + 1):
            data["tries"] = attempt
            name, why = _try(ctx, ship, text)
            data["agent"] = name
            if why is None:
                data.update(outcome="sent", reason=None)
                return _finish(ctx, boot, data, f"sent to the primary agent {name}.")
            data["reason"] = why
            if why == "no-primary":
                data["outcome"] = "no-primary"
                return _finish(ctx, boot, data, "no primary agent (none marked, and no "
                               "[[autostart]] agent in the agent record); nothing sent.")
            if attempt < TRIES:
                time.sleep(wait_budget(ctx, RETRY_S))
        data["outcome"] = "gave-up"
        who = f"the primary agent {data['agent']}" if data["agent"] else "the primary agent"
        return _finish(ctx, boot, data, f"not sent to {who} after {TRIES} tries "
                       f"({data['reason']}); gave up.")


def _systemctl(ctx: Context, *argv: str) -> None:
    r = ctx.runner.run(["systemctl", *argv], timeout=30)
    if not r.ok:
        detail = (r.err.strip().splitlines() or [f"exit {r.rc}"])[-1]
        raise CtlError("internal", f"wake: systemctl {argv[0]} failed: {detail[:300]}")


def sync(ctx: Context, ship: ShipToml, dry_run: bool = False) -> tuple[list[str], list[str]]:
    """ship-wake.timer as ship.toml [wake] says (ship-ctl apply): its delay in a
    drop-in, and enabled only while wake.after_boot is on. Enabling does not start
    it: it first runs after the next boot. (changed, messages)"""
    path = ctx.paths.p(DROP_IN)
    changed: list[str] = []
    on = bool(ship.wake["after_boot"])
    wrote = (write_atomic(path, drop_in(ship), 0o644, dry_run=dry_run) if on
             else remove(path, dry_run=dry_run))
    if wrote:
        changed.append(ctx.paths.agent_path(path))
        if not dry_run:
            _systemctl(ctx, "daemon-reload")
    enabled = ctx.runner.run(["systemctl", "is-enabled", "--quiet", TIMER], timeout=10).ok
    if on != enabled:
        changed.append(f"{TIMER} ({'enabled' if on else 'disabled'})")
        if not dry_run:
            _systemctl(ctx, "enable" if on else "disable", "--quiet", TIMER)
    messages = []
    if on and not enabled:
        messages.append(f"The primary agent is woken {delay_text(int(ship.wake['delay_minutes']))}"
                        " after each boot, from the next boot.")
    return changed, messages
