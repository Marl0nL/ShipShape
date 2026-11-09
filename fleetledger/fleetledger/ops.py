"""Day-2 operations on one Ship or many: the layer both front ends call.

The CLI (`ship-fleet update`, `reboot`, `set`, `tag`, `tools`, `logs`,
`attention`) and the local web UI use these functions, so an action behaves the
same wherever it starts. Each one:

- takes a `Fleet` (the inventory store plus the ways to reach Ships),
- reports progress through `on_event(ship_name, text)` as it goes,
- returns one `Outcome` per Ship and never raises for a single Ship's failure,
  so one bad Ship does not stop a batch (errors that concern the whole request,
  such as an unknown setting or tool, are raised before any Ship is touched).

Everything a Ship does goes through ship-ctl or the reboot command in
contracts/ship-runtime.md; nothing here runs anything else as root (why a reboot
was refused is read as fleet, without sudo: fleetledger.inhibitors).
"""

from __future__ import annotations

import dataclasses
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fleetledger import inhibitors
from fleetledger import status as status_mod
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory, Ship
from fleetledger.seed import bundle as bundle_mod
from fleetledger.seed.shiptoml import TIMEZONE_RE
from fleetledger.shipctl import ShipCtl, ShipCtlError
from fleetledger.ssh import Unreachable

Event = Callable[[str, str], None]

MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
LOG_SOURCES = ("provision", "herdr", "resume", "tools", "status", "system")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
TAG_RE = NAME_RE
CIDR_RE = re.compile(r"^[0-9a-fA-F:.]+(/[0-9]{1,3})?$")
TIME_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")

# `ship-fleet set` short keys -> ship-ctl set keys, with allowed values (None: free,
# checked by a pattern below or by the Ship's schema).
SET_KEYS: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "remote_control": ("claude.remote_control", ("on", "off", "blocked")),
    "default_mode": ("claude.default_mode", MODES),
    "allow_bypass": ("claude.allow_bypass", ("true", "false")),
    "update_channel": ("claude.update_channel", ("latest", "stable")),
    "session_prefix": ("claude.session_prefix", None),
    "sudo": ("agent.sudo", ("true", "false")),
    "lan_guard": ("network.lan_guard", ("true", "false")),
    "lan_guard_allow": ("network.lan_guard_allow", None),
    "ssh_listen": ("network.ssh_listen", ("any", "overlay")),
    "auto_reboot": ("updates.auto_reboot", ("true", "false")),
    "reboot_time": ("updates.reboot_time", None),
    "tags": ("ship.tags", None),
    "timezone": ("ship.timezone", None),
    "github_scopes": ("github.scopes", None),
    "wake_after_boot": ("wake.after_boot", ("true", "false")),
    "wake_delay_minutes": ("wake.delay_minutes", None),
    "wake_text": ("wake.text", None),
}

# Severity of each attention code, for sorting the Needs attention list.
SEVERITY = {
    "host-key": "high",
    "identity": "high",
    "invalid": "high",
    "unreachable": "high",
    "error": "high",
    "signin-wrong-account": "high",
    "signin-expired": "high",
    "provision-failed": "high",
    "unit-failed": "high",
    "host-cert-expired": "high",
    "agent-waiting": "medium",
    "signin-missing": "medium",
    "signin-expiring": "medium",
    "signin-scopes-differ": "medium",  # GitHub's token lacks a chosen scope, or has one more
    "tool-failed": "medium",
    "reboot-required": "medium",
    "herdr-restart-pending": "medium",
    "host-cert-expiring": "medium",
    "host-cert-missing": "medium",
    "overlay-down": "medium",  # phase 5: the Ship cannot be reached off the LAN
    "agent-repo-waiting": "medium",  # its agent starts once GitHub is signed in
    "agent-repo-failed": "medium",
    "agent-relaunch-failed": "medium",  # a recorded agent died and did not start again
    "behind-tooling": "low",
    "not-enrolled": "low",  # in the inventory, never enrolled: being made or made by hand
    "tool-waiting": "low",
    "tags-differ": "low",
    "ssh-trust-behind": "low",
    "net-collector-down": "low",
}
SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

REBOOT_POLICIES = ("auto", "always", "never")


# --- context and results ---------------------------------------------------------


@dataclass
class Fleet:
    """What an operation needs from the outside world (all swappable in tests)."""

    store: Any  # fleetledger.store.Store
    ssh_factory: Callable  # (Inventory) -> SSH
    repo_factory: Callable  # () -> Repo
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    max_parallel: int = 4

    @classmethod
    def from_app(cls, app) -> Fleet:
        return cls(
            store=app.store_factory(),
            ssh_factory=app.ssh_factory,
            repo_factory=app.repo_factory,
            sleep=app.sleep,
            clock=app.clock,
        )

    @property
    def inventory(self) -> Inventory:
        return self.store.load()

    def ssh(self, inventory: Inventory | None = None):
        return self.ssh_factory(inventory or self.inventory)


