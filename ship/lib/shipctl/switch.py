"""`ship-ctl runtime switch core|herdr`: move a live Ship's agents to the other runtime.

Each running Claude agent comes back on the other runtime continuing its own
conversation (`--resume <its session>`) in its own permission mode, and the Ship
runs that runtime from then on (ship.toml `runtime`, the boot unit). If anything
goes wrong the Ship is put back on the runtime it ran, with its agents resumed
there. One agent is never started twice, and nothing ends an agent the hard way.

The steps, all under the agent record's lock (so the status refresh, which would
otherwise start a missing agent, stays out until the switch is over):

1. **Refuse** (changing nothing) unless the target runtime is installed (herdr's
   or herdr-core's binary answers; Claude Code 2.1.295 or newer for core), every
   running Claude agent is in the record with a known session and mode, the Ship
   allows that mode (bypassPermissions needs allow_bypass), none is working,
   waiting at a question or in a state the runtime cannot tell (unless forced), and
   the old runtime runs no other agent its stop would end (herdr: its server goes).
2. **Snapshot** each agent (name, folder, session, mode) into the record, so an
   interrupted switch can still tell what ran. A recorded agent that is not running
   because it was exited on purpose is let go first, as the refresh would.
3. **Stop** each agent gracefully through the old runtime (record.py's restart
   sequence: Escape if needed, Ctrl+C, `/exit`), and pin its session's mode in
   the mode ledger (which also clears the `/exit`, so it counts as an agent to
   resume, not one stopped on purpose). Then the old runtime must list no Claude
   agent at all.
4. **Lift the guard.** `record.elsewhere` keeps either runtime from starting an
   agent whose place this boot is in the other runtime: after an unplanned
   ship.toml change it may still run there. The switch has just seen the old
   runtime run none, so it clears those places. Only then does anything start.
5. **Change runtime:** herdr's own resume stays off; leaving herdr, the stopped
   agents' workspaces are closed (a laptop's herdr TUI that starts the server
   again finds nothing to restore) and `herdr.service` is stopped; ship.toml
   gets the new `runtime`; the old unit is disabled and the new one enabled and
   started. Both runtimes stay installed. Leaving herdr-core, the core keeps
   running (`hc server stop` would end every terminal) and the stopped agents'
   finished terminals are removed.
6. **Relaunch** each agent on the new runtime from the record (record.relaunch,
   the boot resume's own path), in its snapshot mode.
7. **Verify**, like `ship-fleet reboot`: each agent live by its name, exactly
   once, running its own session, in its mode (the mode ledger, which records the
   mode each session starts in); and, leaving herdr-core, none of them live there.
8. **On any failure, roll back:** agents that started on the new runtime are
   stopped gracefully (one that will not stop, or whose start there may still come
   up, is left where it is, and never started a second time), ship.toml and the
   units go back, and every other agent is relaunched and verified on the old
   runtime (and, the core left running, not on it). An agent that did not exit when
   asked and has exited since is resumed with them.

Classic herdr is deprecated (the next release removes it), so the switch is one-way:
a switch to herdr from herdr-core is refused, changing nothing. The rollback of a
failed switch to herdr-core still goes back to herdr, a Ship already on herdr can
still be put right with a switch to herdr (below), and so can one whose switch from
herdr did not finish (interrupted, or a rollback that failed before ship.toml went
back): the switch to herdr then finishes the way back.

A switch to the runtime ship.toml already names changes nothing, unless the last
switch stopped part way or did not roll back cleanly, or that runtime is not
running: then it puts the units right, starts it, and resumes the agents waiting.

Each switch is recorded in the agent record (`switches`, the last few) and
shown in the status document (`agent_record.last_switch`).
"""

from __future__ import annotations

import copy
import signal

from . import agents as agents_mod
from . import apply, record, shiptoml
from .context import Context, utc_now
from .errors import CtlError
from .runtime import RUNTIMES, Runtime, for_ship, wait_until
from .shiptoml import ShipToml
from .status import read_ledger

