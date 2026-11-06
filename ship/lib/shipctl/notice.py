"""Tell the Ship's core agents that a sign-in completed (contracts/ship-ctl.md,
`ship-ctl notify-signin`).

FleetLedger runs `ship-ctl notify-signin SERVICE` once a sign-in it ran (`ship-fleet
login` or the web UI's concierge) has checked out. Agents started before that
sign-in may already have looked and found it missing, so each core agent gets one
line saying it is there now.

The core agents are the Ship's [[autostart]] entries, matched to live herdr agents
the way autostart matches them (name, else the Claude session or pane it
recorded). Every other agent, such as a firstmate's workers, is never messaged.
The line names the account, nothing else, and is typed with `herdr agent prompt`,
which Claude Code queues while a turn is running. Only an agent herdr reports
as idle, working or done is typed into: one waiting at a permission prompt
could have it answered by the text and Enter, and one herdr cannot classify
(`unknown`, as while ship-resume brings panes back after a boot) has a
foreground nobody knows. Until this boot's resume is done, nothing is sent.
"""

from __future__ import annotations

import re

from . import autostart, status
from .context import Context
from .fsutil import read_json
from .shiptoml import ShipToml

# notify-signin SERVICE (FleetLedger's provider id) -> (status auth key, label)
SERVICES = {
    "claude": ("claude", "Claude Code"),
    "gcloud": ("gcloud", "gcloud (with Application Default Credentials)"),
    "gcloud-adc": ("gcloud_adc", "Google Application Default Credentials"),
    "github": ("github", "GitHub"),
}
# herdr agent states it is safe to type into (Claude Code queues input mid-turn).
READY_STATES = ("idle", "working", "done")
# What may appear of an account name in the typed line: no newlines, quotes or
# control characters, whatever the sign-in tool printed.
_UNSAFE = re.compile(r"[^A-Za-z0-9@._+-]")


def record(ctx: Context, message: str, what: str = "notify-signin") -> None:
    """Into the Ship's journal (`ship-ctl logs system`), however ship-ctl was run:
    over SSH its stderr goes back to FleetLedger, not to the journal."""
    ctx.log(message)
    ctx.runner.run(["logger", "-t", "ship-ctl", "--", f"{what}: {message}"], timeout=10)


def text(service: str, account: str | None) -> str:
    label = SERVICES[service][1]
    who = f" as {account}" if account else ""
    return f"ShipShape: {label} is now signed in{who}. Re-check it if you need it."


def clean_account(account: str | None) -> str | None:
    if not account:
        return None
    cleaned = _UNSAFE.sub("", account)[:254]
    return cleaned or None


def notify(ctx: Context, ship: ShipToml, service: str) -> tuple[dict, list[str]]:
    """Send the notice to each live core agent. Returns (data, messages)."""
    key, label = SERVICES[service]
    entry, _ = status.AUTH_PROBES[key](ctx)
    data: dict = {"service": service, "account": None, "sent": [], "skipped": []}
    if entry.get("state") != "ok":
        msg = f"{label} is not signed in on this Ship; no notice sent."
        record(ctx, f"{service}: {msg}")
        return data, [msg]
    account = clean_account(entry.get("account"))
    data["account"] = account
    line = text(service, account)

    note = send_to_cores(ctx, ship, line, data)
    if note:
        record(ctx, f"{service}: {note}")
        return data, [note]
    messages = [f"{label} notice sent to: {', '.join(data['sent']) or 'none'}."]
    messages += [f"{s['name']}: not sent ({s['reason']})." for s in data["skipped"]]
    record(ctx, f"{service} as {account or 'unknown'}: " + " ".join(messages))
    return data, messages


def send_to_cores(ctx: Context, ship: ShipToml, line: str, data: dict) -> str | None:
    """Type one line into each live core agent that is safe to type into, filling
    data["sent"] and data["skipped"]. Returns why nothing was tried, if so."""
    cores = [a["name"] for a in ship.autostart]
    if not cores:
        return "This Ship has no [[autostart]] agents; no notice sent."
    if autostart.resume_pending(ctx):
        data["skipped"] = [{"name": name, "reason": "resume-pending"} for name in cores]
        return "This boot's resume is not done yet; no notice sent."
    _, live, _ = status.herdr_agents(ctx)
    started = read_json(ctx.paths.state / "autostart.json", {})
    started = started if isinstance(started, dict) else {}
    for name in cores:
        match = autostart.find_live(name, started, live or [])
        if match is None:
            data["skipped"].append({"name": name, "reason": "not-running"})
            continue
        state = match.get("agent_status")
        if state == "blocked":
            data["skipped"].append({"name": name, "reason": "waiting-for-permission"})
            continue
        if state not in READY_STATES:
            data["skipped"].append({"name": name, "reason": "not-ready"})
            continue
        target = match.get("name") or match.get("pane_id")
        r = ctx.runner.run(["herdr", "agent", "prompt", str(target), line],
                           timeout=30, agent=True)
        if r.ok:
            data["sent"].append(name)
        else:
            data["skipped"].append({"name": name, "reason": "send-failed"})
    return None
