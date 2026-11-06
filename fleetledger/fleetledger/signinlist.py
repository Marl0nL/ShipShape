"""Every sign-in on every Ship in one list, most urgent first.

The Sign-ins page and `ship-fleet signins` (with no Ship named) show the same
rows in the same order. One row is one Ship's sign-in to one service (the status
document's auth keys: claude, gcloud, gcloud_adc, github), in groups:

- action: needs you. A sign-in waiting for you to finish it (the web UI's
  concierge), expired, signed in as the wrong account, a sign-in that just
  failed and is still not done (one that timed out keeps its row's own state,
  with a note), or missing while something on the Ship needs it
  (Claude, a tool waiting for it, an agent's repository waiting for GitHub);
- underway: a sign-in the concierge is running that does not need you now;
- degraded: expiring soon, the tool's check failed, not known (the Ship does not
  answer), or not signed in while nothing needs it yet;
- healthy: signed in;
- off: the Ship does not use it (ship-fleet signins NAME --off).

Within a group the most urgent comes first, then the oldest. "Since" is when
FleetLedger first saw the sign-in as it is now (status.track_since), when it
expired, or when a sign-in in progress started.

Each row carries a severity (critical, warning, notice) so it can be shown as an
icon and a word as well as a colour. Nothing here holds a secret: account names
and states only, as the status document has them.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

from fleetledger import signins
from fleetledger.inventory import Inventory
from fleetledger.status import SIGNED_IN, ShipView

SERVICES = ("claude", "gcloud", "gcloud_adc", "github")
GROUPS = ("action", "underway", "degraded", "healthy", "off")
GROUP_LABELS = {
    "action": "Needs you",
    "underway": "Under way",
    "degraded": "Degraded or unknown",
    "healthy": "Healthy",
    "off": "Not used",
}
SERVICE_LABELS = {"claude": "Claude", "gcloud": "Google Cloud",
                  "gcloud_adc": "Google Cloud ADC", "github": "GitHub"}
# Status auth key -> the concierge provider that signs it in.
PROVIDER = {key: provider for choice in signins.CHOICES
            for key, provider in zip(signins.STATUS_KEYS[choice], signins.PROVIDERS[choice],
                                     strict=True)}
SERVICE_OF_PROVIDER = {p: k for k, p in PROVIDER.items()}
# A last sign-in that ended so, while the sign-in is still not done, is the row's
# state: it needs you. One that timed out does not (you left it, as a cancelled one):
# the row keeps its own state, with the timeout as a note and Try again.
FAILED_OUTCOMES = ("failed", "wrong-account")
TIMED_OUT = "The last sign-in attempt timed out."
# Flow states that wait for the person (web.flows.WAITING_FOR_YOU).
FLOW_WAITING = ("link", "code", "live-offered", "live")


@dataclass(frozen=True)
class Row:
    ship: str
    service: str  # status auth key
    group: str  # GROUPS
    state: str  # a short word: waiting, expired, wrong account, failed, missing, ...
    level: str | None  # critical | warning | notice, or None (healthy, off, under way)
    message: str
    since: str | None = None  # UTC timestamp, 2026-01-01T00:00:00Z
    account: str | None = None
    rank: int = 0  # urgency within the group, lowest first
    flow: str | None = None  # the concierge flow's id, when one is open or just failed
    batch: str | None = None
    deadline: float | None = None  # wall clock: when the waiting sign-in's link runs out
    answers: bool = False  # the Ship answers: signing in or out can work
    tags: tuple[str, ...] = ()
    expires_at: str | None = None  # an expiring sign-in: the soonest to go comes first
    note: str | None = None  # a second line: what the last attempt did (it timed out)

    @property
    def provider(self) -> str:
        return PROVIDER.get(self.service, self.service)

    @property
    def label(self) -> str:
        return SERVICE_LABELS.get(self.service, self.service)

    @property
    def signed_in(self) -> bool:
        return self.state in ("ok", "expiring", "wrong account")

    @property
    def needs_action(self) -> bool:
        return self.group == "action"

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["tags"] = list(self.tags)
        out["provider"] = self.provider
        return out


def _stamp(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _wall(text: str | None) -> float | None:
    """A status timestamp (RFC 3339, Z) as wall-clock seconds, or None."""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _day(text: str) -> str:
    """A status timestamp for a sentence: 8 Nov 02:13 UTC (as given if it is not one)."""
    wall = _wall(text)
    if wall is None:
        return text
    when = datetime.fromtimestamp(wall, UTC)
    return f"{when.day} {when:%b %H:%M} UTC"


def ago(text: str | None, now: float | None = None) -> str:
    """How long ago a timestamp was: 5 min ago, 3 h ago, 2 d ago; "-" if unknown."""
    wall = _wall(text)
    if wall is None:
        return text or "-"
    seconds = max(0.0, (now if now is not None else datetime.now(UTC).timestamp()) - wall)
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 2 * 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def _needed_by(view: ShipView, service: str) -> str | None:
    """What on the Ship needs this sign-in now, if anything (for a missing one)."""
    tools = (view.status or {}).get("tools") or {}
    for item in view.attention:
        code, subject = item.get("code"), item.get("subject")
        if code == "signin-missing" and subject == service:
            return "its agents need it" if service == "claude" else "the Ship needs it"
        if code == "tool-waiting" and service in ((tools.get(subject) or {})
                                                  .get("waiting_for") or []):
            return f"{subject} waits for it"
        if code == "agent-repo-waiting" and service == "github":
            return "an agent's repository waits for it"
    return None


def _status_row(view: ShipView, service: str, tags: tuple[str, ...]) -> Row:
    """One sign-in as the Ship's status shows it."""
    name = view.ship.name
    base = {"ship": name, "service": service, "tags": tags}
    label = SERVICE_LABELS.get(service, service)
    if service in signins.off_status_keys(view.ship):
        return Row(**base, group="off", state="off", level=None,
                   message=f"{name} does not use {label}.")
    if view.problem or view.status is None:
        why = view.message or ("Still being set up." if view.cloud_init is not None
                               else "Not checked yet.")
        return Row(**base, group="degraded", state="unknown", level="notice", rank=3,
                   message=f"Not known: {why}")
    entry = view.auth(service)
    if entry is None:
        return Row(**base, group="degraded", state="unknown", level="notice", rank=3,
                   message="The Ship does not report this sign-in.", answers=True)
    since = view.auth_since.get(service) or entry.get("checked_at")
    account = entry.get("account")
    base.update(since=since, account=account, answers=True)
    wrong = next((a for a in view.attention if a.get("code") == "signin-wrong-account"
                  and a.get("subject") == service), None)
    state = entry["state"]
    if wrong is not None:
        return Row(**base, group="action", state="wrong account", level="critical", rank=1,
                   message=wrong["message"])
    if state == "expired":
        if entry.get("expires_at"):
            base["since"] = entry["expires_at"]
        return Row(**base, group="action", state="expired", level="critical", rank=1,
                   message=f"The {label} sign-in has expired.")
    if state == "missing":
        needs = _needed_by(view, service)
        if needs:
            return Row(**base, group="action", state="missing", level="warning", rank=2,
                       message=f"Not signed in, and {needs}.")
        return Row(**base, group="degraded", state="missing", level="notice", rank=2,
                   message="Not signed in; nothing on the Ship needs it yet.")
    if state == "expiring":
        when = entry.get("expires_at")
        return Row(**base, group="degraded", state="expiring", level="warning", rank=0,
                   message=f"Expires {_day(when)}." if when else "Expires soon.",
                   expires_at=when)
    if state == "error":
        detail = entry.get("detail")
        return Row(**base, group="degraded", state="error", level="warning", rank=1,
                   message=f"The check failed: {detail}." if detail else "The check failed.")
    if state in SIGNED_IN:
        return Row(**base, group="healthy", state="ok", level=None,
                   message=f"Signed in as {account}." if account else "Signed in.")
    return Row(**base, group="degraded", state=state or "unknown", level="notice", rank=3,
               message="The Ship cannot tell.")