TARGETS = tuple(RUNTIMES)
UNSETTLED = ("working", "unknown", "blocked")  # stopping would interrupt it
SERVER_WAIT_S = 60  # for the new runtime to answer once its unit started
VERIFY_WAIT_S = 90  # for every agent to be back, in its session and mode
HISTORY = 10  # switches kept in the record
UNFINISHED_S = 3600  # a switch still in progress after this long was interrupted
LATE_EXIT_S = 20  # in a rollback, for an agent that did not exit when asked


# Runtimes a person may no longer switch a Ship onto. Classic herdr goes in the next
# release: a Ship already on it can still be put right (settle), and a failed switch
# away from it still rolls back to it (Switch.rollback never comes through switch()).
DEPRECATED = ("herdr",)


def unfinished_from(doc: dict) -> str | None:
    """The runtime a switch that did not finish was leaving (one still in the record,
    interrupted, or whose rollback failed before ship.toml went back), else None. A
    switch back to it finishes that way back, so it is not refused as deprecated."""
    if doc.get("switch"):
        return doc["switch"].get("from")
    last = (doc.get("switches") or [{}])[-1]
    return last.get("from") if last.get("outcome") in ("interrupted", "rollback-failed") \
        else None


def refused_message(target: str) -> str:
    return (f"switching to classic {target} is no longer possible: it is deprecated and the "
            "next ShipShape release removes it, so the switch to herdr-core is one-way; "
            "nothing changed")


class Failed(Exception):
    """A step of the switch went wrong: roll back."""


def _systemctl(ctx: Context, *argv: str, agent: bool = False) -> None:
    """systemctl as root (the units' --global links), or as the agent (--user)."""
    full = ["systemctl", "--user", *argv] if agent else ["systemctl", *argv]
    r = ctx.runner.run(full, timeout=90, agent=agent)
    if not r.ok:
        why = ((r.err or r.out).strip().splitlines() or [f"exit {r.rc}"])[-1]
        raise Failed(f"{' '.join(full)} failed: {why[:200]}")


def _swap_units(ctx: Context, frm: Runtime, to: Runtime) -> None:
    """The boot units say `to`, and its unit runs and answers: frm's unit is
    disabled and, unless frm stays running when left (herdr-core), stopped."""
    _systemctl(ctx, "--global", "disable", frm.unit)
    _systemctl(ctx, "--global", "enable", to.unit)
    if not frm.stays_running_when_left:
        _systemctl(ctx, "stop", frm.unit, agent=True)
    _systemctl(ctx, "start", to.unit, agent=True)
    if not wait_until(ctx, SERVER_WAIT_S, lambda: to.agents()[1] is not None):
        raise Failed(f"{to.kind} did not answer after {to.unit} started")


def _sentence(message: str) -> str:
    """message as the first line of the result: capitalised, with a full stop."""
    return message[0].upper() + message[1:] + "."


def _new_ship(ship: ShipToml, target: str) -> ShipToml:
    doc = copy.deepcopy(ship.doc)
    doc["runtime"] = target
    return ShipToml(doc)


def _the_other(ctx: Context, rt: Runtime) -> Runtime:
    """The runtime that rt is not. There are two; a third would need `settle` to
    check each of the others, so this fails loudly until it does."""
    (kind,) = [k for k in TARGETS if k != rt.kind]
    return RUNTIMES[kind](ctx)


def _history(doc: dict, entry: dict) -> None:
    doc["switches"] = [*doc.get("switches", []), entry][-HISTORY:]


def _preflight(rt: Runtime) -> None:
    """The target runtime answers, and (core) Claude is new enough for it."""
    if rt.version(15) is None:
        raise CtlError("runtime-missing", f"{rt.kind} is not installed on this Ship "
                       f"(/usr/local/bin/{rt.binary} "
                       "does not answer); update the Ship's tooling first; nothing changed")
    why = rt.claude_floor(strict=True)
    if why:
        raise CtlError("runtime-missing", f"{why}; nothing changed")


