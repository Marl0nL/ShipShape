"""The ship-fleet commands (see cli.py for their options and help)."""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from fleetledger import concierge, enrol, ghscopes, images, ops, paths, schemas, signins, status
from fleetledger.catalogue import Catalogue
from fleetledger.concierge import audit, providers
from fleetledger.drivers import SeedFile
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Inventory, Ship, new_ship_id, now_timestamp
from fleetledger.netcmd import cmd_net  # noqa: F401
from fleetledger.seed import Autostart, SeedOptions, cloudinit, hostkey, make_seed
from fleetledger.seed.shiptoml import TIMEZONE_RE, known_timezone, local_timezone, tools_for
from fleetledger.shipctl import STATUS_SCHEMA
from fleetledger.ssh import ship_ctl
from fleetledger.store import new_inventory, ship_entry
from fleetledger.trustcmd import after_enrol, cmd_ca, cmd_trust, cmd_untrust  # noqa: F401

# Kept here for older imports; the definitions live in ops.py.
MODES = ops.MODES
LOG_SOURCES = ops.LOG_SOURCES
NAME_RE = ops.NAME_RE


# --- shared helpers --------------------------------------------------------


def table(rows: list[list[str]], headers: list[str]) -> str:
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    lines = ["  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)).rstrip()
             for row in [headers, *rows]]
    return "\n".join(lines)


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
    if service in signins.off_status_keys(view.ship):
        return "off"  # this Ship does not use it (ship-fleet signins)
    entry = view.auth(service)
    if entry is None:
        return "-"
    if any(a["code"] == "signin-wrong-account" and a.get("subject") == service
           for a in view.attention):
        return "Wrong account"
    if entry["state"] in ("ok", "expiring") and any(
            a["code"] == "signin-scopes-differ" and a.get("subject") == service
            for a in view.attention):
        return "Scopes differ"  # ghscopes: sign in again with the chosen scopes
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
    admin = make_fleet(app, store, args.admin_key, args.agent_key,
                       no_passphrase=args.no_passphrase)
    app.say("Next:")
    app.say(f"  ssh-add {paths.tilde(admin)}                 # FleetLedger uses ssh-agent")
    app.say(f"  edit {paths.tilde(store.path)}   # tags, expected accounts, [drivers.proxmox]")
    app.say("  ship-fleet new ship-01 --tag work")
    return 0


def make_fleet(app, store, admin_key: str, agent_key_values: list[str], *,
               no_passphrase: bool = False, managed: str | None = None) -> Path:
    """The fleet admin key (made if missing) and an empty inventory. Returns the
    admin key's path. managed (keyring | file): FleetLedger makes the key with a
    random passphrase it keeps there (keys.py), and records it as managed."""
    from fleetledger import keys

    if store.exists():
        raise FleetError(f"FleetLedger is already set up ({store.path}).")
    admin = paths.expand(admin_key)
    made_managed = False
    if not admin.exists() and managed:
        keys.make(admin, "fleetledger-admin", managed)
        made_managed = True
        app.say(f"Made the fleet admin key {admin} (FleetLedger keeps its passphrase in "
                f"{keys.describe(managed)}).")
    elif not admin.exists():
        paths.private_dir(admin.parent)
        command = ["ssh-keygen", "-t", "ed25519", "-C", "fleetledger-admin", "-f", str(admin)]
        if no_passphrase:
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

    agent_keys = [paths.expand(k) for k in agent_key_values]
    if not agent_keys:
        default = Path.home() / ".ssh" / "id_ed25519.pub"
        if not default.exists():
            raise ConfigError(
                "Give the public key you will attach to Ships with: --agent-key PATH.pub"
            )
        agent_keys = [default]
    for key in agent_keys:
        cloudinit.read_public_keys(key, "an agent key")

    store.create(new_inventory(paths.tilde(admin), [paths.tilde(k) for k in agent_keys]))
    if made_managed:
        with store.edit() as ed:
            keys.record(ed, managed, [paths.tilde(admin)])
    app.say(f"Wrote {store.path}.")
    return admin


# --- new -----------------------------------------------------------------


def parse_autostart(values: list[str], prompt: str | None = None) -> list[Autostart]:
    """--autostart NAME:CWD[:MODE] values. Without a prompt, the Ship sends its own
    default first prompt (contracts/ship-ctl.md, autostart)."""
    agents = []
    for value in values:
        parts = value.split(":")
        if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
            raise FleetError(f"--autostart {value}: use NAME:CWD or NAME:CWD:MODE.")
        mode = parts[2] if len(parts) == 3 else None
        if mode is not None and mode not in MODES:
            raise FleetError(f"--autostart {value}: unknown mode '{mode}'.")
        agents.append(Autostart(parts[0], parts[1], mode, prompt or None))
    return agents


