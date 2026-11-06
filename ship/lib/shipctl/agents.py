"""`ship-ctl agent restart`: swap a running Claude agent for one in a new mode.

A Claude session keeps its permission mode across a resume, so changing
`default_mode` (or an [[autostart]] mode) reaches only agents started afterwards.
This restarts an agent in place: the same herdr pane (so the same folder and
workspace) and the same herdr name, its conversation continued with
`claude --resume <session>` unless asked for a fresh one, in the mode chosen, else
the mode it would start in now (its [[autostart]] mode, else `default_mode`).

Gracefully, one agent at a time:

1. An agent that is working (or whose state herdr cannot tell) is left alone
   unless forced; forced, its turn is interrupted (Escape) and the result says so.
   An agent waiting at a question has it dismissed (Escape), and the result says so.
2. `/exit` is typed into it (after Ctrl+C, which clears a half-typed line), and
   ship-ctl waits until herdr no longer sees an agent in the pane; if it is still
   there, Ctrl+C twice, then it gives up and leaves the agent running.
3. `herdr agent start` launches claude again in that pane with the mode flag.
   The resume shim passes an explicit mode flag through untouched.
4. The mode ledger records the session in the new mode, so the status document
   reports it and the next reboot or update resumes it in that mode (the shim
   puts the ledger's mode back on herdr's bare `claude --resume`).

Remote Control comes back by itself: it follows the managed settings, and a
resumed session reconnects to the Remote Control session saved in it. Session
names survive a resume; a fresh conversation gets the name autostart gave it.
"""

from __future__ import annotations

import json
import os
import sys
import time

from . import PERMISSION_MODES, naming
from .autostart import DEFAULT_PROMPT, START_TIMEOUT_MS, mode_args
from .context import Context, utc_now
from .errors import CtlError
from .fsutil import read_json, write_atomic
from .paths import Paths
from .shiptoml import ShipToml
from .status import herdr_agents, read_ledger

LEDGER_HOOK = "files/hooks/mode_ledger.py"  # in the release; installed as Paths.LEDGER_HOOK
STOP_TIMEOUT_S = 30  # for /exit to end the agent
SETTLE_TIMEOUT_S = 15  # for Escape to end a turn or a question
POLL_S = 1.0
SETTLED = ("idle", "done")  # at its prompt: /exit can be typed
BUSY_STATES = ("working", "unknown")


def _wait_budget(ctx: Context, seconds: float) -> float:
    """Tests (sandbox only) shorten the waits; a real Ship always uses them in full."""
    if ctx.paths.sandbox and os.environ.get("SHIPCTL_TEST_WAIT_S"):
        return float(os.environ["SHIPCTL_TEST_WAIT_S"])
    return seconds


def _herdr(ctx: Context, argv: list[str], timeout: float = 15) -> tuple[bool, str]:
    """(ok, error code or message) for a herdr command run as the agent."""
    r = ctx.runner.run(["herdr", *argv], timeout=timeout, agent=True)
    if r.ok:
        return True, ""
    try:
        err = json.loads(r.err).get("error", {})
        why = err.get("code") or err.get("message") or f"exit {r.rc}"
    except (ValueError, AttributeError):
        why = (r.err or "").strip().splitlines()[-1:] or [f"exit {r.rc}"]
        why = why[0] if isinstance(why, list) else why
    return False, str(why)[:200]


def _session(raw: dict) -> str | None:
    s = raw.get("agent_session")
    v = s.get("value") if isinstance(s, dict) else None
    return v if isinstance(v, str) and v else None


def _in_pane(ctx: Context, pane: str) -> dict | None:
    """The Claude agent herdr sees in this pane now, or None."""
    _, live, _ = herdr_agents(ctx)
    for a in live or []:
        if a.get("pane_id") == pane and a.get("agent") == "claude":
            return a
    return None


def _wait_until(ctx: Context, seconds: float, done) -> bool:
    deadline = time.monotonic() + _wait_budget(ctx, seconds)
    while True:
        if done():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)


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