class Switch:
    def __init__(self, ctx: Context, ship: ShipToml, target: str, force: bool):
        self.ctx, self.ship, self.target, self.force = ctx, ship, target, force
        self.source = ship.runtime
        self.old: Runtime = for_ship(ctx, ship)
        self.new_ship = _new_ship(ship, target)
        self.new: Runtime = RUNTIMES[target](ctx)
        self.doc: dict = {}
        self.snap: dict[str, dict] = {}  # name -> {cwd, session, mode, was}
        self.notes: list[str] = []
        self.stopped: list[str] = []  # stopped on the old runtime
        self.tried: list[str] = []  # a launch on the new runtime was attempted
        self.unstopped: str | None = None  # asked to exit on the old runtime, did not
        self.toml_changed = False
        self.units_changed = False
        self.launched_at = ""  # when the last round of launches began

    # --- the record -----------------------------------------------------------------
    def save(self, step: str | None = None) -> None:
        if step is not None and self.doc.get("switch"):
            self.doc["switch"]["step"] = step
        record.save(self.ctx, self.doc)

    def pin(self, name: str) -> None:
        """The session's mode, in the mode ledger, is the snapshot's; this also clears
        the `/exit` just typed, so the agent counts as one to resume."""
        s = self.snap[name]
        if not agents_mod.record_mode(self.ctx, s["session"], s["mode"]):
            self.notes.append(f"{name}: its mode could not be pinned in the mode ledger")

    # --- 1. refuse --------------------------------------------------------------------
    def check(self) -> dict[str, dict]:
        ctx, ship = self.ctx, self.ship
        server, live, errors = self.old.agents()
        if server != "running" or live is None:
            raise CtlError("herdr-unavailable", f"{self.source} is not answering "
                           f"({'; '.join(errors) or server}), so what runs is not known; "
                           f"nothing changed (ship-ctl runtime switch {self.source} starts "
                           "it again)")
        doc = self.doc
        self.check_elsewhere()
        if record.sync(ctx, self.old, doc, live, read_ledger(ctx)):
            record.save(ctx, doc)
        found = record.match(doc, live)
        claimed = {id(a) for a in found.values()}
        stray = [a for a in live if a["kind"] == "claude" and id(a) not in claimed]
        if stray:
            where = ", ".join(a["name"] or a["place"] or "?" for a in stray)
            raise CtlError("agent-failed", f"Claude in {where} is not in the agent record (it "
                           "has not reported its session yet), so it could not be resumed; "
                           "wait a moment, or exit it, then switch")
        others = [a for a in live if a["kind"] != "claude"]
        if others and not self.old.stays_running_when_left:
            where = ", ".join(f"{a['name'] or a['place']} ({a['kind']})" for a in others)
            raise CtlError("invalid-input", f"{self.source} also runs {where}; stopping "
                           f"{self.source} would end it, so exit it first")
        ledger = read_ledger(ctx)
        problems, busy = [], []
        for name, a in sorted(found.items()):
            e = doc["agents"][name]
            session = a["session"] or e["session"]
            mode = ledger.get(session or "") or e["mode"]
            if not session:
                problems.append(f"{name}: its session is not known, so its conversation "
                                "could not be continued")
                continue
            if mode is None:
                problems.append(f"{name}: its permission mode is not known (send it a "
                                "message, or restart it with --mode, first)")
                continue
            if mode == "bypassPermissions" and not ship.claude["allow_bypass"]:
                problems.append(f"{name} runs in bypassPermissions, which this Ship no longer "
                                "allows, so it would come back in another mode; restart it "
                                "in another mode first")
                continue
            if not record.folder_exists(ctx, e["cwd"]):
                problems.append(f"{name}: its folder {e['cwd']} is not there")
                continue
            if a["state"] in UNSETTLED:
                busy.append(f"{name} ({a['state']})")
            self.snap[name] = {"cwd": e["cwd"], "session": session, "mode": mode,
                               "was": a["state"]}
        if problems:
            raise CtlError("agent-failed", "; ".join(problems) + "; nothing changed")
        if busy and not self.force:
            raise CtlError("agent-busy", f"not every agent is idle: {', '.join(busy)}; try "
                           "again when they are, or force it (which interrupts them); "
                           "nothing changed")
        return found

    # --- 3. stop ----------------------------------------------------------------------
    def let_go_exited(self) -> None:
        """A recorded agent that is not running now, ran on the old runtime this boot
        and was exited on purpose (or its pane closed) is let go, as the next refresh
        would: lifting its place would otherwise make it one to resume on the new
        runtime. One that crashed stays, and the new runtime resumes it."""
        ctx, doc = self.ctx, self.doc
        boot = record.boot_id(ctx)
        gone = [n for n, e in doc["agents"].items() if n not in self.snap
                and e["want"] == "running" and record.own_place(self.old, e)
                and e["place"].get("boot_id") == boot]
        if not gone:
            return
        sessions, places = record.ledger_sessions(ctx), self.old.places()
        since, autostart = record.boot_started(ctx), {a["name"] for a in self.ship.autostart}
        for name in gone:
            why = record.let_go_exited(doc, name, sessions, places, since, autostart)
            if why:
                self.notes.append(f"{name} stopped ({why}); not moved")
        self.save()

    def stop_all(self, found: dict[str, dict]) -> None:
        for name in self.snap:
            a = found[name]
            ok, notes = self.old.stop(a["place"], a["state"])
            self.notes += [f"{name}: {n}" for n in notes]
            if not ok:
                self.unstopped = name
                raise Failed(f"{name} did not exit when asked; it is still running on "
                             f"{self.source}")
            self.stopped.append(name)
            self.pin(name)
            self.save("stopping")
        server, live, errors = self.old.agents()
        if live is None:
            raise Failed(f"{self.source} could not be listed after the agents stopped "
                         f"({'; '.join(errors) or server})")
        left = [a["name"] or a["place"] for a in live if a["kind"] == "claude"]
        if left:
            raise Failed(f"{self.source} still runs Claude in {', '.join(map(str, left))}")

    def lift(self, rt: Runtime, keep: set[str] = frozenset()) -> list[tuple[str, str | None]]:
        """Clear every recorded place in rt's runtime (but keep's): rt runs no Claude
        now, so none of them may hold one back. Returns the places cleared."""
        cleared = []
        for name, e in self.doc["agents"].items():
            place = e["place"] or {}
            if name not in keep and place.get("runtime") == rt.kind:
                cleared.append((place["id"], place.get("workspace")))
                e["place"] = None
                e["updated_at"] = utc_now()
        self.save()
        return cleared

    # --- 5. units -----------------------------------------------------------------------
    def set_runtime(self, frm: Runtime, to: Runtime, ship: ShipToml) -> None:
        """ship.toml and the boot unit say `to`, and its unit runs."""
        ctx = self.ctx
        apply.herdr_resume_off(ctx)
        self.toml_changed = True
        shiptoml.save(ship.doc, ctx.paths.ship_toml)
        self.doc["runtime"] = to.kind
        self.save()
        self.units_changed = True
        _swap_units(ctx, frm, to)

    # --- 6. relaunch --------------------------------------------------------------------
    def running_on(self, rt: Runtime) -> set[str] | None:
        """The snapshot agents rt runs Claude for now (by name or session), or None
        when rt cannot be listed."""
        _, live, _ = rt.agents()
        if live is None:
            return None
        sessions = {s["session"]: n for n, s in self.snap.items()}
        return {a["name"] if a["name"] in self.snap else sessions.get(a["session"] or "")
                for a in live if a["kind"] == "claude"} - {None}

    def launch_all(self, rt: Runtime, ship: ShipToml, names: list[str]) -> None:
        self.launched_at = utc_now()
        running = self.running_on(rt)
        if running is None:
            raise Failed(f"{rt.kind} could not be listed, so nothing was started there")
        places = rt.places()
        for name in names:
            s = self.snap[name]
            if name in running:
                # It runs there already (never started twice): the check below sees
                # whether it is its own session, in its mode.
                self.notes.append(f"{name} was already running on {rt.kind}; not started again")
                continue
            self.tried.append(name)
            ok, why = record.relaunch(self.ctx, ship, rt, self.doc, name, places,
                                      same_place=False, mode=s["mode"], keep_tries=False)
            self.save()
            if not ok:
                raise Failed(f"{name} did not start on {rt.kind}: {why}")

    # --- 7. verify ------------------------------------------------------------------------
    def problems(self, rt: Runtime, other: Runtime | None, names: list[str]) -> list[str]:
        _, live, errors = rt.agents()
        if live is None:
            return [f"{rt.kind} could not be listed ({'; '.join(errors)})"]
        ledger = read_ledger(self.ctx)
        starts = self.ledger_starts()
        out = []
        for name in names:
            s = self.snap[name]
            mine = [a for a in live if a["kind"] == "claude"
                    and (a["name"] == name or a["session"] == s["session"])]
            if not mine:
                out.append(f"{name} is not running on {rt.kind}")
            elif len(mine) > 1:
                out.append(f"{name} runs {len(mine)} times on {rt.kind}")
            elif mine[0]["session"] != s["session"]:
                out.append(f"{name} is not running its own conversation on {rt.kind}")
            elif starts.get(s["session"], "") < self.launched_at:
                # The mode-ledger hook records the mode a session starts in, from its
                # launch flags: until it has seen this start, its mode is not known.
                out.append(f"{name}: its start on {rt.kind} was not seen by the mode ledger")
            elif ledger.get(s["session"]) != s["mode"]:
                out.append(f"{name} came back in {ledger.get(s['session'])}, was {s['mode']}")
        if other is not None:  # the one left behind still runs (herdr-core): none there
            _, left, _ = other.agents()
            twice = sorted(a["name"] or a["place"] for a in left or [] if a["kind"] == "claude"
                           and (a["name"] in names or a["session"] in
                                {self.snap[n]["session"] for n in names}))
            out += [f"{n} also runs on {other.kind}" for n in twice]
        return out

    def verify(self, rt: Runtime, other: Runtime | None, names: list[str]) -> list[str]:
        found: list[str] = []

        def ok() -> bool:
            found[:] = self.problems(rt, other, names)
            return not found
        wait_until(self.ctx, VERIFY_WAIT_S, ok)
        return found

    def ledger_starts(self) -> dict[str, str]:
        """{session: when the mode-ledger hook last saw it start}."""
        doc = self.ctx.read_home_json(self.ctx.paths.mode_ledger, {})
        sessions = doc.get("sessions") if isinstance(doc, dict) else None
        return {sid: e["started_at"] for sid, e in (sessions or {}).items()
                if isinstance(e, dict) and isinstance(e.get("started_at"), str)}

    # --- 8. roll back -------------------------------------------------------------------
    def _off_the_new(self) -> set[str]:
        """Stop, gracefully, every snapshot agent the new runtime runs; the names that
        could not be stopped, or not seen to be gone (they stay where they are)."""
        server, live, errors = self.new.agents()
        if live is None:
            # Whether they run there cannot be told: starting them on the old runtime
            # could start them twice, so they are left for a person to look at.
            if self.tried:
                self.notes.append(f"{self.target} could not be listed ("
                                  f"{'; '.join(errors) or server}), so "
                                  f"{', '.join(self.tried)} were not started on {self.source}")
            return set(self.tried)
        stuck: set[str] = set()
        sessions = {s["session"]: n for n, s in self.snap.items()}
        for name in sorted(self.new.pending(self.tried)):
            # A start typed into its terminal that has not come up yet: it may still
            # come up, so it is not started on the old runtime as well.
            stuck.add(name)
            self.notes.append(f"{name}'s start on {self.target} had been typed and had not "
                              f"come up, so it was not started on {self.source}")
        for a in live:
            if a["kind"] != "claude":
                continue
            name = a["name"] if a["name"] in self.snap else sessions.get(a["session"] or "")
            if name is None:
                continue
            ok, _ = self.new.stop(a["place"], a["state"])
            if ok:
                self.pin(name)
            else:
                stuck.add(name)
                self.notes.append(f"{name} did not exit on {self.target}; it is left running "
                                  f"there, and was not started on {self.source}")
        still = self.running_on(self.new)
        if still is None or still - stuck:
            missing = set(self.tried) if still is None else still - stuck
            self.notes.append(f"{', '.join(sorted(missing))} could not be seen to stop on "
                              f"{self.target}, so they were not started on {self.source}")
            stuck |= missing
        return stuck

    def rollback(self) -> tuple[str, list[str]]:
        """Back to the old runtime; (outcome, problems left)."""
        stuck: set[str] = set()
        try:
            if self.tried or self.units_changed:
                stuck = self._off_the_new()
                self.lift(self.new, keep=stuck)
            if self.units_changed or self.toml_changed:
                self.set_runtime(self.new, self.old, self.ship)
        except Exception as exc:  # noqa: BLE001 - whatever it was, say so and stop here
            self.notes.append(f"putting {self.source} back failed: {exc}")
            return "rollback-failed", [f"{self.source} was not put back: {exc}"]
        # A runtime that keeps running when the rollback leaves it (herdr-core) may
        # run none of them (a launch that came up late, say), or it would run twice.
        other = self.new if self.new.stays_running_when_left else None
        names = [n for n in self.stopped if n not in stuck]
        left = self.resume_old(names, other)
        late = self.late_exit()
        if late:
            left += self.resume_old(late, other)
        left += [f"{n} is still on {self.target}" for n in sorted(stuck)]
        return ("rolled-back" if not left else "rollback-failed"), left

    def resume_old(self, names: list[str], other: Runtime | None) -> list[str]:
        try:
            self.launch_all(self.old, self.ship, names)
        except Exception as exc:  # noqa: BLE001
            self.notes.append(str(exc))
        return self.verify(self.old, other, names)

    def late_exit(self) -> list[str]:
        """The agent that did not exit when asked, if it has exited since (it was sent
        `/exit`): that exit was the switch's, not a person's, so it is pinned (which
        clears the exit) and resumed like the rest; else the next refresh would let it
        go as one exited on purpose."""
        name = self.unstopped
        if name is None:
            return []
        running: list[set[str] | None] = [None]

        def gone() -> bool:
            running[0] = self.running_on(self.old)
            return running[0] is not None and name not in running[0]
        if not wait_until(self.ctx, LATE_EXIT_S, gone):
            if running[0] is not None:
                self.notes.append(f"{name} did not exit when asked, and runs on {self.source} "
                                  "as before, in its own session and mode")
            return []
        self.unstopped = None
        self.stopped.append(name)
        self.pin(name)
        self.save()
        self.notes.append(f"{name} exited after all; it is resumed on {self.source}")
        return [name]

    # --- the whole switch -----------------------------------------------------------------
    def forward(self, found: dict[str, dict]) -> None:
        self.let_go_exited()
        self.stop_all(found)
        cleared = self.lift(self.old)
        self.save("switching")
        if self.old.release_before_unit_stop:
            self.notes += self.old.release(cleared)
        self.set_runtime(self.old, self.new, self.new_ship)
        if not self.old.release_before_unit_stop:
            self.notes += self.old.release(cleared)
        self.save("starting")
        self.launch_all(self.new, self.new_ship, list(self.snap))
        self.save("verifying")
        left = self.verify(self.new, self.old if self.old.stays_running_when_left else None,
                           list(self.snap))
        if left:
            raise Failed("; ".join(left))

    def run(self) -> tuple[dict, list[str], list[str]]:
        ctx = self.ctx
        with record.held(ctx):
            self.doc = doc = record.load(ctx)
            if doc.get("switch"):  # one that died part way (its lock went with it)
                old = doc.pop("switch")
                _history(doc, {"at": old.get("started_at") or utc_now(), "from": old["from"],
                               "to": old["to"], "outcome": "interrupted",
                               "forced": bool(old.get("forced")), "agents": len(old["agents"]),
                               "message": f"interrupted while {old.get('step') or 'starting'}"})
                self.save()
            if self.source == self.target:
                self.check_elsewhere()
                return self.settle()
            _preflight(self.new)
            found = self.check()
            started = utc_now()
            doc["switch"] = {"from": self.source, "to": self.target, "started_at": started,
                             "forced": self.force, "step": "stopping",
                             "agents": {n: {"cwd": s["cwd"], "session": s["session"],
                                            "mode": s["mode"]} for n, s in self.snap.items()}}
            self.save()
            outcome, left, why = "switched", [], ""
            try:
                self.forward(found)
            except Exception as exc:  # noqa: BLE001 - any failure part way rolls back
                why = str(exc) or type(exc).__name__
                ctx.log(f"runtime switch to {self.target} failed: {why}; rolling back")
                self.save("rolling back")
                outcome, left = self.rollback()
            rt = self.new if outcome == "switched" else self.old
            _, live, _ = rt.agents()
            if live is not None:
                record.sync(ctx, rt, doc, live, read_ledger(ctx))
            doc.pop("switch", None)
            message = {"switched": f"moved {len(self.snap)} agent(s) from {self.source} to "
                                   f"{self.target}",
                       "rolled-back": f"the switch to {self.target} failed and was rolled back "
                                      f"to {self.source}: {why}",
                       "rollback-failed": f"the switch to {self.target} failed ({why}), and "
                                          f"rolling back to {self.source} did not finish "
                                          f"cleanly: {'; '.join(left)}"}[outcome]
            _history(doc, {"at": started, "from": self.source, "to": self.target,
                           "outcome": outcome, "forced": self.force, "agents": len(self.snap),
                           "message": message[:300]})
            self.save()
        data = self.result(outcome, left)
        messages = [_sentence(message), *self.notes]
        if outcome == "switched":
            messages.append(f"Each agent continues its conversation on {self.target}, in its "
                            "own mode. On each laptop that attaches to this Ship, install the "
                            "trust bundle again so its herdr machine entry is replaced.")
            return data, [f"runtime: {self.target}", *self.snap], messages
        raise CtlError("switch-failed", message[:1000]).with_data(data, [], messages)

    def settle(self) -> tuple[dict, list[str], list[str]]:
        """Already on the target. After a switch that stopped part way, or whose
        rollback did not finish, the runtime ship.toml names may not be running or the
        boot units may disagree: this puts the units right, starts the runtime and
        resumes the agents waiting for it, so running the switch again finishes it."""
        ctx, rt, doc = self.ctx, self.old, self.doc
        last = (doc.get("switches") or [{}])[-1]
        unsure = last.get("outcome") in ("interrupted", "rollback-failed")
        if not unsure and rt.agents()[1] is not None:
            return self.result("already", []), [], [f"This Ship already runs {self.target}."]
        other = _the_other(ctx, rt)
        _, there, _ = other.agents()
        sessions = {e["session"] for e in doc["agents"].values() if e["session"]}
        # Stopping a runtime that does not stay running (herdr) ends whatever it runs;
        # a recorded agent on one that does (the core) would run twice.
        odd = [a for a in there or [] if not other.stays_running_when_left or (
            a["kind"] == "claude" and (a["name"] in doc["agents"] or a["session"] in sessions))]
        if odd:
            where = ", ".join(f"{a['name'] or a['place']} ({a['kind']})" for a in odd)
            raise CtlError("invalid-input", f"{other.kind} still runs {where}; exit "
                           f"{'it' if len(odd) == 1 else 'them'} there (or reboot the Ship), "
                           "then run this again; nothing changed")
        started = utc_now()
        try:
            apply.herdr_resume_off(ctx)
            _swap_units(ctx, other, rt)
            _, live, _ = rt.agents()
            _, messages, _ = record.upkeep(ctx, self.ship, rt, doc, live or [], "ready")
            outcome = "repaired"
            message = f"{rt.kind} runs again, and its boot unit is on"
        except Failed as exc:
            outcome, messages = "rollback-failed", []
            message = f"starting {rt.kind} again failed: {exc}"
        _history(doc, {"at": started, "from": rt.kind, "to": rt.kind, "outcome": outcome,
                       "forced": self.force, "agents": 0, "message": message[:300]})
        self.save()
        messages = [_sentence(message), *messages]
        if outcome == "repaired":
            return self.result(outcome, []), [f"runtime: {rt.kind}"], messages
        raise CtlError("switch-failed", message[:1000]).with_data(
            self.result(outcome, [message]), [], messages)

    def check_elsewhere(self) -> None:
        odd = sorted(n for n, e in self.doc["agents"].items()
                     if record.elsewhere(self.old, self.ctx, e))
        if not odd:
            return
        other = self.doc["agents"][odd[0]]["place"]["runtime"]
        head = (f"ship.toml says {self.source}, but {', '.join(odd)} ran under {other} this "
                "boot and may still run there; ")
        last = (self.doc.get("switches") or [{}])[-1]
        if last.get("outcome") == "rollback-failed":
            # The rest of the agents run on ship.toml's runtime: only a boot, which
            # starts that runtime alone, brings these back there without a double.
            raise CtlError("invalid-input", head + f"reboot the Ship (the boot starts only "
                           f"{self.source} and resumes every agent on it), then switch")
        raise CtlError("invalid-input", head + f"put ship.toml's runtime back to {other} "
                       "(or reboot) before switching")

    def result(self, outcome: str, left: list[str]) -> dict:
        ledger = read_ledger(self.ctx)
        return {"from": self.source, "to": self.target, "outcome": outcome,
                "forced": self.force,
                "runtime": self.target if outcome == "switched" else self.source,
                "remote_control": self.ship.claude["remote_control"],
                "agents": [{"name": n, "mode": s["mode"], "was": s["was"],
                            "mode_now": ledger.get(s["session"])}
                           for n, s in sorted(self.snap.items())],
                "problems": left}