@dataclass
class Outcome:
    """What happened on one Ship."""

    ship: str
    ok: bool
    summary: str
    details: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class AttentionItem:
    ship: str
    code: str
    message: str
    subject: str | None
    severity: str

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _quiet(_ship: str, _text: str) -> None:
    pass


def _last_line(stderr: str) -> str:
    """The last line a failed command printed, for its error message."""
    return (stderr.strip().splitlines() or ["no output"])[-1]


def select(
    inventory: Inventory,
    names: Iterable[str] = (),
    tag: str | None = None,
    everything: bool = False,
) -> list[Ship]:
    """The Ships a batch action applies to: names, a tag, or all of them.

    Exactly one of the three must be given; `all` among the names means everything.
    """
    names = list(dict.fromkeys(names))  # a Ship named twice is acted on once
    if "all" in names:
        if len(names) > 1:
            raise FleetError("Give Ship names or 'all', not both.")
        names, everything = [], True
    given = sum(bool(x) for x in (names, tag, everything))
    if given == 0:
        raise FleetError("Which Ships? Give names, --tag TAG, or all.")
    if given > 1:
        raise FleetError("Give Ship names, --tag TAG or all: only one of them.")
    if everything:
        ships = inventory.ships()
        if not ships:
            raise FleetError("There are no Ships in the inventory.")
        return ships
    return inventory.select(names, tag)


def for_each(
    fleet: Fleet,
    ships: Sequence[Ship],
    action: Callable[[Ship, Event], Outcome],
    on_event: Event | None = None,
    max_parallel: int | None = None,
) -> list[Outcome]:
    """Run action on every Ship, a few at a time, in the order given.

    A FleetError on one Ship becomes that Ship's failed Outcome; anything else
    is a bug and is raised.
    """
    on_event = on_event or _quiet
    lock = threading.Lock()

    def event(name: str, text: str) -> None:
        with lock:
            on_event(name, text)

    def one(ship: Ship) -> Outcome:
        try:
            return action(ship, event)
        except FleetError as exc:
            event(ship.name, f"failed: {exc.message}")
            return Outcome(ship.name, False, exc.message,
                           details=[exc.hint] if exc.hint else [])

    ships = list(ships)
    if not ships:
        return []
    workers = max(1, min(max_parallel or fleet.max_parallel, len(ships)))
    if workers == 1:
        return [one(s) for s in ships]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, ships))


# --- status helpers -----------------------------------------------------------


# The status contract's timestamp: UTC, whole or fractional seconds.
TIMESTAMP_RE = re.compile(
    r"^([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(\.[0-9]+)?Z$")


def _parse_ts(value: str) -> float:
    """Seconds since the epoch for a status timestamp.

    Raises FleetError, so a Ship that reports an unreadable time fails on its
    own instead of stopping the whole batch.
    """
    match = TIMESTAMP_RE.match(value) if isinstance(value, str) else None
    try:
        if not match:
            raise ValueError(value)
        whole = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        raise FleetError(f"The Ship reported an unreadable time: {value!r}.") from None
    return whole.timestamp() + float("0" + (match.group(2) or ""))


def _fetch(fleet: Fleet, inventory: Inventory, ship: Ship) -> status_mod.ShipView:
    return status_mod.fetch(fleet.ssh(inventory), inventory, ship)


def _require_status(view: status_mod.ShipView) -> dict[str, Any]:
    if view.problem:
        raise FleetError(view.message or view.problem)
    if view.status is None:
        raise FleetError(f"{view.ship.name} is still on its first boot (no ship-ctl yet).")
    return view.status


def agent_modes(doc: dict[str, Any]) -> dict[str, str | None]:
    """Named agents running now and their permission modes, from a status document."""
    return {a["name"]: a["mode"] for a in doc["herdr"]["agents"] if a.get("name")}


def expected_modes(doc: dict[str, Any]) -> dict[str, str | None]:
    """The agents a reboot must bring back, each in its mode: every agent the Ship's
    agent record keeps running (one that crashed and is about to start again too), and
    every named agent running now. A running agent's mode is the one it runs in now (the
    Ship resumes it in that one); else the record's. Tooling without a record: the
    agents running now."""
    expected = {a["name"]: a["mode"] for a in (doc.get("agent_record") or {}).get("agents", [])
                if a["state"] in ("running", "starting")}
    for name, mode in agent_modes(doc).items():
        expected[name] = mode if mode is not None else expected.get(name)
    return expected


def has_attention(doc: dict[str, Any], code: str) -> bool:
    return any(item["code"] == code for item in doc["attention"])


# --- reboot -------------------------------------------------------------------


@dataclass(frozen=True)
class RebootTimes:
    """How long to wait for a Ship to go down, come back and resume its agents."""

    back: float = 600  # from the reboot command to a fresh status document
    resume: float = 240  # from then until every agent is back
    poll: float = 10


