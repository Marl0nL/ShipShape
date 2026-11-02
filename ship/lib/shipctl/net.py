"""`ship-ctl net` and the status document's `net` section (contracts/ship-ctl.md).

Reads what ship-netwatch recorded (/var/lib/shipshape/net/state.json, root-only)
and vnstat's interface counters, and summarises them as a net-summary document
(contracts/net-summary.schema.json). Everything in the store came from the Ship,
where an agent can choose names, so names and program names are checked again
here before they leave the Ship.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

from .context import Context
from .fsutil import read_json

SCHEMA = "shipshape.net-summary/1"
STATE_SCHEMA = "shipshape.netwatch-state/1"
UNIT = "ship-netwatch.service"
UNIT_FILE = "/etc/systemd/system/ship-netwatch.service"
STATE = "/var/lib/shipshape/net/state.json"
RETENTION_DAYS = 7
MAX_DESTINATIONS = 500
MAX_PROGRAMS = 200
NEW_IN_STATUS = 50
MB = 1_000_000

NAME = re.compile(
    r"^([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}[A-Za-z0-9_])?)(\.([A-Za-z0-9_]([A-Za-z0-9_-]{0,61}"
    r"[A-Za-z0-9_])?))*\.?$|^[0-9.]{7,15}$|^[0-9a-f:]{2,39}$"
)
ADDRESS = re.compile(r"^[0-9.]{7,15}$|^[0-9a-f:]{2,39}$")
PROGRAM = re.compile(r"^[A-Za-z0-9_.+:@-]{1,32}$|^\?$")
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
HOUR = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}$")
IFACE = re.compile(r"^[A-Za-z0-9_.:-]{1,32}$")


def _utc(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hour(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H")


def _ts(value: object) -> str | None:
    return value if isinstance(value, str) and TIMESTAMP.match(value) else None


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


# --- the collector --------------------------------------------------------------
def collector_state(ctx: Context) -> str:
    if not ctx.paths.p(UNIT_FILE).exists():
        return "unavailable"
    res = ctx.runner.run(["systemctl", "is-active", UNIT], timeout=5)
    return "running" if res.out.strip() == "active" else "stopped"


def load_state(ctx: Context) -> dict:
    doc = read_json(ctx.paths.p(STATE), {})
    return doc if isinstance(doc, dict) and doc.get("schema") == STATE_SCHEMA else {}


# --- vnstat ---------------------------------------------------------------------
def _vnstat_hour(entry: dict) -> datetime | None:
    stamp = entry.get("timestamp")
    if isinstance(stamp, int) and stamp > 0:
        return datetime.fromtimestamp(stamp, UTC)
    date, clock = entry.get("date") or {}, entry.get("time") or {}
    try:
        return datetime(date["year"], date["month"], date["day"], clock.get("hour", 0), tzinfo=UTC)
    except (KeyError, TypeError, ValueError):
        return None


def vnstat_totals(ctx: Context, now: datetime) -> tuple[dict | None, list[str]]:
    """Totals from `vnstat --json h`: 24 h, 7 d, per interface, and per hour."""
    res = ctx.runner.run(["vnstat", "--json", "h"], timeout=5)
    if not res.ok:
        return None, [] if res.rc == 127 else [f"net: vnstat exit {res.rc}"]
    try:
        doc = json.loads(res.out)
    except ValueError:
        return None, ["net: vnstat did not print JSON"]
    day, week = now - timedelta(hours=24), now - timedelta(days=RETENTION_DAYS)
    interfaces, hourly = [], {}
    for iface in (doc.get("interfaces") or [])[:16]:
        name = iface.get("name")
        if not isinstance(name, str) or not IFACE.match(name) or name == "lo":
            continue
        sums = {"rx_mb_24h": 0, "tx_mb_24h": 0, "rx_mb_7d": 0, "tx_mb_7d": 0}
        for entry in (iface.get("traffic") or {}).get("hour") or []:
            when = _vnstat_hour(entry) if isinstance(entry, dict) else None
            if when is None or when < week - timedelta(hours=1):
                continue
            rx, tx = _int(entry.get("rx")), _int(entry.get("tx"))
            sums["rx_mb_7d"] += rx
            sums["tx_mb_7d"] += tx
            if when >= day - timedelta(hours=1):
                sums["rx_mb_24h"] += rx
                sums["tx_mb_24h"] += tx
            key = _utc(when.replace(minute=0, second=0, microsecond=0))
            slot = hourly.setdefault(key, [0, 0])
            slot[0] += rx
            slot[1] += tx
        interfaces.append({"name": name, **{k: round(v / MB, 1) for k, v in sums.items()}})
    totals = {
        "source": "vnstat",
        **{
            k: round(sum(i[k] for i in interfaces), 1)
            for k in ("rx_mb_24h", "tx_mb_24h", "rx_mb_7d", "tx_mb_7d")
        },
        "interfaces": interfaces,
        "hourly": [
            {"hour": h, "rx_mb": round(v[0] / MB, 2), "tx_mb": round(v[1] / MB, 2)}
            for h, v in sorted(hourly.items())
        ][-RETENTION_DAYS * 24 :],
    }
    return totals, []


# --- the summary ------------------------------------------------------------------
def _destination(name: str, raw: dict, day_key: str, week_key: str) -> dict | None:
    if not isinstance(raw, dict) or not NAME.match(name) or len(name) > 253:
        return None
    first, last = _ts(raw.get("first_seen")), _ts(raw.get("last_seen"))
    kind = raw.get("kind") if raw.get("kind") in ("domain", "address") else None
    scope = raw.get("scope") if raw.get("scope") in ("internet", "lan", "local") else None
    if not (first and last and kind and scope):
        return None
    sums = {"connections_24h": 0, "connections_7d": 0}
    bytes_ = {"tx_bytes_24h": None, "rx_bytes_24h": None, "tx_bytes_7d": None, "rx_bytes_7d": None}
    programs: dict[str, int] = {}
    for key, bucket in (raw.get("hours") or {}).items():
        if not (
            isinstance(key, str)
            and HOUR.match(key)
            and key >= week_key
            and isinstance(bucket, dict)
        ):
            continue
        recent = key >= day_key
        c = _int(bucket.get("c"))
        sums["connections_7d"] += c
        if recent:
            sums["connections_24h"] += c
        for field in ("tx", "rx"):
            value = bucket.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                k7 = f"{field}_bytes_7d"
                bytes_[k7] = (bytes_[k7] or 0) + value
                if recent:
                    k1 = f"{field}_bytes_24h"
                    bytes_[k1] = (bytes_[k1] or 0) + value
        for prog, n in (bucket.get("p") or {}).items():
            if isinstance(prog, str) and PROGRAM.match(prog):
                programs[prog] = programs.get(prog, 0) + _int(n)
    if not sums["connections_7d"]:
        return None
    if bytes_["tx_bytes_7d"] is not None:  # bytes known this week: 24 h is 0, not unknown
        bytes_["tx_bytes_24h"] = bytes_["tx_bytes_24h"] or 0
        bytes_["rx_bytes_24h"] = bytes_["rx_bytes_24h"] or 0
    ports = [p for p in raw.get("ports") or [] if isinstance(p, int) and 0 <= p <= 65535][:16]
    addresses = [a for a in raw.get("addresses") or [] if isinstance(a, str) and ADDRESS.match(a)][
        :16
    ]
    top = sorted(programs.items(), key=lambda kv: (-kv[1], kv[0]))[:16]
    return {
        "name": name,
        "kind": kind,
        "scope": scope,
        "first_seen": first,
        "last_seen": last,
        **sums,
        **bytes_,
        "ports": ports,
        "addresses": addresses,
        "programs": [{"name": p, "connections_7d": n} for p, n in top],
        "_programs_24h": {
            p: sum(
                _int((b.get("p") or {}).get(p))
                for k, b in (raw.get("hours") or {}).items()
                if isinstance(k, str) and k >= day_key and isinstance(b, dict)
            )
            for p in programs
        },
    }


def summary(ctx: Context, now: datetime | None = None) -> tuple[dict, list[str]]:
    now = now or datetime.now(UTC)
    errors: list[str] = []
    state = collector_state(ctx)
    store = load_state(ctx) if state != "unavailable" else {}
    day_key, week_key = (
        _hour(now - timedelta(hours=23)),
        _hour(now - timedelta(days=7) + timedelta(hours=1)),
    )
    dests = []
    for name, raw in (store.get("destinations") or {}).items():
        d = _destination(name, raw, day_key, week_key) if isinstance(name, str) else None
        if d:
            dests.append(d)
    dests.sort(key=lambda d: (-d["connections_7d"], d["name"]))
    programs: dict[str, list[int]] = {}
    for d in dests:
        for p in d["programs"]:
            slot = programs.setdefault(p["name"], [0, 0, 0])
            slot[1] += p["connections_7d"]
            slot[2] += 1
        for p, n in d.pop("_programs_24h").items():
            programs.setdefault(p, [0, 0, 0])[0] += n
    totals, e = vnstat_totals(ctx, now) if state != "unavailable" else (None, [])
    errors += e
    if totals is None:
        known = [d for d in dests if d["rx_bytes_7d"] is not None]
        totals = {
            "source": "collector" if known else None,
            "rx_mb_24h": round(sum(d["rx_bytes_24h"] or 0 for d in known) / MB, 1),
            "tx_mb_24h": round(sum(d["tx_bytes_24h"] or 0 for d in known) / MB, 1),
            "rx_mb_7d": round(sum(d["rx_bytes_7d"] or 0 for d in known) / MB, 1),
            "tx_mb_7d": round(sum(d["tx_bytes_7d"] or 0 for d in known) / MB, 1),
            "interfaces": [],
        }
    note = store.get("note") if isinstance(store.get("note"), str) else None
    if state == "unavailable":
        note = "ship-netwatch is not installed on this Ship."
    source = store.get("source") if store.get("source") in ("ebpf", "ss") else None
    doc = {
        "schema": SCHEMA,
        "generated_at": _utc(now),
        "collector": {
            "state": state,
            "source": source,
            "since": _ts(store.get("started_at")),
            "updated_at": _ts(store.get("updated_at")),
            "dropped": _int(store.get("dropped")),
            "note": note[:300] if note else None,
        },
        "retention_days": RETENTION_DAYS,
        "totals": totals,
        "destinations": dests[:MAX_DESTINATIONS],
        "programs": [
            {"name": p, "connections_24h": v[0], "connections_7d": v[1], "destinations_7d": v[2]}
            for p, v in sorted(programs.items(), key=lambda kv: (-kv[1][1], kv[0]))
        ][:MAX_PROGRAMS],
        "truncated": max(0, len(dests) - MAX_DESTINATIONS),
    }
    return doc, errors


def status_section(ctx: Context, now: datetime | None = None) -> tuple[dict | None, list[str]]:
    """The status document's `net`: the last 24 hours, or None without a collector."""
    now = now or datetime.now(UTC)
    doc, errors = summary(ctx, now)
    if doc["collector"]["state"] == "unavailable":
        return None, errors
    since = _utc(now - timedelta(hours=24))
    new = sorted(
        (d for d in doc["destinations"] if d["first_seen"] >= since),
        key=lambda d: d["first_seen"],
        reverse=True,
    )
    active = [d for d in doc["destinations"] if d["connections_24h"]]
    return {
        "window_h": 24,
        "rx_mb": doc["totals"]["rx_mb_24h"],
        "tx_mb": doc["totals"]["tx_mb_24h"],
        "new_destinations": [d["name"] for d in new][:NEW_IN_STATUS],
        "collector": doc["collector"]["state"],
        "source": doc["collector"]["source"],
        "destinations": len(active),
        "connections": sum(d["connections_24h"] for d in active),
    }, errors
