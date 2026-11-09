"""`ship-ctl agent restart|forget`: change recorded Claude agents in place.

A Claude session keeps its permission mode across a resume, so changing
`default_mode` (or an [[autostart]] mode) reaches only agents started afterwards.
`restart` restarts an agent in place: the same place (so the same folder and
workspace) and the same name, its conversation continued with
`claude --resume <session>` unless asked for a fresh one, in the mode chosen, else
the mode it would start in now (its [[autostart]] mode, else `default_mode`).

Its targets come from the agent record (record.py), brought up to date first, so
an agent started by hand is restarted by its name too. A recorded agent that is
not running (it stopped, or could not be started again after a crash) is started
again from the record: its folder, its session, the mode chosen.

A running agent is restarted gracefully, one agent at a time:

1. An agent that is working (or whose state the runtime cannot tell) is left
   alone unless forced; forced, its turn is interrupted (Escape) and the result
   says so. An agent waiting at a question has it dismissed (Escape), and the
   result says so.
2. `/exit` is typed into it (after Ctrl+C, which clears a half-typed line), and
   ship-ctl waits until the runtime no longer sees an agent there; if it is still
   there, Ctrl+C twice, then it gives up and leaves the agent running.
3. claude is launched again in that place with the mode flag.
4. The mode ledger and the agent record get the session in the new mode, so the
   status document reports it, `ship-fleet reboot` expects it and the next boot
   resumes it in that mode.

Remote Control comes back by itself: it follows the managed settings, and a
resumed session reconnects to the Remote Control session saved in it. Session
names survive a resume; a fresh conversation gets the name autostart gave it.

`forget` drops recorded agents that are not running, so they are neither resumed
at boot nor started again.
"""

from __future__ import annotations

import json
import sys

from . import PERMISSION_MODES, naming, record
from .autostart import DEFAULT_PROMPT
from .context import Context
from .errors import CtlError
from .paths import Paths
from .runtime import for_ship, mode_args
from .shiptoml import ShipToml
from .status import read_ledger

LEDGER_HOOK = "files/hooks/mode_ledger.py"  # in the release; installed as Paths.LEDGER_HOOK
BUSY_STATES = ("working", "unknown")


def record_mode(ctx: Context, session: str, mode: str) -> bool:
    """Put session -> mode in the agent's mode ledger. The ledger hook itself does it,
    run as the agent (so under its lock, and nothing is written into the agent's home
    as root); True once the ledger says so."""
    event = json.dumps({"hook_event_name": "ShipShapeRestart", "session_id": session,
                        "permission_mode": mode})
    hook = ctx.paths.p(Paths.LEDGER_HOOK)  # the copy Claude Code runs
    hook = hook if hook.is_file() else ctx.paths.bundle / LEDGER_HOOK
    ctx.runner.run([sys.executable, "-I", str(hook)], input=event, timeout=15, agent=True)
    if read_ledger(ctx).get(session) == mode:
        return True
    ctx.log(f"mode ledger: {session} not recorded as {mode}")
    return False


def wanted_mode(ship: ShipToml, name: str, mode: str | None) -> str:
    """The mode asked for, else the one the agent would start in now."""
    if mode:
        return mode
    entry = next((e for e in ship.autostart if e["name"] == name), None)
    return (entry or {}).get("mode") or ship.claude["default_mode"]


def _check_mode(ship: ShipToml, mode: str) -> None:
    if mode not in PERMISSION_MODES:
        raise CtlError("usage", f"unknown mode {mode!r}; modes: {', '.join(PERMISSION_MODES)}")
    if mode == "bypassPermissions" and not ship.claude["allow_bypass"]:
        raise CtlError("invalid-input",
                       "bypassPermissions is not allowed on this Ship (claude.allow_bypass "
                       "is false); allow it first, then restart the agent")


