"""Poll Ships for their status document and add what only FleetLedger knows.

Each Ship answers `ship-ctl status --json` (contracts/ship-status.schema.json).
FleetLedger checks it is the Ship it expects (name and id), then adds its own
attention items on top of the Ship's (contracts/ship-ctl.md):

- signin-wrong-account: a service is signed in as an account the Ship's tags
  do not expect;
- behind-tooling: the Ship's tooling is older than this checkout's versions.toml;
- tags-differ: the Ship's own tags differ from the inventory's.

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

from fleetledger import ca, paths, schemas
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory, Ship
from fleetledger.shipctl import RESULT_SCHEMA, STATUS_SCHEMA, not_installed
from fleetledger.ssh import SSH, HostKeyMismatch, Unreachable, ship_ctl

AUTH_SERVICES = ("claude", "gcloud", "gcloud_adc", "github")
SIGNED_IN = ("ok", "expiring")


@dataclass
class ShipView:
    """One Ship as FleetLedger shows it."""

    ship: Ship
    status: dict[str, Any] | None = None
    cloud_init: dict[str, Any] | None = None  # stand-in before ship-ctl exists
    problem: str | None = None  # unreachable | host-key | identity | invalid | error
    message: str | None = None
    host: str | None = None
    attention: list[dict[str, str]] = field(default_factory=list)

    @property
    def state(self) -> str:
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
    view.attention = list(doc["attention"]) + fleet_attention(inventory, ship, doc, tooling)
    view.attention += ca.attention(ca.config(inventory), ship, doc, ca.cached_krl(inventory))
    try:
        write_cache(ship.name, doc)
    except OSError:
        pass  # the cache only serves `ls --offline`; never fail a poll for it
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


def _cache_path(name: str) -> Path:
    return paths.status_cache_dir() / f"{name}.json"


def write_cache(name: str, doc: dict[str, Any]) -> None:
    folder = paths.private_dir(paths.status_cache_dir())
    paths.write_private(folder / f"{name}.json", json.dumps(doc).encode("utf-8"))


def read_cache(name: str) -> dict[str, Any] | None:
    try:
        return json.loads(_cache_path(name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