def cmd_new(args, app) -> int:
    """`ship-fleet new`; a failure is saved as an attempt (attempts.py) to try again."""
    from fleetledger import attempts

    progress: dict[str, Any] = {"stage": None, "driver": args.driver or "manual"}
    try:
        code = _new(args, app, progress)
    except enrol.StillStarting:  # made: it may well finish by itself
        _quietly_drop(args.name)
        raise
    except BaseException as exc:
        if progress["stage"] is None:  # a bad or taken name: nothing to save under it
            raise
        if isinstance(exc, FleetError):
            error = exc.message
        elif isinstance(exc, KeyboardInterrupt):
            error = "Interrupted."
        else:
            error = f"Internal error ({type(exc).__name__})."
        from fleetledger.concierge.redact import clean

        attempt = attempts.record(args, error=clean(error), stage=progress["stage"],
                                  driver=progress["driver"],
                                  left=getattr(exc, "left_behind", None))
        again = _try_again(attempt)
        if isinstance(exc, FleetError):
            exc.hint = f"{exc.hint}\n  {again}" if exc.hint else again
        else:
            app.note(again)
        raise
    _quietly_drop(args.name)
    return code


def _quietly_drop(name: str) -> None:
    from fleetledger import attempts

    try:
        attempts.drop(name)
    except OSError:
        pass


def _confirm_clearing(app, attempt) -> None:
    """Making attempt's Ship again removes what that attempt left on the hypervisor
    first: ask for its name (as `destroy` does), or want --yes without a terminal."""
    what = ", ".join(attempt.describe_left())
    if not app.stdin_isatty():
        raise FleetError(f"Making {attempt.name} first deletes {what}, left by its failed "
                         "attempt; rerun with --yes.")
    answer = app.confirm(f"Making {attempt.name} first deletes {what}, left by its failed "
                         "attempt. Type its name to go on: ")
    if answer.strip() != attempt.name:
        raise FleetError("Nothing removed or made.")


def _try_again(attempt) -> str:
    """What to run next, for a failed attempt: the exact command, ready to edit."""
    lines = []
    if attempt.stage == "enrol":
        lines.append(f"{attempt.name} stays in the inventory as far as it got: finish it "
                     f"with ship-fleet status {attempt.name} --wait, or remove it with "
                     f"ship-fleet destroy {attempt.name} before trying again.")
    if attempt.left:
        lines.append(f"Trying again removes first what this attempt left: "
                     f"{', '.join(attempt.describe_left())} (it asks; --yes does not).")
    lines.append(f"To try again (edit what you need): {attempt.command()}")
    if "autostart_prompt" in attempt.dropped:
        lines.append("(The first prompt is left out, as it may hold a secret: add "
                     "--autostart-prompt again.)")
    return "\n  ".join(lines)


