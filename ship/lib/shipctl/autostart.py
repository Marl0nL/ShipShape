"""`ship-ctl autostart`: start every [[autostart]] agent that is not running.

An agent counts as running when herdr lists a live agent that is the same one:
the same herdr name, or the Claude session or pane autostart recorded for it last
time. The last two matter after a reboot: herdr restores and resumes the pane, but
the name may not come back, so autostart re-attaches the name instead of starting
a second copy.

At boot, autostart waits for ship-resume to finish (its record carries the boot
id), so it never races herdr's own resume.
"""

from __future__ import annotations

import json

from . import naming
from .apply import expand_cwd
from .context import Context, utc_now
from .errors import CtlError
from .fsutil import ensure_dir, read_json, write_atomic
from .shiptoml import ShipToml
from .status import boot_id, herdr_agents

# Sent to an agent whose [[autostart]] entry has no prompt: a deliberate default,
# so every autostart agent starts by getting its bearings. (Its mode is recorded
# either way: the ledger hook reads it from claude's flags at SessionStart.)
DEFAULT_PROMPT = "Get oriented and ready to work."
RESUME_GRACE_S = 600  # after this much uptime, stop waiting for ship-resume
START_TIMEOUT_MS = 120000


def mode_args(mode: str) -> list[str]:
    if mode == "bypassPermissions":
        return ["--dangerously-skip-permissions"]
    return ["--permission-mode", mode]


def resume_pending(ctx: Context) -> bool:
    rec = ctx.read_home_json(ctx.paths.last_resume, {})
    if isinstance(rec, dict) and rec.get("boot_id") == boot_id(ctx):
        return False
    try:
        uptime = float((ctx.paths.proc / "uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return False
    return uptime < RESUME_GRACE_S


def _session(raw: dict) -> str | None:
    s = raw.get("agent_session")
    return s.get("value") if isinstance(s, dict) else None


def find_live(entry_name: str, record: dict, live: list[dict]) -> dict | None:
    for a in live:
        if a.get("name") == entry_name:
            return a
    rec = record.get(entry_name) or {}
    if rec.get("session"):
        for a in live:
            if _session(a) == rec["session"]:
                return a
    if rec.get("pane"):
        for a in live:
            if a.get("pane_id") == rec["pane"] and a.get("agent") == "claude":
                return a
    return None


def remember(ctx: Context, ship: ShipToml, live: list[dict]) -> None:
    """Record pane and session for every named autostart agent that is live."""
    path = ctx.paths.state / "autostart.json"
    record = read_json(path, {})
    record = record if isinstance(record, dict) else {}
    before = json.dumps(record, sort_keys=True)
    for entry in ship.autostart:
        a = find_live(entry["name"], record, live)
        if a is None:
            continue
        rec = record.setdefault(entry["name"], {})
        if a.get("pane_id"):
            rec["pane"] = a["pane_id"]
        if _session(a):
            rec["session"] = _session(a)
    if json.dumps(record, sort_keys=True) != before:
        write_atomic(path, json.dumps(record, indent=2) + "\n", 0o644)


def _herdr_json(ctx: Context, argv: list[str], timeout: float) -> tuple[dict | None, str]:
    r = ctx.runner.run(["herdr", *argv], timeout=timeout, agent=True)
    if not r.ok:
        try:
            err = json.loads(r.err).get("error", {})
            reason = err.get("code") or err.get("message") or f"exit {r.rc}"
        except (ValueError, AttributeError):
            reason = f"exit {r.rc}"
        return None, str(reason)[:200]
    try:
        return json.loads(r.out), ""
    except ValueError:
        return {}, ""


def run(ctx: Context, ship: ShipToml, state: str) -> tuple[dict, list[str], list[str]]:
    """Returns (data, changed, messages). `state` is the Ship's current status state."""
    started: list[str] = []
    skipped: list[dict] = []
    messages: list[str] = []
    entries = ship.autostart
    if not entries:
        return {"started": [], "skipped": []}, [], []

    if state in ("provisioning", "needs-sign-in"):
        skipped = [{"name": e["name"], "reason": state} for e in entries]
        return {"started": [], "skipped": skipped}, [], []

    server, live, _ = herdr_agents(ctx)
    if server != "running" or live is None:
        raise CtlError("herdr-unavailable", "the herdr server is not answering")
    if resume_pending(ctx):
        skipped = [{"name": e["name"], "reason": "resume-pending"} for e in entries]
        return {"started": [], "skipped": skipped}, [], []

    remember(ctx, ship, live)
    record = read_json(ctx.paths.state / "autostart.json", {})
    record = record if isinstance(record, dict) else {}
    live_names = {a.get("name") for a in live if a.get("name")}
    default_mode = ship.claude["default_mode"]
    agent_home = ctx.paths.agent_path(ctx.paths.agent_home)
    failures = 0
    named = None  # whether claude takes --name; asked once, only if something starts

    for entry in entries:
        name = entry["name"]
        match = find_live(name, record, live)
        if match is not None:
            if match.get("name") != name and name not in live_names and match.get("pane_id"):
                _herdr_json(ctx, ["agent", "rename", match["pane_id"], name], 10)
            skipped.append({"name": name, "reason": "running"})
            continue

        cwd = expand_cwd(entry["cwd"], agent_home)
        if not ctx.paths.sandbox:
            ctx.runner.run(["mkdir", "-p", cwd], timeout=10, agent=True)
        else:
            ensure_dir(ctx.paths.p(cwd))
        ws, why = _herdr_json(
            ctx, ["workspace", "create", "--cwd", cwd, "--label", name, "--no-focus"], 15
        )
        pane = (((ws or {}).get("result") or {}).get("root_pane") or {}).get("pane_id")
        if not pane:
            failures += 1
            skipped.append({"name": name, "reason": f"workspace create failed: {why or 'no pane'}"})
            continue
        mode = entry.get("mode") or default_mode
        session = naming.session_name(ship, name)
        argv = ["agent", "start", name, "--kind", "claude", "--pane", pane]
        argv += ["--timeout", str(START_TIMEOUT_MS), "--", *mode_args(mode)]
        if named is None:
            named = naming.name_flag_supported(ctx)
        argv += naming.launch_args(session, named)
        out, why = _herdr_json(ctx, argv, START_TIMEOUT_MS / 1000 + 15)
        if out is None:
            failures += 1
            skipped.append({"name": name, "reason": f"agent start failed: {why}"})
            continue
        started.append(name)
        record[name] = {
            "pane": pane,
            "mode": mode,
            "session_name": session,
            "started_at": utc_now(),
        }
        prompt = entry.get("prompt") or DEFAULT_PROMPT
        sent, why = _herdr_json(ctx, ["agent", "prompt", name, prompt], 30)
        if sent is None:
            messages.append(f"{name}: started, but the first prompt was not sent ({why}).")

    write_atomic(ctx.paths.state / "autostart.json", json.dumps(record, indent=2) + "\n", 0o644)
    if failures:
        data = {"started": started, "skipped": skipped}
        raise CtlError(
            "herdr-unavailable", f"{failures} autostart agent(s) could not be started", 1
        ).with_data(data, started, messages)
    return {"started": started, "skipped": skipped}, started, messages
