"""The agent record: ShipShape's own list of the Claude agents it keeps running.

`/var/lib/shipshape/agents.json` (root, 0644; contracts/agent-record.schema.json):
one entry per agent, keyed by its name, with its folder, Claude session id,
permission mode, the first prompt autostart gave it, where it last ran and
whether it should be running. The runtime (runtime.py) is told what to do from
this record. Nothing depends on the runtime saving or resuming agents itself.

It is kept in step at every status refresh (`upkeep`), and whenever ship-ctl
starts, restarts or changes an agent:

- **sync**: every live Claude agent that reports a session is in the record. One
  started by hand is picked up ("adopted"), named after its folder if it has no
  name. A recorded agent whose session changed (`/clear`, a restart by hand in
  its pane) is followed, and its mode follows the mode ledger.
- **reconcile**: an agent that should run but is not live is started again,
  with `claude --resume <session>` in its folder and mode. After a boot, that is
  how agents resume. Within one boot, that is the crash policy. An agent counts as
  stopped on purpose, and is not started again (one that is not an [[autostart]]
  agent leaves the record), when Claude reported it exited (SessionEnd
  `prompt_input_exit` or `logout`, kept by the mode ledger) or its place is gone
  (its pane was closed). A start that fails, or an agent that stops within
  STABLE_S of starting, is a failed try; its tries reset once it has run that long.
  After MAX_TRIES failed tries in one boot it is left alone (`failed`, the attention
  item agent-relaunch-failed) until it is restarted or forgotten, or the next boot.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import PERMISSION_MODES
from .context import Context, utc_now
from .errors import CtlError
from .fsutil import ensure_dir, read_json, write_atomic
from .runtime import Herdr, Runtime, for_ship, mode_args, wait_budget
from .shiptoml import ShipToml
from .status import boot_id, read_ledger

SCHEMA = "shipshape.agent-record/1"
FILE = "agents.json"
LOCK = "agents.lock"
OLD_AUTOSTART = "autostart.json"  # autostart's record before this one (imported once)
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")  # herdr's rule for agent names
MAX_TRIES = 3  # failed tries in one boot before an agent is left alone
STABLE_S = 600  # an agent that stops sooner after it started counts as a failed try
LOCK_WAIT_S = 60  # how long a command waits for another ship-ctl's agents to start
DNS_TIMEOUT_S = 3
DELIBERATE_ENDS = ("prompt_input_exit", "logout")  # Claude's SessionEnd reasons
RESUME_GRACE_S = 600  # after this much uptime, nothing waits for this boot's resume
DNS_PROBE = "api.anthropic.com"  # resumed sessions reconnect Remote Control at once
LOG_TAG = "agent record"  # starts every line upkeep logs
RUNTIME_KINDS = ("herdr", "core")
SWITCH_OUTCOMES = ("switched", "rolled-back", "rollback-failed", "interrupted", "repaired")


# --- the file -------------------------------------------------------------------
def _ts(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) == 20 and value.endswith("Z") else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _clean(e: dict) -> dict:
    """An entry with every field the right shape (a hand-edited file is tolerated)."""
    place = e.get("place") if isinstance(e.get("place"), dict) else None
    if place and not _text(place.get("id")):
        place = None
    relaunch = e.get("relaunch") if isinstance(e.get("relaunch"), dict) else {}
    failures = relaunch.get("failures")
    return {
        "cwd": e["cwd"],
        "session": _text(e.get("session")),
        "mode": e.get("mode") if e.get("mode") in PERMISSION_MODES else None,
        "prompt": _text(e.get("prompt")),
        "session_name": _text(e.get("session_name")),
        "origin": e.get("origin") if e.get("origin") in ("autostart", "adopted") else "adopted",
        "want": "stopped" if e.get("want") == "stopped" else "running",
        "place": None if place is None else {
            "runtime": _text(place.get("runtime")) or Herdr.kind, "id": place["id"],
            "workspace": _text(place.get("workspace")), "boot_id": _text(place.get("boot_id"))},
        "relaunch": {
            "boot_id": _text(relaunch.get("boot_id")),
            "failures": failures if isinstance(failures, int) and failures >= 0 else 0,
            "last_error": _text(relaunch.get("last_error")),
            "last_at": _ts(relaunch.get("last_at")),
            "counted_launch": _ts(relaunch.get("counted_launch")),
        },
        "launched_at": _ts(e.get("launched_at")),
        "stopped_at": _ts(e.get("stopped_at")),
        "created_at": _ts(e.get("created_at")) or utc_now(),
        "updated_at": _ts(e.get("updated_at")) or utc_now(),
    }


def load(ctx: Context) -> dict:
    raw = read_json(ctx.paths.state / FILE, None)
    doc: dict = {"schema": SCHEMA, "runtime": Herdr.kind, "agents": {}, "resume": None,
                 "primary": None}
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        if raw is not None:
            ctx.log(f"{FILE}: not an agent record; starting a new one")
        return doc
    doc["runtime"] = _text(raw.get("runtime")) or Herdr.kind
    agents = raw.get("agents") if isinstance(raw.get("agents"), dict) else {}
    for name, e in agents.items():
        if NAME.match(str(name)) and isinstance(e, dict) and _text(e.get("cwd")):
            doc["agents"][name] = _clean(e)
    resume = raw.get("resume")
    if isinstance(resume, dict) and _text(resume.get("boot_id")) and _ts(resume.get("at")):
        doc["resume"] = {"boot_id": resume["boot_id"], "at": resume["at"],
                         "resumed": int(resume.get("resumed") or 0),
                         "failed": int(resume.get("failed") or 0)}
    mark = raw.get("primary")
    if isinstance(mark, str) and mark in doc["agents"]:  # one that left the record loses it
        doc["primary"] = mark
    # `ship-ctl runtime switch` (switch.py): the one in progress, and the last few.
    now = raw.get("switch")
    if (isinstance(now, dict) and now.get("from") in RUNTIME_KINDS
            and now.get("to") in RUNTIME_KINDS and isinstance(now.get("agents"), dict)):
        doc["switch"] = now
    done = [s for s in raw.get("switches") or [] if isinstance(s, dict) and _ts(s.get("at"))
            and s.get("outcome") in SWITCH_OUTCOMES]
    if done:
        doc["switches"] = done
    return doc


def save(ctx: Context, doc: dict) -> bool:
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    return write_atomic(ctx.paths.state / FILE, text, 0o644)


_held = 0  # this process holds the lock (ship-ctl runs one command at a time)


@contextlib.contextmanager
def locked(ctx: Context, wait: bool = True, timeout: float | None = None) -> Iterator[bool]:
    """Hold the record's lock: one ship-ctl at a time starts, stops or records
    agents. Yields False (without the lock) when it is taken and wait is False, or
    still taken after timeout seconds."""
    global _held
    if _held:
        _held += 1
        try:
            yield True
        finally:
            _held -= 1
        return
    ensure_dir(ctx.paths.state, 0o755)
    with open(ctx.paths.state / LOCK, "a") as fh:
        os.chmod(fh.name, 0o600)
        deadline = time.monotonic() + (timeout or 0)
        while True:
            try:
                blocking = wait and timeout is None
                fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                if wait and time.monotonic() < deadline:
                    time.sleep(0.5)
                    continue
                got = False
            else:
                got = True
            break
        if not got:
            yield False
            return
        _held = 1
        try:
            yield True
        finally:
            _held = 0
            fcntl.flock(fh, fcntl.LOCK_UN)


@contextlib.contextmanager
def held(ctx: Context) -> Iterator[None]:
    """The lock, for a command someone runs: it waits at most LOCK_WAIT_S for another
    ship-ctl (a boot's resume, say) to finish starting agents, then gives up saying so."""
    with locked(ctx, timeout=wait_budget(ctx, LOCK_WAIT_S)) as got:
        if not got:
            raise CtlError("agent-busy", "another ship-ctl is starting or stopping agents "
                           "(after a boot, the resume); try again in a minute")
        yield


# --- the host ---------------------------------------------------------------------
def uptime(ctx: Context) -> float | None:
    try:
        return float((ctx.paths.proc / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def boot_started(ctx: Context) -> str:
    """When this boot began, as a timestamp ("" if unknown: everything is after)."""
    up = uptime(ctx)
    if up is None:
        return ""
    return (datetime.now(UTC) - timedelta(seconds=up)).strftime("%Y-%m-%dT%H:%M:%SZ")


def dns_ready(ctx: Context) -> bool:
    """The resolver answers for Claude's API (getent, with a hard timeout)."""
    return ctx.runner.run(["getent", "ahosts", DNS_PROBE], timeout=DNS_TIMEOUT_S).ok


def age_s(stamp: str | None) -> float | None:
    """Seconds since a timestamp of this record, or None."""
    try:
        when = datetime.strptime(stamp or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (datetime.now(UTC) - when).total_seconds()


def resume_pending(ctx: Context, doc: dict | None = None) -> bool:
    """This boot's agents are not resumed yet (and the boot is young enough that
    something should wait for it)."""
    doc = load(ctx) if doc is None else doc
    if (doc.get("resume") or {}).get("boot_id") == boot_id(ctx):
        return False
    up = uptime(ctx)
    return up is not None and up < RESUME_GRACE_S


# --- the mode ledger's word on how sessions started and ended ---------------------------
def ledger_sessions(ctx: Context) -> dict[str, dict]:
    """{session: {"ended": {"reason", "at"} or None, "watched": bool}}. watched: the
    hook saw the session start, so it runs under the SessionEnd hook too (Claude
    Code reads hooks when a session starts) and an exit of it is recorded."""
    doc = ctx.read_home_json(ctx.paths.mode_ledger, {})
    sessions = doc.get("sessions", {}) if isinstance(doc, dict) else {}
    out = {}
    for sid, entry in sessions.items() if isinstance(sessions, dict) else ():
        if not isinstance(entry, dict):
            continue
        ended = entry.get("ended")
        if not (isinstance(ended, dict) and _text(ended.get("reason")) and _ts(ended.get("at"))):
            ended = None
        out[sid] = {"ended": None if ended is None else {"reason": ended["reason"],
                                                         "at": ended["at"]},
                    "watched": _ts(entry.get("started_at")) is not None}
    return out


# --- matching the record to what runs -------------------------------------------------
def same_folder(a: str | None, b: str | None) -> bool:
    return bool(a and b) and os.path.normpath(a) == os.path.normpath(b)


def match(doc: dict, live: list[dict]) -> dict[str, dict]:
    """{record name: the live agent that is it}. A live Claude agent is a recorded
    one if it has its name; else, having no name, if it runs its session, or sits
    in its place in its folder (claude started again by hand there). One started
    by hand in that pane but in another folder is another agent."""
    claude = [a for a in live if a["kind"] == "claude"]
    out: dict[str, dict] = {}
    taken: set[int] = set()

    def claim(name: str, test) -> None:
        a = next((a for a in claude if id(a) not in taken and test(a)), None)
        if a is not None:
            out[name] = a
            taken.add(id(a))

    agents = doc["agents"]
    for name in agents:
        claim(name, lambda a, n=name: a["name"] == n)
    for name, e in agents.items():
        if name not in out and e["session"]:
            claim(name, lambda a, s=e["session"]: a["name"] is None and a["session"] == s)
    for name, e in agents.items():
        if name not in out and e["place"]:
            claim(name, lambda a, p=e["place"]["id"], c=e["cwd"]: a["name"] is None
                  and a["place"] == p and same_folder(a["cwd"], c))
    return out


def find_live(ctx: Context, name: str, live: list[dict], doc: dict | None = None) -> dict | None:
    """The live agent that is record entry `name`, else the live Claude agent by
    that name, or None."""
    doc = load(ctx) if doc is None else doc
    found = match(doc, live).get(name)
    if found is None and name not in doc["agents"]:
        found = next((a for a in live if a["kind"] == "claude" and a["name"] == name), None)
    return found


def tries_this_boot(ctx: Context, e: dict) -> int:
    """Failed starts of entry e in this boot."""
    return e["relaunch"]["failures"] if e["relaunch"]["boot_id"] == boot_id(ctx) else 0


def _place(rt: Runtime, ctx: Context, place: str, workspace: str | None) -> dict:
    return {"runtime": rt.kind, "id": place, "workspace": workspace, "boot_id": boot_id(ctx)}


def own_place(rt: Runtime, e: dict) -> str | None:
    """Where entry e last ran, if that was in this runtime (a place of the other
    one, from before the Ship's runtime changed, is no place here)."""
    place = e["place"] or {}
    return place.get("id") if place.get("runtime") == rt.kind else None


def elsewhere(rt: Runtime, ctx: Context, e: dict) -> bool:
    """Entry e ran this boot under the other runtime (ship.toml's runtime changed
    while it ran): it may run there still, so nothing starts it here until the
    next boot, when only this runtime starts."""
    place = e["place"] or {}
    return (bool(place.get("runtime")) and place["runtime"] != rt.kind
            and place.get("boot_id") == boot_id(ctx))


def new_session(rt: Runtime) -> tuple[str | None, list[str]]:
    """(the session id ShipShape gives a new conversation, its claude arguments):
    on herdr-core ShipShape chooses it (`--session-id`), so the record has it
    before the agent starts; herdr reports the one claude chose."""
    if not rt.chooses_session:
        return None, []
    sid = str(uuid.uuid4())
    return sid, ["--session-id", sid]


def _new_name(cwd: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9_-]+", "-", os.path.basename(cwd.rstrip("/")).lower()).strip("-_")
    if not base or not base[0].isalpha():
        base = f"agent-{base}" if base else "agent"
    base = base[:28].rstrip("-_")
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def _set(e: dict, **fields) -> bool:
    changed = False
    for key, value in fields.items():
        if e.get(key) != value:
            e[key] = value
            changed = True
    if changed:
        e["updated_at"] = utc_now()
    return changed


def new_entry(cwd: str, *, origin: str, session: str | None = None, mode: str | None = None,
              prompt: str | None = None, session_name: str | None = None,
              place: dict | None = None) -> dict:
    now = utc_now()
    return {"cwd": cwd, "session": session, "mode": mode, "prompt": prompt,
            "session_name": session_name, "origin": origin, "want": "running",
            "place": place, "relaunch": {"boot_id": None, "failures": 0, "last_error": None,
                                         "last_at": None, "counted_launch": None},
            "launched_at": now, "stopped_at": None, "created_at": now, "updated_at": now}


def launched(ctx: Context, rt: Runtime, doc: dict, name: str, *, cwd: str, place: str | None,
             workspace: str | None, session: str | None, mode: str | None,
             origin: str = "autostart", prompt: str | None = None,
             session_name: str | None = None, keep_tries: bool = False) -> dict:
    """Record that ship-ctl just started `name` (autostart, restart, or the crash
    policy, which keeps the boot's failed tries: they reset only once it has run
    STABLE_S). place None: it is about to start, in a place not known yet."""
    e = doc["agents"].get(name)
    if e is None:
        e = doc["agents"][name] = new_entry(cwd, origin=origin)
    fields = {"cwd": cwd, "mode": mode, "want": "running", "stopped_at": None,
              "launched_at": utc_now(),
              "place": _place(rt, ctx, place, workspace) if place else e["place"]}
    if not keep_tries:
        fields["relaunch"] = {"boot_id": boot_id(ctx), "failures": 0, "last_error": None,
                              "last_at": None, "counted_launch": None}
    else:  # a new start: an early stop of it is still to be counted
        fields["relaunch"] = {**e["relaunch"], "counted_launch": None}
    if session:
        fields["session"] = session
    if prompt is not None:
        fields["prompt"] = prompt
    if session_name is not None:
        fields["session_name"] = session_name
    _set(e, **fields)
    return e


def sync(ctx: Context, rt: Runtime, doc: dict, live: list[dict], ledger: dict[str, str]) -> bool:
    """Bring the record up to what runs (module docstring). True if it changed."""
    changed = False
    found = match(doc, live)
    for name, a in found.items():
        e = doc["agents"][name]
        if a["name"] is None and a["place"] and rt.rename(a["place"], name):
            a["name"] = name  # its name went (a restart by hand in its pane): back again
        session = a["session"] or e["session"]
        fields = {"session": session, "mode": ledger.get(session or "") or e["mode"],
                  "want": "running", "stopped_at": None}
        if a["cwd"]:
            fields["cwd"] = a["cwd"]
        if a["place"]:
            here = _place(rt, ctx, a["place"], a["workspace"])
            if e["place"] != here:
                fields["place"] = here
        age = age_s(e["launched_at"])
        if e["relaunch"]["failures"] and age is not None and age >= STABLE_S:
            # It has run long enough since its last start: its tries are spent no more.
            fields["relaunch"] = {**e["relaunch"], "failures": 0, "last_error": None}
        changed |= _set(e, **fields)
    claimed = {id(a) for a in found.values()}
    taken = set(doc["agents"]) | {a["name"] for a in live if a["name"]}
    for a in live:
        if id(a) in claimed or a["kind"] != "claude" or not a["session"] or not a["cwd"]:
            continue
        name = a["name"]
        if name is None:
            name = _new_name(a["cwd"], taken)
            if a["place"] and rt.rename(a["place"], name):
                a["name"] = name
        if not NAME.match(name):
            continue
        taken.add(name)
        place = _place(rt, ctx, a["place"], a["workspace"]) if a["place"] else None
        doc["agents"][name] = new_entry(a["cwd"], origin="adopted", session=a["session"],
                                        mode=ledger.get(a["session"]), place=place)
        ctx.log(f"{LOG_TAG}: picked up {name} ({a['cwd']})")
        changed = True
    return changed


def stopped_on_purpose(e: dict, sessions: dict[str, dict], places: dict | None,
                       since: str) -> str | None:
    """Why an agent that ran this boot and is gone now was stopped, not crashed."""
    seen = sessions.get(e["session"] or "") or {"ended": None, "watched": False}
    # An exit ship-ctl made itself (agent restart) is no longer in the ledger: the
    # restart records the session's new mode through the hook, which clears it.
    end = seen["ended"]
    if end and end["reason"] in DELIBERATE_ENDS and end["at"] >= max(since,
                                                                     e["launched_at"] or ""):
        return f"it was exited ({end['reason']})"
    if not seen["watched"]:
        # It started before this tooling's hooks (a Ship just updated): an exit of it
        # looks like a crash, so it is treated as one would have been before, and
        # left stopped. The next boot resumes everything under the new hooks.
        return "it started before the Ship could tell an exit from a crash"
    if places is None:
        return None
    here = places.get(e["place"]["id"])
    if here is None:
        return "its pane was closed"
    if here["agent"]:
        return f"another agent ({here['agent']}) is in its pane"
    if here["cwd"] and not same_folder(here["cwd"], e["cwd"]):
        return f"its pane is in another folder now ({here['cwd']})"
    return None


def let_go(doc: dict, name: str, autostart: set[str]) -> None:
    """Let an agent go: an [[autostart]] one stays, stopped (autostart starts it
    afresh at the next boot); any other leaves the record, as herdr forgets a pane's
    agent when it exits."""
    if name not in autostart:
        del doc["agents"][name]
    else:
        _set(doc["agents"][name], want="stopped", stopped_at=utc_now())


def let_go_exited(doc: dict, name: str, sessions: dict[str, dict], places: dict | None,
                  since: str, autostart: set[str]) -> str | None:
    """Let agent name go if it ran this boot and was stopped on purpose, not crashed
    (stopped_on_purpose); why, or None (it stays, to be resumed)."""
    why = stopped_on_purpose(doc["agents"][name], sessions, places, since)
    if why:
        let_go(doc, name, autostart)
    return why


def prune(doc: dict, ship: ShipToml) -> bool:
    """Drop stopped agents that are no longer [[autostart]] agents (removed from it
    after they stopped): nothing would ever start them again."""
    autostart = {e["name"] for e in ship.autostart}
    gone = [n for n, e in doc["agents"].items() if e["want"] == "stopped" and n not in autostart]
    for name in gone:
        del doc["agents"][name]
    return bool(gone)


def folder_exists(ctx: Context, cwd: str) -> bool:
    return (ctx.paths.p(cwd) if ctx.paths.sandbox else Path(cwd)).is_dir()


def relaunch(ctx: Context, ship: ShipToml, rt: Runtime, doc: dict, name: str,
             places: dict | None, *, same_place: bool, mode: str | None = None,
             fresh: list[str] | None = None, keep_tries: bool = True) -> tuple[bool, str]:
    """Start recorded agent `name` again, continuing its session (or, with fresh,
    a new conversation with those extra arguments): in its own place when
    same_place (it died there this boot), else in its restored place if that is a
    free shell in its folder, else in a new one. (ok, why not)."""
    e = doc["agents"][name]
    if elsewhere(rt, ctx, e):
        return False, (f"it ran under {e['place']['runtime']} this boot and may still run "
                       f"there; it moves to {rt.kind} at the next boot")
    # The ledger has the session's latest mode (a change in Claude reaches the record
    # only at the next sync), so it comes first.
    mode = mode or read_ledger(ctx).get(e["session"] or "") or e["mode"]
    if mode == "bypassPermissions" and not ship.claude["allow_bypass"]:
        mode = ship.claude["default_mode"]
    cwd = e["cwd"]
    if not folder_exists(ctx, cwd):
        return False, f"its folder {cwd} is not there"
    sid, chosen = new_session(rt) if fresh is not None else (None, [])
    args = [*mode_args(mode), *(fresh if fresh is not None else ["--resume", e["session"]]),
            *chosen]
    old = own_place(rt, e)
    spot = (places or {}).get(old or "")
    if same_place:
        tries = [old]
    elif old and places is None:
        tries = [old, None]  # herdr did not list its panes: its own first, if it is there
    elif old and spot and spot["usable"] and same_folder(spot["cwd"], cwd):
        tries = [old, None]
    else:
        tries = [None]
    before = e["session"]
    if sid:  # the record has the new conversation's id before it starts
        e["session"] = sid
        save(ctx, doc)
    why = ""
    for place in tries:
        workspace = (spot or {}).get("workspace") if place else None
        done = rt.launch(name, cwd, args, place, workspace)
        why = done["why"]
        if done["ok"]:
            place = done["place"]
            back = rt.in_place(place, name) or {}
            launched(ctx, rt, doc, name, cwd=cwd, place=place,
                     workspace=back.get("workspace") or done["workspace"],
                     session=back.get("session") or sid or (None if fresh is not None
                                                            else e["session"]),
                     mode=mode, origin=e["origin"], keep_tries=keep_tries)
            if fresh is not None and not (back.get("session") or sid):
                e["session"] = None  # a new conversation: the next sync learns its id
            return True, ""
    e["session"] = before
    return False, why or "it did not start"


def reconcile(ctx: Context, ship: ShipToml, rt: Runtime, doc: dict, live: list[dict]
              ) -> tuple[int, int, list[str]]:
    """Start again every agent that should run and does not (module docstring).
    Returns (started, failed, messages)."""
    found = match(doc, live)
    wanted = [n for n, e in doc["agents"].items() if e["want"] == "running" and n not in found
              and not elsewhere(rt, ctx, e)]
    if not wanted:
        return 0, 0, []
    boot = boot_id(ctx)
    since = boot_started(ctx)
    sessions = ledger_sessions(ctx)
    places = rt.places()
    autostart = {e["name"] for e in ship.autostart}
    started = failed = 0
    messages: list[str] = []
    for name in wanted:
        e = doc["agents"][name]
        r = e["relaunch"]
        if r["boot_id"] != boot:  # a new boot: a fresh set of tries
            r.update(boot_id=boot, failures=0, last_error=None, last_at=None,
                     counted_launch=None)
        this_boot = bool(own_place(rt, e)) and e["place"]["boot_id"] == boot
        why = let_go_exited(doc, name, sessions, places, since, autostart) if this_boot else None
        if why:
            messages.append(f"{name} stopped ({why}); not started again.")
            continue
        if not e["session"]:
            # Nothing to continue (it never reported a session): autostart starts
            # an [[autostart]] agent afresh; any other is let go.
            let_go(doc, name, autostart)
            messages.append(f"{name}: no session to resume; left stopped.")
            continue
        if r["failures"] >= MAX_TRIES:
            continue
        lived = age_s(e["launched_at"]) if this_boot else None
        if lived is not None and lived < STABLE_S and r["counted_launch"] is None:
            # It stopped soon after it started (its transcript too big to load, a
            # crash on start): a failed try, once per start, so a loop of them ends.
            r.update(failures=r["failures"] + 1, counted_launch=e["launched_at"],
                     last_at=utc_now(), last_error=f"it stopped {int(lived)} s after it started")
            e["updated_at"] = utc_now()
            if r["failures"] >= MAX_TRIES:
                failed += 1
                messages.append(f"{name} stopped again soon after it started; left alone "
                                f"after {MAX_TRIES} tries.")
                continue
        ok, why = relaunch(ctx, ship, rt, doc, name, places, same_place=this_boot)
        if ok:
            started += 1
            messages.append(f"{name} started again, continuing its session"
                            + (" (it had stopped)" if this_boot else "") + ".")
        else:
            failed += 1
            r.update(failures=r["failures"] + 1, last_error=why[:200], last_at=utc_now())
            e["updated_at"] = utc_now()
            messages.append(f"{name} did not start again ({why}); try {r['failures']} of "
                            f"{MAX_TRIES}.")
    return started, failed, messages


def import_autostart(ctx: Context, doc: dict, live: list[dict]) -> bool:
    """Bring autostart's old record (tooling before this one) into this one, once:
    an [[autostart]] agent running now under that record is recorded as autostart's,
    with its session name. Agents it lists that are not running are not resumed:
    autostart starts them afresh, as it did."""
    path = ctx.paths.state / OLD_AUTOSTART
    old = read_json(path, None)
    if old is None:
        return False
    for name, rec in (old if isinstance(old, dict) else {}).items():
        if name in doc["agents"] or not isinstance(rec, dict) or not NAME.match(str(name)):
            continue
        claude = [a for a in live if a["kind"] == "claude"]
        a = (next((a for a in claude if a["name"] == name), None)
             or next((a for a in claude if rec.get("session") and a["session"] == rec["session"]
                      and a["name"] is None), None)
             or next((a for a in claude if rec.get("pane") and a["place"] == rec["pane"]
                      and a["name"] is None), None))
        if a is None or not a["cwd"]:
            continue
        doc["agents"][name] = new_entry(
            a["cwd"], origin="autostart", session=a["session"] or _text(rec.get("session")),
            mode=_text(rec.get("mode")), session_name=_text(rec.get("session_name")),
            place={"runtime": Herdr.kind, "id": a["place"], "workspace": a["workspace"],
                   "boot_id": boot_id(ctx)} if a["place"] else None)
    path.unlink()
    return True


def follow_runtime(rt: Runtime, doc: dict) -> bool:
    """The record names the runtime its agents run in now."""
    if doc["runtime"] == rt.kind:
        return False
    doc["runtime"] = rt.kind
    return True


def upkeep(ctx: Context, ship: ShipToml, rt: Runtime, doc: dict, live: list[dict], state: str
           ) -> tuple[list[dict], list[str], bool]:
    """Sync the record with what runs, then start what should run and does not.
    Call it with the lock held. Returns (live agents now, messages, changed)."""
    changed = import_autostart(ctx, doc, live)
    changed |= follow_runtime(rt, doc)
    changed |= sync(ctx, rt, doc, live, read_ledger(ctx))
    changed |= prune(doc, ship)
    if state in ("provisioning", "needs-sign-in"):
        return live, [], changed  # nothing starts before Claude is signed in
    boot = boot_id(ctx)
    first = (doc.get("resume") or {}).get("boot_id") != boot
    if first and not dns_ready(ctx) and (uptime(ctx) or 0) < RESUME_GRACE_S:
        return live, [f"{LOG_TAG}: resume waits for DNS"], changed
    started, failed, messages = reconcile(ctx, ship, rt, doc, live)
    messages = [f"{LOG_TAG}: {m}" for m in messages]  # `ship-ctl logs resume` finds them
    changed |= bool(started or failed or messages)
    if first:
        doc["resume"] = {"boot_id": boot, "at": utc_now(), "resumed": started,
                         "failed": failed}
        changed = True
    if started:
        _, again, _ = rt.agents()
        live = again if again is not None else live
        changed |= sync(ctx, rt, doc, live, read_ledger(ctx))
    return live, messages, changed


def refresh(ctx: Context, ship: ShipToml, state: str) -> list[str]:
    """The status refresh's turn: upkeep, unless another ship-ctl holds the record."""
    rt = for_ship(ctx, ship)
    with locked(ctx, wait=False) as got:
        if not got:
            return []
        server, live, _ = rt.agents()
        if server != "running" or live is None:
            return []
        doc = load(ctx)
        _, messages, changed = upkeep(ctx, ship, rt, doc, live, state)
        if changed:
            save(ctx, doc)
    return messages


def capture(ctx: Context, ship: ShipToml, timeout: float = 15) -> list[str]:
    """Record every running agent now (`ship-ctl apply`, so also every converge):
    a Ship updated from tooling before the record has its agents recorded before it
    next reboots, and comes back with them in their modes."""
    rt = for_ship(ctx, ship)
    with locked(ctx, wait=False) as got:
        if not got:
            return []  # a refresh is keeping the record in step this moment
        server, live, errors = rt.agents(timeout)
        if live is None:
            if server == "running" or "timed out" in " ".join(errors):
                return [f"agent record: herdr's agents could not be read ({'; '.join(errors)}); "
                        "the next status refresh records them"]
            return []  # herdr is not running yet (first boot): nothing to record
        doc = load(ctx)
        changed = import_autostart(ctx, doc, live)
        changed |= follow_runtime(rt, doc)
        changed |= sync(ctx, rt, doc, live, read_ledger(ctx))
        changed |= prune(doc, ship)  # `set autostart.remove=` lets a stopped one go
        if changed:
            save(ctx, doc)
    return []


def forget(ctx: Context, names: list[str]) -> tuple[dict, list[str], list[str]]:
    """Drop agents that are not running from the record. (data, changed, messages)."""
    rt = for_ship(ctx)
    with held(ctx):
        doc = load(ctx)
        server, live, _ = rt.agents()
        if server != "running" or live is None:  # then nothing could be told to run
            raise CtlError("herdr-unavailable", "the herdr server is not answering, so "
                           "whether the agent runs is not known; nothing forgotten")
        found = match(doc, live)
        unknown = [n for n in names if n not in doc["agents"]]
        if unknown:
            raise CtlError("invalid-input", f"not in the agent record: {', '.join(unknown)} "
                           f"(recorded: {', '.join(sorted(doc['agents'])) or 'none'})")
        running = [n for n in names if n in found]
        if running:
            raise CtlError("invalid-input", f"{', '.join(running)} is running; exit it first "
                           "(it leaves the record when it exits)")
        for name in dict.fromkeys(names):
            del doc["agents"][name]
        save(ctx, doc)
    removed = list(dict.fromkeys(names))
    return {"forgotten": removed}, removed, [f"Forgot {', '.join(removed)}: not resumed at "
                                             "boot, not started again."]


# --- the primary agent -------------------------------------------------------------------
def primary(doc: dict, ship: ShipToml) -> tuple[str | None, bool]:
    """The Ship's primary agent: (its name, marked). The one marked with `ship-ctl
    agent primary`, else the first [[autostart]] agent the record has that should
    run, else None. At most one is marked: the record holds one name."""
    if doc.get("primary") in doc["agents"]:
        return doc["primary"], True
    for entry in ship.autostart:
        e = doc["agents"].get(entry["name"])
        if e is not None and e["want"] == "running":
            return entry["name"], False
    return None, False


def mark_primary(ctx: Context, ship: ShipToml, name: str | None
                 ) -> tuple[dict, list[str], list[str]]:
    """Mark one recorded agent as the Ship's primary (None: clear the mark).
    Returns (data, changed, messages)."""
    with held(ctx):
        doc = load(ctx)
        if name is not None and name not in doc["agents"]:
            raise CtlError("invalid-input", f"not in the agent record: {name} (recorded: "
                           f"{', '.join(sorted(doc['agents'])) or 'none'})")
        changed = doc.get("primary") != name
        if changed:
            doc["primary"] = name
            save(ctx, doc)
    now, marked = primary(doc, ship)
    if name is not None:
        message = f"{name} is the primary agent."
    elif now:
        message = f"No agent is marked primary; the default is {now} (the first autostart agent)."
    else:
        message = "No agent is marked primary, and there is no autostart agent to default to."
    return ({"primary": now, "marked": marked}, [f"primary: {name or 'none'}"] if changed else [],
            [message])


# --- the status document ---------------------------------------------------------------
def view(ctx: Context, ship: ShipToml, live: list[dict] | None) -> tuple[dict, dict[str, dict]]:
    """The status document's `agent_record` block, and {record name: live agent}."""
    doc = load(ctx)
    found = match(doc, live or [])
    autostart = {e["name"] for e in ship.autostart}
    agents = []
    for name, e in sorted(doc["agents"].items()):
        tries = tries_this_boot(ctx, e)
        if e["want"] == "stopped":
            state = "stopped"
        elif name in found:
            state = "running"
        elif live is None:
            state = "unknown"
        elif ((e["place"] or {}).get("runtime") not in (None, ship.runtime)
              and e["place"].get("boot_id") == boot_id(ctx)):
            state = "unknown"  # it may still run under the runtime it ran in (elsewhere)
        elif tries >= MAX_TRIES:
            state = "failed"
        else:
            state = "starting"
        item = {"name": name, "cwd": e["cwd"], "mode": e["mode"], "autostart": name in autostart,
                "state": state, "relaunch_failures": tries,
                "created_at": e["created_at"], "updated_at": e["updated_at"]}
        if tries and e["relaunch"]["last_error"]:
            item["last_error"] = e["relaunch"]["last_error"]
        agents.append(item)
    resume = doc["resume"]
    first, marked = primary(doc, ship)
    switching = doc.get("switch")
    switches = doc.get("switches") or []
    last_switch = ({"at": switching["started_at"], "from": switching["from"],
                    "to": switching["to"], "outcome": "in-progress",
                    "forced": bool(switching.get("forced")), "agents": len(switching["agents"]),
                    "message": str(switching.get("step") or "starting")}
                   if switching else dict(switches[-1]) if switches else None)
    block = {"runtime": ship.runtime, "agents": agents,
             "primary": None if first is None else {"name": first, "marked": marked},
             "resume": None if resume is None else {
                 "at": resume["at"], "method": "ship-ctl", "resumed": resume["resumed"],
                 "failed": resume["failed"]}}
    if last_switch is not None:  # only on a Ship that has switched runtime
        block["last_switch"] = last_switch
    return block, found


def attention(block: dict | None) -> list[tuple[str, str, str]]:
    """(code, message, agent) for recorded agents that could not be started again."""
    out = []
    for a in (block or {}).get("agents", []):
        if a["state"] == "failed":
            why = f": {a['last_error']}" if a.get("last_error") else ""
            out.append(("agent-relaunch-failed",
                        f"Agent '{a['name']}' stopped and was not kept running after "
                        f"{a['relaunch_failures']} tries{why}. Restart it (ship-ctl agent "
                        f"restart {a['name']}, --fresh for a new conversation) or forget it "
                        f"(ship-ctl agent forget {a['name']}).", a["name"]))
    return out
