"""A Ship's Settings tab: every `ship-fleet set` key, grouped, with its current value.

The current values come from the Ship's own report (status `settings`, ui-6s). A
Ship whose tooling does not report them yet shows "(unchanged)" instead: no field
claims a value it does not know. Applying sends only what differs from the current
value (or, for an older Ship, what was chosen), as `ship-fleet set` assignments
(ops.set_settings, which checks each one as the CLI does).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fleetledger import ops
from fleetledger.errors import FleetError

BOOL = (("true", "on"), ("false", "off"))


@dataclass(frozen=True)
class Field:
    key: str  # the short `ship-fleet set` key, and the form field's name
    label: str
    choices: tuple[tuple[str, str], ...] | None = None  # (value, label); None: free text
    hint: str = ""

    @property
    def path(self) -> tuple[str, str]:
        """Where its value sits in the status `settings` block (section, field)."""
        section, _, name = ops.SET_KEYS[self.key][0].partition(".")
        return section, name

    @property
    def is_list(self) -> bool:
        return self.key == "lan_guard_allow"


def _modes() -> tuple[tuple[str, str], ...]:
    return tuple((m, m) for m in ops.MODES)


GROUPS: tuple[tuple[str, tuple[Field, ...]], ...] = (
    ("Claude Code", (
        Field("remote_control", "Remote Control",
              (("on", "on"), ("off", "off"), ("blocked", "blocked"))),
        Field("default_mode", "Default permission mode", _modes()),
        Field("allow_bypass", "Allow bypassPermissions", BOOL),
        Field("update_channel", "Update channel", (("latest", "latest"), ("stable", "stable"))),
        Field("session_prefix", "Session name prefix",
              hint="what its sessions are called in the Claude app"),
    )),
    ("Agent", (
        Field("sudo", "Agent sudo", BOOL),
    )),
    ("Network", (
        Field("lan_guard", "LAN guard", BOOL),
        Field("lan_guard_allow", "LAN guard also allows",
              hint="addresses or CIDRs, comma-separated"),
    )),
    ("Updates", (
        Field("auto_reboot", "Reboot for OS updates on its own", BOOL),
        Field("reboot_time", "Reboot time (HH:MM)"),
        Field("timezone", "Time zone", hint="an IANA name such as Europe/Berlin"),
    )),
)
FIELDS = {f.key: f for _, fields in GROUPS for f in fields}
SSH_LISTEN = Field("ssh_listen", "SSH listens", (("any", "on every network"),
                                                 ("overlay", "on the overlay only")))


def current(view: Any, field: Field) -> str | None:
    """The Ship's value as a form value ("true", "a,b", ...), or None: not known."""
    reported = view.settings if view is not None else None
    if not reported:
        return None
    section, name = field.path
    value = (reported.get(section) or {}).get(name)
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ",".join(value)
    return str(value)


def older_hint(view: Any, field: Field) -> str | None:
    """For a Ship that does not report its settings: what its status says elsewhere
    (the claude block), or None."""
    doc = view.status if view is not None else None
    if not doc or view.settings:
        return None
    claude = doc.get("claude") or {}
    value = {"remote_control": claude.get("remote_control"),
             "default_mode": claude.get("default_mode"),
             "allow_bypass": claude.get("allow_bypass"),
             "update_channel": claude.get("channel")}.get(field.key)  # status says channel
    if value is None:
        return None
    return str(value).lower() if isinstance(value, bool) else str(value)


def assignments(fields: dict[str, list[str]], view: Any,
                keys: tuple[str, ...] | None = None) -> list[str]:
    """`set` assignments for what was changed: a value that differs from the current
    one, or (current unknown) any value chosen. Checked by ops.parse_assignments."""
    out = []
    for key in keys or tuple(FIELDS):
        field = FIELDS.get(key) or (SSH_LISTEN if key == "ssh_listen" else None)
        if field is None:
            raise FleetError(f"Unknown setting '{key}'.")
        values = fields.get(key, [])
        if len(values) != 1:
            continue
        value = values[0].strip()
        if field.is_list:
            value = ",".join(a.strip() for a in value.split(",") if a.strip())
        known = current(view, field)
        if known is None and not value:
            continue  # "(unchanged)"
        if value == known:
            continue
        if field.choices is not None and value not in {v for v, _ in field.choices}:
            raise FleetError(f"{field.label}: not one of the choices.")
        out.append(f"{key}={value}")
    ops.parse_assignments(out)  # the CLI's own checks, before anything is sent
    return out


def start_mode(view: Any, name: str) -> str | None:
    """The mode an agent would start in now: its autostart mode, else the Ship's
    default_mode (None while the Ship does not report it)."""
    doc = view.status if view is not None else None
    reported = (view.settings if view is not None else None) or {}
    for entry in reported.get("autostart") or []:
        if entry.get("name") == name and entry.get("mode"):
            return entry["mode"]
    default = (reported.get("claude") or {}).get("default_mode")
    return default or ((doc or {}).get("claude") or {}).get("default_mode")


def restartable(view: Any) -> list[dict[str, Any]]:
    """The running Claude agents `ship-fleet agent restart` can name, each with the
    mode it is in and the one it would start in now (`drifted` when they differ)."""
    doc = view.status if view is not None else None
    out = []
    for a in ((doc or {}).get("herdr") or {}).get("agents") or []:
        if a.get("kind") != "claude" or not a.get("name"):
            continue
        wanted = start_mode(view, a["name"])
        out.append({"name": a["name"], "state": a.get("state"), "mode": a.get("mode"),
                    "start_mode": wanted,
                    "drifted": wanted is not None and a.get("mode") != wanted})
    return out
