"""Poll Ships for their status document and add what only FleetLedger knows.

Each Ship answers `ship-ctl status --json` (contracts/ship-status.schema.json).
FleetLedger checks it is the Ship it expects (name and id), then adds its own
attention items on top of the Ship's (contracts/ship-ctl.md):

- signin-wrong-account: a service is signed in as an account the Ship's tags
  do not expect;
- behind-tooling: the Ship's tooling is older than this checkout's versions.toml;
- tags-differ: the Ship's own tags differ from the inventory's;
- the certificate and SSH trust items from ca.attention (host-cert-missing,
  ssh-trust-behind, ...).

Before ship-ctl exists (first boot), `cloud-init status --format json` stands in.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fleetledger import ca, paths, schemas, signinreg, signins
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory, Ship
from fleetledger.shipctl import RESULT_SCHEMA, STATUS_SCHEMA, not_installed
from fleetledger.ssh import SSH, HostKeyMismatch, Unreachable, ship_ctl

AUTH_SERVICES = signinreg.registry().service_keys
SIGNED_IN = ("ok", "expiring")

# What runs a Ship's agents (status herdr.runtime, from ship.toml runtime), as shown.
RUNTIME_LABELS = {"herdr": "herdr", "core": "herdr-core"}
# A blocked herdr-core agent's blocked_kind, as "waits for ...".
WAITS_FOR = {"permission": "approval", "question": "an answer", "auth": "a sign-in"}


def runtime_of(doc: dict[str, Any] | None) -> str:
    """herdr or core: what runs the Ship's agents. Tooling that predates herdr-core
    leaves herdr.runtime out, and runs herdr."""
    return ((doc or {}).get("herdr") or {}).get("runtime") or "herdr"


def remote_control_agents(doc: dict[str, Any] | None) -> int:
    """How many running Claude agents the Ship gives Remote Control: "all", every one;
    "on", the running agents its agent record gives it to (tooling that does not report
    that per agent gave it to every session); "off", none."""
    doc = doc or {}
    value = (doc.get("claude") or {}).get("remote_control")
    running = [a for a in (doc.get("herdr") or {}).get("agents") or []
               if a.get("kind") == "claude"]
    if value == "all":
        return len(running)
    if value != "on":
        return 0
    recorded = ((doc.get("agent_record") or {}).get("agents")) or []
    if not any("remote_control" in a for a in recorded):
        return len(running)
    return sum(1 for a in recorded if a.get("remote_control") and a.get("state") == "running")


def herdr_restart_pending(doc: dict[str, Any] | None) -> bool:
    """True when the Ship reports herdr-restart-pending and herdr runs its agents: a new
    herdr takes over only after a reboot. A herdr-core Ship's tooling from before
    that rule reports it for any herdr server, another tool's included; herdr is
    not that Ship's runtime, so it never calls for a reboot there."""
    return runtime_of(doc) != "core" and any(
        item["code"] == "herdr-restart-pending" for item in (doc or {}).get("attention", []))


