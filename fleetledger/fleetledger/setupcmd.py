"""ship-fleet setup and ship-fleet doctor: a new user's way in.

setup walks a new computer from nothing to a first Ship, one step at a time:
this computer, FleetLedger's keys and inventory, ssh-agent, the certificate
authority, the hypervisor, the overlay (optional) and a first Ship (optional).
Each step looks first (with the same checks doctor uses, firstrun.py) and skips
what is already done, so setup can be stopped and run again at any point.

doctor is read-only: it reports what is set up, what is missing, and the exact
command for each missing piece.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from fleetledger import ca, firstrun, keys, paths
from fleetledger.commands import table
from fleetledger.errors import FleetError

COMMANDS = ("setup", "doctor")

MARK = {"ok": "ok", "info": "info", "warn": "WARN", "missing": "MISSING", "fail": "FAIL",
        "skip": "-"}
REDO = ("hypervisor", "overlay")
STEPS = ("This computer", "FleetLedger's keys and inventory", "ssh-agent",
         "Certificate authority", "Where Ships run", "Overlay network (optional)",
         "A first Ship (optional)")
ADMIN_KEY = "~/.ssh/shipshape_fleet_ed25519"
HOST_CA_KEY = "~/.ssh/shipshape_host_ca_ed25519"
USER_CA_KEY = "~/.ssh/shipshape_user_ca_ed25519"


def add_parsers(sub, add) -> None:
    p = add(sub, "setup", "set everything up, step by step, in your browser (safe to run again)",
            "Set up FleetLedger on this computer: your fleet (FleetLedger makes and looks\n"
            "after its keys, or you bring your own), where Ships run (Proxmox or manual),\n"
            "the overlay network and a first Ship. Opens the setup page of the web UI on\n"
            "127.0.0.1 (this computer only). Each step shows done or next, so you can stop\n"
            "and come back at any point.\n\n"
            "  ship-fleet setup\n"
            "  ship-fleet setup --text                 in this terminal instead (no browser)\n"
            "  ship-fleet setup --text --redo hypervisor")
    p.add_argument("--text", action="store_true",
                   help="ask in this terminal instead of the browser (headless computers)")
    p.add_argument("--no-browser", action="store_true",
                   help="print the setup page's link instead of opening it")
    p.add_argument("--port", type=int, default=0, help="a fixed port (default: any free one)")
    p.add_argument("--redo", action="append", default=[], choices=REDO,
                   help="--text: do this step again even though it is set up (repeatable)")
    p.add_argument("--admin-key", default=ADMIN_KEY,
                   help="--text: the fleet admin key, made if missing (first run only)")
    p.add_argument("--own-passphrases", action="store_true",
                   help="--text: choose your own passphrases for new keys (default: FleetLedger "
                   "makes and keeps them)")
    p.add_argument("--no-passphrase", action="store_true",
                   help="--text: make new keys without a passphrase (not recommended)")

    p = add(sub, "doctor", "show what is set up, what is missing, and the next command",
            "Check this computer, FleetLedger's keys and inventory, the hypervisor, the\n"
            "overlay and the Ships. Read-only: it changes nothing. Each missing piece\n"
            "comes with the exact command that fixes it.")
    p.add_argument("--offline", action="store_true",
                   help="do not contact the Proxmox API")


# --- doctor ---------------------------------------------------------------------------------


def cmd_doctor(args, app) -> int:
    items = firstrun.everything(app, offline=args.offline)
    rows = [[MARK[i.state], i.area, i.name, i.detail] for i in items]
    app.say(table(rows, ["", "AREA", "CHECK", "DETAIL"]))
    todo = [i for i in items if i.state in ("missing", "fail", "warn", "info") and i.next]
    if todo:
        app.say("\nNext:")
        for item in dict.fromkeys(i.next for i in todo):
            app.say(f"  {item}")
    if not any(i.state in ("missing", "fail", "warn") for i in items):
        app.say("\nEverything is in place.")
    return 1 if any(i.state in ("missing", "fail") for i in items) else 0


# --- setup ----------------------------------------------------------------------------------


class _Setup:
    def __init__(self, args, app) -> None:
        self.args, self.app = args, app
        self.store = app.store_factory()
        self.number = 0
        self.later: list[str] = []  # what is left for the user, said at the end
        self.hypervisor_ok = True  # False: set up but not working right now
        self.run_cmd = getattr(app, "run_cmd", None) or firstrun._run
        self.run_tty = getattr(app, "run_tty", None) or _run_tty
        # New keys: FleetLedger makes and keeps their passphrases (keys.py), unless asked.
        own = getattr(args, "own_passphrases", False) or args.no_passphrase
        self.managed = None if own else keys.default_store()
        self.made_yours: str | None = None

    def head(self) -> None:
        self.number += 1
        self.app.say(f"\n== {self.number}/{len(STEPS)}  {STEPS[self.number - 1]} ==")

    def ask(self, prompt: str, default: str = "") -> str:
        from fleetledger.overlay_setup import _ask

        return _ask(self.app, prompt, default)

    def choose(self, prompt: str, choices: tuple[str, ...], default: str) -> str:
        from fleetledger.overlay_setup import _choose

        return _choose(self.app, prompt, choices, default)

    def yes(self, prompt: str, default: str = "yes") -> bool:
        return self.choose(prompt, ("yes", "no"), default) == "yes"


def _run_tty(argv: list[str]) -> int:
    """Run a command on this terminal (it may ask for a passphrase itself)."""
    try:
        return subprocess.run(argv, check=False).returncode
    except OSError:
        return 127


def cmd_setup(args, app) -> int:
    if not args.text:
        if args.redo:
            raise FleetError("--redo is for the terminal wizard: ship-fleet setup --text --redo "
                             "...; on the setup page, each step has its own Change button.")
        from fleetledger.web.server import serve

        return serve(app, port=args.port, open_browser=not args.no_browser,
                     start_page="/setup")
    if not app.stdin_isatty():
        raise FleetError("setup asks questions; run it in a terminal.",
                         hint="ship-fleet doctor shows what is set up without asking anything.")
    s = _Setup(args, app)
    app.say("ShipShape setup. Each step checks first and skips what is already done;\n"
            "stop whenever you like (Ctrl-C) and run ship-fleet setup again to carry on.")
    _computer(s)
    inv = _fleet(s)
    agent_ok = _agent(s, inv)
    _ca(s)
    if not _hypervisor(s):
        _left_for_later(s)
        return 1
    _overlay(s)
    _first_ship(s, agent_ok)

    app.say("\n== Done ==")
    _left_for_later(s)
    app.say("ship-fleet doctor shows the state of everything at any time; ship-fleet ls "
            "lists your Ships.")
    return 0


def _left_for_later(s: _Setup) -> None:
    if s.later:
        s.app.say("Left for later:")
        for line in s.later:
            s.app.say(f"  {line}")


def _computer(s: _Setup) -> None:
    s.head()
    items = firstrun.computer(s.app)
    for item in items:
        if item.state != "ok":
            s.app.say(f"  {MARK[item.state]}: {item.name}: {item.detail}")
            if item.next:
                s.app.say(f"        fix: {item.next}")
    if any(i.state == "fail" for i in items):
        raise FleetError("Fix what is marked FAIL above, then run ship-fleet setup again.")
    s.app.say("Python, OpenSSH and the ShipShape checkout are in place.")


def _fleet(s: _Setup):
    s.head()
    if s.store.exists():
        inv = s.store.load()
        s.app.say(f"Already set up: {s.store.path} (admin key {inv.admin_key}).")
        return inv
    from fleetledger.commands import make_fleet

    if s.managed:
        s.app.say("FleetLedger keeps an inventory of your Ships and logs in to them with a key\n"
                  f"of its own. It makes its keys and keeps their passphrases in "
                  f"{keys.describe(s.managed)}:\nyou never type them. You attach to Ships with "
                  "a key of your own.")
    else:
        s.app.say("FleetLedger keeps an inventory of your Ships and logs in to them with its "
                  "own\nkey (the fleet admin key, with a passphrase; ssh-agent holds it). You "
                  "attach\nto Ships with a key of your own.")
    agent_key = _your_key(s)
    make_fleet(s.app, s.store, s.args.admin_key, [agent_key],
               no_passphrase=s.args.no_passphrase, managed=s.managed)
    with s.store.edit() as ed:  # not chosen yet: step 5 asks (manual is the default)
        ed.set_default("driver", None)
        if s.made_yours:
            keys.record(ed, s.managed, [s.made_yours])
    return s.store.load()


def _your_key(s: _Setup) -> str:
    default = Path.home() / ".ssh" / "id_ed25519.pub"
    if default.exists():
        answer = s.ask(f"Your public key for attaching to Ships ({_tilde(default)}): ")
        chosen = paths.expand(answer) if answer else default
    elif s.managed and s.yes(f"You have no {_tilde(default)}. Make a key for you now "
                             "(FleetLedger looks after it)"):
        from fleetledger.web.setup import YOUR_NEW_KEY

        private = paths.expand(YOUR_NEW_KEY)
        if not private.exists():
            keys.make(private, "shipshape-you", s.managed)
        s.made_yours = YOUR_NEW_KEY
        chosen = private.with_name(private.name + ".pub")
    elif not s.managed and s.yes(f"You have no {_tilde(default)}. Make one now (ssh-keygen "
                                 "asks for a passphrase)"):
        private = default.with_suffix("")
        paths.private_dir(private.parent)
        argv = ["ssh-keygen", "-t", "ed25519", "-f", str(private)]
        if s.args.no_passphrase:
            argv += ["-N", ""]
        if s.run_tty(argv) != 0:
            raise FleetError("ssh-keygen did not make the key.")
        chosen = default
    else:
        chosen = paths.expand(s.ask("Path to your public key (.pub): "))
    if not chosen.name.endswith(".pub") or not chosen.is_file():
        raise FleetError(f"{chosen} is not a public key file (.pub).")
    return str(chosen)


def _agent(s: _Setup, inv) -> bool:
    """Is the admin key in ssh-agent (loading it if the user agrees)?"""
    s.head()
    admin, public = inv.admin_key, inv.admin_public_key_path
    if admin in keys.managed(inv)[1]:
        try:
            loaded = keys.ensure_loaded(inv)
        except FleetError as exc:
            s.app.note(f"Could not load FleetLedger's keys into ssh-agent: {exc.message}")
            s.later.append("ship-fleet doctor   (FleetLedger's keys did not load)")
            return False
        s.app.say("Loaded FleetLedger's keys into ssh-agent." if loaded else
                  "ssh-agent already holds FleetLedger's keys.")
        return True
    held = firstrun.agent_fingerprints(s.run_cmd)
    if held is None:
        s.app.say("No ssh-agent answers in this shell. FleetLedger logs in to Ships through\n"
                  "it. Start one and load the key, then run setup again:\n"
                  f'  eval "$(ssh-agent)" && ssh-add {_tilde(admin)}\n'
                  "Carrying on with the steps that do not need it.")
        s.later.append(f'eval "$(ssh-agent)" && ssh-add {_tilde(admin)}')
        return False
    fpr = firstrun.fingerprint(str(public), s.run_cmd)
    if fpr and fpr in held:
        s.app.say("ssh-agent already holds the fleet admin key.")
        return True
    if s.yes("Load the fleet admin key into ssh-agent now (asks its passphrase once)"):
        s.run_tty(["ssh-add", str(admin)])
        held = firstrun.agent_fingerprints(s.run_cmd) or []
        if fpr and fpr in held:
            s.app.say("Loaded.")
            return True
        s.app.note("The key is still not in ssh-agent.")
    s.later.append(f"ssh-add {_tilde(admin)}")
    return False


def _ca(s: _Setup) -> None:
    from fleetledger.trustcmd import make_ca

    s.head()
    inv = s.store.load()
    cfg = inv.doc.get("ca")
    if cfg:
        keys = [paths.expand(cfg[k]) for k in ("host_key", "user_key") if k in cfg]
        gone = [k for k in keys if not k.exists()]
        if not gone:
            s.app.say("Already set up (host and user CA keys).")
            return
        s.app.say("The CA is in ships.toml, but these key files are gone: "
                  + ", ".join(str(k) for k in gone))
        s.later.append("ship-fleet ca init --force   (new CA keys; then ship-fleet ca sign "
                       "--force and trust your machines again)")
        return
    s.app.say("FleetLedger can certify Ships' host keys and your machines' keys, so you\n"
              "attach to any Ship from any of your machines without host-key prompts.\n"
              + ("It needs two keys of its own; FleetLedger makes and looks after them."
                 if s.managed else "It needs two keys of its own, each with a passphrase."))
    if not s.yes("Set up the certificate authority now"):
        s.later.append("ship-fleet ca init")
        return
    cfg = make_ca(s.app, s.store, HOST_CA_KEY, USER_CA_KEY, no_passphrase=s.args.no_passphrase,
                  managed=s.managed)
    s.app.say(f"Host CA {ca.fingerprint(cfg.host_public)}")
    s.app.say(f"User CA {ca.fingerprint(cfg.user_public)}")
    if not s.managed and firstrun.agent_fingerprints(s.run_cmd) is not None and s.yes(
            "Load the CA keys into ssh-agent too, so signing does not ask again"):
        s.run_tty(["ssh-add", str(paths.expand(HOST_CA_KEY)), str(paths.expand(USER_CA_KEY))])


def _hypervisor(s: _Setup) -> bool:
    """False when setup has to stop (something to do on the Proxmox host first)."""
    from fleetledger import proxmox_setup

    s.head()
    inv = s.store.load()
    redo = "hypervisor" in s.args.redo
    driver = inv.defaults.get("driver", "manual")
    config = inv.driver_config("proxmox")
    if not redo and driver == "proxmox" and config:
        items = firstrun.hypervisor(s.app, inv)
        bad = [i for i in items if i.state in ("fail", "missing")]
        if not bad:
            s.app.say(f"Proxmox, already set up and working ({config['api_url']}).")
            return True
        for item in bad:
            s.app.say(f"  {MARK[item.state]}: {item.name}: {item.detail}")
        s.app.say("To change or repair it: ship-fleet setup --redo hypervisor")
        s.later.append("ship-fleet setup --redo hypervisor   (Proxmox: see above)")
        s.hypervisor_ok = False
        return True
    if not redo and "driver" in inv.defaults and driver == "manual":
        s.app.say("Manual: FleetLedger prints the steps for your hypervisor. To use Proxmox:\n"
                  "  ship-fleet setup --redo hypervisor")
        return True
    s.app.say("Where should Ships run?\n"
              "  proxmox  FleetLedger creates and removes the VMs itself, through a Proxmox VE\n"
              "           API token scoped to one pool.\n"
              "  manual   FleetLedger writes each Ship's seed ISO and prints the steps to make\n"
              "           the VM on any hypervisor.")
    choice = s.choose("Hypervisor", ("proxmox", "manual"), "proxmox")
    if choice == "manual":
        with s.store.edit() as ed:
            ed.set_default("driver", "manual")
        s.app.say("Manual it is.")
        return True
    result = proxmox_setup.run(s.app, s.store, inv, versions=s.app.repo_factory().versions)
    return result.saved


def _overlay(s: _Setup) -> None:
    from fleetledger import overlay_setup

    s.head()
    inv = s.store.load()
    redo = "overlay" in s.args.redo
    if inv.overlay and not redo:
        s.app.say(f"Already set up ({inv.overlay.get('provider', 'tailscale')}).")
        return
    s.app.say("An overlay network (Tailscale, or your own Headscale) lets you reach Ships\n"
              "from anywhere, with no router ports opened. You can add it later.")
    if s.choose("Set it up now", ("yes", "skip"), "skip") != "yes":
        s.later.append("ship-fleet overlay setup   (optional: reach Ships off your LAN)")
        return
    overlay_setup.setup(argparse.Namespace(force=bool(inv.overlay)), s.app)


def _first_ship(s: _Setup, agent_ok: bool) -> None:
    from fleetledger import cli, commands

    s.head()
    inv = s.store.load()
    ships = inv.ships()
    if ships:
        s.app.say(f"You have {len(ships)} Ship(s): " + ", ".join(x.name for x in ships) + ".")
        return
    if not agent_ok or not s.hypervisor_ok:
        need = ("the admin key in ssh-agent (step 3)" if not agent_ok
                else "a working hypervisor (step 5)")
        s.app.say(f"Making a Ship needs {need}. Once it is there:\n  ship-fleet new ship-01")
        s.later.append("ship-fleet new ship-01, then ship-fleet login ship-01 all")
        return
    while True:
        name = s.ask("Make a first Ship now? Its name (ship-01), or skip: ", "ship-01")
        if name.lower() == "skip":
            s.later.append("ship-fleet new ship-01, then ship-fleet login ship-01 all")
            return
        if commands.NAME_RE.match(name):
            break
        s.app.note("A Ship name is a-z, 0-9 and -, up to 31 characters, starting with a "
                   "letter or digit.")
    tag = s.ask("A tag for it, e.g. work or personal (none): ")
    parser = cli.build_parser()
    argv = ["new", name] + (["--tag", tag] if tag else [])
    code = commands.cmd_new(parser.parse_args(argv), s.app)
    ship = s.store.load().ship(name) if code == 0 else None
    if ship is None or ship.enrolled is None:
        s.later.append(f"ship-fleet status {name} --wait, then ship-fleet login {name} all")
        return
    if s.yes("Sign it in now: Claude Code, gcloud and GitHub, through the concierge"):
        commands.cmd_login(parser.parse_args(["login", name, "all"]), s.app)
    else:
        s.later.append(f"ship-fleet login {name} all")


def _tilde(path) -> str:
    try:
        return "~/" + str(Path(path).resolve().relative_to(Path.home().resolve()))
    except ValueError:
        return str(path)