def _stop(ctx: Context, pane: str, state: str) -> tuple[bool, list[str]]:
    """End the agent in `pane`; (stopped, notes)."""
    notes: list[str] = []
    settled = lambda: (_in_pane(ctx, pane) or {}).get("agent_status") in SETTLED  # noqa: E731
    if state in ("working", "unknown", "blocked"):
        _herdr(ctx, ["agent", "send-keys", pane, "esc"])
        notes.append({"blocked": "its open question was dismissed",
                      "working": "its turn was interrupted"}.get(
                          state, "it was interrupted (herdr could not tell its state)"))
        # herdr refuses a prompt to an agent it still sees at a question, so /exit
        # waits until Escape has visibly taken effect.
        _wait_until(ctx, SETTLE_TIMEOUT_S, settled)
    _herdr(ctx, ["agent", "send-keys", pane, "ctrl+c"])  # clears a half-typed line
    sent, why = _herdr(ctx, ["agent", "prompt", pane, "/exit"], timeout=30)
    if not sent and why == "agent_blocked":  # Escape had not shown yet: once more
        _wait_until(ctx, SETTLE_TIMEOUT_S, settled)
        sent, why = _herdr(ctx, ["agent", "prompt", pane, "/exit"], timeout=30)
    if not sent:
        ctx.log(f"/exit not typed into {pane}: {why}")
    if sent and _wait_until(ctx, STOP_TIMEOUT_S, lambda: _in_pane(ctx, pane) is None):
        return True, notes
    if _in_pane(ctx, pane) is None:
        return True, notes
    _herdr(ctx, ["pane", "send-keys", pane, "ctrl+c", "ctrl+c"])
    if _wait_until(ctx, SETTLE_TIMEOUT_S, lambda: _in_pane(ctx, pane) is None):
        return True, notes
    if not sent:
        notes.append(f"/exit was refused ({why})")
    return False, notes


def _targets(live: list[dict], names: list[str], everyone: bool) -> tuple[list[dict], list[dict]]:
    """(agents to restart, skipped) from herdr's list."""
    claude = [a for a in live if a.get("agent") == "claude"]
    if everyone:
        named = [a for a in claude if isinstance(a.get("name"), str) and a["name"]]
        skipped = [{"name": a.get("pane_id") or "?", "reason": "it has no herdr name"}
                   for a in claude if a not in named]
        return named, skipped
    by_name = {a.get("name"): a for a in claude if a.get("name")}
    missing = [n for n in names if n not in by_name]
    if missing:
        others = sorted(by_name) or ["none"]
        raise CtlError("invalid-input",
                       f"no running Claude agent named {', '.join(missing)} (running: "
                       f"{', '.join(others)})")
    return [by_name[n] for n in dict.fromkeys(names)], []


