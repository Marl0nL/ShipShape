"""The fleet dashboard: a summary, prioritised alerts and charts.

Everything here is computed from what the pages already have: the poller's
Ship views (status documents), the Needs attention items (ops.attention, the
same list as `ship-fleet attention`), the history store (fleetledger.history)
and the cached network summaries (fleetledger.net). Nothing reaches a Ship.

Alerts are the attention items plus a few the dashboard derives itself (a disk
nearly full, a Ship off the overlay when one is configured), each with one
action that fixes it: a sign-in button, a page, or the exact command. Severity
has three levels, shown by an icon and a word as well as a colour:

- critical: something is broken or locked out (ops.SEVERITY high);
- warning: it needs you soon (medium);
- notice: worth doing when convenient (low).

No secret is ever shown: status documents hold account names and numbers,
never tokens, and nothing here adds anything else.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fleetledger import net
from fleetledger.inventory import Inventory
from fleetledger.status import ShipView
from fleetledger.web import charts
from fleetledger.web.fleet import Item

LEVELS = ("critical", "warning", "notice")
LEVEL_OF = {"high": "critical", "medium": "warning", "low": "notice"}
DISK_WARN, DISK_CRIT = 80.0, 90.0

# Chart ranges: key -> (span, bucket) in seconds.
RANGES = {"6h": (6 * 3600, 300), "24h": (86400, 900), "7d": (7 * 86400, 3600)}
DEFAULT_RANGE = "24h"
TRAFFIC_STEP = {"6h": 3600, "24h": 3600, "7d": 6 * 3600}  # net summaries are hourly
CARD_RANGE = "24h"
WIDE = 600  # the Ship page's charts sit in wider columns


@dataclass(frozen=True)
class Action:
    """How to fix an alert: kind is signin (a button), link or command."""

    kind: str
    label: str
    value: str = ""  # link: the href; command: the command; signin: the provider id
    hint: str = ""  # a few words before the action


@dataclass(frozen=True)
class Alert:
    level: str  # critical | warning | notice
    ship: str
    code: str
    message: str
    action: Action
    key: str  # stable identity (browser notifications)
    waiting: bool = False  # an agent waits: the page may notify


def range_key(value: str | None) -> str:
    return value if value in RANGES else DEFAULT_RANGE


# --- alerts -----------------------------------------------------------------------


def action_for(code: str, ship: str, view: ShipView | None, signin: str | None) -> Action:
    """The one action that fixes an attention code on a Ship."""
    if signin:
        return Action("signin", "Sign in", signin)
    if code.startswith("signin-"):
        return Action("link", "Sign-ins", "/signin")
    commands = {
        "unreachable": ("Switched on and on the network? Then check again:",
                        f"ship-fleet status {ship}"),
        "agent-waiting": ("Answer it in herdr:",
                          f"herdr --remote agent@{view.ship.names[0]}" if view
                          else f"ship-fleet ssh {ship}"),
        "host-cert-expired": ("Renew its certificate:", f"ship-fleet ca sign {ship}"),
        "host-cert-expiring": ("Renew its certificate:", f"ship-fleet ca sign {ship}"),
        "host-cert-missing": ("Sign its host key:", f"ship-fleet ca sign {ship}"),
        "ssh-trust-behind": ("Bring its trust up to date:", f"ship-fleet ca sign {ship}"),
        "reboot-required": ("Reboot it (agents resume):", f"ship-fleet reboot {ship}"),
        "herdr-restart-pending": ("Reboot it (agents resume):", f"ship-fleet reboot {ship}"),
        "behind-tooling": ("Update it:", f"ship-fleet update {ship}"),
        "not-on-overlay": ("Join it:", f"ship-fleet overlay join {ship}"),
        "overlay-down": ("Find out why:", "ship-fleet overlay check"),
        "disk-nearly-full": ("See what fills it:",
                             f"ship-fleet ssh {ship} -- du -xhd1 /home/agent"),
    }
    if code in commands:
        hint, command = commands[code]
        return Action("command", "Copy", command, hint)
    links = {
        "provision-failed": ("Provisioning log", f"/ships/{ship}/logs?source=provision"),
        "unit-failed": ("System log", f"/ships/{ship}/logs?source=system"),
        "tool-failed": ("Tools log", f"/ships/{ship}/logs?source=tools"),
        "tool-waiting": ("Tools log", f"/ships/{ship}/logs?source=tools"),
        "error": ("Status log", f"/ships/{ship}/logs?source=status"),
        "invalid": ("Status log", f"/ships/{ship}/logs?source=status"),
        "tags-differ": ("Settings", f"/ships/{ship}/settings"),
        "net-collector-down": ("Network", f"/ships/{ship}/network"),
    }
    label, href = links.get(code, ("Open Ship", f"/ships/{ship}"))
    return Action("link", label, href)


def derived(views: Iterable[ShipView], inventory: Inventory | None,
            items: Sequence[Item]) -> list[Item]:
    """Alerts the Ships do not raise themselves: disk nearly full; not on the overlay."""
    found = []
    flagged = {(i.ship, i.code) for i in items}
    for view in views:
        name = view.ship.name
        if view.problem or view.status is None:
            continue
        disk = float(view.status["host"]["disk_used_pct"])
        if disk >= DISK_WARN:
            level = "high" if disk >= DISK_CRIT else "medium"
            found.append(Item(name, "disk-nearly-full",
                              f"Its disk is {disk:.0f}% full.", level, "disk"))
        if (inventory is not None and inventory.overlay and view.ship.overlay_node is None
                and (name, "overlay-down") not in flagged):
            found.append(Item(name, "not-on-overlay",
                              "Not on the overlay: it cannot be reached off the LAN.",
                              "medium", "overlay"))
    return found


def alerts(views: Sequence[ShipView], items: Sequence[Item], flows_waiting: Sequence[Any],
           inventory: Inventory | None) -> list[Alert]:
    """Every alert, most severe first; within a level, sign-ins in progress first,
    then in the order of ops.attention."""
    by_name = {v.ship.name: v for v in views}
    found = [
        Alert("critical", f.ship, "signin", f"{f.label} sign-in is waiting for you.",
              Action("link", "Open", f"/signin?batch={f.batch}"), f"signin/{f.id}")
        for f in flows_waiting
    ]
    for item in [*items, *derived(views, inventory, items)]:
        found.append(Alert(
            LEVEL_OF.get(item.level, "warning"), item.ship, item.code, item.message,
            action_for(item.code, item.ship, by_name.get(item.ship), item.signin),
            item.key, waiting=item.code == "agent-waiting",
        ))
    order = {level: i for i, level in enumerate(LEVELS)}
    return sorted(found, key=lambda a: order[a.level])


# --- the summary ---------------------------------------------------------------------


def summary(views: Sequence[ShipView], alerts_: Sequence[Alert],
            inventory: Inventory | None) -> dict[str, Any]:
    """The numbers across the top of the dashboard."""
    up = [v for v in views if v.status is not None and not v.problem]
    agents = [(v, a) for v in up for a in v.status["herdr"]["agents"]]
    codes = [a.code for a in alerts_]
    pending = [v for v in up if v.status["host"].get("updates_pending")]
    overlay = None
    if inventory is not None and inventory.overlay:
        members = [v for v in views if v.ship.overlay_node is not None]
        overlay = {
            "on": len(members),
            "total": len(views),
            "down": codes.count("overlay-down"),
        }
    return {
        "ships": len(views),
        "up": len(up),
        "down": sum(1 for v in views if v.problem),
        "starting": sum(1 for v in views if not v.problem and v.state == "provisioning"),
        "agents": len(agents),
        "working": sum(1 for _, a in agents if a["state"] == "working"),
        "waiting": sum(1 for _, a in agents if a["state"] == "blocked"),
        "remote_control": sum(1 for v, _ in agents
                              if v.status["claude"]["remote_control"] == "on"),
        "levels": {level: sum(1 for a in alerts_ if a.level == level) for level in LEVELS},
        "signin_expired": codes.count("signin-expired") + codes.count("signin-wrong-account"),
        "signin_missing": codes.count("signin-missing"),
        "signin_expiring": codes.count("signin-expiring"),
        "reboots": sum(1 for v in up if v.status["host"]["reboot_required"]),
        "tooling_behind": codes.count("behind-tooling"),
        "os_updates": sum(v.status["host"]["updates_pending"] for v in pending),
        "os_update_ships": len(pending),
        "overlay": overlay,
    }


# --- charts ----------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _binned(samples: dict[str, list[Any]], starts: Sequence[float], step: float
            ) -> dict[str, list[list[Any]]]:
    """{ship: [samples in bucket i]}."""
    first = starts[0] if starts else 0.0
    out: dict[str, list[list[Any]]] = {}
    for ship, rows in samples.items():
        bins: list[list[Any]] = [[] for _ in starts]
        for s in rows:
            i = int((s.at - first) // step)
            if 0 <= i < len(starts):
                bins[i].append(s)
        out[ship] = bins
    return out


def _ship_means(bins: dict[str, list[list[Any]]], field: str, i: int) -> list[float]:
    """Each Ship's mean of a field in bucket i (Ships with no reading skipped)."""
    found = []
    for rows in bins.values():
        values = [getattr(s, field) for s in rows[i] if s.up and getattr(s, field) is not None]
        if values:
            found.append(sum(values) / len(values))
    return found