def _flow_row(row: Row, flow: Any) -> Row:
    """The row of a sign-in the concierge has open (or that just failed)."""
    started = _stamp(flow.started_at)
    common = {"ship": row.ship, "service": row.service, "tags": row.tags,
              "account": row.account, "answers": row.answers, "flow": flow.id,
              "batch": flow.batch}
    if not flow.done and flow.state in FLOW_WAITING:
        what = {"live-offered": "The sign-in needs you to look at the Ship's terminal.",
                "live": "You have the Ship's terminal."}.get(
            flow.state, f"The {flow.label} sign-in is waiting for you to finish it.")
        return Row(**common, group="action", state="waiting", level="critical", rank=0,
                   message=what, since=started, deadline=flow.deadline)
    if not flow.done:
        what = {"queued": "Queued behind the sign-ins started before it.",
                "starting": "Reaching the Ship and starting the sign-in.",
                "checking": "Checking the account it signed in as."}.get(
            flow.state, "The sign-in is running.")
        return Row(**common, group="underway", state=flow.state, level=None,
                   message=what, since=started)
    detail = (flow.detail or flow.outcome).rstrip(".")
    return Row(**common, group="action", state="failed", level="critical", rank=1,
               message=f"The last sign-in did not finish: {detail}.", since=row.since)


