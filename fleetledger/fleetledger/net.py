"""Network visibility in FleetLedger: the data behind the Network tab (phase 3).

Each Ship summarises where it connects (`ship-ctl net --json`, a net-summary
document: contracts/net-summary.schema.json). FleetLedger fetches it over SSH,
keeps the last one per Ship, and remembers when each destination was first seen
by each Ship and by the fleet, so a newly contacted domain stands out even
after the Ship's own 7-day window has moved on.

Visibility only: nothing here blocks anything.

The web UI and the CLI use these functions (all return plain JSON-able dicts):

- `refresh(ssh, inventory, ships)`: fetch and cache summaries; returns
  {ship name: Fetched}.
- `ship_view(inventory, ship, summary)`: one Ship's tab: totals and hourly
  traffic, top destinations for 24 hours and 7 days (each with its programs),
  programs, and first-seen flags.
- `new_destinations(inventory, tag=None, *, window=NEW_WINDOW)`: destinations
  first seen within the window (24 hours) across Ships, filterable by tag.
- `who_talked(inventory, domain, tag=None)`: which Ships reached a domain (or
  any of its subdomains), when, how often, and with which programs.

Names and program names come from Ships, where an agent may have chosen them.
They are validated against the contract's patterns before they are cached, but
render them as text, never as markup.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fleetledger import paths, schemas
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory, Ship
from fleetledger.shipctl import ShipCtl, ShipCtlError

NEW_WINDOW = timedelta(hours=24)
TOP = 25
DOMAIN_QUERY = re.compile(r"^[A-Za-z0-9_.:-]{1,253}$")


def net_dir() -> Path:
    return paths.data_dir() / "net"


def _ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- cache -------------------------------------------------------------------------
def _write(path: Path, doc: Any) -> None:
    paths.private_dir(path.parent)
    paths.write_private(path, json.dumps(doc, separators=(",", ":")).encode("utf-8"))


def cached_summary(name: str) -> dict[str, Any] | None:
    doc = paths.read_json(net_dir() / f"{name}.json")
    return doc if doc is not None and schemas.is_valid("net-summary", doc) else None


def seen_ledger() -> dict[str, Any]:
    """{"ships": {ship: {destination: first seen}}, "fleet": {destination: {...}}}."""
    doc = paths.read_json(net_dir() / "seen.json") or {}
    doc.setdefault("ships", {})
    doc.setdefault("fleet", {})
    return doc


def record_seen(name: str, summary: dict[str, Any], ledger: dict[str, Any]) -> list[str]:
    """Fold a Ship's destinations into the ledger; returns those new to the fleet."""
    ship_seen = ledger["ships"].setdefault(name, {})
    fresh = []
    for d in summary["destinations"]:
        first = d["first_seen"]
        if d["name"] not in ship_seen or first < ship_seen[d["name"]]:
            ship_seen[d["name"]] = first
        fleet = ledger["fleet"].get(d["name"])
        if fleet is None:
            ledger["fleet"][d["name"]] = {"first_seen": first, "first_ship": name}
            fresh.append(d["name"])
        elif first < fleet["first_seen"]:
            ledger["fleet"][d["name"]] = {"first_seen": first, "first_ship": name}
    return fresh


# --- fetching ------------------------------------------------------------------------
@dataclass
class Fetched:
    summary: dict[str, Any] | None
    problem: str | None = None
    new_to_fleet: list[str] | None = None


def fetch(ssh, ship: Ship) -> dict[str, Any]:
    ctl = ShipCtl(ssh, ship)
    info = ctl.ensure_compatible()
    if info and info.get("contracts", {}).get("net-summary") != 1:
        raise FleetError(f"{ship.name}'s tooling predates network visibility.",
                         hint=f"Update it: ship-fleet update {ship.name}.")
    try:
        data = ctl.call("net", timeout=60).data
    except ShipCtlError as exc:
        raise FleetError(exc.message) from exc
    problems = schemas.errors("net-summary", data)
    if problems:
        raise FleetError(f"{ship.name}: the network summary breaks the contract: {problems[0]}")
    return data


def refresh(ssh, inventory: Inventory, ships: Iterable[Ship]) -> dict[str, Fetched]:
    """Fetch, cache and fold into the first-seen ledger. Never raises per Ship."""
    ledger = seen_ledger()
    out: dict[str, Fetched] = {}
    for ship in ships:
        try:
            summary = fetch(ssh, ship)
        except FleetError as exc:
            out[ship.name] = Fetched(cached_summary(ship.name), exc.message)
            continue
        fresh = record_seen(ship.name, summary, ledger)
        _write(net_dir() / f"{ship.name}.json", summary)
        out[ship.name] = Fetched(summary, None, fresh)
    _write(net_dir() / "seen.json", ledger)
    return out


