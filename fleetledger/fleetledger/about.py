"""What this FleetLedger is and how it stands: `ship-fleet about` (contracts/ship-fleet.md).

One look at FleetLedger itself, for a person or for a panel that reads `--json`
(`contracts/fleetledger-about.schema.json`):

- its version, and the checkout it runs from (commit, branch, uncommitted changes);
- whether origin has a newer commit, only with --check: that runs one `git fetch` of
  the remote and branch `ship-fleet self-update` uses, and nothing else ever does;
- the web UI's addresses on this computer and through Tailscale Serve, when set;
- whether it runs as a service and what systemd says of it, and whether a running
  UI answers on its control socket (and with how many jobs running);
- the vault's state, the last backup's result and whether backups are scheduled;
- which copy this is (active, standby or retired: ship-fleet migrate);
- the exact command that updates FleetLedger.

It only reads: it never unlocks, updates, restarts or writes a setting, and it runs
while the vault is locked or on a handed-over copy. What it cannot read it says, in
`unread`, and shows the rest.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from fleetledger import __version__
from fleetledger.errors import FleetError

SCHEMA = "shipshape.fleetledger-about/1"
SELF_UPDATE = "ship-fleet self-update"
GIT_TIMEOUT = 30  # a local read
FETCH_TIMEOUT = 300  # as self-update's
COMMIT = re.compile(r"^([0-9a-f]{40}|[0-9a-f]{64})$")
LOCAL = re.compile(r"^http://127\.0\.0\.1:[0-9]{4,5}$")
REMOTE = re.compile(r"^https://[^\s/]+$")
WHEN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")


class _Unread:
    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    def add(self, what: str, why: str) -> None:
        self.items.append({"what": what, "why": why[:1000]})


def _git(root: str, *args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    from fleetledger.repo import git

    return git(root, *args, timeout=timeout)


def _why(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"git exited with {proc.returncode}"


def _checkout(app: Any, unread: _Unread) -> tuple[dict[str, Any], str | None]:
    out: dict[str, Any] = {"path": None, "commit": None, "branch": None, "dirty": None}
    try:
        root = str(app.repo_factory().root)
    except FleetError as exc:
        unread.add("checkout", exc.message)
        return out, None
    out["path"] = root
    try:
        head = _git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
        if head.returncode != 0:
            unread.add("checkout", f"{root} is not a git checkout with a commit")
            return out, None
        commit = head.stdout.strip()
        if not COMMIT.match(commit):
            unread.add("checkout", "git rev-parse HEAD gave no commit id")
            return out, None
        out["commit"] = commit
        branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD")
        out["branch"] = branch.stdout.strip() or None
        # --no-optional-locks: git status may otherwise refresh (write) the index.
        status = _git(root, "--no-optional-locks", "status", "--porcelain",
                      "--untracked-files=no")
        if status.returncode == 0:
            out["dirty"] = bool(status.stdout.strip())
        else:
            unread.add("checkout", f"git status: {_why(status)}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        unread.add("checkout", f"git: {type(exc).__name__}")
    return out, root if out["commit"] else None


def _upstream(root: str | None, here: str | None, check: bool,
              unread: _Unread) -> dict[str, Any]:
    from fleetledger.selfupdate import DEFAULT

    out: dict[str, Any] = {"checked": False, "ref": DEFAULT, "commit": None, "newer": None,
                           "behind": None}
    if not check:
        return out
    if root is None or here is None:
        unread.add("upstream", "no checkout to compare")
        return out
    try:
        # The very fetch self-update makes: the same remote, nothing else.
        fetched = _git(root, "fetch", "--tags", "--quiet", "origin", timeout=FETCH_TIMEOUT)
        if fetched.returncode != 0:
            unread.add("upstream", f"git fetch origin: {_why(fetched)}")
            return out
        found = _git(root, "rev-parse", "--verify", "--quiet", f"{DEFAULT}^{{commit}}")
        if found.returncode != 0:
            unread.add("upstream", f"no {DEFAULT} after the fetch")
            return out
        target = found.stdout.strip()
        if not COMMIT.match(target):
            unread.add("upstream", f"git rev-parse {DEFAULT} gave no commit id")
            return out
        behind = _git(root, "rev-list", "--count", f"HEAD..{target}")
        ahead = _git(root, "merge-base", "--is-ancestor", here, target)
    except (OSError, subprocess.TimeoutExpired) as exc:
        unread.add("upstream", f"git: {type(exc).__name__}")
        return out
    out.update(checked=True, commit=target,
               newer=target != here and ahead.returncode == 0,
               behind=int(behind.stdout.strip()) if behind.returncode == 0
               and behind.stdout.strip().isdigit() else None)
    return out


def _service(app: Any, unread: _Unread) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """(the systemd unit's state, what a running UI says on its control socket)."""
    from fleetledger import service
    from fleetledger.web import control

    out: dict[str, Any] = {"installed": False, "active": None, "enabled": None}
    try:
        out["installed"] = service.unit_path().is_file()
    except OSError as exc:
        unread.add("service", f"the unit: {exc.strerror}")
    if out["installed"]:
        code, active, err = service._systemctl(app, "is-active", service.UNIT)
        if active.strip():
            out["active"] = active.strip()[:100]
        else:
            unread.add("service", f"systemctl --user is-active: {service._last(err) or code}")
        _code, enabled, _err = service._systemctl(app, "is-enabled", service.UNIT)
        out["enabled"] = enabled.strip()[:100] or None
    try:
        answer = control.ask("status")
    except FleetError:
        answer = None  # no UI running here (as this user): a fact, not a failure
    return out, answer


def _ui(answer: dict[str, Any] | None, unread: _Unread) -> dict[str, Any]:
    from fleetledger import service
    from fleetledger.web import remote

    found = None
    try:
        found = remote.load()
    except FleetError as exc:
        unread.add("remote ui", exc.message)
    out: dict[str, Any] = {"local": None, "remote": found.origin if found else None,
                           "running": answer is not None, "as_service": None, "jobs": None,
                           "stopping": None}
    if answer is not None:
        out["local"] = _matching(LOCAL, answer.get("url"))
        out["remote"] = _matching(REMOTE, answer.get("remote")) or out["remote"]
        out["as_service"] = bool(answer.get("service"))
        jobs = answer.get("jobs")
        out["jobs"] = len(jobs) if isinstance(jobs, list) else 0
        out["stopping"] = bool(answer.get("stopping"))
    if out["local"] is None:
        port = found.port if found else service.unit_port()
        if port is not None:
            out["local"] = f"http://127.0.0.1:{port}"
    return out


def _matching(pattern: re.Pattern, value: Any) -> str | None:
    return value if isinstance(value, str) and pattern.match(value) else None


def _backup(unread: _Unread) -> dict[str, Any]:
    from fleetledger import backup, service

    out: dict[str, Any] = {"scheduled": False, "last": None, "last_made": None}
    try:
        out["scheduled"] = (service.unit_dir() / backup.TIMER).is_file()
    except OSError as exc:
        unread.add("backups", exc.strerror or "the timer")
    last = backup.last()
    if last is not None:
        out["last"] = {"at": _matching(WHEN, last.get("at")),
                       "ok": last.get("ok") is True,
                       "to": last.get("to") if isinstance(last.get("to"), str) else None,
                       "why": last.get("why") if isinstance(last.get("why"), str) else None}
        if out["last"]["to"]:
            out["last_made"] = _newest(out["last"]["to"], unread)
    return out


def _newest(folder: str, unread: _Unread) -> str | None:
    """When the newest backup in folder was made, from its file name (fleetledger-
    YYYYMMDD-HHMMSS-...): it outlives a failed attempt, which the last result is."""
    from pathlib import Path

    from fleetledger import backup

    try:
        names = [f.name for f in Path(folder).iterdir() if backup.NAME_RE.match(f.name)]
    except FileNotFoundError:
        return None  # not mounted just now, say
    except OSError as exc:
        unread.add("backups", f"{folder}: {exc.strerror or type(exc).__name__}")
        return None
    if not names:
        return None
    d, t = max(names).split("-")[1:3]
    return f"{d[:4]}-{d[4:6]}-{d[6:]}T{t[:2]}:{t[2:4]}:{t[4:]}Z"


def _copy(app: Any, unread: _Unread) -> str:
    from fleetledger import handover

    try:
        store = app.store_factory()
        if not store.exists():
            return "none"
        found = handover.marker(store)
    except FleetError as exc:
        unread.add("copy", exc.message)
        return "unreadable"
    return found.role if found is not None else "active"


def gather(app: Any, *, check: bool = False) -> dict[str, Any]:
    """The about document (contracts/fleetledger-about.schema.json)."""
    from fleetledger import vault

    unread = _Unread()
    checkout, root = _checkout(app, unread)
    upstream = _upstream(root, checkout["commit"], check, unread)
    unit, answer = _service(app, unread)
    try:
        vault_state = vault.state()
    except OSError as exc:
        unread.add("vault", exc.strerror or type(exc).__name__)
        vault_state = None
    return {
        "schema": SCHEMA,
        "version": __version__,
        "checkout": checkout,
        "upstream": upstream,
        "ui": _ui(answer, unread),
        "service": unit,
        "vault": {"state": vault_state},
        "backup": _backup(unread),
        "copy": _copy(app, unread),
        "self_update": {"command": SELF_UPDATE, "ref": upstream["ref"]},
        "unread": unread.items,
    }


def _when(text: str | None) -> str:
    return f"{text[:16].replace('T', ' ')} UTC" if text else "at an unknown time"


def lines(doc: dict[str, Any]) -> list[str]:
    """The document, for a person."""
    c, up, ui, svc = doc["checkout"], doc["upstream"], doc["ui"], doc["service"]
    out = []
    at = (f", commit {c['commit'][:12]}" + (f" on {c['branch']}" if c["branch"] else
                                             " (detached)")
          + (" with uncommitted changes" if c["dirty"] else "")) if c["commit"] else ""
    out.append(f"FleetLedger {doc['version']}{at}" + (f", from {c['path']}" if c["path"] else
                                                       ""))
    if not up["checked"]:
        out.append(f"Newer on {up['ref']}: not checked (ship-fleet about --check fetches "
                   "once to see).")
    elif up["newer"]:
        count = f", {up['behind']} commit(s) on" if up["behind"] is not None else ""
        out.append(f"Newer on {up['ref']}: yes, {up['commit'][:12]}{count}: "
                   f"{doc['self_update']['command']} moves to it.")
    elif up["commit"] == c["commit"]:
        out.append(f"Newer on {up['ref']}: no, this is it.")
    else:
        out.append(f"Newer on {up['ref']}: no ({up['commit'][:12]} is not ahead of this "
                   "checkout).")
    out.append("Web UI: " + (", ".join(x for x in (
        f"{ui['local']} on this computer" if ui["local"] else "",
        f"{ui['remote']} through Tailscale Serve" if ui["remote"] else "") if x)
        or "no address set (ship-fleet ui starts it)") + ".")
    if svc["installed"]:
        said = ", ".join(x for x in (svc["active"] or "state unknown", svc["enabled"]) if x)
        out.append(f"Service: installed, {said}.")
    else:
        out.append("Service: not installed (ship-fleet service install runs it always on).")
    if ui["running"]:
        jobs = ui["jobs"] or 0
        out.append("Running UI: answers" + ("" if ui["as_service"] else
                                            " (a ship-fleet ui in a terminal)")
                   + (", stopping" if ui["stopping"] else "")
                   + f", {jobs} job(s) running.")
    else:
        out.append("Running UI: none answers here.")
    state = doc["vault"]["state"]
    out.append("Vault: " + {
        "off": "off (secrets in the OS keyring or 0600 files).",
        "locked": "locked (it locks at every boot): FleetLedger is read-only until ship-fleet "
                  "unlock.",
        "unlocked": "unlocked, until the next boot.",
    }.get(state, "could not be read."))
    b = doc["backup"]
    last = b["last"]
    if last is None:
        out.append("Backups: " + ("scheduled; none made yet." if b["scheduled"] else
                                  "none made or scheduled here."))
    elif last["ok"]:
        out.append(f"Backups: last one {_when(last['at'])}, to {last['to']}"
                   + ("; scheduled daily." if b["scheduled"] else "."))
    else:
        out.append(f"Backups: the last one failed ({_when(last['at'])}): "
                   f"{last['why'] or 'no reason given'}"
                   + (f"; the newest in {last['to']} is from {_when(b['last_made'])}."
                      if b["last_made"] else "."))
    out.append(f"This copy: {doc['copy']}" + {
        "none": " (no fleet here yet).",
        "standby": " (ship-fleet migrate activate makes it act).",
        "retired": " (it handed the fleet on: ship-fleet doctor says where).",
    }.get(doc["copy"], "."))
    out.append(f"Update FleetLedger: {doc['self_update']['command']} (to "
               f"{doc['self_update']['ref']}; no Ship changes).")
    for item in doc["unread"]:
        out.append(f"Could not read {item['what']}: {item['why']}")
    return out


def cmd_about(args: Any, app: Any) -> int:
    doc = gather(app, check=args.check)
    if args.json:
        app.say(json.dumps(doc, indent=2))
    else:
        for line in lines(doc):
            app.say(line)
    return 0


def add_parsers(sub, add) -> None:
    p = add(sub, "about", "FleetLedger itself: version, addresses, service, vault, backups",
            "Show FleetLedger itself: its version and checkout, whether origin has a\n"
            "newer commit (--check only), the web UI's addresses, the service, the vault,\n"
            "the last backup, which copy this is, and the command that updates it.\n"
            "Reads only, and works while the vault is locked. --json prints\n"
            "contracts/fleetledger-about.schema.json.")
    p.add_argument("--check", action="store_true",
                   help="also fetch origin once (as self-update does) to see if it is newer")
    p.add_argument("--json", action="store_true", help="print JSON")
