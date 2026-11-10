"""The agent runtime: the one place ship-ctl talks to what runs the agents.

Two runtimes, chosen per Ship by ship.toml `runtime` (contracts/settings.md):
herdr's server (`herdr agent ...`, `herdr pane ...`, the default) and herdr-core
(`hc ...`, `runtime = "core"`). Everything else (the agent record, autostart,
restart, notices, the status document) reaches them only through `for_ship(ctx)`,
and both classes here have the same methods.

An agent, as every runtime reports it:

    name       the runtime's name for it, or None
    kind       "claude", "codex", ...
    state      idle | working | blocked | done | unknown
    session    the Claude session id it runs, or None
    place      where it runs (herdr: its pane id); opaque outside this module
    workspace  the place's group (herdr: workspace id), or None
    cwd        its folder, when the runtime knows it

A place is also what the record keeps to put an agent back where it was.

`launch(name, cwd, args, place)` starts claude with args, in place when it can (a
free shell there), else in a new place; `report(raw)` is one agent as the status
document's `herdr.agents` lists it.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
import tomllib

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


def launched(ok: bool, place: str | None = None, workspace: str | None = None,
             why: str = "", step: str = "start") -> dict:
    """What `launch` did: ok, the place it ran in, its workspace, why not, and which
    step failed (place: no place for it; start: claude did not start there)."""
    return {"ok": ok, "place": place, "workspace": workspace, "why": why, "step": step}


class Herdr:
    """herdr 0.9 server: agents are panes herdr recognises; places are pane ids.

    Restoring after a restart, herdr brings every pane back under its old id, as a
    fresh shell in its saved folder (it resumes no agent: ship-ctl apply turns
    `session.resume_agents_on_restore` off, so the record alone decides).
    """

    kind = "herdr"
    chooses_session = False  # herdr reports the session claude picked

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

    @staticmethod
    def report(raw: dict) -> dict:
        """One herdr AgentInfo as a status document herdr.agents item, without its
        mode (status.agent_entry adds it)."""
        entry: dict = {
            "name": raw.get("name") if isinstance(raw.get("name"), str) else None,
            "kind": str(raw.get("agent") or "unknown"),
            "state": raw.get("agent_status") if raw.get("agent_status") in AGENT_STATES
            else "unknown",
        }
        for src, dst in (("pane_id", "pane"), ("workspace_id", "workspace"), ("cwd", "cwd")):
            if isinstance(raw.get(src), str):
                entry[dst] = raw[src]
        return entry

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

    def in_place(self, place: str, name: str | None = None) -> dict | None:
        """The Claude agent in this place now (with name: only one by that name, as
        `start` names the agent it starts), or None."""
        _, live, _ = self.agents()
        return next((a for a in live or [] if a["place"] == place and a["kind"] == "claude"
                     and (name is None or a["name"] == name)), None)

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
        yet; ok when the agent it started, by its name, is in the place afterwards,
        whatever herdr answered. Another agent there is never taken for it."""
        ok, why = self.start(name, place, args)
        if not ok and self.in_place(place, name) is None:
            if self.in_place(place) is not None:
                return False, "another agent is in that pane"
            time.sleep(min(3.0, wait_budget(self.ctx, 3.0)))
            ok, why = self.start(name, place, args)
        return ok or self.in_place(place, name) is not None, why

    def launch(self, name: str, cwd: str, args: list[str], place: str | None = None,
               workspace: str | None = None, settled: bool = True) -> dict:
        """Start claude with args, named name: in place (a pane at its shell prompt),
        else in a new workspace in cwd. settled: once more if the shell was not
        back at its prompt yet (start_settled)."""
        if place is None:
            place, workspace, why = self.new_place(cwd, name)
            if place is None:
                return launched(False, why=why, step="place")
        ok, why = (self.start_settled if settled else self.start)(name, place, args)
        return launched(ok, place, workspace, why)

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


