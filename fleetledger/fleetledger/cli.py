"""ship-fleet: find, show and manage ShipShape Ships.

Every command is a function taking the parsed arguments and an App, which holds
everything that touches the outside world (inventory, repo, SSH, drivers,
prompts), so each command can be tested without a network or a Ship.
"""

from __future__ import annotations

import argparse
import ipaddress
import re
import socket
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TextIO

from fleetledger import __version__
from fleetledger.errors import FleetError

COMMANDS = (
    "setup", "doctor", "init", "new", "adopt", "scan", "ls", "status", "login", "logout",
    "signins", "owner", "set", "agent", "tag", "tools", "update", "reboot", "attention", "pins",
    "ssh", "logs", "ca", "trust", "untrust", "net", "ui", "destroy", "resize", "snapshot",
    "image", "overlay", "put", "inbox", "migrate", "service", "unlock", "vault",
    "appliance", "backup", "self-update",
)  # fmt: skip

# Modules whose commands live outside commands.py; each has add_parsers(sub, add)
# and cmd_<name> handlers.
EXTRA_COMMAND_MODULES = ("fleetledger.lifecycle", "fleetledger.access", "fleetledger.setupcmd",
                         "fleetledger.inbox", "fleetledger.migrate", "fleetledger.service",
                         "fleetledger.vaultcmd", "fleetledger.appliance", "fleetledger.backup",
                         "fleetledger.selfupdate")


@dataclass
class App:
    """The outside world, swappable in tests."""

    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    store_factory: Callable = None  # () -> Store
    repo_factory: Callable = None  # () -> Repo
    ssh_factory: Callable = None  # (Inventory) -> SSH
    driver_factory: Callable = None  # (Inventory, driver id) -> Driver
    prompter_factory: Callable = None  # (ship, label, open_browser) -> Prompter
    spawner: Callable | None = None  # concierge terminal spawner (tests)
    browse: Callable = None  # (timeout) -> [Announcement]
    keyscan: Callable = None  # (host) -> "ssh-ed25519 AAAA..."
    confirm: Callable[[str], str] = input
    resolves: Callable[[str], bool] = None  # (host) -> does it resolve here?
    sleep: Callable[[float], None] = None
    clock: Callable[[], float] = time.monotonic
    http_factory: Callable = None  # () -> pins.Http
    herdr: bool = True  # installing a trust bundle here may add herdr saved machines
    # (args) -> stdout of the local `tailscale` command (read-only; web/remote.py)
    tailscale: Callable[[list[str]], str] | None = None
    # (argv) -> (exit code, stdout, stderr) of systemctl or loginctl (service.py)
    system: Callable[[list[str]], tuple[int, str, str]] | None = None
    stdin_isatty: Callable[[], bool] = field(default_factory=lambda: sys.stdin.isatty)

    def __post_init__(self) -> None:
        from fleetledger import scan
        from fleetledger.concierge.terminal import TerminalPrompter
        from fleetledger.drivers import get_driver
        from fleetledger.repo import Repo
        from fleetledger.store import TomlFileStore

        self.store_factory = self.store_factory or TomlFileStore
        self.repo_factory = self.repo_factory or Repo.locate
        self.ssh_factory = self.ssh_factory or _ssh_with_managed_keys
        self.driver_factory = self.driver_factory or get_driver
        self.prompter_factory = self.prompter_factory or (
            lambda ship, label, open_browser: TerminalPrompter(
                ship, label, stream=self.err, open_browser=open_browser
            )
        )
        self.browse = self.browse or scan.browse
        self.resolves = self.resolves or _resolves
        self.sleep = self.sleep or time.sleep
        self.keyscan = self.keyscan or scan.keyscan
        if self.http_factory is None:
            from fleetledger.pins import Http

            self.http_factory = Http

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def note(self, text: str) -> None:
        print(text, file=self.err)


def _ssh_with_managed_keys(inventory):
    """SSH to Ships, after loading any keys FleetLedger manages into ssh-agent."""
    from fleetledger import keys
    from fleetledger.ssh import SSH

    keys.ensure_loaded(inventory)
    return SSH(inventory)


RESOLVE_TIMEOUT = 2.0


