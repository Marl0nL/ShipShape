"""ship-fleet: find, show and manage ShipShape Ships.

Every command is a function taking the parsed arguments and an App, which holds
everything that touches the outside world (inventory, repo, SSH, drivers,
prompts), so each command can be tested without a network or a Ship.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TextIO

from fleetledger import __version__
from fleetledger.errors import FleetError

COMMANDS = (
    "init", "new", "adopt", "scan", "ls", "status", "login", "logout",
    "set", "tag", "tools", "update", "reboot", "attention", "ssh", "logs", "ca", "trust",
    "untrust", "net",
)  # fmt: skip


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
    stdin_isatty: Callable[[], bool] = field(default_factory=lambda: sys.stdin.isatty)

    def __post_init__(self) -> None:
        from fleetledger import scan
        from fleetledger.concierge.terminal import TerminalPrompter
        from fleetledger.drivers import get_driver
        from fleetledger.repo import Repo
        from fleetledger.ssh import SSH
        from fleetledger.store import TomlFileStore

        self.store_factory = self.store_factory or TomlFileStore
        self.repo_factory = self.repo_factory or Repo.locate
        self.ssh_factory = self.ssh_factory or SSH
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

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def note(self, text: str) -> None:
        print(text, file=self.err)


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 22)
    except OSError:
        return False
    return True


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
    p.add_argument("--driver", choices=("manual", "proxmox"),
                   help="manual (default) prints the steps; proxmox is the phase 1 test helper")
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
             "  ship-fleet login ship-01 claude\n  ship-fleet login --tag work all")
    p.add_argument("args", nargs="+", metavar="[SHIP ...] SERVICE")
    p.add_argument("--tag", help="every Ship with this tag")
    p.add_argument("--open", action="store_true", help="open each link in your browser")

    p = _add(sub, "logout", "sign a Ship out of a service")
    p.add_argument("name")
    p.add_argument("service", help=services)

    p = _add(sub, "set", "change settings on Ships (ship-ctl set)",
             "Change settings on one Ship, several, every Ship with a tag, or all.\n"
             "Keys: remote_control (on|off; blocked = off), default_mode, allow_bypass,\n"
             "update_channel (latest|stable), session_prefix, sudo, lan_guard,\n"
             "lan_guard_allow (addresses or CIDRs, comma-separated), auto_reboot,\n"
             "reboot_time (HH:MM, the Ship's local time), timezone (e.g. Europe/Berlin),\n"
             "tags; or their full names (claude.remote_control, ...).\n\n"
             "  ship-fleet set ship-01 remote_control=off default_mode=bypassPermissions\n"
             "  ship-fleet set --tag work update_channel=stable\n"
             "  ship-fleet set all lan_guard=true")
    p.add_argument("args", nargs="+", metavar="[SHIP ...|all] KEY=VALUE")
    p.add_argument("--tag", help="every Ship with this tag")

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
             "  ship-fleet update ship-01\n  ship-fleet update --tag work\n"
             "  ship-fleet update all --reboot never")
    p.add_argument("names", nargs="*", metavar="SHIP|all")
    p.add_argument("--tag", help="every Ship with this tag")
    p.add_argument("--reboot", choices=("auto", "always", "never"), default="auto",
                   help="when to reboot after converging (default auto: only if herdr changed)")
    p.add_argument("--no-wait", action="store_true",
                   help="do not wait for rebooted Ships to come back")
    p.add_argument("--force", action="store_true",
                   help="converge even if the Ship already has this bundle")
    p.add_argument("--yes", action="store_true", help="do not ask before acting on many Ships")
    p.add_argument("--json", action="store_true", help="print the results as JSON")

    p = _add(sub, "reboot", "reboot Ships cleanly and check their agents come back",
             "Reboot with systemctl reboot (never forced), so herdr saves its state;\n"
             "then wait for each Ship and check every agent resumed in its mode.\n\n"
             "  ship-fleet reboot ship-01\n  ship-fleet reboot --tag work --yes")
    p.add_argument("names", nargs="*", metavar="SHIP|all")
    p.add_argument("--tag", help="every Ship with this tag")
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

    p = _add(sub, "ssh", "open a shell on a Ship (as agent by default)")
    p.add_argument("name")
    p.add_argument("--as", dest="user", choices=("agent", "fleet"), default="agent",
                   help="agent (your key) or fleet (the admin key)")
    p.add_argument("remote", nargs="*",
                   help="command to run instead of a shell (put it after --)")

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
    p.add_argument("machine", help="a name for the machine (e.g. laptop)")
    p.add_argument("--key", help="the machine's ed25519 public key (file or line); first time only")
    p.add_argument("--scope", action="append", metavar="all|tag:TAG|ship:NAME",
                   help="which Ships it may attach to (repeatable; default all)")
    p.add_argument("--hours", type=int, help="certificate validity (default from [ca])")
    p.add_argument("--identity", default="~/.ssh/shipshape/id_ed25519",
                   help="path of the private key on that machine (default %(default)s)")
    p.add_argument("--out", help="bundle folder (default ~/.local/share/fleetledger/trust/NAME)")
    p.add_argument("--print-cert", action="store_true", help="print only the certificate")
    p.add_argument("--install", action="store_true",
                   help="also install the bundle here (this is the machine)")
    p.add_argument("--no-herdr", action="store_true", help="install: skip herdr saved machines")

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
    handler = getattr(commands, "cmd_" + args.command.replace("-", "_"))
    try:
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