REBOOT = ["sudo", "-n", "systemctl", "reboot"]
# Skips logind's block locks only: services still stop, and delay locks (herdr's)
# still get their InhibitDelayMaxSec before the Ship goes down.
FORCE_REBOOT = [*REBOOT, "--check-inhibitors=no"]


def _issue_reboot(fleet: Fleet, inventory: Inventory, ship: Ship, host: str | None,
                  force: bool = False) -> None:
    """`sudo -n systemctl reboot` (contracts/ship-runtime.md), over one name only,
    so a dropped connection is never retried on another name.

    When logind refuses it because something holds a shutdown block lock, raises
    inhibitors.RebootRefused naming what holds it (tried once more if the lock has
    gone by the time it is looked up). force overrides the block locks.
    """
    target = dataclasses.replace(ship, names=(host,)) if host else ship
    ssh = fleet.ssh(inventory)
    try:
        for attempt in (1, 2):
            try:
                result = ssh.run(target, FORCE_REBOOT if force else REBOOT, timeout=60)
            except Unreachable:
                return  # the Ship closed the connection while going down: expected
            if result.returncode in (0, 255):
                return
            said = _last_line(result.stderr)
            if force or "inhibit" not in said.lower():
                raise FleetError(f"{ship.name}: the reboot command failed: {said}")
            blockers = inhibitors.probe(ssh, target)
            if blockers == [] and attempt == 1:
                continue  # released while we looked: try again
            raise inhibitors.RebootRefused(ship.name, said, blockers)
    finally:
        forget = getattr(ssh, "forget", None)
        if forget is not None:
            forget(ship)


def reboot_one(
    fleet: Fleet,
    ship: Ship,
    on_event: Event,
    *,
    wait: bool = True,
    times: RebootTimes | None = None,
    before: dict[str, Any] | None = None,
    host: str | None = None,
    force: bool = False,
) -> Outcome:
    """Reboot one Ship cleanly and, with wait, check its agents came back in their modes.

    A reboot refused by a shutdown lock is a failed Outcome that says what holds the
    lock and what to do (data["refused"]); force overrides such locks.
    """
    times = times or RebootTimes()
    inventory = fleet.inventory
    if before is None:
        view = _fetch(fleet, inventory, ship)
        before = _require_status(view)
        host = view.host
    expected = expected_modes(before)
    on_event(ship.name, "rebooting, overriding any shutdown lock" if force else "rebooting")
    started = fleet.clock()
    try:
        _issue_reboot(fleet, inventory, ship, host, force=force)
    except inhibitors.RebootRefused as exc:
        on_event(ship.name, "reboot refused: something holds a shutdown lock")
        return Outcome(ship.name, False, exc.message, list(exc.steps),
                       {"waited": False, "forced": False, "refused": exc.data()})
    if not wait:
        return Outcome(ship.name, True, "reboot started", data={"waited": False,
                                                                "forced": force})

    doc = _wait_back(fleet, inventory, ship, started, times, on_event)
    back_after = round(fleet.clock() - started)
    on_event(ship.name, f"back after {back_after} s; waiting for its agents")
    doc = _wait_resumed(fleet, inventory, ship, doc, expected, times)
    after = agent_modes(doc)
    missing = sorted(n for n in expected if n not in after)
    changed = sorted(n for n in expected if n in after and after[n] != expected[n])
    details = [f"agent {n} did not come back" for n in missing]
    details += [f"agent {n} came back in {after[n]}, was {expected[n]}" for n in changed]
    data = {"waited": True, "forced": force, "back_after_s": back_after,
            "agents_before": expected, "agents_after": after, "state": doc["state"]}
    if not expected:
        summary = f"back after {back_after} s (no agents were running)"
    else:
        same = len(expected) - len(missing) - len(changed)
        summary = (f"back after {back_after} s; {same}/{len(expected)} agents resumed "
                   "in their modes")
    return Outcome(ship.name, not details, summary, details, data)


def _wait_back(fleet, inventory, ship, started, times, on_event) -> dict[str, Any]:
    """Poll until the Ship answers with a status from a boot after `started`."""
    while True:
        fleet.sleep(times.poll)
        elapsed = fleet.clock() - started
        view = _fetch(fleet, inventory, ship)
        if view.problem in ("host-key", "identity"):
            raise FleetError(view.message or view.problem)
        doc = view.status
        # Uptime is measured on the Ship, so no clock comparison across machines:
        # a Ship that rebooted has been up for less time than we have waited.
        if doc is not None and doc["host"]["uptime_s"] < elapsed:
            return doc
        if elapsed > times.back:
            raise FleetError(f"{ship.name} did not come back within {int(times.back)} s.",
                             hint=f"Check it with: ship-fleet status {ship.name}")


def _resumed_this_boot(doc: dict[str, Any]) -> bool:
    """The Ship has started its agents again since this boot: its agent record's
    resume, else (tooling without one) herdr's."""
    last = ((doc.get("agent_record") or {}).get("resume")
            or doc["herdr"]["last_resume"])
    if not last:
        return False
    boot = _parse_ts(doc["generated_at"]) - doc["host"]["uptime_s"]
    return _parse_ts(last["at"]) >= boot - 5


