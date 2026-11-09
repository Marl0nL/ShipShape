"""The agent runtime: the one place ship-ctl talks to what runs the agents.

Today that is herdr's server (`herdr agent ...`, `herdr pane ...`). Everything
else (the agent record, autostart, restart, notices, the status document) reaches
it only through `for_ship(ctx)`, so moving a Ship to another runtime, such as
herdr-core, means a second class here with the same methods, not changes there.

An agent, as every runtime reports it:

    name       the runtime's name for it, or None
    kind       "claude", "codex", ...
    state      idle | working | blocked | done | unknown
    session    the Claude session id it runs, or None
    place      where it runs (herdr: its pane id); opaque outside this module
    workspace  the place's group (herdr: workspace id), or None
    cwd        its folder, when the runtime knows it

A place is also what the record keeps to put an agent back where it was.
"""

from __future__ import annotations

import json
import os
import time

from .context import Context
from .runner import NOT_FOUND_RC, TIMEOUT_RC

AGENT_STATES = ("idle", "working", "blocked", "done", "unknown")
START_TIMEOUT_MS = 120000
STOP_TIMEOUT_S = 30  # for /exit to end the agent
SETTLE_TIMEOUT_S = 15  # for Escape to end a turn or a question
POLL_S = 1.0
SETTLED = ("idle", "done")  # at its prompt: /exit can be typed


def wait_budget(ctx: Context, seconds: float) -> float:
    """Tests (sandbox only) shorten the waits; a real Ship always uses them in full."""
    if ctx.paths.sandbox and os.environ.get("SHIPCTL_TEST_WAIT_S"):
        return float(os.environ["SHIPCTL_TEST_WAIT_S"])
    return seconds


def wait_until(ctx: Context, seconds: float, done) -> bool:
    deadline = time.monotonic() + wait_budget(ctx, seconds)
    while True:
        if done():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)


def mode_args(mode: str | None) -> list[str]:
    """claude's flags for a permission mode (none for an unknown one)."""
    if mode == "bypassPermissions":
        return ["--dangerously-skip-permissions"]
    return ["--permission-mode", mode] if mode else []