def fleet_charts(history: Any, views: Sequence[ShipView], range_: str,
                 now: float) -> dict[str, Any]:
    """The dashboard's charts over a range (6h, 24h or 7d)."""
    span, step = RANGES[range_]
    starts = charts.buckets(span, step, now)
    names = [v.ship.name for v in views]
    samples = history.since(starts[0], names) if history is not None else {}
    bins = _binned(samples, starts, step)
    idx = range(len(starts))

    def avg_max(field: str) -> tuple[list[float | None], list[float | None]]:
        avg, top = [], []
        for i in idx:
            means = _ship_means(bins, field, i)
            avg.append(_mean(means))
            top.append(max(means) if means else None)
        return avg, top

    cpu_avg, cpu_max = avg_max("cpu")
    mem_avg, mem_max = avg_max("mem")
    disk_avg, disk_max = avg_max("disk")
    working, waiting, idle, up = [], [], [], []
    for i in idx:
        latest = [rows[i][-1] for rows in bins.values() if rows[i]]
        live = [s for s in latest if s.up and s.agents is not None]
        working.append(sum(s.working or 0 for s in live) if latest else None)
        waiting.append(sum(s.waiting or 0 for s in live) if latest else None)
        idle.append(sum((s.agents or 0) - (s.working or 0) - (s.waiting or 0)
                        for s in live) if latest else None)
        up.append(float(sum(1 for s in latest if s.up)) if latest else None)
    disk_rules = [(DISK_WARN, f"{DISK_WARN:.0f}%", "warn"),
                  (DISK_CRIT, f"{DISK_CRIT:.0f}%", "crit")]
    return {
        "range": range_,
        "has_history": bool(samples),
        "cpu": charts.line_chart(starts, step, [
            charts.Series("s1", "Average", cpu_avg),
            charts.Series("s2", "Busiest Ship", cpu_max)], ymax=100),
        "mem": charts.line_chart(starts, step, [
            charts.Series("s1", "Average", mem_avg),
            charts.Series("s2", "Fullest Ship", mem_max)], ymax=100),
        "disk": charts.line_chart(starts, step, [
            charts.Series("s1", "Average", disk_avg),
            charts.Series("s2", "Fullest Ship", disk_max)], ymax=100, thresholds=disk_rules),
        "agents": charts.stacked_columns(starts, step, [
            charts.Series("s1", "Working", working),
            charts.Series("warn", "Waiting for you", waiting),
            charts.Series("s3", "Idle", idle)]),
        "up": charts.line_chart(starts, step, [charts.Series("s1", "Ships answering", up)],
                                unit="", ymax=float(max(1, len(views)))),
        "traffic": traffic_chart(views, range_, now),
        "disk_now": disk_bars(views, disk_rules),
    }