def _wait_resumed(fleet, inventory, ship, doc, expected, times) -> dict[str, Any]:
    """Poll until every agent that ran before is back with its mode, or time is up."""
    deadline = fleet.clock() + times.resume
    while True:
        after = agent_modes(doc)
        if all(after.get(n) == m for n, m in expected.items()) and (
            _resumed_this_boot(doc) or not expected
        ):
            return doc
        if fleet.clock() >= deadline:
            return doc
        fleet.sleep(times.poll)
        view = _fetch(fleet, inventory, ship)
        if view.status is not None:
            doc = view.status


def reboot(
    fleet: Fleet,
    ships: Sequence[Ship],
    *,
    wait: bool = True,
    on_event: Event | None = None,
    times: RebootTimes | None = None,
    force: bool = False,
) -> list[Outcome]:
    """Reboot each Ship (force: even past a shutdown lock, see reboot_one)."""
    return for_each(fleet, ships,
                    lambda s, ev: reboot_one(fleet, s, ev, wait=wait, times=times, force=force),
                    on_event)


# --- update -------------------------------------------------------------------

INCOMING = "/home/fleet/incoming"
UPLOAD_SCRIPT = (
    'set -eu; umask 077; d="$1"; f="$2"; mkdir -p "$d"; cat > "$d/$f.part"; '
    'printf "%s  bundle.tar.gz\\n" "$3" > "$d/$f.sha256"; mv "$d/$f.part" "$d/$f"'
)
REMOVE_SCRIPT = 'rm -f -- "$1/$2" "$1/$2.sha256"'


def upload_bundle(fleet: Fleet, inventory: Inventory, ship: Ship,
                  bundle: bundle_mod.Bundle) -> str:
    """Copy the bundle and its .sha256 into fleet's incoming folder; returns the path.

    ship-ctl converge checks the bundle against the .sha256 before unpacking it.
    """
    name = f"bundle-{bundle.sha256[:16]}.tar.gz"
    result = fleet.ssh(inventory).run(
        ship, ["bash", "-c", UPLOAD_SCRIPT, "upload", INCOMING, name, bundle.sha256],
        input=bundle.data, timeout=300,
    )
    if result.returncode != 0:
        raise FleetError(f"{ship.name}: uploading the bundle failed: {_last_line(result.stderr)}")
    return f"{INCOMING}/{name}"


def _remove_upload(fleet: Fleet, inventory: Inventory, ship: Ship, path: str) -> None:
    folder, _, name = path.rpartition("/")
    try:
        fleet.ssh(inventory).run(ship, ["bash", "-c", REMOVE_SCRIPT, "rm", folder, name],
                                 timeout=60)
    except FleetError:
        pass  # a leftover upload is harmless; the next update overwrites it


def up_to_date(doc: dict[str, Any], bundle: bundle_mod.Bundle) -> bool:
    commit = doc["ship"].get("bundle_commit")
    manifest = bundle.manifest
    return (
        not manifest["dirty"]
        and commit is not None
        and manifest["commit"].startswith(commit)
        and doc["ship"]["tooling"] == manifest["tooling_version"]
    )


@dataclass(frozen=True)
class Backwards:
    """Why updating a Ship to this checkout's bundle may take it back, and what to do."""

    why: str
    steps: list[str]


def rolls_back(repo, doc: dict[str, Any], bundle: bundle_mod.Bundle,
               ship_name: str = "SHIP") -> Backwards | None:
    """Why converging the Ship (its status) to bundle may roll its tooling back to an
    older ShipShape commit, or None. Told by git ancestry: the Ship's commit must be
    the bundle's or one in its history. Nothing to tell when the Ship reports no
    commit (it predates bundles), already runs the bundle's commit, or this checkout
    is not a git checkout. When git cannot tell, it counts as backwards."""
    theirs = doc["ship"].get("bundle_commit")
    ours = bundle.manifest["commit"]
    if not theirs or ours.startswith(theirs) or not repo.has_commit(ours):
        return None
    force = (f"ship-fleet update {ship_name} --force (in the web UI: Fleet actions, Update, "
             "with its \"converge even\" box ticked)")
    if not repo.has_commit(theirs) and repo.is_shallow():
        return Backwards(f"it runs ShipShape {theirs}, which this shallow clone cannot see "
                         "(it may well be older)",
                         ["Fetch the rest of the history (git fetch --unshallow), then update "
                          f"again; or, if you know it is older: {force}."])
    if not repo.has_commit(theirs):
        return Backwards(f"it runs ShipShape {theirs}, a commit this checkout does not have "
                         "(newer, from another computer's checkout, or from a branch since "
                         "deleted)",
                         ["If another checkout is ahead, bring this one up to date (git fetch, "
                          "git pull), then update again.",
                          "If the Ship was updated from a branch since merged or rebased, this "
                          f"is the move forward: {force}."])
    if repo.is_ancestor(theirs, ours):
        return None
    return Backwards(f"it runs ShipShape {theirs}, newer than this checkout's {ours[:12]} (or "
                     "on another branch)",
                     ["If this checkout is behind, bring it up to date (git pull), then update "
                      "again.",
                      "If the Ship was updated from a branch since merged or rebased (the move "
                      f"forward), or to roll it back on purpose: {force}."])