def ship_attention(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The Ship's own attention items, less a herdr-restart-pending that is not about
    its runtime (herdr_restart_pending)."""
    return [item for item in doc["attention"] if item["code"] != "herdr-restart-pending"
            or herdr_restart_pending(doc)]


def tmp_pending(doc: dict[str, Any] | None) -> bool:
    """True when /tmp is still a tmpfs and the next reboot puts it on the disk.
    Tooling that predates the disk-backed /tmp leaves host.tmp out."""
    return ((doc or {}).get("host") or {}).get("tmp") == {"fs": "tmpfs", "next_boot": "disk"}


def waiting_reason(agent: dict[str, Any]) -> str | None:
    """What a blocked agent waits for, e.g. "approval: npm test", or None. Only
    herdr-core agents say (blocked_kind, message); herdr's never do."""
    if agent.get("state") != "blocked":
        return None
    kind, message = agent.get("blocked_kind"), agent.get("message")
    what = WAITS_FOR.get(kind or "", kind)
    if what and message:
        return f"{what}: {message}"
    return what or message or None


@dataclass
class ShipView:
    """One Ship as FleetLedger shows it."""

    ship: Ship
    status: dict[str, Any] | None = None
    cloud_init: dict[str, Any] | None = None  # stand-in before ship-ctl exists
    problem: str | None = None  # unreachable | host-key | identity | invalid | error | unknown
    message: str | None = None
    host: str | None = None
    attention: list[dict[str, str]] = field(default_factory=list)
    # service -> when its sign-in was first seen as it is now (track_since)
    auth_since: dict[str, str] = field(default_factory=dict)

    @property
    def unenrolled(self) -> bool:
        """In the inventory but never enrolled, whether or not it answers (a Ship
        made by hand that booted before anyone came back, say)."""
        return self.ship is not None and self.ship.enrolled is None

    @property
    def not_enrolled(self) -> bool:
        """In the inventory but not enrolled yet (being made, made by hand, or cut
        short), and not answering: the inventory says so, not a running job."""
        return self.problem == "unreachable" and self.unenrolled

    @property
    def state(self) -> str:
        if self.not_enrolled:
            return "not-enrolled"
        if self.problem:
            return self.problem
        if self.status is not None:
            return self.status["state"]
        if self.cloud_init is not None:
            return "provisioning"
        return "unknown"

    @property
    def provision_result(self) -> str:
        """ok | failed | running | unknown, for enrolment."""
        if self.status is not None:
            result = self.status["provision"]["result"]
            return "running" if result == "never" else result
        if self.cloud_init is not None:
            return {"done": "ok", "error": "failed", "running": "running"}.get(
                self.cloud_init.get("status", ""), "unknown"
            )
        return "unknown"

    @property
    def settings(self) -> dict[str, Any] | None:
        """The Ship's settings as it reports them (status `settings`), or None: no
        status, or older tooling that does not report them (shown as unknown)."""
        if self.status is None:
            return None
        return self.status.get("settings")

    def auth(self, service: str) -> dict[str, Any] | None:
        if self.status is None:
            return None
        return self.status["auth"].get(service)


def version_tuple(version: str) -> tuple[int, ...]:
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", version)
    return tuple(int(x) for x in match.groups()) if match else (0, 0, 0)


def fleet_attention(
    inventory: Inventory, ship: Ship, status: dict[str, Any], tooling: str | None
) -> list[dict[str, str]]:
    found = []
    for service in AUTH_SERVICES:
        entry = status["auth"].get(service)
        if not entry or entry["state"] not in SIGNED_IN or not entry["account"]:
            continue
        expected = inventory.expected_account(ship, service)
        if expected.matches(entry["account"]) is False:
            wanted = " or ".join(sorted(expected.accounts))
            why = "its tags disagree" if expected.conflicting else f"its tags expect {wanted}"
            found.append(
                {
                    "code": "signin-wrong-account",
                    "subject": service,
                    "message": f"{service} is signed in as {entry['account']}, but {why}.",
                }
            )
    if tooling and version_tuple(status["ship"]["tooling"]) < version_tuple(tooling):
        found.append(
            {
                "code": "behind-tooling",
                "subject": "tooling",
                "message": f"Tooling {status['ship']['tooling']} is older than {tooling}; "
                "run ship-fleet update.",
            }
        )
    tags = inventory.effective_tags(ship)
    if set(status["ship"]["tags"]) != set(tags):
        found.append(
            {
                "code": "tags-differ",
                "subject": "tags",
                "message": "The Ship's tags ("
                + (", ".join(status["ship"]["tags"]) or "none")
                + ") differ from the inventory's ("
                + (", ".join(tags) or "none")
                + ").",
            }
        )
    return found


def cached_view(inventory: Inventory, ship: Ship, missing: str) -> ShipView:
    """A Ship as its last fetched status shows it (offline), with the Ship's own
    attention and FleetLedger's sign-in and tag checks (sign-ins it does not use left
    out). Unlike a live fetch it has no behind-tooling or certificate items."""
    doc = read_cache(ship.name)
    view = ShipView(ship, status=doc)
    if doc is None:
        view.problem, view.message = "unknown", missing
        return view
    attention = ship_attention(doc) + fleet_attention(inventory, ship, doc, None)
    view.attention = signins.filter_attention(ship, attention, doc.get("tools"))
    view.auth_since = read_since(ship.name)
    return view


def fetch(
    ssh: SSH, inventory: Inventory, ship: Ship, tooling: str | None = None
) -> ShipView:
    """One Ship's status. Never raises for Ship-side problems: they go in the view."""
    view = ShipView(ship)
    try:
        result = ssh.run(ship, ship_ctl("status", "--json"), timeout=45)
        view.host = result.host
        if not_installed(result):
            fallback = ssh.run(ship, ["cloud-init", "status", "--format", "json"], timeout=30)
            view.cloud_init = _cloud_init(fallback.text)
            return view
        doc = json.loads(result.text)
    except HostKeyMismatch as exc:
        view.problem, view.message = "host-key", exc.message
        return view
    except Unreachable as exc:
        view.problem, view.message = "unreachable", exc.message
        return view
    except FleetError as exc:
        view.problem, view.message = "error", exc.message
        return view
    except json.JSONDecodeError:
        view.problem, view.message = "invalid", "ship-ctl status did not print JSON."
        return view

    if isinstance(doc, dict) and doc.get("schema") == RESULT_SCHEMA:
        error = (doc.get("error") or {}).get("message", "unknown error")
        view.problem, view.message = "error", f"ship-ctl status failed: {error}"
        return view
    if not isinstance(doc, dict) or doc.get("schema") != STATUS_SCHEMA:
        view.problem, view.message = "invalid", "Unknown status document version."
        return view
    doc = schemas.known_part("ship-status", doc)  # a newer Ship's unknown blocks go
    problems = schemas.errors("ship-status", doc)
    if problems:
        view.problem, view.message = "invalid", f"Status breaks the contract: {problems[0]}"
        return view
    if doc["ship"]["name"] != ship.name or doc["ship"]["id"] != ship.id:
        view.problem = "identity"
        view.message = (
            f"The Ship at {view.host} says it is {doc['ship']['name']} ({doc['ship']['id']}), "
            f"not {ship.name} ({ship.id}); ignoring its status."
        )
        return view
    view.status = doc
    view.attention = ship_attention(doc) + fleet_attention(inventory, ship, doc, tooling)
    view.attention += ca.attention(ca.config(inventory), ship, doc, ca.cached_krl(inventory))
    view.attention = signins.filter_attention(ship, view.attention, doc.get("tools"))
    try:
        write_cache(ship.name, doc)
    except OSError:
        pass  # the cache only serves `ls --offline`; never fail a poll for it
    view.auth_since = track_since(ship.name, doc)
    return view


def _cloud_init(text: str) -> dict[str, Any]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {"status": "unknown"}
    return doc if isinstance(doc, dict) else {"status": "unknown"}


def poll(
    ssh: SSH,
    inventory: Inventory,
    ships: Iterable[Ship],
    *,
    tooling: str | None = None,
    max_workers: int = 8,
    fetcher: Callable[..., ShipView] = fetch,
) -> list[ShipView]:
    """Status of many Ships at once, in the order given."""
    ships = list(ships)
    if not ships:
        return []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(ships))) as pool:
        return list(pool.map(lambda s: fetcher(ssh, inventory, s, tooling), ships))