def _resolves(host: str, timeout: float = RESOLVE_TIMEOUT) -> bool:
    """Does host resolve here? An address answers at once; a name gets `timeout`
    seconds, so an mDNS .local name nobody answers does not stall a command."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    result: list[bool] = []

    def lookup() -> None:
        try:
            socket.getaddrinfo(host, 22)
            result.append(True)
        except OSError:
            result.append(False)

    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(timeout)
    return bool(result and result[0])


class _TopHelp(argparse.HelpFormatter):
    """Line command names up with their help even when one is long (argparse
    measures subcommands one indent level too shallow)."""

    def add_argument(self, action):
        super().add_argument(action)
        if action.help is not argparse.SUPPRESS:
            for sub in self._iter_indented_subactions(action):
                width = len(self._format_action_invocation(sub)) + self._current_indent
                self._action_max_length = max(self._action_max_length, width)


def _add(sub, name: str, help_text: str, description: str | None = None):
    parser = sub.add_parser(
        name,
        help=help_text,
        description=description or help_text,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(command=name)
    return parser


def build_parser() -> argparse.ArgumentParser:
    from fleetledger.commands import LOG_SOURCES, MODES

    parser = argparse.ArgumentParser(
        prog="ship-fleet",
        description="Find, show and manage ShipShape Ships.",
        epilog="Run `ship-fleet <command> --help` for a command's options.",
        formatter_class=_TopHelp,
    )
    parser.add_argument("--version", action="version", version=f"ship-fleet {__version__}")
    sub = parser.add_subparsers(title="commands", metavar="<command>")

    p = _add(sub, "init", "set up FleetLedger: the fleet admin key and an empty inventory",
             "Create the fleet admin key (if needed) and an empty inventory at\n"
             "~/.config/fleetledger/ships.toml.")
    p.add_argument("--admin-key", default="~/.ssh/shipshape_fleet_ed25519",
                   help="private key FleetLedger logs in to Ships with (made if missing)")
    p.add_argument("--agent-key", action="append", default=[],
                   help="public key put on each Ship's agent user for attaching "
                   "(repeatable; default ~/.ssh/id_ed25519.pub)")
    p.add_argument("--no-passphrase", action="store_true",
                   help="make the admin key without a passphrase (not recommended)")

    p = _add(sub, "new", "create a Ship: write its seed and hand it to a driver",
             "Create a Ship: pin a new host key, write its seed ISO, record it in the\n"
             "inventory, then have the driver create the VM (manual: print the steps).")
    p.add_argument("name", help="Ship name, also its hostname (e.g. ship-03)")
    p.add_argument("--tag", action="append", default=[], help="tag (repeatable)")
    p.add_argument("--mode", choices=MODES, help="default permission mode for new agents")
    p.add_argument("--allow-bypass", action="store_true",
                   help="allow bypassPermissions here (implied by --mode bypassPermissions)")
    p.add_argument("--tools", help="extra catalogue tools, comma-separated")
    p.add_argument("--no-default-tools", action="store_true",
                   help="install only the tools given with --tools")
    p.add_argument("--profile", action="append", default=[], help="profile (repeatable)")
    p.add_argument("--driver", "--on", dest="driver", choices=("manual", "proxmox"),
                   help="where to create it: manual prints the steps; proxmox does it "
                   "through the API (default: defaults.driver)")
    p.add_argument("--image", metavar="ID|stock",
                   help="base image (default: the pinned one, defaults.image; see "
                   "ship-fleet image ls)")
    p.add_argument("--vmid", type=int, help="VM id to use (Proxmox)")
    p.add_argument("--host", action="append", default=[],
                   help="name to reach the Ship by (repeatable; default <name>.local)")
    p.add_argument("--cores", type=int)
    p.add_argument("--memory-mb", type=int)
    p.add_argument("--disk-gb", type=int)
    p.add_argument("--autostart", action="append", default=[], metavar="NAME:CWD[:MODE]",
                   help="agent to start at boot, e.g. main:~/work/main:auto (repeatable)")
    p.add_argument("--autostart-prompt", metavar="TEXT",
                   help="first prompt for each autostart agent (default: the Ship's own, "
                   "'Get oriented and ready to work.')")
    p.add_argument("--agent-sudo", action="store_true", help="give the agent user sudo")
    p.add_argument("--timezone", metavar="ZONE",
                   help="the Ship's time zone, e.g. Europe/Berlin (default: this computer's)")
    p.add_argument("--no-wait", action="store_true",
                   help="do not wait for the Ship to provision and enrol")
    p.add_argument("--repo", metavar="OWNER/NAME",
                   help="a GitHub repo for the first agent to work in: cloned into "
                   "~/work/NAME first (a private one after the GitHub sign-in)")
    p.add_argument("--signins", metavar="claude,gcloud,github",
                   help="the sign-ins it uses (default: all); see ship-fleet signins")
    p.add_argument("--github-scopes", metavar="workflow,...",
                   help="extra GitHub scopes its GitHub sign-in asks for (default: none, "
                   "gh's minimum): workflow, project, write:packages")
    p.add_argument("--no-overlay", action="store_true",
                   help="do not join the overlay after it enrols (with [overlay] set up)")
    p.add_argument("--yes", action="store_true",
                   help="remove what a failed attempt at this name left on the hypervisor "
                   "without asking (it is removed first, so there is never a second VM)")

    p = _add(sub, "adopt", "enrol a Ship FleetLedger did not create, after checking its key",
             "Pin the host key of a running Ship after you compare its fingerprint with\n"
             "the one cloud-init prints on the VM console. Also re-pins a rebuilt Ship.")
    p.add_argument("name", help="the Ship's name")
    p.add_argument("--host", help="name or address to reach it by (default <name>.local)")
    p.add_argument("--fingerprint", help="expected SHA256:... fingerprint, instead of asking")
    p.add_argument("--tag", action="append", default=[],
                   help="tags (default: the tags the Ship reports)")
    p.add_argument("--replace", action="store_true", help="re-pin a Ship already in the inventory")

    p = _add(sub, "scan", "list Ships announcing themselves on the LAN (hints only)")
    p.add_argument("--timeout", type=float, default=3.0, help="seconds to listen (default 3)")

    p = _add(sub, "ls", "list Ships with their state and sign-ins")
    p.add_argument("--tag", help="only Ships with this tag")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--offline", action="store_true", help="show the last known status only")

    p = _add(sub, "status", "show one Ship in detail; --wait to finish enrolling it")
    p.add_argument("name")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--wait", action="store_true",
                   help="wait until it has provisioned, then finish enrolment")

    services = "claude, gcloud (with ADC), gcloud-adc, github or all"
    p = _add(sub, "login", "sign Ships in to a service through the concierge",
             "Sign Ships in to a service. FleetLedger runs the tool's own sign-in on the\n"
             "Ship, shows you the link, types the code you paste, and checks the account\n"
             f"against the Ship's tags. Services: {services}.\n\n"
             "  ship-fleet login ship-01 claude\n  ship-fleet login --tag work all\n"
             "  ship-fleet login ship-01 github --scopes workflow")
    p.add_argument("args", nargs="+", metavar="[SHIP ...] SERVICE")
    p.add_argument("--tag", help="every Ship with this tag")
    p.add_argument("--open", action="store_true", help="open each link in your browser")
    p.add_argument("--scopes", metavar="workflow,...|none",
                   help="github: choose the Ship's extra GitHub scopes first (as ship-fleet "
                   "set SHIP github_scopes=...), then sign in with them; a Ship already "
                   "signed in has its token's scopes refreshed in place")

    p = _add(sub, "logout", "sign a Ship out of a service")
    p.add_argument("name")
    p.add_argument("service", help=services)

    p = _add(sub, "signins", "every sign-in on every Ship; choose which ones a Ship uses",
             "With no Ship named: every sign-in on every Ship, across every service, most\n"
             "urgent first: what needs you (expired, wrong account, missing while something\n"
             "needs it), then what is degraded or unknown, then what is fine, then what a\n"
             "Ship does not use; within a group the oldest first. The same list as the web\n"
             "UI's Sign-ins page, each with the command that fixes it.\n\n"
             "  ship-fleet signins                               every sign-in\n"
             "  ship-fleet signins --group action                only what needs you\n"
             "  ship-fleet signins --tag work --service claude   narrowed\n\n"
             "Named, it shows or changes which sign-ins that Ship uses:\n\n"
             "  ship-fleet signins ship-01                       show them\n"
             "  ship-fleet signins ship-01 --off gcloud          stop using one (signs it out)\n"
             "  ship-fleet signins ship-01 --on gcloud           use it again\n"
             "  ship-fleet signins ship-01 --only claude,github  exactly these\n\n"
             "A sign-in a Ship does not use gets no prompt, no sign-in flow and no attention\n"
             "item, and is shown as off; turning one off signs it out on the Ship, so no\n"
             "credentials for it stay there. Its tool may stay installed.")
    p.add_argument("name", nargs="?", help="a Ship (omit it: every sign-in on every Ship)")
    p.add_argument("--on", action="append", default=[], metavar="NAME")
    p.add_argument("--off", action="append", default=[], metavar="NAME")
    p.add_argument("--only", metavar="NAME,NAME", help="exactly these (empty: none)")
    p.add_argument("--tag", help="list: only Ships with this tag")
    p.add_argument("--service", metavar="SERVICE",
                   help="list: only this service (claude, gcloud, gcloud_adc, github)")
    p.add_argument("--group", choices=("action", "underway", "degraded", "healthy", "off"),
                   help="list: only this group (action: what needs you)")
    p.add_argument("--json", action="store_true", help="list: print JSON")
    p.add_argument("--offline", action="store_true", help="list: the last known status only")

    p = _add(sub, "set", "change settings on Ships (ship-ctl set)",
             "Change settings on one Ship, several, every Ship with a tag, or all.\n"
             "Keys: remote_control (on|off; blocked = off), default_mode, allow_bypass,\n"
             "update_channel (latest|stable), session_prefix, sudo, lan_guard,\n"
             "lan_guard_allow (addresses or CIDRs, comma-separated), auto_reboot,\n"
             "reboot_time (HH:MM, the Ship's local time), timezone (e.g. Europe/Berlin),\n"
             "tags; or their full names (claude.remote_control, ...).\n"
             "Agents: autostart.add=NAME:FOLDER[:MODE] starts one now (once Claude is\n"
             "signed in) and on every boot; autostart.remove=NAME stops starting it.\n\n"
             "  ship-fleet set ship-01 remote_control=off default_mode=bypassPermissions\n"
             "  ship-fleet set ship-01 autostart.add=main:~/work\n"
             "  ship-fleet set --tag work update_channel=stable\n"
             "  ship-fleet set all lan_guard=true")
    p.add_argument("args", nargs="+", metavar="[SHIP ...|all] KEY=VALUE")
    p.add_argument("--tag", help="every Ship with this tag")

    p = _add(sub, "agent", "restart running agents in a new permission mode, in place",
             "Swap running Claude agents for ones in another mode. Each restarts in its own\n"
             "herdr pane, with the same name and folder, and continues its conversation\n"
             "(claude --resume); Remote Control and its session name come back as before.\n"
             "Without --mode, each takes the mode it would start in now: its own autostart\n"
             "mode, else the Ship's default_mode. The Ship records the new mode, so a\n"
             "reboot or update resumes it in that mode.\n\n"
             "An agent that is working is left alone unless --force (which interrupts its\n"
             "turn); one waiting for an answer has that question dismissed. bypassPermissions\n"
             "needs allow_bypass=true on the Ship.\n\n"
             "  ship-fleet agent restart ship-01 main --mode bypassPermissions\n"
             "--all restarts every named agent, also one already in the mode it starts\n"
             "in (so a restart in the same mode picks up other settings, such as Remote\n"
             "Control); the web UI's Apply the default mode restarts only the others.\n\n"
             "  ship-fleet agent restart ship-01 --all          after changing default_mode\n"
             "  ship-fleet agent restart ship-01 main --fresh   a new conversation")
    p.add_argument("action", choices=("restart",))
    p.add_argument("ship", metavar="SHIP")
    p.add_argument("agents", nargs="*", metavar="AGENT", help="herdr agent names")
    p.add_argument("--all", action="store_true", help="every running Claude agent with a name")
    p.add_argument("--mode", choices=MODES,
                   help="default: its autostart mode, else the Ship's default_mode")
    p.add_argument("--fresh", action="store_true",
                   help="start a new conversation instead of continuing it")
    p.add_argument("--force", action="store_true",
                   help="also restart an agent that is working, interrupting its turn")
    p.add_argument("--json", action="store_true", help="print the result as JSON")

    p = _add(sub, "tag", "show or change a Ship's tags",
             "  ship-fleet tag ship-01            show its tags\n"
             "  ship-fleet tag ship-01 +work -personal\n"
             "  ship-fleet tag ship-01 none       clear them (no defaults.tags either)")
    p.add_argument("name")
    p.add_argument("changes", nargs="*", metavar="+TAG|-TAG")
    p.add_argument("--local", action="store_true", help="change the inventory only")

    p = _add(sub, "tools", "list catalogue tools, or add and remove them on Ships",
             "  ship-fleet tools ls\n  ship-fleet tools add ship-01 tasks-axi\n"
             "  ship-fleet tools add --tag work lavish-axi tasks-axi\n"
             "  ship-fleet tools remove all tasks-axi")
    p.add_argument("action", choices=("ls", "add", "remove"))
    p.add_argument("rest", nargs="*", metavar="SHIP|all ID")
    p.add_argument("--tag", help="every Ship with this tag (then give only tool ids)")

    p = _add(sub, "update", "bring Ships to this checkout's tooling and pins",
             "Pack ship/ and versions.toml from this checkout into a tooling bundle,\n"
             "copy it to each Ship and converge (ship-ctl converge): the provisioning\n"
             "script, settings and catalogue tools run again, so pin changes (herdr,\n"
             "npm tools, uv) and recipe changes roll out. Agents keep running.\n\n"
             "A new herdr binary takes over only after a reboot. --reboot auto (the\n"
             "default) reboots exactly the Ships whose herdr changed, then checks every\n"
             "agent came back in its permission mode.\n\n"
             "It never rolls a Ship back: a Ship on a newer commit than this checkout's\n"
             "(or one this checkout does not have) is left as it is unless --force.\n\n"
             "  ship-fleet update ship-01\n  ship-fleet update --tag work\n"
             "  ship-fleet update all --reboot never")
    p.add_argument("names", nargs="*", metavar="SHIP|all")
    p.add_argument("--tag", help="every Ship with this tag")
    p.add_argument("--reboot", choices=("auto", "always", "never"), default="auto",
                   help="when to reboot after converging (default auto: only if herdr changed)")
    p.add_argument("--no-wait", action="store_true",
                   help="do not wait for rebooted Ships to come back")
    p.add_argument("--force", action="store_true",
                   help="converge even if the Ship already has this bundle, or runs a newer "
                   "ShipShape commit than this checkout (that rolls it back)")
    p.add_argument("--yes", action="store_true", help="do not ask before acting on many Ships")
    p.add_argument("--json", action="store_true", help="print the results as JSON")

    p = _add(sub, "reboot", "reboot Ships cleanly and check their agents come back",
             "Reboot with systemctl reboot (a clean reboot, never a hard reset), so herdr\n"
             "saves its state; then wait for each Ship and check every agent resumed in\n"
             "its mode.\n\n"
             "A program holding a shutdown lock (apt installing packages, say, in an\n"
             "open login session) makes the Ship refuse: FleetLedger says what holds it\n"
             "and where. Let it finish or close that session, or reboot anyway with\n"
             "--force (systemctl reboot --check-inhibitors=no): still a clean reboot in\n"
             "which herdr saves every agent, but what holds the lock is cut off.\n\n"
             "  ship-fleet reboot ship-01\n  ship-fleet reboot --tag work --yes\n"
             "  ship-fleet reboot ship-01 --force")
    p.add_argument("names", nargs="*", metavar="SHIP|all")
    p.add_argument("--tag", help="every Ship with this tag")
    p.add_argument("--force", action="store_true",
                   help="reboot even if something holds a shutdown lock (it is cut off)")
    p.add_argument("--no-wait", action="store_true", help="do not wait for the Ships")
    p.add_argument("--yes", action="store_true", help="do not ask before acting on many Ships")
    p.add_argument("--json", action="store_true", help="print the results as JSON")

    p = _add(sub, "attention", "list what needs you across the fleet (Needs attention)",
             "Waiting agents, missing, expiring or wrong-account sign-ins, failed services\n"
             "or tools, pending reboots, Ships behind on tooling or unreachable; most\n"
             "urgent first. --watch keeps polling and prints only new items.")
    p.add_argument("--tag", help="only Ships with this tag")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.add_argument("--watch", type=float, nargs="?", const=30.0, metavar="SECONDS",
                   help="poll every SECONDS (default 30) and print new items")

    p = _add(sub, "pins", "check or move the versions.toml pins in this checkout",
             "  ship-fleet pins                   herdr, uv, the Ubuntu image and npm tools\n"
             "                                    against their newest releases\n"
             "  ship-fleet pins set herdr 0.9.6   new URLs; each download must match\n"
             "                                    GitHub's published SHA-256\n"
             "  ship-fleet pins set ubuntu 20261101\n"
             "                                    hashes from the signed SHA256SUMS\n"
             "  ship-fleet pins set gh-axi 0.1.48 must exist in the npm registry\n\n"
             "Then run scripts/check, commit, and roll it out with ship-fleet update\n"
             "(docs/runbooks/update-fleet.md).")
    p.add_argument("action", nargs="?", choices=("check", "set"), default="check")
    p.add_argument("pin", nargs="?", help="herdr, uv, ubuntu or an npm package")
    p.add_argument("value", nargs="?", help="the version (or image build) to pin")
    p.add_argument("--json", action="store_true", help="print JSON (check)")

    p = _add(sub, "ssh", "open a shell on a Ship (as agent by default)")
    p.add_argument("name")
    p.add_argument("--as", dest="user", choices=("agent", "owner", "fleet"), default="agent",
                   help="agent (your key), owner (your own login, with sudo) or fleet (the "
                   "admin key)")
    p.add_argument("remote", nargs="*",
                   help="command to run instead of a shell (put it after --)")

    p = _add(sub, "owner", "your own login name on Ships (sudo, apart from agent)",
             "  ship-fleet owner              show it (default: human)\n"
             "  ship-fleet owner sam          use sam: on every Ship now, and on new Ships\n\n"
             "Your login has passwordless sudo, your keys and the same certificates as agent;\n"
             "agent keeps no sudo. Connect as yourself: ssh <owner>@<ship>, or\n"
             "ship-fleet ssh <ship> --as owner. The agent's herdr session is agent's:\n"
             "herdr --remote <ship> (or ssh <ship>) logs in as agent with the same key.")
    p.add_argument("name", nargs="?")

    p = _add(sub, "logs", "show Ships' logs (ship-ctl logs)",
             f"Sources: {', '.join(LOG_SOURCES)}.\n\n"
             "  ship-fleet logs ship-01 herdr\n"
             "  ship-fleet logs --tag work provision --lines 50   (lines prefixed by Ship)")
    p.add_argument("args", nargs="+", metavar="[SHIP ...|all] SOURCE")
    p.add_argument("--tag", help="every Ship with this tag")
    p.add_argument("--lines", type=int, default=200)

    p = _add(sub, "ca", "the SSH certificate authority: init, sign Ships, show",
             "FleetLedger as the fleet's SSH certificate authority.\n\n"
             "  ship-fleet ca init          make the host and user CA keys\n"
             "  ship-fleet ca sign [SHIP]   certify Ships' pinned host keys and install the\n"
             "                              user CA and revocation list (renews as needed)\n"
             "  ship-fleet ca show          CA fingerprints, certificates, trusted machines")
    p.add_argument("action", choices=("init", "sign", "show"))
    p.add_argument("names", nargs="*", metavar="SHIP", help="sign: these Ships (default: all)")
    p.add_argument("--tag", help="sign: every Ship with this tag")
    p.add_argument("--force", action="store_true",
                   help="sign: re-issue even if current; init: replace [ca]")
    p.add_argument("--host-key", default="~/.ssh/shipshape_host_ca_ed25519",
                   help="init: host CA private key (made if missing)")
    p.add_argument("--user-key", default="~/.ssh/shipshape_user_ca_ed25519",
                   help="init: user CA private key (made if missing)")
    p.add_argument("--no-passphrase", action="store_true",
                   help="init: make the CA keys without a passphrase (not recommended)")
    p.add_argument("--json", action="store_true", help="show: print JSON")

    p = _add(sub, "trust", "certify one of your machines to attach to Ships (herdr --remote)",
             "Give a machine a short-lived certificate for the agent user on the Ships its\n"
             "scope allows, and write its bundle: certificate, SSH config, CA line and herdr\n"
             "saved machines. Run it again to renew.\n\n"
             "  ship-fleet trust laptop --key laptop.pub --scope tag:work\n"
             "  ship-fleet trust laptop --print-cert     renew: just the new certificate")
    p.add_argument("machine", nargs="?", help="a name for the machine (e.g. laptop)")
    p.add_argument("--this-computer", action="store_true",
                   help="trust this computer with your own key and install it here, so "
                   "ssh <ship> and herdr --remote <ship> work (the web setup does this)")
    p.add_argument("--key", help="the machine's ed25519 public key (file or line); first time only")
    p.add_argument("--scope", action="append", metavar="all|tag:TAG|ship:NAME",
                   help="which Ships it may attach to (repeatable; default all)")
    p.add_argument("--logins", metavar="agent|owner|both",
                   help="what its certificates log in as: agent (attach to the agent, "
                   "herdr --remote), owner (your own login, with sudo) or both (the "
                   "default for a new machine: one of yours)")
    p.add_argument("--hours", type=int, help="certificate validity (default from [ca])")
    p.add_argument("--identity", default="~/.ssh/shipshape/id_ed25519",
                   help="path of the private key on that machine (default %(default)s)")
    p.add_argument("--out", help="bundle folder (default ~/.local/share/fleetledger/trust/NAME)")
    p.add_argument("--hostnames", choices=("here", "overlay"), default="here",
                   help="each Ship's HostName in the bundle: the first of its names that "
                   "resolves here (default), or its overlay name when it is on the overlay "
                   "(for a machine elsewhere, such as the web UI's Download bundle)")
    p.add_argument("--print-cert", action="store_true", help="print only the certificate")
    p.add_argument("--install", action="store_true",
                   help="also install the bundle here (this is the machine)")
    p.add_argument("--no-herdr", action="store_true",
                   help="install, --this-computer: skip herdr saved machines")

    p = _add(sub, "untrust", "revoke a machine's certificates on every Ship")
    p.add_argument("machine")

    p = _add(sub, "net", "where Ships connect: destinations, programs, traffic (visibility only)",
             "Network visibility. Ships record which programs reach which domains; nothing\n"
             "is blocked.\n\n"
             "  ship-fleet net                     every Ship: traffic, destinations, new ones\n"
             "  ship-fleet net ship-01 [--week]    one Ship's top destinations and programs\n"
             "  ship-fleet net --new [--tag work]  destinations first contacted in the last day\n"
             "  ship-fleet net --who example.com   which Ships reached it (or a subdomain)")
    p.add_argument("name", nargs="?", metavar="SHIP")
    p.add_argument("--tag", help="only Ships with this tag")
    p.add_argument("--new", action="store_true", help="first-seen destinations across Ships")
    p.add_argument("--who", metavar="DOMAIN", help="which Ships reached this domain")
    p.add_argument("--week", action="store_true", help="rank by the last 7 days, not 24 hours")
    p.add_argument("--offline", action="store_true", help="use the last fetched summaries")
    p.add_argument("--json", action="store_true", help="print JSON")

    p = _add(sub, "ui", "open the local web UI: dashboard and sign-ins in your browser",
             "Serve the web UI on 127.0.0.1 (this computer only) and open it with a\n"
             "one-time link. Press Enter in this terminal for a new link.\n"
             "With `ship-fleet ui remote` on, your other devices reach it too, through\n"
             "Tailscale Serve (docs/runbooks/remote-ui.md).\n"
             "See docs/web-ui.md.")
    p.add_argument("--port", type=int, default=0,
                   help="port on 127.0.0.1 (default: any free port; the remote UI's port "
                        "when that is on)")
    p.add_argument("--no-browser", action="store_true",
                   help="print the link instead of opening a browser")
    p.add_argument("--interval", type=float, default=30.0,
                   help="seconds between status checks while the UI is open (default 30)")
    p.add_argument("--local", action="store_true",
                   help="this computer only, even when the remote UI is on")
    p.add_argument("--service", action="store_true",
                   help="run as a service (what ship-fleet service install sets up): no "
                        "terminal or browser, no link printed (ship-fleet ui link --local "
                        "gives one), the poller always on, and a stop lets running jobs "
                        "finish first")
    ui_sub = p.add_subparsers(dest="ui_command", metavar="<action>", title="actions")
    r = ui_sub.add_parser(
        "remote", help="show, turn on or turn off the web UI for your other devices",
        description="Let your other devices (laptop, phone) open this computer's web UI\n"
                    "through Tailscale Serve, which gives it HTTPS at this computer's\n"
                    "MagicDNS name and forwards to 127.0.0.1:PORT. FleetLedger still listens\n"
                    "on 127.0.0.1 only.\n\n"
                    "  tailscale serve --bg 127.0.0.1:8765          first, once\n"
                    "  ship-fleet ui remote https://NAME --port 8765 --login you@example.com\n"
                    "  ship-fleet ui remote                         what is set, and the check\n"
                    "  ship-fleet ui remote --login you@example.com  who may (replaces the list)\n"
                    "  ship-fleet ui remote --off\n\n"
                    "Every start of ship-fleet ui checks that Serve sends exactly that name\n"
                    "to that port, with Funnel off, and refuses to start otherwise. Only the\n"
                    "Tailscale logins you list get past it, and each browser still pairs\n"
                    "once (ship-fleet ui link).\n"
                    "Read the risks first: docs/runbooks/remote-ui.md.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    r.add_argument("origin", nargs="?", metavar="https://NAME",
                   help="this computer's MagicDNS name, as Tailscale Serve serves it")
    r.add_argument("--port", type=int, default=None,
                   help="the loopback port Tailscale Serve forwards to")
    r.add_argument("--login", action="append", default=[], metavar="LOGIN",
                   help="a Tailscale login that may use it (repeat for more; the list given "
                        "replaces the one saved)")
    r.add_argument("--off", action="store_true", help="turn the remote web UI off")
    lk = ui_sub.add_parser(
        "link", help="a one-time link that pairs one of your devices with the remote web UI",
        description="Print a pairing link for the remote web UI: open it on the laptop or\n"
                    "phone, signed in to Tailscale as a login you allowed, while ship-fleet ui\n"
                    "runs. It works once, within 15 minutes, and the device stays signed in\n"
                    "(across restarts) until it lapses or you revoke it. Fine over ssh:\n\n"
                    "  ssh desktop ship-fleet ui link --name laptop\n"
                    "  ship-fleet ui link --name phone --qr         a QR code to scan\n\n"
                    "With --local, a one-time sign-in link for this computer's own address\n"
                    "instead (127.0.0.1), from the running ship-fleet ui or service: for a\n"
                    "browser here, or through an ssh tunnel to it.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    lk.add_argument("--local", dest="link_local", action="store_true",
                    help="a sign-in link for this computer's address (127.0.0.1), from the "
                         "running UI")
    lk.add_argument("--name", help="what to call the device (default: a guess from its "
                                   "browser, renamable in Settings)")
    lk.add_argument("--qr", action="store_true", help="also print the link as a QR code")
    lk.add_argument("--machine", help="the trusted machine the device runs on, so the UI "
                                      "shows its attach commands and bundle")
    dv = ui_sub.add_parser(
        "devices", help="list the devices paired with the remote web UI, or change one",
        description="List the paired devices; or, naming one, rename it or say which\n"
                    "trusted machine it runs on (the UI then shows that machine's attach\n"
                    "commands and offers its bundle):\n\n"
                    "  ship-fleet ui devices laptop --machine laptop\n"
                    "  ship-fleet ui devices 1a2b3c4d --name phone --no-machine",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    dv.add_argument("device", nargs="?", metavar="NAME|ID", help="the device to change")
    dv.add_argument("--name", help="its new name")
    dv.add_argument("--machine", help="the trusted machine it runs on")
    dv.add_argument("--no-machine", action="store_true", help="it runs on no trusted machine")
    rv = ui_sub.add_parser("revoke", help="sign a paired device out of the remote web UI")
    rv.add_argument("device", nargs="?", metavar="NAME|ID",
                    help="its name or id, as ship-fleet ui devices shows")
    rv.add_argument("--all", action="store_true", help="every paired device")

    import importlib

    for module in EXTRA_COMMAND_MODULES:
        importlib.import_module(module).add_parsers(sub, _add)
    return parser


TAG_CHANGE = re.compile(r"^[+-][a-z0-9][a-z0-9-]{0,30}$")


def _protect_tag_removals(argv: list[str]) -> list[str]:
    """`ship-fleet tag ship-01 -work`: keep -work from being read as an option."""
    if not argv or argv[0] != "tag" or "--" in argv:
        return argv
    changes = [a for a in argv[2:] if TAG_CHANGE.match(a)]
    if not any(c.startswith("-") for c in changes):
        return argv
    rest = [a for a in argv[2:] if not TAG_CHANGE.match(a)]
    return [*argv[:2], *rest, "--", *changes]


TAG_FIRST = {"set", "tools", "update", "reboot", "logs"}


def _hoist_tag_option(argv: list[str]) -> list[str]:
    """`ship-fleet tools add --tag work ID`: argparse cannot fill a positional list
    after an option, so --tag moves to just after the command name."""
    if not argv or argv[0] not in TAG_FIRST or "--" in argv:
        return argv
    rest, hoisted, i = [], [], 1
    while i < len(argv):
        word = argv[i]
        if word == "--tag" and i + 1 < len(argv):
            hoisted += argv[i : i + 2]
            i += 2
            continue
        if word.startswith("--tag="):
            hoisted.append(word)
        else:
            rest.append(word)
        i += 1
    return [argv[0], *hoisted, *rest]


def _in_use(args, app):
    """Every command but migrate holds FleetLedger's in-use lock (shared) while it runs,
    so ship-fleet migrate never copies the fleet in the middle of a change. Where there
    is no FleetLedger folder yet there is nothing to copy, and no lock."""
    from contextlib import nullcontext

    from fleetledger import inuse

    if args.command == "migrate":
        return nullcontext()  # it takes the lock exclusively itself
    store = app.store_factory()
    path = getattr(store, "path", None)
    if path is None or not path.parent.is_dir():
        return nullcontext()
    return inuse.using(store)


def _handed_over(args, app) -> None:
    """A copy on standby or retired (ship-fleet migrate) does not act on the fleet:
    every command but migrate's own, doctor, service, unlock and vault refuses, the web
    UI included."""
    from fleetledger import handover

    if args.command in handover.ALLOWED:
        return
    if args.command == "image" and args.action == "build" and getattr(args, "out", None):
        return  # an image file for another computer's FleetLedger touches no fleet here
    store = app.store_factory()
    if getattr(store, "path", None) is not None:
        handover.gate(args.command, store)


# What still runs while FleetLedger's vault is locked (vault.py): unlocking it, the
# vault itself, doctor, the web UI (read-only, with its Unlock page) and the service
# that runs it, updating FleetLedger's own checkout; and any read from the caches alone
# (--offline).
LOCKED_ALLOWED = ("unlock", "vault", "doctor", "ui", "service", "self-update")


def _vault_locked(args, app) -> None:
    """While the vault is locked FleetLedger is read-only: every other command refuses,
    saying how to unlock, before it starts (rather than failing half-way at the first
    secret it needs)."""
    from fleetledger import vault

    if args.command in LOCKED_ALLOWED or getattr(args, "offline", False) is True:
        return
    if args.command == "migrate" and getattr(args, "action", None) == "retire":
        return  # stepping aside needs no secret, and must not wait for an unlock
    if vault.state() == "locked":
        raise FleetError("FleetLedger's vault is locked, so FleetLedger is read-only: it "
                         "changes nothing and reaches no Ship. Your Ships keep running.",
                         hint=vault.UNLOCK_HINT + " Meanwhile: ship-fleet ls --offline.")


def main(argv: Sequence[str] | None = None, app: App | None = None) -> int:
    from fleetledger import commands

    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    remote = None
    if argv[:1] == ["ssh"] and "--" in argv:  # everything after -- runs on the Ship
        cut = argv.index("--")
        argv, remote = argv[:cut], argv[cut + 1 :]
    args = parser.parse_args(_hoist_tag_option(_protect_tag_removals(argv)))
    if remote is not None:
        args.remote = remote
    app = app or App()
    if not getattr(args, "command", None):
        parser.print_help(app.out)
        return 0
    import importlib

    name = "cmd_" + args.command.replace("-", "_")
    handler = getattr(commands, name, None)
    for module in EXTRA_COMMAND_MODULES:
        handler = handler or getattr(importlib.import_module(module), name, None)
    try:
        try:
            _handed_over(args, app)
        except FleetError as exc:
            if getattr(args, "service", False):
                # The service: a handed-over copy stays refused until someone acts, so
                # systemd should not start it again and again (service.REFUSED).
                exc.exit_code = 2
            raise
        _vault_locked(args, app)
        with _in_use(args, app):
            return handler(args, app) or 0
    except FleetError as exc:
        app.note(f"ship-fleet: {exc.message}")
        if exc.hint:
            app.note(f"  {exc.hint}")
        return exc.exit_code
    except KeyboardInterrupt:
        app.note("ship-fleet: interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