def _check_reboot_policy(policy: str) -> None:
    if policy not in REBOOT_POLICIES:
        raise FleetError(f"Unknown reboot policy '{policy}' (choose {', '.join(REBOOT_POLICIES)}).")


def update_one(
    fleet: Fleet,
    ship: Ship,
    bundle: bundle_mod.Bundle,
    on_event: Event,
    *,
    reboot_policy: str = "auto",
    wait: bool = True,
    force: bool = False,
    times: RebootTimes | None = None,
) -> Outcome:
    """Bring one Ship to this checkout's tooling, then reboot it if the policy says so.

    reboot_policy: auto reboots only when the converge replaced herdr (the running
    server keeps the old binary until a reboot); always and never do what they say.
    Never rolls a Ship back to an older ShipShape commit (rolls_back) unless force:
    an older checkout, on another computer say, must not undo a newer roll-out.
    """
    _check_reboot_policy(reboot_policy)
    inventory = fleet.inventory
    view = _fetch(fleet, inventory, ship)
    before = _require_status(view)
    if before["state"] == "provisioning":
        raise FleetError(f"{ship.name} is still provisioning; update it once it is ready.")
    release = bundle.release
    data: dict[str, Any] = {"from_tooling": before["ship"]["tooling"],
                            "from_commit": before["ship"].get("bundle_commit"),
                            "to": release, "rebooted": False}
    ctl = ShipCtl(fleet.ssh(inventory), ship)
    ctl.ensure_compatible()

    if up_to_date(before, bundle) and not force:
        on_event(ship.name, f"already on {release}")
        details = []
        if has_attention(before, "herdr-restart-pending") and reboot_policy != "never":
            return _reboot_after(fleet, ship, on_event, Outcome(
                ship.name, True, f"already on {release}", details, data), wait, times)
        return Outcome(ship.name, True, f"already on {release}", details, data)

    backwards = rolls_back(fleet.repo_factory(), before, bundle, ship.name)
    if backwards and not force:
        on_event(ship.name, "not updated: that would roll it back")
        return Outcome(ship.name, False, f"not updated: {backwards.why}", backwards.steps, data)
    if backwards:  # forced: deliberate, and said so in the events, the result and --json
        on_event(ship.name, f"rolling back on purpose (--force): {backwards.why}")
        data["rolled_back"] = backwards.why

    on_event(ship.name, f"uploading the bundle ({len(bundle.data) // 1024} KiB)")
    path = upload_bundle(fleet, inventory, ship, bundle)
    try:
        on_event(ship.name, f"converging to {release}")
        result = ctl.converge(path)
    finally:
        _remove_upload(fleet, inventory, ship, path)
    data["converge"] = result.data
    on_event(ship.name, f"converged ({result.data.get('from') or 'none'} -> "
                        f"{result.data.get('to') or release})")

    after_view = _fetch(fleet, inventory, ship)
    after = _require_status(after_view)
    details: list[str] = list(result.messages)
    if not after["ship"].get("bundle_commit") or not bundle.manifest["commit"].startswith(
        after["ship"]["bundle_commit"]
    ):
        return Outcome(ship.name, False,
                       f"converge finished but the Ship reports bundle "
                       f"{after['ship'].get('bundle_commit')}, not {release}", details, data)
    pending = has_attention(after, "herdr-restart-pending")
    if data.get("rolled_back"):
        details.insert(0, f"Rolled back on purpose (--force): {data['rolled_back']}.")
    outcome = Outcome(ship.name, True, ("rolled back to " if data.get("rolled_back") else
                                        "updated to ") + release, details, data)
    needs_reboot = reboot_policy == "always" or (reboot_policy == "auto" and pending)
    if not needs_reboot:
        if pending:
            outcome.details.append(
                f"herdr was updated; it switches at the next reboot (ship-fleet reboot "
                f"{ship.name})."
            )
        return outcome
    return _reboot_after(fleet, ship, on_event, outcome, wait, times, before=before,
                         host=after_view.host)


def _reboot_after(fleet, ship, on_event, outcome, wait, times, before=None, host=None):
    rebooted = reboot_one(fleet, ship, on_event, wait=wait, times=times, before=before,
                          host=host)
    outcome.data["rebooted"] = "refused" not in rebooted.data
    outcome.data["reboot"] = rebooted.data
    outcome.ok = outcome.ok and rebooted.ok
    outcome.summary = f"{outcome.summary}; {rebooted.summary}"
    outcome.details.extend(rebooted.details)
    return outcome