def _new(args, app, progress: dict[str, Any]) -> int:
    from fleetledger import attempts

    store = app.store_factory()
    inv = store.load()
    name = args.name
    if not NAME_RE.match(name):
        raise FleetError(f"'{name}' is not a valid Ship name (a-z, 0-9 and -, up to 31).")
    previous = attempts.get(name)
    if inv.has_ship(name):
        said = previous is not None and attempts.blocker(inv, previous)
        raise FleetError(said or f"{name} is already in the inventory.")
    if previous is not None and previous.left and not getattr(args, "yes", False):
        _confirm_clearing(app, previous)  # asks as destroy does; refused: nothing saved
    progress["stage"] = "checks"
    repo = app.repo_factory()
    catalogue = Catalogue.from_repo(repo)

    claude: dict[str, Any] = {}
    if args.mode:
        claude["default_mode"] = args.mode
    if args.allow_bypass or args.mode == "bypassPermissions":
        claude["allow_bypass"] = True
    from fleetledger.lifecycle import check_size

    size = check_size({"cores": args.cores, "memory_mb": args.memory_mb,
                       "disk_gb": args.disk_gb})  # the size contract's limits, said plainly
    driver_id = args.driver or inv.defaults.get("driver", "manual")
    image_id = args.image or inv.pinned_image
    image = images.published(inv, image_id, driver_id)  # checks it exists for this driver
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
        image=image_id,
        signins=(None if args.signins is None else
                 tuple(signins.check(args.signins.split(",")))),
        github_scopes=tuple(ghscopes.parse(args.github_scopes or "")),
    )
    if args.tools is not None or args.no_default_tools:
        base = [] if args.no_default_tools else tools_for(inv, draft, catalogue)
        extra = [t.strip() for t in (args.tools or "").split(",") if t.strip()]
        draft = Ship(**{**draft.__dict__, "tools": tuple(dict.fromkeys([*base, *extra]))})

    if args.timezone is not None and not (
            TIMEZONE_RE.match(args.timezone) and known_timezone(args.timezone)):
        raise FleetError(f"--timezone is an IANA zone name such as Europe/Berlin, "
                         f"not '{args.timezone}'.")
    agents = parse_autostart(args.autostart, args.autostart_prompt)
    if args.repo:
        from fleetledger import github

        gh_repo = github.check_name(args.repo)
        first = agents[0] if agents else Autostart("main", github.folder(gh_repo), None,
                                                   args.autostart_prompt or None)
        agents = [dataclasses.replace(first, cwd=github.folder(gh_repo), repo=gh_repo),
                  *agents[1:]]
        check = getattr(app, "github_visibility", None) or github.visibility
        app.say(github.what_happens(gh_repo, check(gh_repo)))
    options = SeedOptions(
        autostart=agents,
        agent_sudo=True if args.agent_sudo else None,
        timezone=args.timezone or local_timezone(),
    )
    progress["driver"] = driver_id
    if previous is not None and previous.left:  # never a second VM of this name
        attempts.clear_left(app, inv, previous, app.say)
    seed = make_seed(inv, draft, repo, options)
    ref: dict[str, Any] = {"seed": seed.instance_id}
    if args.vmid is not None:
        ref["vmid"] = args.vmid
    ship = Ship(**{**draft.__dict__, "host_key": seed.host_public_key, "driver_ref": ref})
    with store.edit() as ed:
        ed.add_ship(name, ship_entry(ship))
    progress["stage"] = "create"
    app.say(f"{name}: seed written, host key {seed.host_fingerprint} pinned.")

    try:
        driver = app.driver_factory(store.load(), driver_id,
                                    ubuntu_release=repo.versions["ubuntu"]["release"])
    except BaseException:  # e.g. no Proxmox token: the name is not left claimed
        with store.edit() as ed:
            ed.remove_ship(name)
        seed.path.unlink(missing_ok=True)
        raise
    with audit.recorded(name, "create", driver_id) as audited:
        try:
            outcome = driver.create(ship, SeedFile(seed.path, seed.instance_id),
                                    inv.effective_size(ship), image)
        except BaseException:
            with store.edit() as ed:
                ed.remove_ship(name)
            seed.path.unlink(missing_ok=True)
            raise
        with store.edit() as ed:  # the VM id is saved before the entry is written
            ed.set_field(name, "driver_ref", dict(outcome.driver_ref))
        progress["stage"] = "enrol"  # from here on the Ship stays in the inventory
        if not outcome.done:
            audited["outcome"] = "steps"
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
    return _enrol(app, store, name, overlay=not args.no_overlay)


def _reachable_name(app, store, driver, name: str, timeout: float = 900) -> None:
    """Make sure FleetLedger can reach a Ship the driver just started.

    Once the hypervisor reports the Ship's address, its names are checked for
    about 30 seconds (the mDNS announcement can come up a little after the
    address). If none resolves on this computer (for example .local names
    without mDNS name resolution), the address is added as the last name to try.
    """
    ship = store.load().ship(name)
    waited = 0.0
    app.note(f"{name}: booting; waiting for the hypervisor to report its address.")
    while True:
        addresses = driver.status(ship).addresses
        if addresses:
            break
        if waited >= timeout:
            app.note(f"{name}: the hypervisor reported no address; trying its names only.")
            return
        app.sleep(10)
        waited += 10
    for attempt in range(4):
        if any(app.resolves(host) for host in ship.names):
            return
        if attempt < 3:
            app.sleep(10)
    with store.edit() as ed:
        ed.set_field(name, "names", [*ship.names, addresses[0]])
    app.note(f"{name}: {', '.join(ship.names)} does not resolve on this computer, so "
             f"{addresses[0]} is added as a fallback name (see fleetledger/README.md, "
             "\"Names\").")


HEARTBEAT = 60  # seconds between "still ..." lines while a Ship's first boot runs


