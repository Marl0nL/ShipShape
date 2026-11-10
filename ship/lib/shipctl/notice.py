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
(`unknown`, as just after it was started again) has a foreground nobody knows.
Until this boot's resume is done, nothing is sent.
"""

from __future__ import annotations

import re

from . import record as agent_record
from . import signincheck, signins
from .context import Context
from .runtime import for_ship
from .shiptoml import ShipToml

# herdr agent states it is safe to type into (Claude Code queues input mid-turn).
READY_STATES = ("idle", "working", "done")
# What may appear of an account name in the typed line: no newlines, quotes or
# control characters, whatever the sign-in tool printed.
_UNSAFE = re.compile(r"[^A-Za-z0-9@._+-]")


def journal(ctx: Context, message: str, what: str = "notify-signin") -> None:
    """Into the Ship's journal (`ship-ctl logs system`), however ship-ctl was run:
    over SSH its stderr goes back to FleetLedger, not to the journal."""
    ctx.log(message)
    ctx.runner.run(["logger", "-t", "ship-ctl", "--", f"{what}: {message}"], timeout=10)


def text(label: str, account: str | None) -> str:
    who = f" as {account}" if account else ""
    return f"ShipShape: {label} is now signed in{who}. Re-check it if you need it."


def clean_account(account: str | None) -> str | None:
    if not account:
        return None
    cleaned = _UNSAFE.sub("", account)[:254]
    return cleaned or None


def notify(ctx: Context, ship: ShipToml, service: str) -> tuple[dict, list[str]]:
    """Send the notice to each live core agent. Returns (data, messages).

    SERVICE is any sign-in file's id (signins.py); the account is the one its own
    status key reports, checked now, and the notice names every key it signs in."""
    reg = signins.registry(ctx.paths)
    key = reg.flows[service].keys[0]  # the account of the first key the flow signs in
    label = reg.notice_label(service)
    entry, _ = signincheck.check(ctx, reg, [key], fresh=True)[key]
    data: dict = {"service": service, "account": None, "sent": [], "skipped": []}
    if not signincheck.signed_in(entry):
        msg = f"{label} is not signed in on this Ship; no notice sent."
        journal(ctx, f"{service}: {msg}")
        return data, [msg]
    account = clean_account(entry.get("account"))
    data["account"] = account
    line = text(label, account)

    note, messages = broadcast(ctx, ship, line, data, label)
    if note:
        journal(ctx, f"{service}: {note}")
    else:
        journal(ctx, f"{service} as {account or 'unknown'}: " + " ".join(messages))
    return data, messages


def broadcast(
    ctx: Context, ship: ShipToml, line: str, data: dict, label: str
) -> tuple[str | None, list[str]]:
    """send_to_cores, plus the lines that report it. Returns (note, messages): note
    says why nothing was tried (then it is the only message), else None and the
    "LABEL notice sent to: ..." line with one "NAME: not sent (REASON)." per skip."""
    note = send_to_cores(ctx, ship, line, data)
    if note:
        return note, [note]
    messages = [f"{label} notice sent to: {', '.join(data['sent']) or 'none'}."]
    messages += [f"{s['name']}: not sent ({s['reason']})." for s in data["skipped"]]
    return None, messages


def send_to_cores(ctx: Context, ship: ShipToml, line: str, data: dict) -> str | None:
    """Type one line into each live core agent that is safe to type into, filling
    data["sent"] and data["skipped"]. Returns why nothing was tried, if so."""
    cores = [a["name"] for a in ship.autostart]
    if not cores:
        return "This Ship has no [[autostart]] agents; no notice sent."
    if agent_record.resume_pending(ctx):
        data["skipped"] = [{"name": name, "reason": "resume-pending"} for name in cores]
        return "This boot's resume is not done yet; no notice sent."
    rt = for_ship(ctx, ship)
    _, live, _ = rt.agents()
    doc = agent_record.load(ctx)
    for name in cores:
        why = send_one(ctx, rt, name, live or [], doc, line)
        if why is None:
            data["sent"].append(name)
        else:
            data["skipped"].append({"name": name, "reason": why})
    return None


def send_one(ctx: Context, rt, name: str, live: list[dict], doc: dict, line: str) -> str | None:
    """Type one line into recorded agent `name` if it is safe to: None when it was
    sent, else why not (not-running, waiting-for-permission, not-ready, send-failed)."""
    match = agent_record.find_live(ctx, name, live, doc)
    if match is None:
        return "not-running"
    state = match["state"]
    if state == "blocked":
        return "waiting-for-permission"
    if state not in READY_STATES:
        return "not-ready"
    sent, _ = rt.prompt(str(match["name"] or match["place"]), line)
    return None if sent else "send-failed"