def update(
    fleet: Fleet,
    ships: Sequence[Ship],
    *,
    reboot_policy: str = "auto",
    wait: bool = True,
    force: bool = False,
    on_event: Event | None = None,
    times: RebootTimes | None = None,
    bundle: bundle_mod.Bundle | None = None,
) -> list[Outcome]:
    """Update every Ship to the tooling bundle packed from this checkout."""
    _check_reboot_policy(reboot_policy)
    bundle = bundle or bundle_mod.pack(fleet.repo_factory())
    if bundle.manifest["dirty"] and on_event:
        on_event("*", f"note: packing {bundle.release} from a checkout with uncommitted "
                      "changes under ship/ (marked dirty)")
    return for_each(
        fleet, ships,
        lambda s, ev: update_one(fleet, s, bundle, ev, reboot_policy=reboot_policy,
                                 wait=wait, force=force, times=times),
        on_event,
    )


# --- settings, tags, tools, logs ----------------------------------------------


AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
AUTOSTART_KEYS = ("autostart.add", "autostart.remove")


def parse_autostart_change(key: str, val: str) -> str:
    """autostart.add=NAME:CWD[:MODE] or autostart.remove=NAME, checked."""
    if key == "autostart.remove":
        if not AGENT_NAME_RE.match(val):
            raise FleetError(f"autostart.remove: '{val}' is not an agent name.")
        return f"{key}={val}"
    parts = val.split(":")
    mode = parts.pop() if len(parts) > 2 and parts[-1] in MODES else None
    name, cwd = parts[0], ":".join(parts[1:])
    if len(parts) == 3 and parts[1] == "github":  # NAME:github:OWNER/REPO
        from fleetledger import github

        if not AGENT_NAME_RE.match(name):
            raise FleetError("autostart.add: a name of a-z, 0-9, - and _.")
        cwd = "github:" + github.check_name(parts[2])
    elif not AGENT_NAME_RE.match(name) or not cwd.startswith(("~/", "/")):
        raise FleetError("autostart.add=NAME:FOLDER[:MODE] (or NAME:github:OWNER/REPO): a name "
                         "of a-z, 0-9, - and _, and a folder starting ~/ or /.")
    return f"{key}={name}:{cwd}" + (f":{mode}" if mode else "")


def parse_assignments(values: Iterable[str]) -> list[str]:
    """`set` arguments (short or full key names) as ship-ctl set KEY=VALUE pairs."""
    out: dict[str, str] = {}
    agents: list[str] = []  # autostart changes: several may be given, in order
    full_names = {full: (full, allowed) for full, allowed in SET_KEYS.values()}
    for value in values:
        key, sep, val = value.partition("=")
        if not sep:
            raise FleetError(f"'{value}' is not KEY=VALUE.")
        if key in AUTOSTART_KEYS:
            agents.append(parse_autostart_change(key, val))
            continue
        full, allowed = SET_KEYS.get(key) or full_names.get(key) or (None, None)
        if full is None:
            raise FleetError(f"Unknown setting '{key}' (settings: {', '.join(SET_KEYS)}).")
        if allowed and val not in allowed:
            raise FleetError(f"{key} must be one of: {', '.join(allowed)}.")
        if full == "ship.tags" and not all(TAG_RE.match(t) for t in val.split(",") if t):
            raise FleetError(f"Invalid tags '{val}'.")
        if full == "network.lan_guard_allow" and not all(
            CIDR_RE.match(a) for a in val.split(",") if a
        ):
            raise FleetError(f"lan_guard_allow takes addresses or CIDRs, comma-separated: "
                             f"'{val}'.")
        if full == "updates.reboot_time" and not TIME_RE.match(val):
            raise FleetError(f"reboot_time is HH:MM on a 24-hour clock, not '{val}'.")
        if full == "wake.delay_minutes" and not (val.isdigit() and 1 <= int(val) <= 1440):
            raise FleetError(f"wake_delay_minutes is a number of minutes, 1 to 1440, not '{val}'.")
        if full == "wake.text" and (len(val) > 1000 or any(ord(c) < 32 or ord(c) == 127
                                                           for c in val)):
            raise FleetError("wake_text is one line of at most 1000 characters.")
        if full == "ship.timezone" and not TIMEZONE_RE.match(val):
            raise FleetError(f"timezone is an IANA zone name such as Europe/Berlin, not '{val}'.")
        if full == "claude.session_prefix" and not PREFIX_RE.match(val):
            raise FleetError(f"Invalid session_prefix '{val}'.")
        if full == "github.scopes":
            from fleetledger import ghscopes

            val = ",".join(ghscopes.parse(val))  # catalogue scopes only; "none" is none
        out[full] = val
    if out.get("claude.default_mode") == "bypassPermissions":
        out.setdefault("claude.allow_bypass", "true")
    return [f"{k}={v}" for k, v in out.items()] + agents


DEFAULT_MODE_NOTE = (
    "Running agents keep the mode they are in; the new default applies to agents started "
    "from now on. To switch them now, each continuing its conversation: "
    "ship-fleet agent restart {ship} --all")