def _targets(doc: dict, live: list[dict], found: dict[str, dict], names: list[str],
             everyone: bool) -> tuple[list[tuple[str, dict | None]], list[dict]]:
    """([(name, its live agent or None when it is recorded but not running)], skipped)."""
    by_id = {id(a): n for n, a in found.items()}
    claude = [a for a in live if a["kind"] == "claude"]
    if everyone:
        targets, skipped = [], []
        for a in claude:
            name = by_id.get(id(a)) or a["name"]
            if name:
                targets.append((name, a))
            else:
                skipped.append({"name": a["place"] or "?", "reason": "it has no herdr name"})
        return targets, skipped
    named = {a["name"]: a for a in claude if a["name"]}
    out, missing = [], []
    for n in dict.fromkeys(names):
        if n in found:
            out.append((n, found[n]))
        elif n in doc["agents"]:
            out.append((n, None))
        elif n in named:
            out.append((n, named[n]))
        else:
            missing.append(n)
    if missing:
        running = sorted({by_id.get(id(a)) or a["name"] for a in claude} - {None}) or ["none"]
        stopped = sorted(set(doc["agents"]) - set(running))
        raise CtlError("invalid-input",
                       f"no Claude agent named {', '.join(missing)} is running or recorded "
                       f"(running: {', '.join(running)}"
                       + (f"; recorded, not running: {', '.join(stopped)}" if stopped else "")
                       + ")")
    return out, []


def restart(ctx: Context, ship: ShipToml, names: list[str], *, everyone: bool = False,
            mode: str | None = None, fresh: bool = False,
            force: bool = False) -> tuple[dict, list[str], list[str]]:
    """Returns (data, changed, messages); raises CtlError if any agent was not restarted
    (with what did happen attached)."""
    if bool(names) == everyone:
        raise CtlError("usage", "name the agents to restart, or give --all")
    if mode is not None:
        _check_mode(ship, mode)
    rt = for_ship(ctx)
    server, live, _ = rt.agents()
    if server != "running" or live is None:
        raise CtlError("herdr-unavailable", "the herdr server is not answering")
    # A mode not given is the one it would start in (or, for one not running, the one
    # it was in), which is bypassPermissions only where allow_bypass is true.
    with record.held(ctx):
        doc = record.load(ctx)
        if record.sync(ctx, rt, doc, live, read_ledger(ctx)):
            record.save(ctx, doc)
        found = record.match(doc, live)
        targets, skipped = _targets(doc, live, found, names, everyone)
        if not targets and not skipped:
            return {"restarted": [], "skipped": []}, [], ["No Claude agent is running."]
        data, changed, messages, busy, failed = _restart_all(
            ctx, ship, rt, doc, targets, skipped, mode=mode, fresh=fresh, force=force)
        record.save(ctx, doc)
    if busy and not failed:
        raise CtlError("agent-busy", f"{busy} agent(s) are busy and were not restarted "
                       "(force to interrupt them)").with_data(data, changed, messages)
    if failed or busy:
        raise CtlError("agent-failed", f"{failed + busy} agent(s) could not be "
                       "restarted").with_data(data, changed, messages)
    return data, changed, messages