# --- herdr-core ---------------------------------------------------------------------
LAUNCHER = "/opt/shipshape/bin/agent-launch"  # ship/files/bin/agent-launch
SESSION_LABEL = "shipshape.session"  # the Claude session a core terminal was started with
CORE_SPACE = "shipshape"  # herdr.space: the desktop app shows a Ship's agents as one space
SHELLS = ("sh", "bash", "zsh", "fish", "nu", "dash", "ksh", "tcsh")  # as herdr-core has them
CORE_CLAUDE_FLOOR = (2, 1, 295)  # the first Claude Code to report its state (OSC 7501)
SHELL_WAIT_S = 20  # for a kept terminal to get its new shell after a reboot or a crash
LAUNCH_CHECK_S = 5  # for claude to be running in its terminal after a launch
LAUNCHING_LABEL = "shipshape.launching"  # when the launch was typed into it (unix time)
LAUNCH_GRACE_S = 120  # a launch typed this recently is never typed again
CLAUDE_PROGRAMS = ("claude", "node", "agent-launch")  # claude's foreground while it starts
VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


class Core:
    """herdr-core (`hc`): agents are terminals; places are terminal addresses (`t3`).

    ShipShape starts every agent with `hc run --bg --keep` through the agent
    launcher (LAUNCHER), which gives claude the Ship's environment whatever
    environment the core has (a laptop's `hc` can start the core over a bare SSH
    login). Each terminal is labelled with its Claude session (SESSION_LABEL) and,
    for the desktop app, herdr.space and herdr.tab. States come from Claude itself.

    Kept terminals survive their program: after a reboot or a crash, a lost one
    becomes a shell in its folder under the same address and name (for a moment it
    has no program at all), and `launch` types `exec agent-launch ...` into it.
    An agent that exits (`/exit`, or claude ending with an exit code) leaves its
    terminal finished; `launch` then runs a new one. Nothing here calls `hc kill`
    or `hc server stop`: both end agents.
    """

    kind = "core"
    chooses_session = True  # new agents get `--session-id <uuid>` from ShipShape

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._claude_too_old: str | None = None  # "" once claude passed the floor

    # --- calls ------------------------------------------------------------------
    def _run(self, argv: list[str], timeout: float):
        return self.ctx.runner.run(["hc", *argv], timeout=timeout, agent=True)

    def _call(self, argv: list[str], timeout: float = 15) -> tuple[dict | None, str]:
        """(the JSON answer, or None on failure; hc's error kind or message).
        argv is [command, *rest]: --json goes right after the command."""
        r = self._run([argv[0], "--json", *argv[1:]], timeout)
        if r.rc == NOT_FOUND_RC:
            return None, "herdr-core is not installed"
        if r.rc == TIMEOUT_RC:
            return None, f"hc {argv[0]}: timed out"
        if not r.ok:
            why = f"exit {r.rc}"
            for line in reversed((r.err or "").strip().splitlines()):
                try:
                    err = json.loads(line).get("error", {})
                    why = err.get("kind") or err.get("message") or why
                    break
                except (ValueError, AttributeError):
                    why = line.strip() or why
                    break
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
    def terminals(self, timeout: float = 5.0, finished: bool = False
                  ) -> tuple[str, list[dict] | None, list[str]]:
        """(core state, every live terminal as `hc get` shows it (and, with finished,
        the finished ones as `hc ls` does), errors). Any hc call starts the core if
        none runs, so "stopped" means it could not be started."""
        deadline = time.monotonic() + timeout
        r = self._run(["ls", "--json", *(["--all"] if finished else [])], timeout)
        if r.rc == NOT_FOUND_RC:
            return "unknown", None, ["herdr-core: not installed"]
        if r.rc == TIMEOUT_RC:
            return "unknown", None, ["hc ls: timed out"]
        if not r.ok:
            return "stopped", None, ["herdr-core: the core is not answering"]
        try:
            rows = json.loads(r.out)["terminals"]
            if not isinstance(rows, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            return "running", None, ["hc ls: unreadable output"]
        out = []
        seen = self.ledger_terminals()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("address"), str):
                continue
            if row.get("state") == "live":  # hc ls leaves out labels and the foreground
                full = self.get(row["address"], max(1.0, deadline - time.monotonic()))
                if full is None:  # never guess what runs there from the short row
                    return "running", None, [f"hc get {row['address']}: no answer"]
                row = full
                session = self._newest(seen.get(row["address"]), row)
                if session:
                    row = {**row, "ledger_session": session}
            out.append(row)
        return "running", out, []

    def ledger_terminals(self) -> dict[str, list[tuple[str, str]]]:
        """{terminal address: [(started_at, session)]}: the sessions the mode-ledger
        hook saw start in each herdr-core terminal and not end."""
        doc = self.ctx.read_home_json(self.ctx.paths.mode_ledger, {})
        sessions = doc.get("sessions") if isinstance(doc, dict) else None
        out: dict[str, list[tuple[str, str]]] = {}
        for sid, e in (sessions.items() if isinstance(sessions, dict) else ()):
            if (isinstance(e, dict) and isinstance(e.get("terminal"), str)
                    and isinstance(e.get("started_at"), str) and not e.get("ended")):
                out.setdefault(e["terminal"], []).append((e["started_at"], sid))
        return out

    @staticmethod
    def _newest(seen: list[tuple[str, str]] | None, raw: dict) -> str | None:
        """The session that started last in this terminal, after it was created (an
        address from a core whose state was wiped is not this terminal)."""
        created = raw.get("created_ms")
        since = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created // 1000))
                 if isinstance(created, int) and created > 0 else "")
        found = [x for x in seen or [] if x[0] >= since]
        return max(found)[1] if found else None

    def get(self, target: str, timeout: float = 10) -> dict | None:
        doc, _ = self._call(["get", target], timeout)
        return doc if doc and isinstance(doc.get("address"), str) else None

    @staticmethod
    def program(raw: dict) -> str | None:
        """The program in its foreground now ("" if not known), or None while a
        lost kept terminal waits for its new shell."""
        if "foreground" in raw:  # hc get
            fg = raw["foreground"]
            return str(fg.get("name") or "") if isinstance(fg, dict) else None
        return str(raw.get("agent") or "")  # hc ls (its own short word for it)

    @staticmethod
    def view(raw: dict) -> dict:
        """One terminal as a runtime-neutral agent (module docstring). Its kind is
        claude (Claude says so, or ShipShape started it and it is not back at a
        shell), shell (a kept terminal whose agent is gone), lost (a kept terminal
        waiting for its shell), finished, or what else runs in it."""
        status = raw.get("status")
        word = status.get("state") if isinstance(status, dict) else status
        app = status.get("app") if isinstance(status, dict) else None
        labels = raw.get("labels") if isinstance(raw.get("labels"), dict) else {}
        fg = raw.get("foreground") if isinstance(raw.get("foreground"), dict) else {}
        # The session it runs now, as the mode-ledger hook saw it start there (it
        # follows /clear), else the one ShipShape started it with.
        session = raw.get("ledger_session") or labels.get(SESSION_LABEL)
        session = session if isinstance(session, str) and session else None
        program = Core.program(raw)
        argv = [os.path.basename(str(x)) for x in (fg.get("argv") or [])[:2]]
        starting = program in CLAUDE_PROGRAMS or any(x in CLAUDE_PROGRAMS for x in argv)
        if raw.get("state") != "live":
            kind = "finished"
        elif program is None:
            kind = "lost"  # no program at all, whatever its last word was
        elif program in SHELLS and not starting:
            # Claude is a terminal's top program (it replaced the shell, or the
            # launcher exec'd it), so a shell in front means it is gone, whatever its
            # last word was. (The launcher is a sh script: that is not a free shell.)
            kind = "shell"
        elif app == "claude-code":
            kind = "claude"
        elif session and starting:
            kind = "claude"  # launched by ShipShape, before Claude's first report
        else:
            kind = str(app or program or "unknown")
        launching = labels.get(LAUNCHING_LABEL)

        def text(value: object) -> str | None:
            return value if isinstance(value, str) and value else None

        out = {
            "name": text(raw.get("name")),
            "kind": kind,
            "state": word if word in ("idle", "working", "blocked", "done") else "unknown",
            "session": session,
            "place": raw["address"],
            "workspace": text(labels.get("herdr.space")),
            "cwd": text(fg.get("cwd")) or text(raw.get("cwd")),
            "reporting": isinstance(word, str),
            "launching": int(launching) if isinstance(launching, str) and launching.isdigit()
            else None,
        }
        if out["state"] == "blocked" and isinstance(status, dict):
            out["blocked_kind"] = text(status.get("kind"))
            out["message"] = text(status.get("msg"))
        return out

    @staticmethod
    def is_agent(view: dict) -> bool:
        """A live terminal that is an agent: Claude, or a program reporting a state."""
        return view["kind"] == "claude" or (
            view["reporting"] and view["kind"] not in ("finished", "lost", "shell"))

    def raw_agents(self, timeout: float) -> tuple[str, list[dict] | None, list[str]]:
        """(core state, the agents' terminals as hc get shows them, errors)."""
        server, terms, errors = self.terminals(timeout)
        if terms is None:
            return server, None, errors
        return server, [t for t in terms if self.is_agent(self.view(t))], errors

    @staticmethod
    def report(raw: dict) -> dict:
        """One agent's terminal as a status document herdr.agents item, without its
        mode: pane is its address; a blocked one says what it waits for."""
        v = Core.view(raw)
        entry: dict = {"name": v["name"], "kind": v["kind"], "state": v["state"],
                       "pane": v["place"]}
        for key in ("workspace", "cwd"):
            if v[key]:
                entry[key] = v[key]
        if v.get("blocked_kind"):
            entry["blocked_kind"] = v["blocked_kind"][:40]
        if v.get("message"):
            entry["message"] = v["message"][:500]
        return entry

    def agents(self, timeout: float = 5.0) -> tuple[str, list[dict] | None, list[str]]:
        server, raw, errors = self.raw_agents(timeout)
        return server, (None if raw is None else [self.view(a) for a in raw]), errors

    def places(self) -> dict[str, dict] | None:
        """Every terminal, finished ones too: {address: {workspace, cwd, agent,
        usable}}. usable: a kept terminal back at (or about to get) its shell."""
        _, terms, _ = self.terminals(finished=True)
        if terms is None:
            return None
        out = {}
        for t in terms:
            v = self.view(t)
            out[v["place"]] = {"workspace": v["workspace"], "cwd": v["cwd"],
                               "agent": v["kind"] if self.is_agent(v) else None,
                               "usable": v["kind"] in ("shell", "lost")}
        return out

    def in_place(self, place: str, name: str | None = None) -> dict | None:
        """The Claude agent in this terminal now (with name: only one by that name),
        or None."""
        raw = self.get(place)
        if raw is None:
            return None
        v = self.view(raw)
        if v["kind"] != "claude" or (name is not None and v["name"] != name):
            return None
        return v

    # --- starting -------------------------------------------------------------------
    def claude_floor(self) -> str:
        """Why claude is too old for herdr-core ("" if it is not, or if its version
        cannot be read: the launch then says what is wrong)."""
        if self._claude_too_old is None:
            r = self.ctx.runner.run(["claude", "--version"], timeout=15, agent=True)
            m = VERSION.match(r.out.strip()) if r.ok else None
            found = tuple(int(x) for x in m.groups()) if m else None
            floor = ".".join(map(str, CORE_CLAUDE_FLOOR))
            self._claude_too_old = (
                f"Claude Code {'.'.join(map(str, found))} is older than {floor}, the "
                "first that reports its state to herdr-core; update it (claude update)"
                if found is not None and found < CORE_CLAUDE_FLOOR else "")
        return self._claude_too_old

    @staticmethod
    def session_of(args: list[str]) -> str | None:
        for flag in ("--resume", "--session-id"):
            if flag in args[:-1]:
                return args[args.index(flag) + 1]
        return None

    def _labels(self, name: str, args: list[str]) -> list[str]:
        out = [f"herdr.space={CORE_SPACE}", f"herdr.tab={name}"]
        session = self.session_of(args)
        return [f"{SESSION_LABEL}={session}", *out] if session else out

    def _running(self, place: str, name: str) -> tuple[bool, str]:
        """Wait a moment for claude, named name, to be running in place."""
        last: dict = {}

        def up() -> bool:
            raw = self.get(place)
            last["raw"] = raw
            if raw is None or raw.get("state") != "live":
                return True  # gone or finished: no use waiting
            v = self.view(raw)
            return v["kind"] == "claude" and v["name"] == name

        wait_until(self.ctx, LAUNCH_CHECK_S, up)
        raw = last.get("raw")
        if raw is None:
            return False, "its terminal is gone"
        if raw.get("state") != "live":
            code = raw.get("exit_code")
            return False, "claude exited at once" + (f" (exit {code})" if code is not None
                                                     else "")
        v = self.view(raw)
        if v["kind"] == "claude" and v["name"] == name:
            return True, ""
        return False, f"claude is not running in {place} ({v['kind']} is)"

    def _exec_in(self, place: str, name: str, cwd: str, args: list[str]) -> tuple[bool, str]:
        """Type the launch into a kept terminal: once it has its shell, `exec` the
        launcher there, in the agent's folder (`--resume` finds a session by its
        folder), so the terminal (and an app pane on it) keeps its address."""
        def shell() -> bool:
            raw = self.get(place)
            return raw is not None and self.view(raw)["kind"] == "shell"

        if not wait_until(self.ctx, SHELL_WAIT_S, shell):
            return False, f"its terminal {place} did not get its shell back"
        self._call(["label", place, *self._labels(name, args),
                    f"{LAUNCHING_LABEL}={int(time.time())}"])
        line = f"cd -- {shlex.quote(cwd)} && exec {shlex.join([LAUNCHER, *args])}"
        doc, why = self._call(["send", place, "--", line], 90)
        if doc is None:
            return False, f"the launch was not typed into {place}: {why}"
        ok, why = self._running(place, name)
        raw = self.get(place)
        if not ok and raw is not None and raw.get("state") == "live":
            # Typed, and the shell has not run it yet (a slow start): it is on its way,
            # and typing it again would leave a second line for claude to read.
            return True, ""
        return ok, why

    def launch(self, name: str, cwd: str, args: list[str], place: str | None = None,
               workspace: str | None = None, settled: bool = True) -> dict:
        """Start claude (through the launcher) with args, named name: in its kept
        terminal (place, else the live one by that name) when that is at its
        shell, else in a new terminal in cwd. Already running by that name (hc's
        name_taken) is not a failure. settled is herdr's; core needs no retry."""
        del workspace, settled  # every core agent is in CORE_SPACE
        _, terms, _ = self.terminals()
        live = [self.view(t) for t in terms or []]
        ours = (next((v for v in live if place and v["place"] == place
                      and v["name"] in (name, None)), None)
                or next((v for v in live if v["name"] == name), None))
        for attempt in range(2):
            if ours is not None and ours["kind"] == "claude" and ours["name"] == name:
                return launched(True, ours["place"], CORE_SPACE)  # running already
            why = self.claude_floor()
            if why:
                return launched(False, why=why)
            if ours is not None:
                typed = ours.get("launching")
                if (ours["kind"] in ("shell", "lost") and typed is not None
                        and 0 <= time.time() - typed < LAUNCH_GRACE_S):
                    return launched(True, ours["place"], CORE_SPACE)  # typed already
                if ours["kind"] in ("shell", "lost"):
                    ok, why = self._exec_in(ours["place"], name, cwd, args)
                    return launched(ok, ours["place"], CORE_SPACE, why)
                if ours["name"] == name:  # its terminal, but something else runs there
                    return launched(False, why=f"{ours['place']} ({name}) runs "
                                    f"{ours['kind']}, not claude")
            if attempt:
                break
            labels = [x for label in self._labels(name, args) for x in ("--label", label)]
            doc, why = self._call(["run", "--bg", "--keep", "--name", name, "--cwd", cwd,
                                   *labels, "--", LAUNCHER, *args], 60)
            if doc is not None and isinstance(doc.get("address"), str):
                ok, why = self._running(doc["address"], name)
                if ok and place and terms is not None and place not in {v["place"] for v in live}:
                    self._call(["rm", place])  # its old, finished terminal (never a live one)
                return launched(ok, doc["address"], CORE_SPACE, why)
            if why != "name_taken":
                return launched(False, why=why or "hc run gave no terminal")
            # Another ship-ctl (or a person) started it a moment ago: it runs already.
            raw = self.get(name)
            ours = self.view(raw) if raw is not None and raw.get("state") == "live" else None
        return launched(False, why="name_taken: a terminal by that name runs something else")

    # --- talking to an agent ----------------------------------------------------------
    def prompt(self, target: str, text: str) -> tuple[bool, str]:
        """Paste text and Enter into an agent (by name or address). Never forced:
        hc refuses while the agent is blocked (why: blocked), and waits for a
        person's unfinished draft."""
        doc, why = self._call(["send", target, "--", text], 90)
        return doc is not None, why

    def rename(self, place: str, name: str) -> bool:
        """herdr-core names a terminal when it starts, and only then."""
        return False

    def stop(self, place: str, state: str) -> tuple[bool, list[str]]:
        """End the Claude agent in place, gracefully, as Herdr.stop does: Escape
        first if it is mid-turn or at a question, then Ctrl+C and `/exit`; Ctrl+C
        twice if it is still there. Never `hc kill` (it ends claude with a signal)."""
        notes: list[str] = []
        ctx = self.ctx
        settled = lambda: (self.in_place(place) or {}).get("state") in SETTLED  # noqa: E731
        gone = lambda: self.in_place(place) is None  # noqa: E731
        if state in ("working", "unknown", "blocked"):
            self._call(["keys", place, "esc"])
            notes.append({"blocked": "its open question was dismissed",
                          "working": "its turn was interrupted"}.get(
                              state, "it was interrupted (Claude had not reported its state)"))
            wait_until(ctx, SETTLE_TIMEOUT_S, settled)  # hc send refuses while blocked
        self._call(["keys", place, "ctrl+c"])
        sent, why = self.prompt(place, "/exit")
        if not sent and why == "blocked":  # Escape had not shown yet: once more
            wait_until(ctx, SETTLE_TIMEOUT_S, settled)
            sent, why = self.prompt(place, "/exit")
        if not sent:
            ctx.log(f"/exit not typed into {place}: {why}")
        if sent and wait_until(ctx, STOP_TIMEOUT_S, gone):
            return True, notes
        if gone():
            return True, notes
        self._call(["keys", place, "ctrl+c", "ctrl+c"])
        if wait_until(ctx, SETTLE_TIMEOUT_S, gone):
            return True, notes
        if not sent:
            notes.append(f"/exit was refused ({why})")
        return False, notes


Runtime = Herdr | Core
RUNTIMES = {"herdr": Herdr, "core": Core}


def selected(ctx: Context) -> str:
    """ship.toml's `runtime` (herdr when unset, or when ship.toml cannot be read)."""
    try:
        doc = tomllib.loads(ctx.paths.ship_toml.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return Herdr.kind
    return doc.get("runtime") if doc.get("runtime") in RUNTIMES else Herdr.kind


def for_ship(ctx: Context, ship=None) -> Herdr | Core:
    """The runtime this Ship's agents run in (ship: its ShipToml, if loaded)."""
    kind = ship.runtime if ship is not None else selected(ctx)
    return RUNTIMES.get(kind, Herdr)(ctx)
