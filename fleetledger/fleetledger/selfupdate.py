"""Update FleetLedger itself: `ship-fleet self-update` (docs/runbooks/fleetledger-vm.md).

FleetLedger runs from its ShipShape checkout (installed editable), and that checkout
is also the next Ship tooling (`ship-fleet update` packs ship/ from it). So updating
FleetLedger is moving the checkout, and only that:

1. refuse uncommitted changes in the checkout (nothing of yours is ever thrown away);
2. fetch, and find the commit asked for (--to REF: a tag, a branch, a commit;
   default origin/main);
3. refuse a commit that is not ahead of this one, unless --force: going back would
   take FleetLedger back too, and with it the tooling the next `update` puts on Ships
   (the same rule `update` keeps for Ships);
4. move to it: a branch checked out (a desktop on main) is fast-forwarded, so git
   pull keeps working; otherwise (the VM's detached checkout, or --force back) it is
   checked out detached;
5. run fleetledger/install.sh again only if the install itself changed
   (fleetledger/pyproject.toml or install.sh); if that fails, the checkout goes back
   to where it was, so a running FleetLedger keeps code that matches its install;
6. restart the service, if FleetLedger runs as one: it lets running jobs finish first.

FleetLedger is installed editable, so between moving the checkout and the restart a
running service may load a module or two from the new commit: the restart follows at
once (and --no-restart says what that leaves).

No Ship changes: rolling the new tooling out stays a separate `ship-fleet update`.
"""

from __future__ import annotations

import subprocess
from typing import Any

from fleetledger.errors import ConfigError, FleetError
from fleetledger.repo import git as git_in

DEFAULT = "origin/main"
INSTALL_FILES = ("fleetledger/pyproject.toml", "fleetledger/install.sh")


def _git(root: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = git_in(root, *args, timeout=300)
    if check and proc.returncode != 0:
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise FleetError(f"git {' '.join(args)} failed" + (f": {lines[-1]}" if lines else "."))
    return proc


def run_install(root: str) -> int:
    try:
        return subprocess.run([f"{root}/fleetledger/install.sh"], check=False).returncode
    except OSError:
        return 127


def cmd_self_update(args: Any, app: Any) -> int:
    from fleetledger import service
    from fleetledger.concierge import audit

    repo = app.repo_factory()
    root = str(repo.root)
    if _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise FleetError(f"{root} has uncommitted changes; self-update never throws them away.",
                         hint="Commit or stash them first (or update by hand: git pull, then "
                              "fleetledger/install.sh).")
    here = _git(root, "rev-parse", "HEAD").stdout.strip()
    app.say("Fetching...")
    _git(root, "fetch", "--tags", "--quiet", "origin")
    ref = args.to or DEFAULT
    found = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if found.returncode != 0:
        raise FleetError(f"No commit {ref!r} in the checkout or on origin.")
    target = found.stdout.strip()
    if target == here:
        app.say(f"FleetLedger is at {ref} ({here[:12]}) already.")
        return 0
    ahead = _git(root, "merge-base", "--is-ancestor", here, target, check=False).returncode == 0
    if not ahead and not args.force:
        raise FleetError(f"{ref} ({target[:12]}) is not ahead of this checkout ({here[:12]}): "
                         "it would take FleetLedger back, and with it the tooling the next "
                         "ship-fleet update puts on Ships.",
                         hint="Sure? Add --force.")
    changed = _git(root, "diff", "--name-only", here, target, "--", *INSTALL_FILES).stdout.split()
    branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()
    if branch and ahead:
        _git(root, "merge", "--ff-only", "--quiet", target)  # the branch moves with it
    else:
        _git(root, "checkout", "--quiet", "--detach", target)
    audit.record("-", "self-update", "fleetledger", f"{here[:12]}..{target[:12]}"
                 + (" forced" if not ahead else ""))
    app.say(f"FleetLedger is now at {ref} ({target[:12]}; it was {here[:12]}).")
    if changed:
        app.say("Its install changed (" + ", ".join(changed) + "): running fleetledger/install.sh.")
        runner = getattr(app, "run_install", None) or run_install
        if runner(root) != 0:
            # Back to where it was: the installed code and the checkout match again.
            if branch and ahead:
                _git(root, "reset", "--keep", "--quiet", here)
            elif branch:  # it was left where it was; just back onto it
                _git(root, "checkout", "--quiet", branch)
            else:
                _git(root, "checkout", "--quiet", "--detach", here)
            raise ConfigError(f"fleetledger/install.sh failed, so FleetLedger is back at "
                              f"{here[:12]}, as it was; nothing was restarted.",
                              hint=f"See its output above; then ship-fleet self-update "
                                   f"--to {ref} again.")
    if args.no_restart:
        app.say("Not restarted (--no-restart): a running FleetLedger may now load parts of "
                "the new code beside the old; ship-fleet service restart soon.")
    elif service.unit_path().is_file():
        service.cmd_restart(args, app)
    else:
        app.say("Restart ship-fleet ui (if it runs) to use it.")
    app.say("No Ship changed: roll the new tooling out with ship-fleet update when you choose "
            "(docs/runbooks/update-fleet.md).")
    return 0


def add_parsers(sub, add) -> None:
    p = add(sub, "self-update", "update FleetLedger itself (its checkout), then restart it",
            "Move FleetLedger's ShipShape checkout to a newer commit (default origin/main),\n"
            "reinstall if its install changed, and restart the service (running jobs\n"
            "finish first). Refuses uncommitted changes, and going back unless --force.\n"
            "No Ship changes: ship-fleet update rolls the new tooling out.")
    p.add_argument("--to", metavar="REF", help=f"a tag, branch or commit (default {DEFAULT})")
    p.add_argument("--force", action="store_true", help="also to a commit that is not ahead")
    p.add_argument("--no-restart", action="store_true", help="do not restart the service")
