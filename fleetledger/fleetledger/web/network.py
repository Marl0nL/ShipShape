"""The Network tab and page: phase 3's visibility data, read through fleetledger.net.

fleetledger.net (shared with `ship-fleet net`) fetches each Ship's 7-day
summary (`ship-ctl net --json`), caches it, and keeps the fleet's first-seen
ledger. The UI shows what is cached and refreshes on request, so a page never
waits on SSH unless you ask it to. Before a Ship has a cached summary, its
status document's 24-hour `net` digest is shown instead.

Visibility only: nothing is blocked. Destination and program names come from
Ships (an agent could choose them), so they are always rendered as text.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fleetledger import net
from fleetledger.inventory import Inventory, Ship
from fleetledger.status import ShipView

MAX_DESTINATIONS = 50


def ship_view(inventory: Inventory, ship: Ship) -> dict[str, Any]:
    """net.ship_view over the cached summary (available is False without one)."""
    return net.ship_view(inventory, ship, net.cached_summary(ship.name))


def refresh(app: Any, names: list[str] | None = None, tag: str | None = None) -> dict[str, str]:
    """Fetch fresh summaries over SSH; returns {ship: problem} for those that failed."""
    inventory = app.store_factory().load()
    ships = inventory.select(names or [], tag) if (names or tag) else inventory.ships()
    fetched = net.refresh(app.ssh_factory(inventory), inventory, ships)
    return {name: f.problem for name, f in fetched.items() if f.problem}


def status_digest(view: ShipView) -> dict[str, Any] | None:
    """The status document's 24-hour `net` digest, or None without a collector."""
    if view.status is None:
        return None
    digest = view.status.get("net")
    if not isinstance(digest, dict) or digest.get("collector") == "unavailable":
        return None
    try:
        return {
            "window_h": int(digest["window_h"]),
            "rx_mb": float(digest["rx_mb"]),
            "tx_mb": float(digest["tx_mb"]),
            "new_destinations": [str(d) for d in digest.get("new_destinations", [])][
                :MAX_DESTINATIONS],
            "collector": str(digest.get("collector") or "running"),
            "source": str(digest["source"]) if digest.get("source") else None,
            "destinations": _count(digest.get("destinations")),
            "connections": _count(digest.get("connections")),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def since(text: str | None) -> str:
    return net.since(datetime.now(UTC), text) if text else "-"
