"""What is left after first-run setup: `ship-fleet doctor --next` (and `--json`).

Setup stops at the first Ship. This is the checklist from there: sign it in, attach
from this computer, reach the web UI from your other devices, trust a laptop, run
FleetLedger always on, the vault, backups. Each step has a state and the command that
does it.

It is built from doctor's items (firstrun.everything, read offline), not a second set
of checks: a step reads the items doctor already has (the inventory, the Ships, the
CA, trusted machines' certificates, the vault, backups) and adds only what doctor
does not look at (this computer's trust, the remote web UI setting, the service
unit, a Ship's last saved sign-ins), each from a file on this computer. Read-only:
it runs no command but the local ones doctor runs (ssh-agent, ssh-keygen, the
keyring), and contacts no Ship, Proxmox or Tailscale.

The JSON shape is contracts/next-steps.schema.json; the Herdr Fleet panel reads it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from fleetledger import firstrun
from fleetledger.errors import FleetError

SCHEMA = "fleetledger.next-steps/1"
STATES = ("done", "todo", "attention", "blocked", "skip")
# done: nothing to do. todo: not done yet; command does it. attention: was set up and
# needs fixing; command fixes it. blocked: waits for the step named by after.
# skip: not needed on this computer.

GUIDE = "docs/guide/your-own-fleet.md"
VAULT_DOC = "docs/vault.md"
REMOTE_DOC = "docs/runbooks/remote-ui.md"
TRUST_DOC = "docs/runbooks/trust-machines.md"
SERVICE_DOC = "docs/runbooks/fleetledger-service.md"
SIGNIN_DOC = "docs/runbooks/sign-in.md"
BACKUP_DOC = "docs/runbooks/fleetledger-vm.md"

REMOTE_COMMAND = "ship-fleet ui remote https://NAME.TAILNET.ts.net --port 8765 --login LOGIN"
LAPTOP_COMMAND = "ship-fleet trust laptop --key laptop.pub --scope all"
BACKUP_COMMAND = "ship-fleet backup schedule --to DIR"
# Items that have a step of their own, so the setup step does not count them.
OWN_STEP = ("vault", "vault key in RAM", "swap", "backups")


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    state: str  # one of STATES
    detail: str = ""
    command: str = ""  # what does it (todo) or fixes it (attention); "" otherwise
    docs: str = ""  # a path in the ShipShape checkout
    optional: bool = False
    after: str = ""  # blocked: the key of the step it waits for


def _first(items, *, area: str | None = None, name: str | None = None):
    return next((i for i in items if (area is None or i.area == area)
                 and (name is None or i.name == name)), None)


def steps(app) -> list[Step]:
    """The checklist, in order, with every step present on every computer."""
    from fleetledger import vault

    items, inv = firstrun._everything(app, offline=True)
    locked = vault.state() == "locked"
    out = [_setup(items, inv, locked)]
    # The rest is read from the inventory and files beside it, so it needs an inventory,
    # not a clean doctor: an admin key out of ssh-agent hides no Ship (setup says so).
    out.append(_first_ship(items, inv))
    out.append(_signins(inv, out[-1].state == "done"))
    out.append(_this_computer(items, inv))
    out.append(_other_devices())
    out.append(_laptop(items, inv))
    out.append(_always_on(inv))
    out.append(_vault(items, app.store_factory()))
    out.append(_backups(items, out[-1]))
    return out


def as_json(found: list[Step]) -> dict:
    left = [s for s in found if s.state in ("todo", "attention", "blocked")]
    return {"schema": SCHEMA, "left": len(left),
            "left_required": sum(1 for s in left if not s.optional),
            "steps": [asdict(s) for s in found]}


# --- each step ----------------------------------------------------------------------------


def _setup(items, inv, locked: bool) -> Step:
    title = "FleetLedger set up"
    if locked:
        return Step("setup", title, "blocked", "the vault is locked, so its secrets cannot "
                    "be checked until it is unlocked", docs=GUIDE, after="vault")
    core = [i for i in items if i.area in ("computer", "fleet", "hypervisor")
            and i.name not in OWN_STEP and not i.name.startswith("machine ")]
    bad = next((i for i in core if i.state in ("missing", "fail")
                or (i.name == "this copy" and i.state == "warn")), None)
    if bad is not None:
        state = "todo" if inv is None and bad.state == "missing" else "attention"
        return Step("setup", title, state, f"{bad.name}: {bad.detail}",
                    bad.next or "ship-fleet doctor", GUIDE)
    return Step("setup", title, "done", "ship-fleet doctor finds nothing missing", docs=GUIDE)


def _first_ship(items, inv) -> Step:
    title = "A first Ship"
    if inv is None:
        return Step("first-ship", title, "blocked", "needs FleetLedger set up",
                    docs="docs/runbooks/create-ship.md", after="setup")
    pending = [i for i in items if i.area == "ships" and i.state == "warn"]
    ships = _first(items, area="ships", name="Ships")
    if ships is not None and ships.state == "info":
        return Step("first-ship", title, "todo", ships.detail, ships.next,
                    "docs/runbooks/create-ship.md")
    enrolled = [s for s in inv.ships() if s.enrolled is not None]
    if not enrolled and pending:
        return Step("first-ship", title, "todo", f"{pending[0].name}: {pending[0].detail}",
                    pending[0].next, "docs/runbooks/create-ship.md")
    return Step("first-ship", title, "done",
                ", ".join(s.name for s in enrolled[:3]) + (" ..." if len(enrolled) > 3 else ""),
                docs="docs/runbooks/create-ship.md")


def _signins(inv, have_ship: bool) -> Step:
    """Done once one Ship has every sign-in it uses, as its last saved status says
    (ship-fleet ls --offline's cache). Nothing is asked of a Ship."""
    from fleetledger import signins, status

    title = "Sign the first Ship in"
    if not have_ship:
        return Step("signins", title, "blocked", "needs a Ship", docs=SIGNIN_DOC,
                    after="first-ship")
    enrolled = [s for s in inv.ships() if s.enrolled is not None]
    said = ""
    for ship in enrolled:
        doc = status.read_cache(ship.name)
        auth = (doc or {}).get("auth")
        if not isinstance(auth, dict):
            said = said or f"{ship.name}: not checked yet (ship-fleet status {ship.name})"
            continue
        used = [k for k in status.AUTH_SERVICES if k not in signins.off_status_keys(ship)]
        missing = [k for k in used if not isinstance(auth.get(k), dict)
                   or auth[k].get("state") not in status.SIGNED_IN]
        if not missing:
            return Step("signins", title, "done", f"{ship.name}: every sign-in it uses",
                        docs=SIGNIN_DOC)
        said = said or f"{ship.name}: not signed in to {', '.join(missing)}"
    name = enrolled[0].name
    return Step("signins", title, "todo", said, f"ship-fleet login {name} all", SIGNIN_DOC)


def _machine_due(items, name: str):
    return _first(items, area="fleet", name=f"machine {name}")


def _this_computer(items, inv) -> Step:
    """Plain ssh, herdr --remote and hc from this computer (trust --this-computer)."""
    from fleetledger import ca, ops, trustcmd

    title = "Attach from this computer"
    if inv is not None and firstrun.on_its_own_vm(inv):
        return Step("this-computer", title, "skip", "this is FleetLedger's own VM: attach "
                    "from your own computers (Another computer, below)", docs=TRUST_DOC,
                    optional=True)
    if inv is None or ca.config(inv) is None:
        return Step("this-computer", title, "blocked", "needs FleetLedger's certificate "
                    "authority (FleetLedger set up, above)", docs=TRUST_DOC, optional=True,
                    after="setup")
    here = trustcmd.this_computer()
    m = ca.machine(inv, here["machine"]) if here else None
    if m is None or m.revoked:
        return Step("this-computer", title, "todo", "not trusted: ship-fleet ssh works, plain "
                    "ssh and hc do not yet", ops.THIS_COMPUTER_COMMAND, TRUST_DOC,
                    optional=True)
    due = _machine_due(items, m.name)
    if due is not None:
        return Step("this-computer", title, "attention", due.detail, due.next, TRUST_DOC,
                    optional=True)
    return Step("this-computer", title, "done", f"trusted as {m.name}", docs=TRUST_DOC,
                optional=True)


def _other_devices() -> Step:
    """The web UI from your phone and other computers, through Tailscale Serve."""
    from fleetledger.web import remote

    title = "Your other devices (the web UI from anywhere)"
    try:
        setting = remote.load()
    except FleetError as exc:
        return Step("other-devices", title, "attention", exc.message,
                    exc.hint or REMOTE_COMMAND, REMOTE_DOC, optional=True)
    if setting is None:
        return Step("other-devices", title, "todo", "off: the web UI answers on this "
                    "computer only", REMOTE_COMMAND, REMOTE_DOC, optional=True)
    return Step("other-devices", title, "done", f"on: {setting.origin}; pair a browser "
                "with ship-fleet ui link", docs=REMOTE_DOC, optional=True)


def _laptop(items, inv) -> Step:
    from fleetledger import ca, trustcmd

    title = "Another computer (a laptop)"
    if inv is None or ca.config(inv) is None:
        return Step("laptop", title, "blocked", "needs FleetLedger's certificate "
                    "authority (FleetLedger set up, above)", docs=TRUST_DOC, optional=True,
                    after="setup")
    here = trustcmd.this_computer()
    mine = here["machine"] if here else None
    others = [m for m in ca.machines(inv) if not m.revoked and m.name != mine]
    if not others:
        return Step("laptop", title, "todo", "none trusted yet", LAPTOP_COMMAND, TRUST_DOC,
                    optional=True)
    for m in others:
        due = _machine_due(items, m.name)
        if due is not None:
            return Step("laptop", title, "attention", due.detail, due.next, TRUST_DOC,
                        optional=True)
    return Step("laptop", title, "done", ", ".join(m.name for m in others),
                docs=TRUST_DOC, optional=True)


def _always_on(inv) -> Step:
    from fleetledger import service

    title = "Always on"
    if inv is not None and firstrun.on_its_own_vm(inv):
        return Step("always-on", title, "done", "this is FleetLedger's own VM",
                    docs="docs/runbooks/fleetledger-vm.md", optional=True)
    if service.unit_path().is_file():
        return Step("always-on", title, "done", "the fleetledger service is installed",
                    docs=SERVICE_DOC, optional=True)
    return Step("always-on", title, "todo", "FleetLedger runs only while you run it: "
                "certificates renew and Ships are checked only then (or give it its own VM: "
                "docs/runbooks/fleetledger-vm.md)", "ship-fleet service install",
                SERVICE_DOC, optional=True)


def _vault(items, store) -> Step:
    from fleetledger import vaultcmd
    from fleetledger.overlay import secret_store

    title = "The vault"
    bad = next((i for i in items if i.name in ("vault", "vault key in RAM")
                and i.state in ("warn", "fail")), None)
    if bad is not None:
        return Step("vault", title, "attention", bad.detail, bad.next, VAULT_DOC)
    if _first(items, name="vault") is not None:
        try:
            left = vaultcmd.unfinished(store)
        except FleetError:  # an unreadable ships.toml: the setup step says so
            left = []
        if left:
            return Step("vault", title, "attention", "on, but a vault init stopped before "
                        "it moved every secret in: " + ", ".join(left),
                        "ship-fleet vault init", VAULT_DOC)
        return Step("vault", title, "done", "on and unlocked", docs=VAULT_DOC)
    usable, why = secret_store.keyring_usable()
    if usable:
        return Step("vault", title, "todo", "off: secrets are in your OS keyring, unlocked "
                    "with your login; the vault is for a computer without one, and backups "
                    "need it", "ship-fleet vault init", VAULT_DOC, optional=True)
    return Step("vault", title, "todo", f"off, and no usable OS keyring here ({why}): "
                "FleetLedger's secrets are in files only you can read, so any copy of this "
                "disk opens the fleet. Recommended", "ship-fleet vault init", VAULT_DOC)


def _backups(items, vault_step: Step) -> Step:
    title = "Backups"
    item = _first(items, name="backups")
    if item is not None and item.state == "ok":
        return Step("backups", title, "done", item.detail, docs=BACKUP_DOC, optional=True)
    if item is not None and item.state == "warn":
        return Step("backups", title, "attention", item.detail, item.next, BACKUP_DOC,
                    optional=True)
    if vault_step.state != "done":
        return Step("backups", title, "blocked", "backups are sealed with the vault",
                    docs=BACKUP_DOC, optional=True, after="vault")
    if item is not None:  # scheduled, none made yet
        return Step("backups", title, "todo", item.detail, item.next, BACKUP_DOC,
                    optional=True)
    return Step("backups", title, "todo", "none scheduled: a daily one to a folder off this "
                "disk (a NAS, say)", BACKUP_COMMAND, BACKUP_DOC, optional=True)