def _record_settings(fleet: Fleet, ship: Ship, pairs: Sequence[str]) -> None:
    """Keep the inventory in step, so a later seed for this Ship matches."""
    with fleet.store.edit() as ed:
        for pair in pairs:
            key, _, value = pair.partition("=")
            section, _, field_name = key.partition(".")
            if section == "claude":
                parsed: Any = {"true": True, "false": False}.get(value, value)
                ed.set_nested(ship.name, "claude", field_name, parsed)
            elif key == "ship.tags":
                ed.set_tags(ship.name, [t for t in value.split(",") if t])
            elif key == "github.scopes":
                ed.set_field(ship.name, "github_scopes", [s for s in value.split(",") if s]
                             or None)


def set_settings(
    fleet: Fleet,
    ships: Sequence[Ship],
    assignments: Iterable[str],
    on_event: Event | None = None,
) -> list[Outcome]:
    pairs = parse_assignments(assignments)

    def one(ship: Ship, event: Event) -> Outcome:
        ctl = ShipCtl(fleet.ssh(), ship)
        result = ctl.set(pairs)
        _record_settings(fleet, ship, pairs)
        summary = ", ".join(result.changed) or "no change"
        messages = list(result.messages)
        if any(p.startswith("claude.default_mode=") for p in pairs):
            messages.append(DEFAULT_MODE_NOTE.format(ship=ship.name))
        if any(p.startswith("autostart.add=") for p in pairs):
            # Start it now (it starts once Claude is signed in, and on every boot).
            try:
                messages += list(ctl.call("autostart", timeout=300).messages)
            except ShipCtlError as exc:
                messages.append(f"Not started yet: {exc.message}")
        event(ship.name, summary)
        return Outcome(ship.name, True, summary, messages, {"changed": result.changed})

    return for_each(fleet, ships, one, on_event)


# --- agents ---------------------------------------------------------------------

AGENT_RESTART_TIMEOUT = 360  # per agent: stop, then start again (the Ship's own limits)


def restart_agents(
    fleet: Fleet,
    ship: Ship,
    agents: Sequence[str] = (),
    *,
    everyone: bool = False,
    mode: str | None = None,
    fresh: bool = False,
    force: bool = False,
    on_event: Event | None = None,
) -> Outcome:
    """Restart running agents on one Ship in place (ship-ctl agent restart): the same
    pane and name, the conversation continued unless fresh, in mode or the mode each
    would start in now. The Ship refuses bypassPermissions where it is not allowed, and
    leaves a working agent alone unless force. Its mode ledger then says the new mode,
    so a later reboot (and `ship-fleet reboot`'s check) expects it."""
    on_event = on_event or _quiet
    agents = list(dict.fromkeys(agents))
    if bool(agents) == everyone:
        raise FleetError("Which agents? Give their names, or --all.")
    bad = [a for a in agents if not AGENT_NAME_RE.match(a)]
    if bad:
        raise FleetError(f"'{bad[0]}' is not an agent name.")
    if mode is not None and mode not in MODES:
        raise FleetError(f"Unknown mode '{mode}' (modes: {', '.join(MODES)}).")
    argv = ["restart", *(["--all"] if everyone else agents)]
    argv += [*(["--mode", mode] if mode else []), *(["--fresh"] if fresh else []),
             *(["--force"] if force else [])]
    who = "every agent" if everyone else ", ".join(agents)
    on_event(ship.name, f"restarting {who}" + (f" in {mode}" if mode else ""))
    ctl = ShipCtl(fleet.ssh(), ship)
    try:
        result = ctl.call("agent", *argv, partial=True,
                          timeout=AGENT_RESTART_TIMEOUT * (8 if everyone else len(agents)))
    except ShipCtlError as exc:
        if exc.code == "usage" and "invalid choice" in exc.message:
            raise FleetError(f"{ship.name}'s tooling cannot restart agents yet.",
                             hint=f"Update it first: ship-fleet update {ship.name}") from exc
        raise
    data = result.data or {}
    done = data.get("restarted") or []
    if result.ok and not done and not data.get("skipped"):
        return Outcome(ship.name, True, "no Claude agent is running", data=data)
    summary = ", ".join(f"{r['name']} in {r['mode']}" for r in done)
    summary = f"restarted {summary}" if done else "nothing restarted"
    if not result.ok:
        summary += f"; {(result.error or {}).get('message', 'failed')}"
    details = list(result.messages)
    details += [f"{s['name']}: {s['reason']}" for s in data.get("skipped") or []
                if not any(d.startswith(f"{s['name']} ") for d in details)]
    return Outcome(ship.name, result.ok, summary, details, data)


def apply_tag_changes(current: Iterable[str], changes: Iterable[str]) -> list[str]:
    """+tag, -tag, a bare tag (add) or none (clear), applied in order."""
    tags = list(current)
    for change in changes:
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
    return tags