def _first_boot_phase(view) -> str:
    """What a status poll during the first boot means, in words. Not answering (or
    refusing the admin key before cloud-init has set it up) is normal for a while."""
    if view.state in ("unreachable", "not-enrolled"):
        return "not answering yet: still booting"
    if view.state == "provisioning":
        return "first boot: cloud-init is installing (the slow part)"
    if view.state == "error":
        return f"{view.message or 'an error'} (it may still be starting; still trying)"
    if view.state == "invalid":
        return f"{view.message or 'an unexpected answer'} (still trying)"
    return view.state


def _enrol(app, store, name: str, say=None, overlay: bool = True) -> int:
    say = say or app.say
    inv = store.load()
    ship = inv.ship(name)
    driver = app.driver_factory(inv, ship.driver)
    started = app.clock()
    last = {"said": None, "at": started}

    def progress(view):
        said = _first_boot_phase(view)
        now = app.clock()
        if said != last["said"]:
            last["said"], last["at"] = said, now
            app.note(f"{name}: {said}")
        elif now - last["at"] >= HEARTBEAT:
            last["at"] = now
            app.note(f"{name}: still waiting, {(now - started) / 60:.0f} min so far")

    result = enrol.enrol(store, _ssh(app, inv), driver, ship, on_update=progress,
                         sleep=app.sleep, clock=app.clock)
    for message in result.messages:
        say(message)
    if result.instructions:
        say("Then remove the seed (it holds the Ship's private host key):")
        for line in result.instructions:
            say(f"  {line}")
    if overlay:
        from fleetledger.access import join_after_enrol

        join_after_enrol(app, store, name, say)  # before certifying: its overlay name too
    after_enrol(app, store, name, say)
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
    if not isinstance(report, dict) or report.get("schema") != STATUS_SCHEMA:
        raise FleetError(f"{host} did not give a valid status document.")
    report = schemas.known_part("ship-status", report)  # a newer Ship's unknown blocks go
    if not schemas.is_valid("ship-status", report):
        raise FleetError(f"{host} did not give a valid status document.")
    if report["ship"]["name"] != name:
        raise FleetError(f"{host} says it is {report['ship']['name']}, not {name}.")

    tags = args.tag or report["ship"]["tags"]
    with store.edit() as ed:
        if inv.has_ship(name):
            old = inv.ship(name).host_key_type_and_blob
            if inv.doc.get("ca") and old != key:
                # Its old key may carry a certificate: trusted machines must refuse it.
                ed.add_to_ca_list("revoked_host_keys", old)
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
    if inv.has_ship(name):
        audit.record(name, "re-pin", "host-key", "ok")
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
        return [status.cached_view(inv, ship, "no cached status") for ship in ships]
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
    from fleetledger import trustcmd

    try:  # this computer's certificate, in its last day: renewed (plain ssh keeps working)
        if trustcmd.renew_this_computer_if_due(app, store):
            app.note("Renewed this computer's certificate.")
    except FleetError as exc:
        app.note(f"This computer's certificate was not renewed ({exc.message}).")
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
    wanted, token = ghscopes.chosen(ship, doc), ghscopes.granted(doc)
    if wanted or ghscopes.extras(token):
        app.say(f"  GitHub scopes: chosen {ghscopes.describe(wanted)}"
                + (f"; the token has {ghscopes.describe(ghscopes.extras(token))}"
                   if token is not None else ""))
    app.say("Agents:")
    for agent in doc["herdr"]["agents"] or []:
        app.say(f"  {agent['name'] or '-':<12} {agent['state']:<8} {agent['mode'] or ''}")
    if not doc["herdr"]["agents"]:
        app.say("  none")
    host = doc["host"]
    pending = host.get("updates_pending")
    app.say(f"Host: {usage_cell(view)} (cpu · memory · disk), up {host['uptime_s'] // 3600} h"
            + (", reboot required" if host["reboot_required"] else "")
            + (f", {pending} OS updates pending" if pending else ""))
    app.say(f"Claude {doc['claude']['version'] or '?'} ({doc['claude']['channel']}), "
            f"default mode {doc['claude']['default_mode']}, "
            f"Remote Control {doc['claude']['remote_control']}")
    herdr = doc["herdr"]
    resume = herdr["last_resume"]
    resumed = (f"; last resume {resume['at']} ({resume['resumed']} resumed"
               + (f", {resume['failed']} failed" if resume.get("failed") else "") + ")"
               if resume else "")
    app.say(f"herdr {herdr['version'] or '?'} ({herdr['server']}){resumed}")
    commit = doc["ship"].get("bundle_commit")
    app.say(f"Tooling {doc['ship']['tooling']}" + (f" ({commit})" if commit else "")
            + f"; provisioned {doc['provision']['result']}"
            + (f" at {doc['provision']['last_run']}" if doc["provision"]["last_run"] else ""))
    if doc["tools"]:
        app.say("Tools: " + ", ".join(f"{k} {v['state']}" for k, v in sorted(doc["tools"].items())))
    ov = doc.get("overlay")
    if ov:
        where = f" as {ov['name']}" if ov["name"] else ""
        logins = "the overlay only" if ov["ssh_listen"] == "overlay" else "any interface"
        state = ov["state"] if ov["joined"] else "not joined"
        app.say(f"Overlay: {state}{where}; SSH logins on {logins}")
    if doc.get("net"):
        n = doc["net"]
        new = n["new_destinations"]
        app.say(f"Network (24 h): {n['rx_mb']:,.1f} MB in, {n['tx_mb']:,.1f} MB out"
                + (f"; first seen: {', '.join(new[:5])}" + (" ..." if len(new) > 5 else "")
                   if new else "") + f"  (ship-fleet net {ship.name})")
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
    if args.scopes is not None:
        if not any(p.takes_scopes for p in chosen):
            raise FleetError("--scopes is for the GitHub sign-in: ship-fleet login SHIP github "
                             "--scopes workflow")
        scopes = ghscopes.parse(args.scopes)
        on = [s for s in ships if signins.provider_on(s, "github")]
        outcomes = ops.set_settings(_fleet(app), on, [f"github_scopes={','.join(scopes)}"])
        for outcome in outcomes:
            app.say(f"{outcome.ship}: GitHub scopes {ghscopes.describe(scopes)}"
                    if outcome.ok else outcome.summary)
        if not all(o.ok for o in outcomes):
            return 1  # never sign in with scopes the Ship did not take
        inv = app.store_factory().load()
        ships = inv.select(names, args.tag)
    ssh = _ssh(app, inv)
    failures = 0
    for ship in ships:
        for provider in chosen:
            if not signins.provider_on(ship, provider.id):
                if service != "all" and not args.tag:
                    choice = signins.choice_of_provider(provider.id)
                    raise FleetError(f"{ship.name} does not use {signins.LABELS[choice]} (its "
                                     "sign-ins "
                                     f"are {', '.join(signins.used(ship)) or 'none'}).",
                                     hint=f"Turn it on: ship-fleet signins {ship.name} "
                                     f"--on {choice}")
                continue  # off on this Ship: no sign-in
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


