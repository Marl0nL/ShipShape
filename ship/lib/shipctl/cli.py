"""ship-ctl command line (contracts/ship-ctl.md).

With --json every command prints exactly one JSON document on stdout: the status
document for a successful `status`, otherwise a result document
(ship-ctl-result.schema.json). Diagnostics go to stderr. Exit codes follow the
contract: 0 ok, 1 failed, 2 usage, 3 not root, 4 invalid ship.toml.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import (
    CONTRACTS,
    PERMISSION_MODES,
    agents,
    apply,
    autostart,
    converge,
    inbox,
    languard,
    logs,
    net,
    notice,
    overlay,
    shiptoml,
    sshtrust,
    status,
    tools,
)
from . import record as agent_record
from .context import Context
from .errors import CtlError
from .fsutil import read_json, write_atomic

RESULT_SCHEMA = "shipshape.ship-ctl-result/1"

SET_KEYS = {
    "ship.tags": "tags",
    "ship.timezone": "str",
    "claude.remote_control": "str",
    "claude.default_mode": "str",
    "claude.allow_bypass": "bool",
    "claude.update_channel": "str",
    "claude.session_prefix": "str",
    "agent.sudo": "bool",
    "network.lan_guard": "bool",
    "network.lan_guard_allow": "list",
    "updates.auto_reboot": "bool",
    "updates.reboot_time": "str",
    "network.ssh_listen": "str",
    "owner.name": "str",
    "owner.keys": "list",
    "github.scopes": "list",
}
RC_CHANGE_NOTE = (
    "Remote Control changes reach running Claude sessions only when they restart: "
    "`ship-fleet agent restart {ship} --all` restarts them, continuing their conversations "
    "(on the Ship: `ship-ctl agent restart --all`; a reboot restarts them too). The Claude "
    "app can take a few minutes to show or drop them."
)
PREFIX_CHANGE_NOTE = (
    "Running sessions keep their names, even across a restart; send /rename <name>, or "
    "start them in a new conversation (`ship-fleet agent restart {ship} --all --fresh`). "
    "New autostart sessions use the new prefix."
)


def result(command, ok=True, changed=(), messages=(), data=None, error=None) -> dict:
    doc = {
        "schema": RESULT_SCHEMA,
        "command": command,
        "ok": ok,
        "changed": [str(c)[:300] for c in changed],
        "messages": [str(m)[:1000] for m in messages],
        "error": error,
    }
    if data is not None:
        doc["data"] = data
    return doc


class UsageError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse would print and exit 2; we report it instead
        raise UsageError(message)


def build_parser() -> Parser:
    p = Parser(prog="ship-ctl", description="Inspect and manage this Ship (contracts/ship-ctl.md).")
    sub = p.add_subparsers(dest="command", metavar="COMMAND", parser_class=Parser)

    def cmd(name: str, help: str) -> Parser:
        sp = sub.add_parser(name, help=help, description=help)
        sp.add_argument("--json", action="store_true", help="print one JSON document")
        return sp

    s = cmd("status", "Print the Ship's status document.")
    s.add_argument("--write", metavar="PATH", help="also write it atomically to PATH")
    a = cmd("apply", "Render the configuration that derives from ship.toml.")
    a.add_argument("--dry-run", action="store_true", help="list what would change; write nothing")
    st = cmd("set", "Change ship.toml, then apply.")
    st.add_argument("pairs", nargs="*", metavar="KEY=VALUE")
    st.add_argument("--file", metavar="-", help="read a complete replacement ship.toml from stdin")
    cmd("autostart", "Start the [[autostart]] agents that are not running.")
    ag = cmd("agent", "Restart Claude agents in place (in a new permission mode), or forget "
                      "stopped ones.")
    ag.add_argument("action", choices=["restart", "forget"])
    ag.add_argument("names", nargs="*", metavar="NAME", help="agent names (the agent record's)")
    ag.add_argument("--all", dest="everyone", action="store_true",
                    help="every named Claude agent")
    ag.add_argument("--mode", choices=PERMISSION_MODES,
                    help="default: its [[autostart]] mode, else claude.default_mode; for one "
                         "not running, the mode it was in")
    ag.add_argument("--fresh", action="store_true",
                    help="start a new conversation instead of continuing it")
    ag.add_argument("--force", action="store_true",
                    help="also restart an agent that is working (interrupts its turn)")
    ns = cmd("notify-signin", "Tell the [[autostart]] agents that a sign-in has completed.")
    ns.add_argument("service", choices=sorted(notice.SERVICES))
    c = cmd("converge", "Apply a tooling bundle.")
    c.add_argument("--bundle", required=True, metavar="PATH")
    t = cmd("tools", "List, install or check catalogue tools.")
    t.add_argument("action", choices=["list", "run", "add", "remove"])
    t.add_argument("ids", nargs="*", metavar="ID")
    lg = cmd("logs", "Show recent log lines.")
    lg.add_argument("source", choices=logs.SOURCES)
    lg.add_argument("--lines", type=int, default=200)
    sv = cmd("ssh-trust", "Install SSH certificates and CA keys from FleetLedger (stdin).")
    sv.add_argument("--file", required=True, metavar="-", help="read the document from stdin")
    cmd("net", "Show where this Ship connects: the 7-day network visibility summary.")
    cmd("version", "Print the tooling version and contract versions.")
    cmd("grow-root", "Grow / to fill its disk (after the disk was grown).")
    o = cmd("overlay", "Join or leave the overlay network (the auth key comes on stdin).")
    o.add_argument("action", choices=["join", "leave", "status"])
    o.add_argument("--hostname", help="join: the Ship's name on the overlay")
    o.add_argument("--tag", action="append", default=[], help="join: overlay tag (repeatable)")
    o.add_argument("--login-server", help="join: Headscale (or other) coordination server")
    ib = cmd("inbox", "List, receive (stdin), remove or announce files in the agent's ~/inbox.")
    ib.add_argument("action", choices=["list", "put", "remove", "notify"])
    ib.add_argument("names", nargs="*", metavar="NAME", help="remove: file names in the inbox")
    ib.add_argument("--name", help="put: the file's name (cleaned up; never overwrites)")
    ib.add_argument("--size", type=int, help="put: its exact size in bytes")
    ib.add_argument("--count", type=int, help="notify: how many files arrived")
    r = sub.add_parser("refresh", help="(internal) refresh the status cache and react to sign-ins")
    r.add_argument("--json", action="store_true")
    g = sub.add_parser("lan-guard", help="(internal) reload the LAN guard rules (boot unit)")
    g.add_argument("--json", action="store_true")
    return p


# --- commands ------------------------------------------------------------------
def require_root(ctx: Context) -> None:
    if not ctx.paths.sandbox and os.geteuid() != 0:
        raise CtlError("not-root", "ship-ctl needs root; run it with sudo")


def cmd_version(ctx: Context, args) -> dict:
    manifest = read_json(ctx.paths.bundle_json, {})
    manifest = manifest if isinstance(manifest, dict) else {}
    tooling = manifest.get("tooling_version") or ctx.versions.get("shipshape", {}).get("tooling")
    data = {
        "tooling": tooling or "0.0.0",
        "commit": str(manifest.get("commit", "unknown"))[:12],
        "contracts": dict(CONTRACTS),
    }
    return result("version", data=data, messages=[f"ship-ctl {data['tooling']} ({data['commit']})"])


def cmd_apply(ctx: Context, args) -> dict:
    ship = shiptoml.load(ctx.paths)
    changed, messages = apply.apply(ctx, ship, dry_run=args.dry_run)
    if args.dry_run:
        messages.insert(0, "Dry run: nothing was written.")
    else:
        messages.insert(0, "Applied." if changed else "Nothing to change.")
    return result("apply", changed=changed, messages=messages)


def _coerce(key: str, kind: str, raw: str) -> object:
    if kind == "bool":
        if raw not in ("true", "false"):
            raise CtlError("usage", f"{key} must be true or false")
        return raw == "true"
    if kind in ("tags", "list"):
        return [t.strip() for t in raw.split(",") if t.strip()]
    return raw


AUTOSTART_KEYS = ("autostart.add", "autostart.remove")


def _autostart_change(doc: dict, key: str, raw: str) -> None:
    """autostart.add=NAME:CWD[:MODE] adds an [[autostart]] agent (ship-ctl autostart then
    starts it); autostart.remove=NAME drops one (a running agent keeps running)."""
    entries = doc.setdefault("autostart", [])
    if key == "autostart.remove":
        kept = [e for e in entries if e.get("name") != raw]
        if len(kept) == len(entries):
            raise CtlError("usage", f"no autostart agent named {raw!r}")
        doc["autostart"] = kept
        return
    parts = raw.split(":")
    mode = parts.pop() if len(parts) > 2 and parts[-1] in PERMISSION_MODES else None
    name, cwd = parts[0], ":".join(parts[1:])
    repo = None
    if len(parts) == 3 and parts[1] == "github":  # NAME:github:OWNER/REPO
        repo = parts[2]
        cwd = "~/work/" + repo.rsplit("/", 1)[-1]
    if not name or not cwd:
        raise CtlError("usage", "autostart.add=NAME:CWD[:MODE] or NAME:github:OWNER/REPO[:MODE]")
    if any(e.get("name") == name for e in entries):
        raise CtlError("usage", f"an autostart agent named {name!r} already exists")
    entries.append({"name": name, "cwd": cwd, **({"repo": repo} if repo else {}),
                    **({"mode": mode} if mode else {})})


def cmd_set(ctx: Context, args) -> dict:
    current = shiptoml.load(ctx.paths)
    if args.file is not None:
        if args.file != "-" or args.pairs:
            raise CtlError("usage", "use either KEY=VALUE pairs or --file -")
        new = shiptoml.parse(sys.stdin.read(), ctx.paths).doc
        if new["ship"]["name"] != current.name or new["ship"]["id"] != current.id:
            raise CtlError("invalid-ship-toml", "ship.name and ship.id must not change")
    else:
        if not args.pairs:
            raise CtlError("usage", "nothing to set: give KEY=VALUE pairs or --file -")
        new = json.loads(json.dumps(current.doc))
        for pair in args.pairs:
            key, sep, raw = pair.partition("=")
            if sep and key in AUTOSTART_KEYS:
                _autostart_change(new, key, raw)
                continue
            if not sep or key not in SET_KEYS:
                raise CtlError("usage", f"cannot set {key!r}; keys: "
                               f"{', '.join([*SET_KEYS, *AUTOSTART_KEYS])}")
            table, field = key.split(".")
            new.setdefault(table, {})[field] = _coerce(key, SET_KEYS[key], raw)
        errors = shiptoml.check(new, ctx.paths)
        if errors:
            raise CtlError("invalid-ship-toml", "; ".join(errors[:5]))
    zone = new["ship"].get("timezone")
    if zone and zone != current.doc["ship"].get("timezone") and not shiptoml.zone_installed(
            ctx.paths, zone):
        raise CtlError("invalid-ship-toml",
                       f"ship.timezone: {zone!r} is not a time zone installed on this Ship")
    _check_listen(ctx, current, new)
    changed = []
    if shiptoml.save(new, ctx.paths.ship_toml):
        changed.append(ctx.paths.agent_path(ctx.paths.ship_toml))
    new_ship = shiptoml.ShipToml(new)
    applied, messages = apply.apply(ctx, new_ship)
    messages = messages or ["Saved and applied."]
    if (current.claude["remote_control"] == "on") != (new_ship.claude["remote_control"] == "on"):
        messages.append(RC_CHANGE_NOTE.format(ship=new_ship.name))
    if current.claude["session_prefix"] != new_ship.claude["session_prefix"]:
        messages.append(PREFIX_CHANGE_NOTE.format(ship=new_ship.name))
    return result("set", changed=changed + applied, messages=messages)


def _check_listen(ctx: Context, current: shiptoml.ShipToml, new: dict) -> None:
    """ssh_listen = overlay only once the Ship is on the overlay: never a lock-out."""
    wanted = new.get("network", {}).get("ssh_listen", "any")
    if wanted == "overlay" and overlay.ssh_listen(current) != "overlay":
        if not overlay.joined(ctx) or not overlay.read_state(ctx).get("addresses"):
            raise CtlError("overlay-not-joined",
                           "join the overlay first (ship-ctl overlay join); until then sshd "
                           "keeps accepting logins on every interface")


def cmd_overlay(ctx: Context, args) -> dict:
    ship = shiptoml.load(ctx.paths)
    if args.action == "status":
        return result("overlay", data={"overlay": overlay.status(ctx, ship)})
    if args.action == "join":
        if not args.hostname or not args.tag:
            raise CtlError("usage", "overlay join needs --hostname and at least one --tag")
        key = overlay.read_key(sys.stdin)
        data, changed, messages = overlay.join(ctx, ship, hostname=args.hostname,
                                               tags=args.tag, login_server=args.login_server,
                                               key=key)
        return result("overlay", changed=changed, messages=messages, data={"overlay": data})
    changed: list[str] = []
    messages: list[str] = []
    if overlay.ssh_listen(ship) == "overlay":
        # Listen everywhere again first, so leaving can never lock the Ship out.
        doc = json.loads(json.dumps(ship.doc))
        doc["network"]["ssh_listen"] = "any"
        if shiptoml.save(doc, ctx.paths.ship_toml):
            changed.append(ctx.paths.agent_path(ctx.paths.ship_toml))
        ship = shiptoml.ShipToml(doc)
        did, _ = overlay.apply_listen(ctx, ship)
        if did:
            changed.append(ctx.paths.agent_path(overlay.listen_conf_path(ctx)))
        messages.append("network.ssh_listen is back to any.")
    left, notes = overlay.leave(ctx, ship)
    return result("overlay", changed=changed + left, messages=notes + messages,
                  data={"overlay": overlay.status(ctx, ship)})


def cmd_inbox(ctx: Context, args) -> dict:
    """The file inbox (inbox.py): FleetLedger streams a file to `put` on stdin."""
    if args.action != "remove" and args.names:
        raise CtlError("usage", f"inbox {args.action} takes no file names")
    if args.action == "list":
        return result("inbox", data=inbox.listing(ctx))
    if args.action == "put":
        if not args.name or args.size is None:
            raise CtlError("usage", "inbox put needs --name and --size (the bytes come on stdin)")
        data, changed, messages = inbox.put(ctx, args.name, args.size, sys.stdin.buffer)
        return result("inbox", changed=changed, messages=messages, data=data)
    if args.action == "remove":
        if not args.names:
            raise CtlError("usage", "inbox remove needs at least one file name")
        data, changed, messages = inbox.remove(ctx, args.names)
        return result("inbox", changed=changed, messages=messages, data=data)
    if args.count is None:
        raise CtlError("usage", "inbox notify needs --count")
    data, messages = inbox.notify(ctx, shiptoml.load(ctx.paths), args.count)
    return result("inbox", messages=messages, data=data)


def cmd_status(ctx: Context, args) -> dict:
    ship = shiptoml.load(ctx.paths)
    doc = status.build(ctx, ship)
    if args.write:
        status.write_cache(ctx.paths.p(args.write) if ctx.paths.sandbox else Path(args.write), doc)
    return doc


def cmd_autostart(ctx: Context, args) -> dict:
    ship = shiptoml.load(ctx.paths)
    state = status.build(ctx, ship)["state"]
    data, changed, messages = autostart.run(ctx, ship, state)
    return result("autostart", changed=changed, messages=messages, data=data)


def cmd_agent(ctx: Context, args) -> dict:
    ship = shiptoml.load(ctx.paths)
    if args.action == "forget":
        if args.everyone or args.mode or args.fresh or args.force:
            raise CtlError("usage", "agent forget takes only names")
        data, changed, messages = agents.forget(ctx, args.names)
        return result("agent", changed=changed, messages=messages, data=data)
    data, changed, messages = agents.restart(ctx, ship, args.names, everyone=args.everyone,
                                             mode=args.mode, fresh=args.fresh,
                                             force=args.force)
    return result("agent", changed=changed, messages=messages, data=data)


def cmd_notify_signin(ctx: Context, args) -> dict:
    ship = shiptoml.load(ctx.paths)
    data, messages = notice.notify(ctx, ship, args.service)
    return result("notify-signin", messages=messages, data=data)


def cmd_converge(ctx: Context, args) -> dict:
    data = converge.converge(ctx, args.bundle)
    return result("converge", changed=[data["to"]], data=data, messages=[f"Now on {data['to']}."])


def cmd_tools(ctx: Context, args) -> dict:
    ship = shiptoml.load(ctx.paths)
    if args.action == "list":
        if args.ids:
            raise CtlError("usage", "tools list takes no ids")
        installed, _ = status.tool_states(ctx, ship)
        return result("tools", data={"installed": installed, "available": tools.available(ctx)})
    if args.action == "run":
        data, changed, messages = tools.run(ctx, ship, args.ids or None)
        return result("tools", changed=changed, messages=messages, data=data)
    if not args.ids:
        raise CtlError("usage", f"tools {args.action} needs at least one id")
    known = set(tools.available(ctx))
    unknown = [i for i in args.ids if i not in known]
    if unknown and args.action == "add":
        raise CtlError("usage", f"not in the catalogue: {', '.join(unknown)}")
    doc = json.loads(json.dumps(ship.doc))
    install = doc.setdefault("tools", {}).setdefault("install", [])
    if args.action == "add":
        install += [i for i in args.ids if i not in install]
    else:
        doc["tools"]["install"] = [i for i in install if i not in args.ids]
    errors = shiptoml.check(doc, ctx.paths)
    if errors:
        raise CtlError("invalid-ship-toml", "; ".join(errors[:5]))
    changed = []
    if shiptoml.save(doc, ctx.paths.ship_toml):
        changed.append(ctx.paths.agent_path(ctx.paths.ship_toml))
    ship = shiptoml.ShipToml(doc)
    if args.action == "add":
        data, ran, messages = tools.run(ctx, ship, args.ids)
        return result("tools", changed=changed + ran, messages=messages, data=data)
    return result(
        "tools",
        changed=changed,
        messages=["Stopped managing: " + ", ".join(args.ids) + " (not uninstalled)."],
    )


def cmd_ssh_trust(ctx: Context, args) -> dict:
    if args.file != "-":
        raise CtlError("usage", "ssh-trust reads its document from stdin: --file -")
    ship = shiptoml.load(ctx.paths)
    doc = sshtrust.load_document(ctx, sys.stdin.read(1 << 21))
    data, changed, messages = sshtrust.install(ctx, ship, doc)
    return result("ssh-trust", changed=changed, messages=messages, data=data)


def cmd_net(ctx: Context, args) -> dict:
    data, errors = net.summary(ctx)
    for e in errors:
        ctx.log(e)
    c = data["collector"]
    messages = [f"collector {c['state']}" + (f" ({c['source']})" if c["source"] else "")]
    if c["note"]:
        messages.append(c["note"])
    return result("net", messages=messages, data=data)


def cmd_logs(ctx: Context, args) -> dict:
    lines = logs.read(ctx, args.source, args.lines)
    return result("logs", data={"lines": lines})


def cmd_refresh(ctx: Context, args) -> dict:
    """Run by ship-status.service: write the status cache, then react to sign-ins.

    A service whose state changed to ok since the previous refresh releases the
    tools waiting on it; Claude changing to ok runs autostart once. The previous
    states live in /run (cleared at boot), so the first refresh after a boot
    compares against "not signed in". Then the agent record's upkeep: after a boot,
    that resumes the recorded agents; later, it starts again one that crashed.
    """
    ship = shiptoml.load(ctx.paths)
    doc = status.build(ctx, ship)
    status.write_cache(ctx.paths.status_cache, doc)
    messages = [f"state: {doc['state']}"]
    if doc["state"] == "provisioning":
        return result("status", messages=messages)
    prev_path = ctx.paths.run / "auth-previous.json"
    prev = read_json(prev_path, {})
    prev = prev if isinstance(prev, dict) else {}
    now = {svc: entry["state"] for svc, entry in doc["auth"].items()}
    newly_ok = {svc for svc, st in now.items() if st == "ok" and prev.get(svc) != "ok"}
    record = dict(now)
    waiting = tools.waiting_on(ctx, newly_ok)
    if waiting:
        messages.append(f"signed in: {', '.join(sorted(newly_ok))}; finishing {', '.join(waiting)}")
        try:
            tools.run(ctx, ship, waiting)
        except CtlError as exc:
            messages.append(exc.message)
    retry = tools.failed_profiles(ctx, ship) if newly_ok else []
    if retry:  # e.g. a private repo clone that needed the GitHub sign-in
        states, why = tools.run_profiles(ctx, ship, retry)
        messages.append("profiles retried: " + ", ".join(f"{p} {s}" for p, s in states.items()))
        messages += why
    waited = autostart.waiting_for_github(ctx, ship) if "github" in newly_ok else set()
    if any(e.get("repo") for e in ship.autostart):  # a public repo clones at once
        messages += autostart.prepare_repos(ctx, ship, retry="github" in newly_ok)
    repo_waiting = bool(waited)
    upkept: list[str] = []  # what the agent record's upkeep did (resumes, restarts)
    if ("claude" in newly_ok or repo_waiting) and ship.autostart:
        state = doc["state"]
        if "claude" in newly_ok and state in ("needs-sign-in", "unknown"):
            state = "ready"  # Claude is signed in now; a GitHub sign-in alone never starts
        try:
            data, _, notes = autostart.run(ctx, ship, state,
                                           None if "claude" in newly_ok else waited)
            upkept += [m for m in notes if m.startswith(agent_record.LOG_TAG)]
            if any(s["reason"] == "resume-pending" for s in data["skipped"]):
                record["claude"] = prev.get("claude", "missing")  # try again next refresh
            elif data["started"]:
                messages.append(f"autostart: started {', '.join(data['started'])}")
        except CtlError as exc:
            messages.append(f"autostart: {exc.message}")
            upkept += [m for m in exc.messages if m.startswith(agent_record.LOG_TAG)]
            record["claude"] = prev.get("claude", "missing")
    if doc["herdr"]["server"] == "running":
        # The agent record: pick up agents started by hand, start again what should
        # run and does not (after a boot, every recorded agent: the resume).
        upkept += agent_record.refresh(ctx, ship, doc["state"])
    messages += upkept
    if upkept or any(m.startswith("autostart: started") for m in messages):
        status.write_cache(ctx.paths.status_cache, status.build(ctx, ship))
    write_atomic(prev_path, json.dumps(record) + "\n", 0o644)
    for m in messages:
        ctx.log(m)
    return result("status", messages=messages)


def cmd_lan_guard(ctx: Context, args) -> dict:
    """Run by ship-lan-guard.service at boot: find the gateways and DNS servers
    again and reload the table (or remove it if the guard is off)."""
    ship = shiptoml.load(ctx.paths)
    changed, messages = languard.sync(ctx, ship, reload=True)
    for m in messages:
        ctx.log(m)
    state = "on" if ship.lan_guard else "off"
    return result("apply", changed=changed, messages=[f"LAN guard {state}.", *messages])


def cmd_grow_root(ctx: Context, args) -> dict:
    """growpart the partition holding /, then resize2fs it, online."""
    import re as _re

    src = ctx.runner.run(["findmnt", "-n", "-o", "SOURCE", "/"], timeout=10).out.strip()
    match = _re.match(r"^/dev/([a-z0-9]+?)p?([0-9]+)$", src)
    disk = ctx.runner.run(["lsblk", "-n", "-o", "PKNAME", src], timeout=10).out.strip()
    if not match or not _re.match(r"^[a-z0-9]+$", disk):
        raise CtlError("internal", f"cannot tell the partition holding / ({src!r})")
    grown = ctx.runner.run(["growpart", f"/dev/{disk}", match.group(2)], timeout=60)
    if grown.rc not in (0, 1):  # 1: NOCHANGE, already as big as the disk
        why = (grown.err or grown.out).strip().splitlines()[-1:] or ["growpart failed"]
        raise CtlError("internal", f"growpart: {why[0][:200]}")
    # Always: the partition may have been grown before (cloud-init, an interrupted
    # run) without its filesystem. resize2fs says when there is nothing to do.
    fs = ctx.runner.run(["resize2fs", src], timeout=300)
    if not fs.ok:
        raise CtlError("internal", f"resize2fs {src} failed")
    changed = grown.rc == 0 or "nothing to do" not in (fs.out + fs.err).lower()
    if not changed:
        return result("grow-root", messages=[f"{src} already fills its disk."],
                      data={"device": src, "grown": False})
    return result("grow-root", changed=[src], messages=[f"Grew {src} to fill its disk."],
                  data={"device": src, "grown": True})


COMMANDS = {
    "grow-root": cmd_grow_root,
    "overlay": cmd_overlay,
    "inbox": cmd_inbox,
    "status": cmd_status,
    "apply": cmd_apply,
    "set": cmd_set,
    "autostart": cmd_autostart,
    "agent": cmd_agent,
    "notify-signin": cmd_notify_signin,
    "converge": cmd_converge,
    "tools": cmd_tools,
    "logs": cmd_logs,
    "version": cmd_version,
    "refresh": cmd_refresh,
    "ssh-trust": cmd_ssh_trust,
    "net": cmd_net,
    "lan-guard": cmd_lan_guard,
}


# Internal commands report under a contract command name (ship-ctl-result.schema.json).
INTERNAL_REPORTS = {"refresh": "status", "lan-guard": "apply"}


def _print_human(doc: dict) -> None:
    if doc.get("schema") == status.SCHEMA:
        print(f"{doc['ship']['name']}: {doc['state']}")
        for item in doc["attention"]:
            print(f"  ! {item['message']}")
        for svc, entry in doc["auth"].items():
            print(
                f"  {svc}: {entry['state']}"
                + (f" ({entry['account']})" if entry["account"] else "")
            )
        for agent in doc["herdr"]["agents"]:
            who = agent["name"] or agent.get("pane")
            print(f"  agent {who}: {agent['state']} {agent['mode'] or ''}")
        for tid, tool in doc["tools"].items():
            print(f"  tool {tid}: {tool['state']}")
        return
    for m in doc["messages"]:
        print(m)
    for c in doc["changed"]:
        print(f"  changed: {c}")
    data = doc.get("data") or {}
    if doc["command"] == "logs":
        print("\n".join(data.get("lines", [])))
    elif doc["command"] == "net" and data:
        t = data["totals"]
        print(f"last 24 h: {t['rx_mb_24h']} MB in, {t['tx_mb_24h']} MB out")
        for d in data["destinations"][:30]:
            progs = ",".join(p["name"] for p in d["programs"][:3])
            print(f"  {d['connections_24h']:>6} {d['connections_7d']:>7}  {d['name']}  ({progs})")
    elif doc["command"] == "inbox" and "files" in data:
        for f in data["files"]:
            print(f"  {f['size']:>12}  {f['modified']}  {f['name']}")
        if not data["files"]:
            print(f"{data['path']} is empty.")
    elif doc["command"] in ("tools", "autostart", "ssh-trust", "agent", "inbox") and data:
        print(json.dumps(data, indent=2))
    if doc["error"]:
        print(f"ship-ctl: {doc['error']['message']}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    first = next((a for a in argv if not a.startswith("-")), None)
    command = first if first in COMMANDS else "status"
    ctx = Context.from_env()
    try:
        try:
            args = build_parser().parse_args(argv)
        except UsageError as exc:
            raise CtlError("usage", str(exc)) from exc
        if args.command is None:
            build_parser().print_help()
            return 2
        command = args.command
        if command != "version":
            require_root(ctx)
        doc = COMMANDS[command](ctx, args)
        code = 0
    except CtlError as exc:
        reported = INTERNAL_REPORTS.get(command, command)
        doc = result(
            reported,
            ok=False,
            changed=exc.changed,
            messages=exc.messages,
            data=exc.data,
            error={"code": exc.code, "message": exc.message},
        )
        code = exc.exit_code
    except Exception as exc:  # anything else still ends in exactly one JSON document
        ctx.log(f"internal error: {type(exc).__name__}: {exc}")
        doc = result(
            INTERNAL_REPORTS.get(command, command),
            ok=False,
            error={"code": "internal", "message": f"{type(exc).__name__}: {exc}"[:1000]},
        )
        code = 1
    if as_json:
        print(json.dumps(doc, indent=2))
    else:
        _print_human(doc)
    return code