# --- cache ---------------------------------------------------------------


def cache_path(name: str) -> Path:
    """Where a Ship's last fetched status is kept (for `ls --offline`)."""
    return paths.status_cache_dir() / f"{name}.json"


def write_cache(name: str, doc: dict[str, Any]) -> None:
    paths.private_dir(paths.status_cache_dir())
    paths.write_private(cache_path(name), json.dumps(doc).encode("utf-8"))


def read_cache(name: str) -> dict[str, Any] | None:
    """The last status document saved for name, or None if there is none or it is
    not one (unreadable, not UTF-8, not JSON, not an object): callers want a dict."""
    try:
        doc = json.loads(cache_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):  # ValueError: JSONDecodeError and UnicodeDecodeError
        return None
    return doc if isinstance(doc, dict) else None


# --- since when each sign-in has been as it is ------------------------------------
# A status document says when each sign-in was last checked, not since when it has
# been that way. Next to the cache FleetLedger keeps, per service, what it last saw
# (state and account) and when it first saw it so; the Sign-ins page and
# `ship-fleet signins` show that and put the oldest first.


def since_path(name: str) -> Path:
    return paths.status_cache_dir() / f"{name}.since.json"


def _read_since_file(name: str) -> dict[str, dict[str, str]]:
    try:
        doc = json.loads(since_path(name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return {k: v for k, v in doc.items() if isinstance(v, dict)
            and isinstance(v.get("seen"), str) and isinstance(v.get("since"), str)}


def read_since(name: str) -> dict[str, str]:
    """{service: since} as last recorded (offline)."""
    return {k: v["since"] for k, v in _read_since_file(name).items()}


def track_since(name: str, doc: dict[str, Any]) -> dict[str, str]:
    """Record this status's sign-ins: a service seen as before keeps its since; a
    changed one (another state or account) starts now, at its checked_at."""
    old = _read_since_file(name)
    new = {}
    for service, entry in doc["auth"].items():
        seen = f"{entry['state']}:{entry.get('account') or ''}"
        before = old.get(service)
        since = before["since"] if before and before["seen"] == seen else entry["checked_at"]
        new[service] = {"seen": seen, "since": since}
    if new != old:
        try:
            paths.private_dir(paths.status_cache_dir())
            paths.write_private(since_path(name), json.dumps(new).encode("utf-8"))
        except OSError:
            pass  # a convenience: never fail a poll for it
    return {k: v["since"] for k, v in new.items()}
