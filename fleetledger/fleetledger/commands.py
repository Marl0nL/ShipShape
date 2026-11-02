"""The ship-fleet commands (see cli.py for their options and help)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from fleetledger import concierge, enrol, paths, schemas, status
from fleetledger.catalogue import Catalogue
from fleetledger.concierge import providers
from fleetledger.drivers import SeedFile
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Inventory, Ship, new_ship_id, now_timestamp
from fleetledger.seed import Autostart, SeedOptions, cloudinit, hostkey, make_seed
from fleetledger.seed.shiptoml import tools_for
from fleetledger.shipctl import STATUS_SCHEMA, ShipCtl
from fleetledger.ssh import ship_ctl
from fleetledger.store import new_inventory, ship_entry

MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
LOG_SOURCES = ("provision", "herdr", "resume", "tools", "status", "system")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
TAG_RE = NAME_RE

# `ship-fleet set` short keys -> ship-ctl set keys, with allowed values.
SET_KEYS: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "remote_control": ("claude.remote_control", ("on", "off", "blocked")),
    "default_mode": ("claude.default_mode", MODES),
    "allow_bypass": ("claude.allow_bypass", ("true", "false")),
    "update_channel": ("claude.update_channel", ("latest", "stable")),
    "session_prefix": ("claude.session_prefix", None),
    "sudo": ("agent.sudo", ("true", "false")),
    "lan_guard": ("network.lan_guard", ("true", "false")),
    "tags": ("ship.tags", None),
}


# --- shared helpers --------------------------------------------------------


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    lines = ["  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
             for row in [headers, *rows]]
    return "\n".join(lines)


def _tilde(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(path.resolve().relative_to(home.resolve()))
    except ValueError:
        return str(path)


def _ssh(app, inventory: Inventory):
    return app.ssh_factory(inventory)


AUTH_LABELS = {
    "ok": "OK",
    "missing": "Missing",
    "expiring": "Expiring",
    "expired": "Expired",
    "error": "Error",
    "unknown": "?",
}


def auth_cell(view: status.ShipView, service: str) -> str:
    entry = view.auth(service)
    if entry is None:
        return "-"
    if any(a["code"] == "signin-wrong-account" and a.get("subject") == service
           for a in view.attention):
        return "Wrong account"
    return AUTH_LABELS.get(entry["state"], entry["state"])


def agents_cell(view: status.ShipView) -> str:
    if view.status is None:
        return "-"
    agents = view.status["herdr"]["agents"]
    if not agents:
        return "none"
    counts: dict[str, int] = {}
    for agent in agents:
        label = {"blocked": "waiting"}.get(agent["state"], agent["state"])
        counts[label] = counts.get(label, 0) + 1
    return " · ".join(f"{n} {label}" for label, n in counts.items())


def usage_cell(view: status.ShipView) -> str:
    if view.status is None:
        return "-"
    host = view.status["host"]
    cpu = min(100, round(100 * host["load1"] / max(1, host["cpus"])))
    return f"{cpu}% · {round(host['mem_used_pct'])}% · {round(host['disk_used_pct'])}%"


def notes_cell(view: status.ShipView) -> str:
    if view.problem:
        return view.message or view.problem
    if view.attention:
        first = view.attention[0]["message"]
        more = len(view.attention) - 1
        return first + (f" (+{more} more)" if more else "")
    return ""


def view_json(view: status.ShipView, tags: tuple[str, ...]) -> dict[str, Any]:
    return {
        "name": view.ship.name,
        "tags": list(tags),
        "state": view.state,
        "problem": view.problem,
        "message": view.message,
        "host": view.host,
        "attention": view.attention,
        "status": view.status,
        "cloud_init": view.cloud_init,
    }


# --- init ----------------------------------------------------------------


def cmd_init(args, app) -> int:
    store = app.store_factory()
    if store.exists():
        raise FleetError(f"FleetLedger is already set up ({store.path}).")
    admin = paths.expand(args.admin_key)
    if not admin.exists():
        paths.private_dir(admin.parent)
        command = ["ssh-keygen", "-t", "ed25519", "-C", "fleetledger-admin", "-f", str(admin)]
        if args.no_passphrase:
            command += ["-N", ""]
        else:
            app.note("Choose a passphrase for the fleet admin key (ssh-agent will hold it).")
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise FleetError("ssh-keygen could not make the fleet admin key.") from exc
        app.say(f"Made the fleet admin key {admin}.")
    public = admin.with_name(admin.name + ".pub")
    cloudinit.read_public_keys(public, "the fleet admin public key")

    agent_keys = [paths.expand(k) for k in args.agent_key]
    if not agent_keys:
        default = Path.home() / ".ssh" / "id_ed25519.pub"
        if not default.exists():
            raise ConfigError(
                "Give the public key you will attach to Ships with: --agent-key PATH.pub"
            )
        agent_keys = [default]
    for key in agent_keys:
        cloudinit.read_public_keys(key, "an agent key")

    store.create(new_inventory(_tilde(admin), [_tilde(k) for k in agent_keys]))
    app.say(f"Wrote {store.path}.")
    app.say("Next:")
    app.say(f"  ssh-add {_tilde(admin)}                 # FleetLedger uses ssh-agent")
    app.say(f"  edit {_tilde(store.path)}   # tags, expected accounts, [drivers.proxmox]")
    app.say("  ship-fleet new ship-01 --tag work")
    return 0


# --- new -----------------------------------------------------------------


def parse_autostart(values: list[str]) -> list[Autostart]:
    agents = []
    for value in values:
        parts = value.split(":")
        if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
            raise FleetError(f"--autostart {value}: use NAME:CWD or NAME:CWD:MODE.")
        mode = parts[2] if len(parts) == 3 else None
        if mode is not None and mode not in MODES:
            raise FleetError(f"--autostart {value}: unknown mode '{mode}'.")
        agents.append(Autostart(parts[0], parts[1], mode))
    return agents


def cmd_new(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    name = args.name
    if not NAME_RE.match(name):
        raise FleetError(f"'{name}' is not a valid Ship name (a-z, 0-9 and -, up to 31).")
    if inv.has_ship(name):
        raise FleetError(f"{name} is already in the inventory.")
    repo = app.repo_factory()
    catalogue = Catalogue.from_repo(repo)

    claude: dict[str, Any] = {}
    if args.mode:
        claude["default_mode"] = args.mode
    if args.allow_bypass or args.mode == "bypassPermissions":
        claude["allow_bypass"] = True
    size = {k: v for k, v in (("cores", args.cores), ("memory_mb", args.memory_mb),
                               ("disk_gb", args.disk_gb)) if v is not None}
    driver_id = args.driver or inv.defaults.get("driver", "manual")
    draft = Ship(
        name=name,
        id=new_ship_id(),
        driver=driver_id,
        names=tuple(args.host or [f"{name}.local"]),
        host_key="ssh-ed25519 AAAA",  # replaced by the generated key below
        tags=tuple(dict.fromkeys(args.tag)) or None,  # none given: defaults.tags
        created=now_timestamp(),
        size=size,
        claude=claude,
        profiles=tuple(args.profile) or None,
    )
    if args.tools is not None or args.no_default_tools:
        base = [] if args.no_default_tools else tools_for(inv, draft, catalogue)
        extra = [t.strip() for t in (args.tools or "").split(",") if t.strip()]
        draft = Ship(**{**draft.__dict__, "tools": tuple(dict.fromkeys([*base, *extra]))})

    options = SeedOptions(
        autostart=parse_autostart(args.autostart),
        agent_sudo=True if args.agent_sudo else None,
    )
    seed = make_seed(inv, draft, repo, options)
    ref: dict[str, Any] = {"seed": seed.instance_id}
    if args.vmid is not None:
        ref["vmid"] = args.vmid
    ship = Ship(**{**draft.__dict__, "host_key": seed.host_public_key, "driver_ref": ref})
    with store.edit() as ed:
        ed.add_ship(name, ship_entry(ship))
    app.say(f"{name}: seed written, host key {seed.host_fingerprint} pinned.")

    driver = app.driver_factory(store.load(), driver_id,
                                ubuntu_release=repo.versions["ubuntu"]["release"])
    try:
        outcome = driver.create(ship, SeedFile(seed.path, seed.instance_id),
                                inv.effective_size(ship))
    except BaseException:
        with store.edit() as ed:
            ed.remove_ship(name)
        seed.path.unlink(missing_ok=True)
        raise
    with store.edit() as ed:
        ed.set_field(name, "driver_ref", dict(outcome.driver_ref))
    for message in outcome.messages:
        app.say(message)
    if outcome.instructions:
        app.say(f"\nSeed ISO: {seed.path}")
        app.say("Create the VM:")
        for line in outcome.instructions:
            app.say(f"  {line}")
    if not outcome.done or args.no_wait:
        app.say(f"\nWhen it has booted: ship-fleet status {name} --wait")
        return 0
    _reachable_name(app, store, driver, name)
    return _enrol(app, store, name)


def _reachable_name(app, store, driver, name: str, timeout: float = 900) -> None:
    """Make sure FleetLedger can reach a Ship the driver just started.

    Once the hypervisor reports the Ship's address, its names are checked. If
    none resolves on this computer (for example .local names without mDNS
    name resolution), the address is added as the last name to try.
    """
    ship = store.load().ship(name)
    waited = 0.0
    while True:
        addresses = driver.status(ship).addresses
        if addresses:
            break
        if waited >= timeout:
            app.note(f"{name}: the hypervisor reported no address; trying its names only.")
            return
        app.sleep(10)
        waited += 10
    if any(app.resolves(host) for host in ship.names):
        return
    with store.edit() as ed:
        ed.set_field(name, "names", [*ship.names, addresses[0]])
    app.note(f"{name}: {', '.join(ship.names)} does not resolve on this computer, so "
             f"{addresses[0]} is added as a fallback name (see fleetledger/README.md, "
             "\"Names\").")


def _enrol(app, store, name: str, say=None) -> int:
    say = say or app.say
    inv = store.load()
    ship = inv.ship(name)
    driver = app.driver_factory(inv, ship.driver)
    last = {"state": None}

    def progress(view):
        if view.state != last["state"]:
            last["state"] = view.state
            app.note(f"{name}: {view.state}")

    result = enrol.enrol(store, _ssh(app, inv), driver, ship, on_update=progress)
    for message in result.messages:
        say(message)
    if result.instructions:
        say("Then remove the seed (it holds the Ship's private host key):")
        for line in result.instructions:
            say(f"  {line}")
    say(f"Next: ship-fleet login {name} all")
    return 0


# --- adopt and scan ----------------------------------------------------------


def cmd_adopt(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    name = args.name
    if inv.has_ship(name) and not args.replace:
        raise FleetError(f"{name} is already in the inventory.",
                         hint="Use --replace to re-pin a rebuilt Ship.")
    host = args.host or (inv.ship(name).names[0] if inv.has_ship(name) else f"{name}.local")
    key = app.keyscan(host)
    fingerprint = hostkey.fingerprint(key)
    app.say(f"{host} presents host key {fingerprint}")
    if args.fingerprint:
        if args.fingerprint.strip() != fingerprint:
            raise FleetError("That is not the fingerprint you gave; not adopting it.")
    else:
        if not app.stdin_isatty():
            raise FleetError("Compare the fingerprint with the VM console, then rerun with "
                             "--fingerprint SHA256:...")
        answer = app.confirm("Does it match the fingerprint on the VM console? Type yes: ")
        if answer.strip().lower() != "yes":
            raise FleetError("Not adopted.")

    # Ask the Ship who it is, over a connection pinned to the key just checked.
    probe = Ship(name=name, id=new_ship_id(), driver="manual", names=(host,),
                 host_key=f"{key} {name}")
    doc = dict(inv.doc)
    doc["ship"] = {**{k: v for k, v in inv.doc.get("ship", {}).items() if k != name},
                   name: ship_entry(probe)}
    result = _ssh(app, Inventory(doc)).run(probe, ship_ctl("status", "--json"), timeout=45)
    try:
        report = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise FleetError(f"{host} did not answer ship-ctl status; is it a Ship?") from exc
    if report.get("schema") != STATUS_SCHEMA or not schemas.is_valid("ship-status", report):
        raise FleetError(f"{host} did not give a valid status document.")
    if report["ship"]["name"] != name:
        raise FleetError(f"{host} says it is {report['ship']['name']}, not {name}.")

    tags = args.tag or report["ship"]["tags"]
    with store.edit() as ed:
        if inv.has_ship(name):
            ed.set_field(name, "host_key", f"{key} {name}")
            ed.set_field(name, "id", report["ship"]["id"])
            if args.host:
                ed.set_field(name, "names", [host])
            if args.tag:
                ed.set_tags(name, tags)
        else:
            entry = ship_entry(Ship(name=name, id=report["ship"]["id"], driver="manual",
                                    names=(host,), host_key=f"{key} {name}", tags=tuple(tags)))
            ed.add_ship(name, entry)
        ed.mark_enrolled(name, now_timestamp())
    app.say(f"{'Re-pinned' if inv.has_ship(name) else 'Adopted'} {name} "
            f"({report['ship']['id']}), state {report['state']}.")
    return 0


def cmd_scan(args, app) -> int:
    inv = app.store_factory().load()
    found = app.browse(args.timeout)
    if not found:
        app.say("No Ships announced themselves (announcements are hints; "
                "a Ship may still be there).")
        return 0
    by_id = {s.id: s for s in inv.ships()}
    rows = []
    for a in found:
        known = by_id.get(a.ship_id) if a.ship_id else None
        if known is None and inv.has_ship(a.name):
            known = inv.ship(a.name)
            verdict = "known name, different id" if a.ship_id and a.ship_id != known.id else "known"
        elif known is not None:
            verdict = "known" if known.name == a.name else f"known as {known.name}"
        else:
            verdict = f"new: ship-fleet adopt {a.name} --host {a.host}"
        rows.append([a.name, a.host, ", ".join(a.addresses) or "-", verdict])
    app.say(table(rows, ["SHIP", "HOST", "ADDRESSES", "INVENTORY"]))
    return 0


# --- ls and status -----------------------------------------------------------


def _views(app, inv: Inventory, ships: list[Ship], offline: bool) -> list[status.ShipView]:
    if offline:
        views = []
        for ship in ships:
            doc = status.read_cache(ship.name)
            view = status.ShipView(ship, status=doc)
            if doc is None:
                view.problem, view.message = "unknown", "no cached status"
            else:
                view.attention = list(doc["attention"]) + status.fleet_attention(
                    inv, ship, doc, None)
            views.append(view)
        return views
    tooling = app.repo_factory().tooling_version
    return status.poll(_ssh(app, inv), inv, ships, tooling=tooling)


def cmd_ls(args, app) -> int:
    inv = app.store_factory().load()
    ships = inv.ships(args.tag)
    views = _views(app, inv, ships, args.offline)
    if args.json:
        app.say(json.dumps([view_json(v, inv.effective_tags(v.ship)) for v in views], indent=2))
        return 0
    if not views:
        app.say("No Ships" + (f" with tag {args.tag}." if args.tag else " yet."))
        return 0
    rows = [
        [v.ship.name, ",".join(inv.effective_tags(v.ship)) or "-", v.state, agents_cell(v),
         auth_cell(v, "claude"), auth_cell(v, "gcloud"), auth_cell(v, "gcloud_adc"),
         auth_cell(v, "github"), usage_cell(v), notes_cell(v)]
        for v in views
    ]
    app.say(table(rows, ["SHIP", "TAGS", "STATE", "AGENTS", "CLAUDE", "GCLOUD", "ADC",
                         "GITHUB", "CPU·MEM·DISK", "NOTES"]))
    return 0


def cmd_status(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    ship = inv.ship(args.name)
    if args.wait:
        if ship.enrolled is None:
            _enrol(app, store, ship.name, say=app.note if args.json else None)
        else:
            enrol.wait_for_provisioning(_ssh(app, inv), inv, ship)
        inv = store.load()
        ship = inv.ship(args.name)
    view = _views(app, inv, [ship], offline=False)[0]
    if args.json:
        app.say(json.dumps(view_json(view, inv.effective_tags(ship)), indent=2))
        return 0 if view.problem is None else 1
    app.say(f"{ship.name}  [{', '.join(inv.effective_tags(ship)) or 'no tags'}]  {view.state}")
    if view.problem:
        app.say(f"  {view.message}")
        return 1
    for item in view.attention:
        app.say(f"  ! {item['message']}")
    if view.status is None:
        app.say(f"  cloud-init: {view.cloud_init.get('status', 'unknown')}")
        return 0
    doc = view.status
    app.say("Sign-ins:")
    for service in doc["auth"]:
        entry = doc["auth"][service]
        expected = inv.expected_account(ship, service)
        wanted = " or ".join(sorted(expected.accounts))
        want = f" (tag expects {wanted})" if wanted else ""
        app.say(f"  {service:<11} {auth_cell(view, service):<14} {entry['account'] or ''}{want}")
    app.say("Agents:")
    for agent in doc["herdr"]["agents"] or []:
        app.say(f"  {agent['name'] or '-':<12} {agent['state']:<8} {agent['mode'] or ''}")
    if not doc["herdr"]["agents"]:
        app.say("  none")
    host = doc["host"]
    app.say(f"Host: {usage_cell(view)} (cpu · memory · disk), up {host['uptime_s'] // 3600} h"
            + (", reboot required" if host["reboot_required"] else ""))
    app.say(f"Claude {doc['claude']['version'] or '?'} ({doc['claude']['channel']}), "
            f"default mode {doc['claude']['default_mode']}, "
            f"Remote Control {doc['claude']['remote_control']}; tooling {doc['ship']['tooling']}")
    if doc["tools"]:
        app.say("Tools: " + ", ".join(f"{k} {v['state']}" for k, v in sorted(doc["tools"].items())))
    return 0


# --- login and logout ---------------------------------------------------------


def cmd_login(args, app) -> int:
    *names, service = args.args
    if args.tag and names:
        raise FleetError("Give Ship names or --tag, not both.")
    if not args.tag and not names:
        raise FleetError("Which Ship? Give its name, or --tag TAG.")
    chosen = providers.resolve(service)
    inv = app.store_factory().load()
    ships = inv.select(names, args.tag)
    ssh = _ssh(app, inv)
    failures = 0
    for ship in ships:
        for provider in chosen:
            prompter = app.prompter_factory(ship.name, provider.label, args.open)
            options = {"spawner": app.spawner} if app.spawner else {}
            result = concierge.login(ssh, inv, ship, provider, prompter, **options)
            failures += _report_login(app, result)
    return 1 if failures else 0


def _report_login(app, result: concierge.LoginResult) -> int:
    head = f"{result.ship} {result.provider}:"
    if result.outcome == "ok":
        accounts = ", ".join(f"{c.service} {c.account}" for c in result.checks if c.account)
        app.say(f"{head} signed in" + (f" ({accounts})" if accounts else "") + ".")
    elif result.outcome == "wrong-account":
        for check in result.checks:
            if check.matches is False:
                wanted = " or ".join(check.expected)
                app.say(f"{head} WRONG ACCOUNT: {check.service} is {check.account}, "
                        f"the Ship's tags expect {wanted}.")
        app.say(f"  Sign it out with: ship-fleet logout {result.ship} {result.provider}")
    else:
        app.say(f"{head} {result.outcome}." + (f" {result.detail}" if result.detail else ""))
        for check in result.checks:
            if not check.signed_in:
                app.say(f"  {check.service}: not signed in")
    for note in result.notes:
        app.say(f"  note: {note}")
    return 0 if result.outcome == "ok" else 1


def cmd_logout(args, app) -> int:
    inv = app.store_factory().load()
    ship = inv.ship(args.name)
    ssh = _ssh(app, inv)
    failed = 0
    for provider in providers.resolve(args.service):
        if concierge.logout(ssh, ship, provider):
            app.say(f"{ship.name}: signed out of {provider.label}.")
        else:
            app.say(f"{ship.name}: signing out of {provider.label} failed.")
            failed += 1
    return 1 if failed else 0


# --- set, tag, tools ----------------------------------------------------------


def parse_assignments(values: list[str]) -> list[str]:
    """`ship-fleet set` arguments as ship-ctl set KEY=VALUE pairs."""
    out: dict[str, str] = {}
    full_names = {full: (full, allowed) for full, allowed in SET_KEYS.values()}
    for value in values:
        key, sep, val = value.partition("=")
        if not sep:
            raise FleetError(f"'{value}' is not KEY=VALUE.")
        full, allowed = SET_KEYS.get(key) or full_names.get(key) or (None, None)
        if full is None:
            keys = ", ".join(SET_KEYS)
            raise FleetError(f"Unknown setting '{key}' (settings: {keys}).")
        if allowed and val not in allowed:
            raise FleetError(f"{key} must be one of: {', '.join(allowed)}.")
        if full == "ship.tags" and not all(TAG_RE.match(t) for t in val.split(",") if t):
            raise FleetError(f"Invalid tags '{val}'.")
        out[full] = val
    if out.get("claude.default_mode") == "bypassPermissions":
        out.setdefault("claude.allow_bypass", "true")
    return [f"{k}={v}" for k, v in out.items()]


def cmd_set(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    ship = inv.ship(args.name)
    pairs = parse_assignments(args.assignments)
    result = ShipCtl(_ssh(app, inv), ship).set(pairs)
    # Keep the inventory in step, so a later seed for this Ship matches.
    with store.edit() as ed:
        for pair in pairs:
            key, _, value = pair.partition("=")
            section, _, field_name = key.partition(".")
            if section == "claude":
                parsed: Any = {"true": True, "false": False}.get(value, value)
                ed.set_nested(ship.name, "claude", field_name, parsed)
            elif key == "ship.tags":
                ed.set_tags(ship.name, [t for t in value.split(",") if t])
    app.say(f"{ship.name}: " + (", ".join(result.changed) or "no change") + ".")
    for message in result.messages:
        app.say(f"  {message}")
    return 0


def cmd_tag(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    ship = inv.ship(args.name)
    if not args.changes:
        app.say(" ".join(inv.effective_tags(ship)) or "(no tags)")
        return 0
    tags = list(inv.effective_tags(ship))
    for change in args.changes:
        if change == "none":
            tags = []
            continue
        op, tag = (change[0], change[1:]) if change[:1] in "+-" else ("+", change)
        if not TAG_RE.match(tag):
            raise FleetError(f"'{tag}' is not a valid tag.")
        if op == "+" and tag not in tags:
            tags.append(tag)
        elif op == "-" and tag in tags:
            tags.remove(tag)
    with store.edit() as ed:
        ed.set_tags(ship.name, tags)
    app.say(f"{ship.name}: tags {' '.join(sorted(tags)) or '(none)'}.")
    if args.local:
        return 0
    try:
        ShipCtl(_ssh(app, store.load()), ship).set([f"ship.tags={','.join(sorted(tags))}"])
    except FleetError as exc:
        app.note(f"The inventory is updated, but the Ship was not: {exc.message}")
        return 1
    return 0


def cmd_tools(args, app) -> int:
    catalogue = Catalogue.from_repo(app.repo_factory())
    if args.action == "ls":
        if args.rest:
            raise FleetError("tools ls takes no arguments.")
        rows = [[r.id, r.kind, "yes" if r.default else "", ",".join(r.needs) or "-",
                 r.description] for r in catalogue.recipes.values()]
        app.say(table(rows, ["TOOL", "KIND", "DEFAULT", "NEEDS", "DESCRIPTION"])
                if rows else "The catalogue (ship/tools/) is empty.")
        if catalogue.profiles:
            app.say("")
            app.say(table([[p.id, ",".join(p.tools), p.description]
                           for p in catalogue.profiles.values()],
                          ["PROFILE", "TOOLS", "DESCRIPTION"]))
        return 0
    if len(args.rest) < 2:
        raise FleetError(f"Usage: ship-fleet tools {args.action} SHIP ID...")
    name, *ids = args.rest
    catalogue.check_tools(ids)
    store = app.store_factory()
    inv = store.load()
    ship = inv.ship(name)
    result = ShipCtl(_ssh(app, inv), ship).tools(args.action, ids)
    current = tools_for(inv, ship, catalogue)
    if args.action == "add":
        updated = list(dict.fromkeys([*current, *ids]))
    else:
        updated = [t for t in current if t not in ids]
    with store.edit() as ed:
        ed.set_field(ship.name, "tools", updated)
    app.say(f"{ship.name}: tools {', '.join(updated) or '(none)'}.")
    for message in result.messages:
        app.say(f"  {message}")
    return 0


# --- ssh and logs --------------------------------------------------------------


def cmd_ssh(args, app) -> int:
    inv = app.store_factory().load()
    ship = inv.ship(args.name)
    remote = args.remote
    user = "fleet" if args.user == "fleet" else "agent"
    _ssh(app, inv).exec_interactive(ship, user=user, command=remote)
    return 0  # not reached: exec replaces this process


def cmd_logs(args, app) -> int:
    inv = app.store_factory().load()
    ship = inv.ship(args.name)
    for line in ShipCtl(_ssh(app, inv), ship).logs(args.source, args.lines):
        app.say(line)
    return 0

