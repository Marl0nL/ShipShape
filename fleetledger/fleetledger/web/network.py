"""The Network tab: what a Ship connected to (phase 3's visibility data).

Phase 3 adds a collector on each Ship; its summary arrives in the status
document's `net` field (contracts/ship-status.schema.json): the window, bytes
in and out, and destinations seen for the first time. Until a Ship reports it
(`net` is null or absent), the tab says the data is not available.

Domains only, never URLs: there is no TLS inspection. Every value comes from
the Ship and is shown as text.
"""

from __future__ import annotations

from typing import Any

from fleetledger.status import ShipView

MAX_DESTINATIONS = 200


def summary(view: ShipView) -> dict[str, Any] | None:
    """The Ship's network summary, or None when it has none (yet)."""
    if view.status is None:
        return None
    net = view.status.get("net")
    if not isinstance(net, dict):
        return None
    try:
        new = [str(d) for d in net.get("new_destinations", [])][:MAX_DESTINATIONS]
        return {
            "window_h": int(net["window_h"]),
            "rx_mb": float(net["rx_mb"]),
            "tx_mb": float(net["tx_mb"]),
            "new_destinations": new,
            # Richer fields phase 3 may add (top domains, programs) are shown
            # when present, as plain rows.
            "top": [
                {k: str(v) for k, v in row.items()}
                for row in net.get("top_domains", [])[:MAX_DESTINATIONS]
                if isinstance(row, dict)
            ],
        }
    except (KeyError, TypeError, ValueError):
        return None