def cmd_signins(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    listing = any((args.tag, args.service, args.group, args.json, args.offline))
    changing = args.only is not None or args.on or args.off
    if args.name is None or (listing and not changing):
        if changing:
            raise FleetError("Name the Ship whose sign-ins to change.")
        return _list_signins(args, app, inv)
    if listing:
        raise FleetError("--tag, --service, --group, --json and --offline list sign-ins; "
                         "give them without --on, --off or --only.")
    ship = inv.ship(args.name)
    if args.only is not None:
        if args.on or args.off:
            raise FleetError("Give --only, or --on/--off, not both.")
        wanted = signins.check(args.only.split(","))
    elif args.on or args.off:
        wanted = signins.change(ship, [*(f"+{n}" for n in args.on),
                                       *(f"-{n}" for n in args.off)])
    else:
        for choice in signins.CHOICES:
            state = "on" if choice in signins.used(ship) else "off"
            app.say(f"{choice:8} {state}")
        if ship.github_scopes:
            app.say(f"github scopes: {', '.join(ship.github_scopes)}")
        return 0
    return 0 if set_signins(app, store, ship.name, wanted, app.say) else 1


def _list_signins(args, app, inv: Inventory) -> int:
    """Every sign-in on every Ship (or those asked for), most urgent first: the same
    rows, in the same order, as the web UI's Sign-ins page (fleetledger.signinlist)."""
    from fleetledger import signinlist

    service = (args.service or "").replace("-", "_") or None
    if service is not None and service not in signinlist.SERVICES:
        raise FleetError(f"Unknown service {args.service} (choose from "
                         f"{', '.join(signinlist.SERVICES)}).")
    ships = inv.ships(args.tag)
    if args.name is not None:
        ships = [s for s in ships if s.name == inv.ship(args.name).name]
    if not ships:
        app.say("No Ships" + (f" with tag {args.tag}." if args.tag else " yet."))
        return 0
    found = signinlist.rows(_views(app, inv, ships, args.offline), inv)
    found = signinlist.narrow(found, service=service, group=args.group)
    if args.json:
        app.say(json.dumps([{**r.as_dict(), "fix": signinlist.fix_command(r)} for r in found],
                           indent=2))
        return 0
    if not found:
        app.say("No sign-ins match.")
        return 0
    counts = signinlist.counts(found)
    app.say(" · ".join(f"{signinlist.GROUP_LABELS[g]}: {n}" for g, n in counts.items() if n))
    rows = [[r.level or "", r.ship, r.service, r.state, signinlist.ago(r.since), r.message,
             signinlist.fix_command(r)] for r in found]
    lines = table(rows, ["LEVEL", "SHIP", "SERVICE", "STATE", "SINCE", "DETAIL", "FIX"]
                  ).split("\n")
    app.say("")
    app.say("  " + lines[0])
    group = None
    for row, line in zip(found, lines[1:], strict=True):
        if row.group != group:
            group = row.group
            app.say(f"{signinlist.GROUP_LABELS[group]} ({counts[group]})")
        app.say("  " + line)
    return 0


def set_signins(app, store, name: str, wanted: list[str], say) -> bool:
    """Record the sign-ins a Ship uses, signing it out of each one turned off."""
    inv = store.load()
    ship = inv.ship(name)
    turned_off = [c for c in signins.used(ship) if c not in wanted]
    ok = True
    if turned_off:
        ssh = _ssh(app, inv)
        view = status.fetch(ssh, inv, ship)
        for choice in turned_off:
            states = [(view.auth(k) or {}).get("state") for k in signins.STATUS_KEYS[choice]]
            if view.status is not None and all(st in (None, "missing") for st in states):
                say(f"{name}: {signins.LABELS[choice]} was not signed in there; it is off now.")
                continue
            for provider in providers.resolve(list(signins.PROVIDERS[choice][:1])):
                if concierge.logout(ssh, ship, provider):
                    say(f"{name}: signed out of {provider.label}; it is off now.")
                else:
                    say(f"{name}: signing out of {provider.label} failed; it is off anyway "
                        f"(retry: ship-fleet logout {name} {provider.id}).")
                    ok = False
    with store.edit() as ed:
        ed.set_field(name, "signins", None if wanted == list(signins.CHOICES) else wanted)
    say(f"{name} uses: {', '.join(wanted) or 'no sign-ins'}.")
    return ok


OWNER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


def cmd_owner(args, app) -> int:
    store = app.store_factory()
    inv = store.load()
    if not args.name:
        app.say(inv.owner)
        return 0
    return 0 if set_owner(app, store, args.name, app.say) else 1


def set_owner(app, store, name: str, say) -> bool:
    """fleet.owner, and the owner login on every Ship (ship-ctl set owner.*)."""
    from fleetledger.shipctl import ShipCtl, ShipCtlError

    if not OWNER_RE.match(name) or name in ("root", "fleet", "agent", "nobody"):
        raise FleetError(f"'{name}' cannot be your login name: a-z, 0-9, _ and -, not root, "
                         "fleet, agent or nobody.")
    with store.edit() as ed:
        ed.doc["fleet"]["owner"] = name
    inv = store.load()
    keys = [k for path in inv.agent_key_paths
            for k in cloudinit.read_public_keys(path, "an agent key")]
    ok = True
    ssh = _ssh(app, inv)
    for ship in inv.ships():
        try:
            ShipCtl(ssh, ship).set([f"owner.name={name}", f"owner.keys={','.join(keys)}"])
            say(f"{ship.name}: your login is {name} (ssh {name}@{ship.name}).")
        except (FleetError, ShipCtlError) as exc:
            say(f"{ship.name}: not changed ({exc.message}); its tooling may need ship-fleet "
                "update first.")
            ok = False
    say(f"New Ships get the login {name}.")
    return ok


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


# --- batch helpers -------------------------------------------------------------


def _fleet(app) -> ops.Fleet:
    return ops.Fleet.from_app(app)


def _progress(app) -> ops.Event:
    def event(ship: str, text: str) -> None:
        app.note(f"{ship}: {text}" if ship != "*" else text)
    return event


def _report(app, outcomes: list[ops.Outcome], as_json: bool = False) -> int:
    if as_json:
        app.say(json.dumps([o.as_dict() for o in outcomes], indent=2))
    else:
        for outcome in outcomes:
            mark = "" if outcome.ok else "FAILED: "
            app.say(f"{outcome.ship}: {mark}{outcome.summary}")
            for detail in outcome.details:
                app.say(f"  {detail}")
        failed = [o.ship for o in outcomes if not o.ok]
        if len(outcomes) > 1:
            app.say(f"{len(outcomes) - len(failed)} of {len(outcomes)} Ships OK"
                    + (f"; failed: {', '.join(failed)}." if failed else "."))
    return 1 if any(not o.ok for o in outcomes) else 0


def _confirm_many(app, ships: list[Ship], what: str, yes: bool) -> None:
    """Ask before a disruptive action on more than one Ship."""
    if len(ships) < 2 or yes:
        return
    names = ", ".join(s.name for s in ships)
    if not app.stdin_isatty():
        raise FleetError(f"This would {what} {len(ships)} Ships ({names}); add --yes to go ahead.")
    answer = app.confirm(f"{what.capitalize()} {len(ships)} Ships ({names})? Type yes: ")
    if answer.strip().lower() != "yes":
        raise FleetError("Nothing done.")


def _split_names(values: list[str]) -> tuple[list[str], list[str]]:
    """Words without '=' are Ship names, wherever they come; the rest are KEY=VALUE."""
    names = [v for v in values if "=" not in v]
    pairs = [v for v in values if "=" in v]
    return names, pairs


# --- set, tag, tools ----------------------------------------------------------


def cmd_set(args, app) -> int:
    names, pairs = _split_names(args.args)
    if not pairs:
        raise FleetError("Give at least one KEY=VALUE.")
    ops.parse_assignments(pairs)  # reject a bad setting before touching any Ship
    fleet = _fleet(app)
    ships = ops.select(fleet.inventory, names, args.tag)
    outcomes = ops.set_settings(fleet, ships, pairs)
    return _report(app, outcomes)


def cmd_agent(args, app) -> int:
    """`ship-fleet agent restart SHIP [AGENT ...|--all]`: swap running agents for ones in
    a new mode, in place (ops.restart_agents)."""
    if args.all and args.agents:
        raise FleetError("Give agent names or --all, not both.")
    fleet = _fleet(app)
    ship = fleet.inventory.ship(args.ship)
    outcome = ops.restart_agents(fleet, ship, args.agents, everyone=args.all, mode=args.mode,
                                 fresh=args.fresh, force=args.force, on_event=_progress(app))
    return _report(app, [outcome], args.json)


def cmd_tag(args, app) -> int:
    fleet = _fleet(app)
    ship = fleet.inventory.ship(args.name)
    if not args.changes:
        app.say(" ".join(fleet.inventory.effective_tags(ship)) or "(no tags)")
        return 0
    outcome = ops.set_tags(fleet, [ship], args.changes, local=args.local)[0]
    app.say(f"{ship.name}: {outcome.summary}." if outcome.ok else outcome.summary)
    return 0 if outcome.ok else 1


def cmd_tools(args, app) -> int:
    if args.action == "ls":
        if args.rest or args.tag:
            raise FleetError("tools ls takes no arguments.")
        catalogue = Catalogue.from_repo(app.repo_factory())
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
    if args.tag:
        names, ids = [], list(args.rest)
    else:
        names, ids = args.rest[:1], args.rest[1:]
    if not ids or (not names and not args.tag):
        raise FleetError(f"Usage: ship-fleet tools {args.action} SHIP|all ID... "
                         f"(or --tag TAG ID...)")
    fleet = _fleet(app)
    ships = ops.select(fleet.inventory, names, args.tag)
    outcomes = ops.change_tools(fleet, ships, args.action, ids)
    return _report(app, outcomes)


# --- update, reboot, attention ---------------------------------------------------


def cmd_update(args, app) -> int:
    fleet = _fleet(app)
    ships = ops.select(fleet.inventory, args.names, args.tag)
    if args.reboot != "never":
        _confirm_many(app, ships, "update (and reboot if needed)", args.yes)
    outcomes = ops.update(fleet, ships, reboot_policy=args.reboot, wait=not args.no_wait,
                          force=args.force, on_event=_progress(app))
    return _report(app, outcomes, args.json)


def cmd_reboot(args, app) -> int:
    fleet = _fleet(app)
    ships = ops.select(fleet.inventory, args.names, args.tag)
    _confirm_many(app, ships, "force-reboot" if args.force else "reboot", args.yes)
    outcomes = ops.reboot(fleet, ships, wait=not args.no_wait, on_event=_progress(app),
                          force=args.force)
    return _report(app, outcomes, args.json)


def _attention_rows(items: list[ops.AttentionItem]) -> str:
    rows = [[i.severity, i.ship, i.code, i.message] for i in items]
    return table(rows, ["", "SHIP", "WHAT", "DETAIL"])


def cmd_attention(args, app) -> int:
    inv = app.store_factory().load()
    ships = inv.ships(args.tag)
    if not ships:
        app.say("No Ships" + (f" with tag {args.tag}." if args.tag else " yet."))
        return 0
    seen: set[tuple[str, str, str | None]] = set()
    while True:
        items = ops.attention(_views(app, inv, ships, offline=False))
        if args.json:
            app.say(json.dumps([i.as_dict() for i in items], indent=2))
        elif args.watch is None:
            app.say(_attention_rows(items) if items else "Nothing needs attention.")
        else:
            fresh = [i for i in items if (i.ship, i.code, i.subject) not in seen]
            stamp = time.strftime("%H:%M:%S")
            for item in fresh:
                app.say(f"{stamp}  {item.severity:<6} {item.ship}: {item.message}")
            seen = {(i.ship, i.code, i.subject) for i in items}
        if args.watch is None:
            return 0
        app.sleep(args.watch)
        inv = app.store_factory().load()
        ships = inv.ships(args.tag)


# --- pins ----------------------------------------------------------------------


def cmd_pins(args, app) -> int:
    from fleetledger import pins

    repo = app.repo_factory()
    http = app.http_factory()
    if args.action == "check":
        if args.pin or args.value:
            raise FleetError("pins check takes no arguments.")
        rows = pins.check(repo, http, on_error=lambda pin, why: app.note(f"{pin}: {why}"))
        if args.json:
            app.say(json.dumps([{**r.__dict__, "behind": r.behind} for r in rows], indent=2))
            return 0
        app.say(table([[r.pin, r.current, r.latest or "?", "newer available" if r.behind
                        else "", r.note] for r in rows],
                      ["PIN", "PINNED", "LATEST", "", "NOTE"]))
        if any(r.behind for r in rows):
            app.say("\nMove one with: ship-fleet pins set PIN VERSION")
        return 0
    if not args.pin or not args.value:
        raise FleetError("Usage: ship-fleet pins set PIN VERSION")
    change = pins.set_pin(repo, args.pin, args.value, http)
    app.say(f"{change.pin}: {change.old} -> {change.new} in {repo.versions_path}")
    for detail in change.details:
        app.say(f"  {detail}")
    app.say("Next: scripts/check, commit, then ship-fleet update (one Ship first).")
    return 0


# --- ssh and logs --------------------------------------------------------------


def cmd_ssh(args, app) -> int:
    inv = app.store_factory().load()
    ship = inv.ship(args.name)
    remote = args.remote
    user = {"fleet": "fleet", "owner": inv.owner}.get(args.user, "agent")
    # An interactive session cannot fall back name by name as ssh.run does, so dial
    # the first name that resolves here (the pinned host key is checked either way).
    host = next((h for h in ship.names if app.resolves(h)), ship.names[0])
    _ssh(app, inv).exec_interactive(ship, user=user, command=remote, host=host)
    return 0  # not reached: exec replaces this process


def cmd_logs(args, app) -> int:
    *names, source = args.args
    if source not in ops.LOG_SOURCES:
        raise FleetError(f"Unknown log source '{source}' "
                         f"(sources: {', '.join(ops.LOG_SOURCES)}).")
    fleet = _fleet(app)
    ships = ops.select(fleet.inventory, names, args.tag)
    if len(ships) == 1:
        for line in ops.logs(fleet, ships[0], source, args.lines):
            app.say(line)
        return 0
    outcomes = ops.fleet_logs(fleet, ships, source, args.lines)
    for outcome in outcomes:
        if not outcome.ok:
            app.say(f"{outcome.ship}: FAILED: {outcome.summary}")
            continue
        for line in outcome.data["lines"]:
            app.say(f"{outcome.ship}: {line}")
    return 1 if any(not o.ok for o in outcomes) else 0


# --- ui ---------------------------------------------------------------------------


def cmd_ui(args, app) -> int:
    from fleetledger.web.server import cmd_ui as serve_ui  # web dependencies load only here

    return serve_ui(args, app)