def _restart_all(ctx, ship, rt, doc, targets, skipped, *, mode, fresh, force):
    autostart = {e["name"]: e for e in ship.autostart}
    restarted: list[dict] = []
    messages: list[str] = []
    busy = failed = 0
    named = None  # whether claude takes --name; asked once, only for a fresh start
    ledger = read_ledger(ctx)
    states = {name: ("stopped" if e["want"] == "stopped" else
                     "failed" if record.tries_this_boot(ctx, e) >= record.MAX_TRIES
                     else "starting")
              for name, e in doc["agents"].items()}

    def label(name: str) -> str:
        rec = doc["agents"].get(name) or {}
        return rec.get("session_name") or naming.session_name(ship, name)

    def fresh_args(name: str) -> list[str]:
        nonlocal named
        if named is None:
            named = naming.name_flag_supported(ctx)
        return naming.launch_args(label(name), named)

    for name, a in targets:
        rec = doc["agents"].get(name)
        new_mode = wanted_mode(ship, name, mode)
        if a is None and mode is None:  # not running: back as it was
            new_mode = ledger.get(rec["session"] or "") or rec["mode"] or new_mode
            if new_mode == "bypassPermissions" and not ship.claude["allow_bypass"]:
                new_mode = ship.claude["default_mode"]
        previous = (rec or {}).get("mode")  # before this restart records the new one
        if a is not None and (a["session"] or (rec or {}).get("session")) in ledger:
            previous = ledger[a["session"] or rec["session"]]
        if a is None:  # recorded, not running: start it again from the record
            was = states.get(name) or rec["want"]
            if not fresh and not rec["session"]:
                failed += 1
                skipped.append({"name": name, "reason": "its session is not known, so its "
                                "conversation cannot be continued; restart it fresh instead"})
                continue
            ok, why = record.relaunch(ctx, ship, rt, doc, name, rt.places(), same_place=False,
                                      mode=new_mode, fresh=fresh_args(name) if fresh else None,
                                      keep_tries=False)
            if not ok:
                failed += 1
                skipped.append({"name": name, "reason": f"did not start again: {why}"})
                continue
            session = doc["agents"][name]["session"]
            if session:
                record_mode(ctx, session, new_mode)
        else:
            was, place, session = a["state"], a["place"], a["session"] or (rec or {}).get(
                "session")
            if was in BUSY_STATES and not force:
                busy += 1
                why = "working" if was == "working" else "in a state herdr cannot tell"
                skipped.append({"name": name, "reason": f"it is {why}; not interrupted"})
                messages.append(f"{name} is {why}: left alone. Try again when it is idle, or "
                                "force it (which interrupts its turn).")
                continue
            if not place:
                failed += 1
                skipped.append({"name": name, "reason": "herdr reports no pane for it"})
                continue
            if not fresh and session is None:
                failed += 1
                skipped.append({"name": name, "reason": "its session is not known, so its "
                                "conversation cannot be continued; restart it fresh instead"})
                continue
            stopped, notes = rt.stop(place, was)
            if not stopped:
                failed += 1
                skipped.append({"name": name, "reason": "it did not exit; left running"})
                messages.append(f"{name} did not exit when asked; it is still running as before"
                                + (f" ({'; '.join(notes)})" if notes else "") + ".")
                continue
            if not fresh:  # stopped: from now on the session is in its new mode
                record_mode(ctx, session, new_mode)
            args = [*mode_args(new_mode), *(fresh_args(name) if fresh else ["--resume", session])]
            ok, why = rt.start_settled(name, place, args)
            back = rt.in_place(place, name)
            cwd = a["cwd"] or (rec or {}).get("cwd")
            if not ok and back is None:
                failed += 1
                skipped.append({"name": name, "reason": f"stopped, but did not start again: {why}"})
                launch = " ".join(["claude", *mode_args(new_mode),
                                   *([] if fresh else ["--resume", session])])
                messages.append(f"{name} stopped but did not start again ({why}). Start it in its "
                                f"pane with: {launch}")
                if rec is not None and not fresh:
                    # A crash, as far as the record goes (the restart cleared its /exit
                    # from the ledger): the next refresh starts it again, in its new mode,
                    # and counts this as its first failed try (it stopped at once).
                    record.launched(ctx, rt, doc, name, cwd=rec["cwd"], place=place,
                                    workspace=a["workspace"], session=session, mode=new_mode,
                                    origin=rec["origin"])
                    rec["relaunch"].update(last_error=why[:200])
                continue
            new_session = (back or {}).get("session") or (None if fresh else session)
            if new_session and (fresh or new_session != session):
                record_mode(ctx, new_session, new_mode)
            if cwd:
                record.launched(ctx, rt, doc, name, cwd=cwd, place=place,
                                workspace=(back or a).get("workspace"), session=new_session,
                                mode=new_mode, origin=(rec or {}).get("origin", "adopted"),
                                session_name=label(name) if fresh else None)
                if fresh and not new_session:
                    doc["agents"][name]["session"] = None
        if fresh:
            prompt = (autostart.get(name) or {}).get("prompt") or (
                DEFAULT_PROMPT if name in autostart else (rec or {}).get("prompt"))
            if prompt:
                sent, why = rt.prompt(name, prompt)
                if not sent:
                    messages.append(f"{name}: started, but the first prompt was not sent "
                                    f"({why}).")
        restarted.append({"name": name, "mode": new_mode, "previous_mode": previous,
                          "conversation": "fresh" if fresh else "continued", "was": was})
        said = f"{name} restarted in {new_mode}, " + (
            "in a fresh conversation" if fresh else "continuing its conversation")
        notes = notes if a is not None else []
        messages.append(said + (f" ({'; '.join(notes)})" if notes else "") + ".")

    data = {"restarted": restarted, "skipped": skipped}
    return data, [r["name"] for r in restarted], messages, busy, failed


def forget(ctx: Context, names: list[str]) -> tuple[dict, list[str], list[str]]:
    if not names:
        raise CtlError("usage", "name the agents to forget")
    return record.forget(ctx, names)