def rows(views: Sequence[ShipView], inventory: Inventory | None,
         flows: Iterable[Any] = ()) -> list[Row]:
    """Every sign-in on these Ships, most urgent first.

    flows: the web UI's concierge flows, newest first (web.flows.Concierge.flows).
    An open one replaces its row; a finished one that failed does too while the
    sign-in is still not done. The CLI has none."""
    open_flows: dict[tuple[str, str], Any] = {}
    last_done: dict[tuple[str, str], Any] = {}
    for flow in flows:
        key = (flow.ship, SERVICE_OF_PROVIDER.get(flow.provider, flow.provider))
        if not flow.done:
            open_flows.setdefault(key, flow)
        else:
            last_done.setdefault(key, flow)
    found = []
    order = {}
    for i, view in enumerate(views):
        tags = (inventory.effective_tags(view.ship) if inventory is not None
                else view.ship.tags or ())
        order[view.ship.name] = i
        for service in SERVICES:
            row = _status_row(view, service, tuple(tags))
            key = (view.ship.name, service)
            if row.group != "off" and key in open_flows:
                row = _flow_row(row, open_flows[key])
            elif key in last_done and row.group != "off" and not row.signed_in:
                last = last_done[key]
                if last.outcome in FAILED_OUTCOMES:
                    row = _flow_row(row, last)
                elif last.outcome == "timeout":
                    row = replace(row, note=TIMED_OUT, flow=last.id, batch=last.batch)
            found.append(row)
    return sorted(found, key=lambda r: _sort_key(r, order))


def _sort_key(row: Row, order: dict[str, int]) -> tuple:
    group = GROUPS.index(row.group)
    by_age = row.group in ("action", "underway", "degraded")
    when = _wall(row.since) if by_age else None
    if row.state == "waiting" and row.deadline is not None:
        when = row.deadline  # the link that runs out first
    elif row.state == "expiring" and _wall(row.expires_at) is not None:
        when = _wall(row.expires_at)  # the sign-in that goes first
    return (group, row.rank, when is None, when or 0.0, order.get(row.ship, 0),
            SERVICES.index(row.service) if row.service in SERVICES else len(SERVICES))


def counts(found: Iterable[Row]) -> dict[str, int]:
    out = dict.fromkeys(GROUPS, 0)
    for row in found:
        out[row.group] += 1
    return out


def needing_action(views: Sequence[ShipView], inventory: Inventory | None,
                   flows: Iterable[Any] = ()) -> int:
    """How many sign-ins need you (the nav's badge)."""
    return sum(1 for r in rows(views, inventory, flows) if r.needs_action)


def narrow(found: Iterable[Row], *, ship: str | None = None, service: str | None = None,
           tag: str | None = None, group: str | None = None) -> list[Row]:
    """The rows that match every filter given (None or "": any)."""
    return [r for r in found
            if (not ship or r.ship == ship) and (not service or r.service == service)
            and (not tag or tag in r.tags) and (not group or r.group == group)]


def fix_command(row: Row) -> str:
    """The CLI command that resolves a row, for `ship-fleet signins`."""
    if row.group == "off":
        return f"ship-fleet signins {row.ship} --on {_choice(row.service)}"
    if row.state == "unknown":
        return f"ship-fleet status {row.ship}"
    if row.state == "wrong account":
        return f"ship-fleet logout {row.ship} {row.provider}"
    if row.group == "healthy":
        return ""
    return f"ship-fleet login {row.ship} {row.provider}"


def _choice(service: str) -> str:
    return next((c for c, keys in signins.STATUS_KEYS.items() if service in keys), service)