def switch(ctx: Context, ship: ShipToml, target: str, *, force: bool = False
           ) -> tuple[dict, list[str], list[str]]:
    """(data, changed, messages); raises CtlError when it refused (nothing changed) or
    rolled back (switch-failed, with what happened attached)."""
    if target not in TARGETS:
        raise CtlError("usage", f"switch to one of: {', '.join(TARGETS)}")
    if (target in DEPRECATED and ship.runtime != target
            and unfinished_from(record.load(ctx)) != target):
        raise CtlError("invalid-input", refused_message(target))
    # The switch must finish even if the SSH session that started it drops: a half
    # switch is what the rollback exists to avoid.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    return Switch(ctx, ship, target, force).run()


# --- the status document ---------------------------------------------------------------
def attention(block: dict | None) -> list[tuple[str, str, str]]:
    """(code, message, subject) when the last switch needs a person."""
    sw = (block or {}).get("last_switch")
    if not sw:
        return []
    if sw["outcome"] == "rollback-failed":
        what = (f"The switch from {sw['from']} to {sw['to']} failed and was not rolled back "
                "cleanly" if sw["from"] != sw["to"] else f"{sw['to']} could not be put right")
        return [("runtime-switch-failed", f"{what}: {sw['message']}", "runtime")]
    age = record.age_s(sw["at"])
    if sw["outcome"] == "in-progress" and age is not None and age > UNFINISHED_S:
        return [("runtime-switch-unfinished", f"A switch from {sw['from']} to {sw['to']} "
                 f"stopped part way ({sw['message']}). Run ship-ctl runtime switch "
                 f"{block['runtime']} (the runtime ship.toml names) to put it right.",
                 "runtime")]
    return []