def set_tags(
    fleet: Fleet,
    ships: Sequence[Ship],
    changes: Sequence[str],
    *,
    local: bool = False,
    on_event: Event | None = None,
) -> list[Outcome]:
    """Change tags in the inventory, then on the Ship (unless local)."""
    apply_tag_changes([], [c for c in changes if c != "none"])  # validate up front

    def one(ship: Ship, event: Event) -> Outcome:
        inv = fleet.inventory
        tags = sorted(apply_tag_changes(inv.effective_tags(inv.ship(ship.name)), changes))
        with fleet.store.edit() as ed:
            ed.set_tags(ship.name, tags)
        summary = f"tags {' '.join(tags) or '(none)'}"
        event(ship.name, summary)
        if local:
            return Outcome(ship.name, True, summary, data={"tags": tags})
        try:
            ShipCtl(fleet.ssh(), ship).set([f"ship.tags={','.join(tags)}"])
        except FleetError as exc:
            return Outcome(ship.name, False, f"{summary} in the inventory, but the Ship was "
                           f"not updated: {exc.message}", data={"tags": tags})
        return Outcome(ship.name, True, summary, data={"tags": tags})

    return for_each(fleet, ships, one, on_event)


def change_tools(
    fleet: Fleet,
    ships: Sequence[Ship],
    action: str,
    ids: Sequence[str],
    on_event: Event | None = None,
) -> list[Outcome]:
    """Add or remove catalogue tools on Ships (ship-ctl tools add|remove)."""
    from fleetledger.catalogue import Catalogue
    from fleetledger.seed.shiptoml import tools_for

    if action not in ("add", "remove"):
        raise FleetError(f"Unknown tools action '{action}'.")
    if not ids:
        raise FleetError("Which tools? Give at least one catalogue id.")
    catalogue = Catalogue.from_repo(fleet.repo_factory())
    catalogue.check_tools(ids)

    def one(ship: Ship, event: Event) -> Outcome:
        inv = fleet.inventory
        result = ShipCtl(fleet.ssh(inv), ship).tools(action, ids)
        current = tools_for(inv, inv.ship(ship.name), catalogue)
        if action == "add":
            updated = list(dict.fromkeys([*current, *ids]))
        else:
            updated = [t for t in current if t not in ids]
        with fleet.store.edit() as ed:
            ed.set_field(ship.name, "tools", updated)
        summary = f"tools {', '.join(updated) or '(none)'}"
        event(ship.name, summary)
        return Outcome(ship.name, True, summary, list(result.messages), {"tools": updated})

    return for_each(fleet, ships, one, on_event)


def logs(fleet: Fleet, ship: Ship, source: str, lines: int = 200) -> list[str]:
    """The last lines of one log source on one Ship (ship-ctl logs)."""
    if source not in LOG_SOURCES:
        raise FleetError(f"Unknown log source '{source}' (sources: {', '.join(LOG_SOURCES)}).")
    if not 1 <= lines <= 10000:
        raise FleetError("--lines must be between 1 and 10000.")
    return ShipCtl(fleet.ssh(), ship).logs(source, lines)


def fleet_logs(
    fleet: Fleet,
    ships: Sequence[Ship],
    source: str,
    lines: int = 200,
    on_event: Event | None = None,
) -> list[Outcome]:
    """logs() for several Ships; each Outcome's data["lines"] holds that Ship's lines."""
    def one(ship: Ship, _event: Event) -> Outcome:
        found = logs(fleet, ship, source, lines)
        return Outcome(ship.name, True, f"{len(found)} lines", data={"lines": found})

    return for_each(fleet, ships, one, on_event)


# --- Needs attention ------------------------------------------------------------


NOT_ENROLLED = ("Not enrolled yet: it may still be starting, or its VM is still to be "
                "made by hand. Finish enrolling it once it is up.")


NOT_ENROLLED_UP = "Not enrolled yet, though it answers: finish enrolling it."


def attention(views: Iterable[status_mod.ShipView]) -> list[AttentionItem]:
    """Everything across the fleet a person should look at, most urgent first."""
    items = []
    for view in views:
        if view.not_enrolled:  # being made, or made by hand: not an outage
            items.append(AttentionItem(view.ship.name, "not-enrolled", NOT_ENROLLED, None,
                                       SEVERITY["not-enrolled"]))
            continue
        if view.problem:
            items.append(AttentionItem(view.ship.name, view.problem,
                                       view.message or view.problem, None,
                                       SEVERITY.get(view.problem, "high")))
            continue
        if view.unenrolled:  # it answers, but its seed, overlay and certificate wait
            items.append(AttentionItem(view.ship.name, "not-enrolled", NOT_ENROLLED_UP, None,
                                       SEVERITY["not-enrolled"]))
        for entry in view.attention:
            items.append(AttentionItem(view.ship.name, entry["code"], entry["message"],
                                       entry.get("subject"),
                                       SEVERITY.get(entry["code"], "medium")))
    order = {name: i for i, name in enumerate(dict.fromkeys(i.ship for i in items))}
    return sorted(items, key=lambda i: (SEVERITY_ORDER[i.severity], order[i.ship]))