def traffic_chart(views: Sequence[ShipView], range_: str, now: float) -> dict[str, Any]:
    """Fleet network traffic per hour (or per 6 hours over a week), from each Ship's
    cached 7-day summary (fleetledger.net; refreshed from the Network page)."""
    span, _ = RANGES[range_]
    step = TRAFFIC_STEP[range_]
    starts = charts.buckets(span, step, now)
    rx: list[float | None] = [None] * len(starts)
    tx: list[float | None] = [None] * len(starts)
    sources = 0
    for view in views:
        summary = net.cached_summary(view.ship.name)
        hourly = (summary or {}).get("totals", {}).get("hourly") or []
        if hourly:
            sources += 1
        for hour in hourly:
            try:
                at = datetime.strptime(hour["hour"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=UTC).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            i = int((at - starts[0]) // step)
            if 0 <= i < len(starts):
                rx[i] = (rx[i] or 0.0) + float(hour["rx_mb"])
                tx[i] = (tx[i] or 0.0) + float(hour["tx_mb"])
    chart = charts.stacked_columns(starts, step, [
        charts.Series("s1", "Received", rx), charts.Series("s2", "Sent", tx)], unit="MB")
    chart["sources"] = sources
    return chart


def disk_bars(views: Sequence[ShipView], rules: Sequence[tuple[float, str, str]]
              ) -> dict[str, Any]:
    rows = []
    for view in views:
        if view.status is None or view.problem:
            continue
        disk = float(view.status["host"]["disk_used_pct"])
        cls = "crit" if disk >= DISK_CRIT else "warn" if disk >= DISK_WARN else "s1"
        rows.append((view.ship.name, disk, cls))
    rows.sort(key=lambda r: -r[1])
    return charts.bars(rows, thresholds=rules)


def sparklines(history: Any, views: Sequence[ShipView], now: float
               ) -> dict[str, dict[str, Any]]:
    """{ship: {"cpu": sparkline, "mem": sparkline}} over the last day, for the cards."""
    span, step = RANGES[CARD_RANGE]
    starts = charts.buckets(span, step, now)
    names = [v.ship.name for v in views]
    samples = history.since(starts[0], names) if history is not None else {}
    bins = _binned(samples, starts, step)
    found = {}
    for name in names:
        rows = bins.get(name, [[] for _ in starts])
        found[name] = {
            field: charts.sparkline([
                _mean([getattr(s, field) for s in b if s.up and getattr(s, field) is not None])
                for b in rows])
            for field in ("cpu", "mem")
        }
    return found


def ship_charts(history: Any, view: ShipView, range_: str, now: float) -> dict[str, Any]:
    """One Ship's CPU, memory and disk over a range, for its page."""
    span, step = RANGES[range_]
    starts = charts.buckets(span, step, now)
    name = view.ship.name
    samples = history.since(starts[0], [name]) if history is not None else {}
    bins = _binned(samples, starts, step).get(name, [[] for _ in starts])

    def series(field: str) -> list[float | None]:
        return [_mean([getattr(s, field) for s in b if s.up and getattr(s, field) is not None])
                for b in bins]

    def agents(field: str) -> list[float | None]:
        return [float(getattr(b[-1], field) or 0) if b and b[-1].up else None for b in bins]

    idle = [None if a is None else a - (w or 0) - (q or 0) for a, w, q in
            zip(agents("agents"), agents("working"), agents("waiting"), strict=True)]
    return {
        "range": range_,
        "has_history": bool(samples),
        "usage": charts.line_chart(starts, step, [
            charts.Series("s1", "CPU", series("cpu")),
            charts.Series("s2", "Memory", series("mem")),
            charts.Series("s3", "Disk", series("disk"))], ymax=100,
            thresholds=[(DISK_CRIT, f"{DISK_CRIT:.0f}%", "crit")], width=WIDE, height=190),
        "agents": charts.stacked_columns(starts, step, [
            charts.Series("s1", "Working", agents("working")),
            charts.Series("warn", "Waiting for you", agents("waiting")),
            charts.Series("s3", "Idle", idle)], width=WIDE, height=190),
    }