def restart(ctx: Context, ship: ShipToml, names: list[str], *, everyone: bool = False,
            mode: str | None = None, fresh: bool = False,
            force: bool = False) -> tuple[dict, list[str], list[str]]:
    """Returns (data, changed, messages); raises CtlError if any agent was not restarted
    (with what did happen attached)."""
    if bool(names) == everyone:
        raise CtlError("usage", "name the agents to restart, or give --all")
    if mode is not None:
        _check_mode(ship, mode)
    server, live, _ = herdr_agents(ctx)
    if server != "running" or live is None:
        raise CtlError("herdr-unavailable", "the herdr server is not answering")
    targets, skipped = _targets(live, names, everyone)
    if not targets and not skipped:
        return {"restarted": [], "skipped": []}, [], ["No Claude agent is running."]
    # A mode not given is the one it would start in, which ship.toml's schema only
    # allows to be bypassPermissions where allow_bypass is true: nothing more to check.

    path = ctx.paths.state / "autostart.json"
    record = read_json(path, {})
    record = record if isinstance(record, dict) else {}
    autostart = {e["name"]: e for e in ship.autostart}
    restarted: list[dict] = []
    messages: list[str] = []
    busy = failed = 0
    named = None  # whether claude takes --name; asked once, only for a fresh start
    ledger = read_ledger(ctx)

    for a in targets:
        name, pane = a["name"], a.get("pane_id")
        state = a.get("agent_status") or "unknown"
        new_mode = wanted_mode(ship, name, mode)
        session = _session(a)
        if state in BUSY_STATES and not force:
            busy += 1
            why = "working" if state == "working" else "in a state herdr cannot tell"
            skipped.append({"name": name, "reason": f"it is {why}; not interrupted"})
            messages.append(f"{name} is {why}: left alone. Try again when it is idle, or "
                            "force it (which interrupts its turn).")
            continue
        if not pane:
            failed += 1
            skipped.append({"name": name, "reason": "herdr reports no pane for it"})
            continue
        if not fresh and session is None:
            failed += 1
            skipped.append({"name": name, "reason": "its session is not known, so its "
                            "conversation cannot be continued; restart it fresh instead"})
            continue

        stopped, notes = _stop(ctx, pane, state)
        if not stopped:
            failed += 1
            skipped.append({"name": name, "reason": "it did not exit; left running"})
            messages.append(f"{name} did not exit when asked; it is still running as before"
                            + (f" ({'; '.join(notes)})" if notes else "") + ".")
            continue
        if not fresh:  # stopped: from now on the session is in its new mode
            record_mode(ctx, session, new_mode)

        args = mode_args(new_mode)
        if fresh:
            if named is None:
                named = naming.name_flag_supported(ctx)
            label = (record.get(name) or {}).get("session_name") or naming.session_name(ship,
                                                                                        name)
            args += naming.launch_args(label, named)
        else:
            args += ["--resume", session]
        argv = ["agent", "start", name, "--kind", "claude", "--pane", pane,
                "--timeout", str(START_TIMEOUT_MS), "--", *args]
        ok, why = _herdr(ctx, argv, START_TIMEOUT_MS / 1000 + 15)
        if not ok and _in_pane(ctx, pane) is None:  # the shell may not have been back yet
            time.sleep(min(3.0, _wait_budget(ctx, 3.0)))
            ok, why = _herdr(ctx, argv, START_TIMEOUT_MS / 1000 + 15)
        back = _in_pane(ctx, pane)
        if not ok and back is None:
            failed += 1
            skipped.append({"name": name, "reason": f"stopped, but did not start again: {why}"})
            launch = " ".join(["claude", *mode_args(new_mode),
                               *([] if fresh else ["--resume", session])])
            messages.append(f"{name} stopped but did not start again ({why}). Start it in its "
                            f"pane with: {launch}")
            continue
        new_session = _session(back or {}) or (None if fresh else session)
        if new_session and (fresh or new_session != session):
            record_mode(ctx, new_session, new_mode)
        if name in autostart:
            rec = record.setdefault(name, {})
            rec.update({"pane": pane, "mode": new_mode, "restarted_at": utc_now()})
            if new_session:
                rec["session"] = new_session
            if fresh:
                rec["session_name"] = label
        if fresh and name in autostart:
            prompt = autostart[name].get("prompt") or DEFAULT_PROMPT
            sent, why = _herdr(ctx, ["agent", "prompt", name, prompt], 30)
            if not sent:
                messages.append(f"{name}: started, but the first prompt was not sent ({why}).")
        restarted.append({"name": name, "mode": new_mode,
                          "previous_mode": ledger.get(session or ""),
                          "conversation": "fresh" if fresh else "continued", "was": state})
        said = f"{name} restarted in {new_mode}, " + (
            "in a fresh conversation" if fresh else "continuing its conversation")
        messages.append(said + (f" ({'; '.join(notes)})" if notes else "") + ".")

    if any(a["name"] in autostart for a in targets):
        write_atomic(path, json.dumps(record, indent=2) + "\n", 0o644)
    data = {"restarted": restarted, "skipped": skipped}
    changed = [r["name"] for r in restarted]
    if busy and not failed:
        raise CtlError("agent-busy", f"{busy} agent(s) are busy and were not restarted "
                       "(force to interrupt them)").with_data(data, changed, messages)
    if failed or busy:
        raise CtlError("agent-failed", f"{failed + busy} agent(s) could not be "
                       "restarted").with_data(data, changed, messages)
    return data, changed, messages