class Herdr:
    """herdr 0.9 server: agents are panes herdr recognises; places are pane ids.

    Restoring after a restart, herdr brings every pane back under its old id, as a
    fresh shell in its saved folder (it resumes no agent: ship-ctl apply turns
    `session.resume_agents_on_restore` off, so the record alone decides).
    """

    kind = "herdr"

    def __init__(self, ctx: Context):
        self.ctx = ctx

    # --- calls ------------------------------------------------------------------
    def _run(self, argv: list[str], timeout: float):
        return self.ctx.runner.run(["herdr", *argv], timeout=timeout, agent=True)

    def _call(self, argv: list[str], timeout: float = 15) -> tuple[dict | None, str]:
        """(the JSON answer, or None on failure; herdr's error code or message)."""
        r = self._run(argv, timeout)
        if not r.ok:
            try:
                err = json.loads(r.err).get("error", {})
                why = err.get("code") or err.get("message") or f"exit {r.rc}"
            except (ValueError, AttributeError):
                why = (r.err or "").strip().splitlines()[-1:] or [f"exit {r.rc}"]
                why = why[0]
            return None, str(why)[:200]
        try:
            doc = json.loads(r.out)
        except ValueError:
            doc = {}
        return (doc if isinstance(doc, dict) else {}), ""

    def version(self, timeout: float) -> str | None:
        r = self._run(["--version"], timeout)
        return r.out.split()[-1] if r.ok and r.out.split() else None

    # --- what is running ----------------------------------------------------------
    def raw_agents(self, timeout: float) -> tuple[str, list[dict] | None, list[str]]:
        """(server state, herdr's own AgentInfo list or None, errors)."""
        r = self._run(["agent", "list"], timeout)
        if r.rc == NOT_FOUND_RC:
            return "unknown", None, ["herdr: not installed"]
        if r.rc == TIMEOUT_RC:
            return "unknown", None, ["herdr agent list: timed out"]
        if not r.ok:
            return "stopped", None, ["herdr: server not answering"]
        try:
            agents = json.loads(r.out)["result"]["agents"]
            if not isinstance(agents, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return "running", None, ["herdr agent list: unreadable output"]
        return "running", [a for a in agents if isinstance(a, dict)], []

    @staticmethod
    def view(raw: dict) -> dict:
        """One herdr AgentInfo as a runtime-neutral agent (module docstring)."""
        session = raw.get("agent_session")
        sid = session.get("value") if isinstance(session, dict) else None
        state = raw.get("agent_status")

        def text(key: str) -> str | None:
            value = raw.get(key)
            return value if isinstance(value, str) and value else None

        return {
            "name": text("name"),
            "kind": str(raw.get("agent") or "unknown"),
            "state": state if state in AGENT_STATES else "unknown",
            "session": sid if isinstance(sid, str) and sid else None,
            "place": text("pane_id"),
            "workspace": text("workspace_id"),
            "cwd": text("cwd"),
        }

    def agents(self, timeout: float = 5.0) -> tuple[str, list[dict] | None, list[str]]:
        server, raw, errors = self.raw_agents(timeout)
        return server, (None if raw is None else [self.view(a) for a in raw]), errors

    def places(self) -> dict[str, dict] | None:
        """Every pane herdr has now, agent or not: {pane id: {workspace, cwd, agent,
        usable}}. usable: a plain shell an agent can be started in. None when herdr
        does not say."""
        doc, _ = self._call(["pane", "list"])
        panes = ((doc or {}).get("result") or {}).get("panes")
        if not isinstance(panes, list):
            return None
        out = {}
        for p in panes:
            if not isinstance(p, dict) or not isinstance(p.get("pane_id"), str):
                continue
            out[p["pane_id"]] = {
                "workspace": p.get("workspace_id"),
                "cwd": p.get("cwd") if isinstance(p.get("cwd"), str) else None,
                "agent": p.get("agent") or None,
                "usable": not p.get("agent") and not p.get("restore_error"),
            }
        return out

    def in_place(self, place: str) -> dict | None:
        """The Claude agent in this place now, or None."""
        _, live, _ = self.agents()
        return next((a for a in live or [] if a["place"] == place and a["kind"] == "claude"),
                    None)

    # --- starting -------------------------------------------------------------------
    def new_place(self, cwd: str, label: str) -> tuple[str | None, str | None, str]:
        """A new workspace in cwd, labelled for the agent: (its pane, its workspace,
        why not)."""
        ws, why = self._call(["workspace", "create", "--cwd", cwd, "--label", label,
                              "--no-focus"])
        result = (ws or {}).get("result") or {}
        pane = (result.get("root_pane") or {}).get("pane_id")
        workspace = (result.get("workspace") or {}).get("workspace_id")
        if not pane:
            return None, None, f"workspace create failed: {why or 'no pane'}"
        return pane, workspace if isinstance(workspace, str) else None, ""

    def start(self, name: str, place: str, args: list[str]) -> tuple[bool, str]:
        """Launch claude with args in place (a pane at its shell prompt), named name."""
        argv = ["agent", "start", name, "--kind", "claude", "--pane", place,
                "--timeout", str(START_TIMEOUT_MS), "--", *args]
        doc, why = self._call(argv, START_TIMEOUT_MS / 1000 + 15)
        return doc is not None, why

    def start_settled(self, name: str, place: str, args: list[str]) -> tuple[bool, str]:
        """start, once more after a moment if the shell was not back at its prompt
        yet; ok when claude is in the place afterwards, whatever herdr answered."""
        ok, why = self.start(name, place, args)
        if not ok and self.in_place(place) is None:
            time.sleep(min(3.0, wait_budget(self.ctx, 3.0)))
            ok, why = self.start(name, place, args)
        return ok or self.in_place(place) is not None, why

    # --- talking to an agent ----------------------------------------------------------
    def prompt(self, target: str, text: str) -> tuple[bool, str]:
        """Type text and Enter into an agent (by name or place)."""
        doc, why = self._call(["agent", "prompt", target, text], 30)
        return doc is not None, why

    def rename(self, place: str, name: str) -> bool:
        doc, _ = self._call(["agent", "rename", place, name], 10)
        return doc is not None

    def stop(self, place: str, state: str) -> tuple[bool, list[str]]:
        """End the Claude agent in place, gracefully; (stopped, notes for the person).

        Escape first if it is mid-turn or at a question, then Ctrl+C (clears a
        half-typed line) and `/exit`; Ctrl+C twice if it is still there."""
        notes: list[str] = []
        ctx = self.ctx
        settled = lambda: (self.in_place(place) or {}).get("state") in SETTLED  # noqa: E731
        gone = lambda: self.in_place(place) is None  # noqa: E731
        if state in ("working", "unknown", "blocked"):
            self._call(["agent", "send-keys", place, "esc"])
            notes.append({"blocked": "its open question was dismissed",
                          "working": "its turn was interrupted"}.get(
                              state, "it was interrupted (herdr could not tell its state)"))
            # herdr refuses a prompt to an agent it still sees at a question, so /exit
            # waits until Escape has visibly taken effect.
            wait_until(ctx, SETTLE_TIMEOUT_S, settled)
        self._call(["agent", "send-keys", place, "ctrl+c"])
        sent, why = self.prompt(place, "/exit")
        if not sent and why == "agent_blocked":  # Escape had not shown yet: once more
            wait_until(ctx, SETTLE_TIMEOUT_S, settled)
            sent, why = self.prompt(place, "/exit")
        if not sent:
            ctx.log(f"/exit not typed into {place}: {why}")
        if sent and wait_until(ctx, STOP_TIMEOUT_S, gone):
            return True, notes
        if gone():
            return True, notes
        self._call(["pane", "send-keys", place, "ctrl+c", "ctrl+c"])
        if wait_until(ctx, SETTLE_TIMEOUT_S, gone):
            return True, notes
        if not sent:
            notes.append(f"/exit was refused ({why})")
        return False, notes


def for_ship(ctx: Context) -> Herdr:
    """The runtime this Ship's agents run in."""
    return Herdr(ctx)
