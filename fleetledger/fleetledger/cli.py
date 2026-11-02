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
    "set", "tag", "tools", "ssh", "logs",
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

    p = _add(sub, "set", "change a Ship's settings (ship-ctl set)",
             "Change settings on a Ship. Keys: remote_control (on|off; blocked = off),\n"
             "default_mode, allow_bypass, update_channel (latest|stable), session_prefix,\n"
             "sudo, lan_guard, tags; or their full names (claude.remote_control, ...).\n\n"
             "  ship-fleet set ship-01 remote_control=off default_mode=bypassPermissions")
    p.add_argument("name")
    p.add_argument("assignments", nargs="+", metavar="KEY=VALUE")

    p = _add(sub, "tag", "show or change a Ship's tags",
             "  ship-fleet tag ship-01            show its tags\n"
             "  ship-fleet tag ship-01 +work -personal\n"
             "  ship-fleet tag ship-01 none       clear them (no defaults.tags either)")
    p.add_argument("name")
    p.add_argument("changes", nargs="*", metavar="+TAG|-TAG")
    p.add_argument("--local", action="store_true", help="change the inventory only")

    p = _add(sub, "tools", "list catalogue tools, or add and remove them on a Ship",
             "  ship-fleet tools ls\n  ship-fleet tools add ship-01 tasks-axi\n"
             "  ship-fleet tools remove ship-01 tasks-axi")
    p.add_argument("action", choices=("ls", "add", "remove"))
    p.add_argument("rest", nargs="*", metavar="SHIP ID")

    p = _add(sub, "ssh", "open a shell on a Ship (as agent by default)")
    p.add_argument("name")
    p.add_argument("--as", dest="user", choices=("agent", "fleet"), default="agent",
                   help="agent (your key) or fleet (the admin key)")
    p.add_argument("remote", nargs="*",
                   help="command to run instead of a shell (put it after --)")

    p = _add(sub, "logs", "show a Ship's logs (ship-ctl logs)")
    p.add_argument("name")
    p.add_argument("source", choices=LOG_SOURCES)
    p.add_argument("--lines", type=int, default=200)
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


def main(argv: Sequence[str] | None = None, app: App | None = None) -> int:
    from fleetledger import commands

    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    remote = None
    if argv[:1] == ["ssh"] and "--" in argv:  # everything after -- runs on the Ship
        cut = argv.index("--")
        argv, remote = argv[:cut], argv[cut + 1 :]
    args = parser.parse_args(_protect_tag_removals(argv))
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