# --- views -------------------------------------------------------------------------------
def _row(d: dict[str, Any], ship: str, ledger: dict[str, Any], now: datetime) -> dict[str, Any]:
    first = ledger["ships"].get(ship, {}).get(d["name"], d["first_seen"])
    first = min(first, d["first_seen"])
    fleet = ledger["fleet"].get(d["name"], {"first_seen": first, "first_ship": ship})
    return {
        **d,
        "first_seen": first,
        "new": now - _ts(first) <= NEW_WINDOW,
        "new_to_fleet": fleet.get("first_ship") == ship and now - _ts(fleet["first_seen"])
        <= NEW_WINDOW,
    }


def ship_view(inventory: Inventory, ship: Ship, summary: dict[str, Any] | None,
              *, now: datetime | None = None, top: int = TOP) -> dict[str, Any]:
    """Everything the Network tab shows for one Ship."""
    now = now or datetime.now(UTC)
    base = {"ship": ship.name, "tags": list(inventory.effective_tags(ship))}
    if summary is None:
        return {**base, "available": False, "collector": None, "totals": None,
                "top_24h": [], "top_7d": [], "programs": [], "new": []}
    ledger = seen_ledger()
    rows = [_row(d, ship.name, ledger, now) for d in summary["destinations"]]
    top_24h = sorted((r for r in rows if r["connections_24h"]),
                     key=lambda r: (-r["connections_24h"], r["name"]))[:top]
    top_7d = sorted(rows, key=lambda r: (-r["connections_7d"], r["name"]))[:top]
    new = sorted((r for r in rows if r["new"]), key=lambda r: r["first_seen"], reverse=True)
    return {
        **base,
        "available": summary["collector"]["state"] != "unavailable",
        "generated_at": summary["generated_at"],
        "collector": summary["collector"],
        "totals": summary["totals"],
        "top_24h": top_24h,
        "top_7d": top_7d,
        "programs": summary["programs"],
        "new": new,
        "truncated": summary["truncated"],
    }


def new_destinations(inventory: Inventory, tag: str | None = None, *,
                     now: datetime | None = None,
                     window: timedelta = NEW_WINDOW) -> list[dict[str, Any]]:
    """First-seen destinations across Ships (from the cache), newest first."""
    now = now or datetime.now(UTC)
    ledger = seen_ledger()
    found = []
    for ship in inventory.ships(tag):
        summary = cached_summary(ship.name)
        if summary is None:
            continue
        for d in summary["destinations"]:
            row = _row(d, ship.name, ledger, now)
            if now - _ts(row["first_seen"]) <= window:
                found.append({"ship": ship.name, "name": d["name"], "kind": d["kind"],
                              "scope": d["scope"], "first_seen": row["first_seen"],
                              "new_to_fleet": row["new_to_fleet"],
                              "connections_24h": d["connections_24h"],
                              "programs": [p["name"] for p in d["programs"]]})
    return sorted(found, key=lambda r: (r["first_seen"], r["ship"]), reverse=True)


def who_talked(inventory: Inventory, domain: str, tag: str | None = None) -> list[dict[str, Any]]:
    """Ships that reached `domain` or a subdomain of it (or that exact address)."""
    query = domain.strip().rstrip(".").lower()
    if not DOMAIN_QUERY.match(query):
        raise FleetError("Give a domain name or an address, e.g. example.com.")
    ledger = seen_ledger()
    found = []
    for ship in inventory.ships(tag):
        summary = cached_summary(ship.name)
        if summary is None:
            continue
        for d in summary["destinations"]:
            name = d["name"].lower()
            if name == query or name.endswith("." + query):
                first = ledger["ships"].get(ship.name, {}).get(d["name"], d["first_seen"])
                found.append({"ship": ship.name, "name": d["name"],
                              "first_seen": min(first, d["first_seen"]),
                              "last_seen": d["last_seen"],
                              "connections_24h": d["connections_24h"],
                              "connections_7d": d["connections_7d"],
                              "programs": [p["name"] for p in d["programs"]]})
    return sorted(found, key=lambda r: r["last_seen"], reverse=True)


def human_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    for unit in ("B", "kB", "MB", "GB"):
        if n < 1000:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def since(now: datetime, text: str) -> str:
    delta = now - _ts(text)
    if delta < timedelta(hours=1):
        return f"{max(0, int(delta.total_seconds() // 60))} min ago"
    if delta < timedelta(days=2):
        return f"{int(delta.total_seconds() // 3600)} h ago"
    return f"{delta.days} d ago"

