"""The overlay network (phase 5): reach Ships from your other machines off the LAN.

The overlay itself sits behind a provider interface (overlay/providers.py):
Tailscale and Headscale today; docs/overlay-providers.md says how to add one.

Ships join a Tailscale tailnet (or a Headscale server) after they boot. For each
join FleetLedger gets a one-off auth key, either pasted by you (read without
echo) or minted through Tailscale's API with a scoped OAuth client, and hands it
to the Ship over SSH on stdin (`ship-ctl overlay join`). Keys never go into
seeds, command lines, logs, the inventory or the audit log, and FleetLedger
keeps no copy. OpenSSH with the pinned host key stays the only way in, so the
overlay adds a route, not a new kind of trust.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Inventory, Ship
from fleetledger.overlay.keys import PromptKeys  # noqa: F401  (re-exported for callers)
from fleetledger.overlay.tsapi import read_secret  # noqa: F401  (overlay check)
from fleetledger.shipctl import ShipCtl, ShipCtlError

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")  # ships.toml names
DEFAULT_TAGS = ("tag:ship",)


def settings(inventory: Inventory) -> Mapping[str, Any]:
    config = inventory.overlay
    if not config:
        raise ConfigError(
            "No [overlay] section in ships.toml.",
            hint="See docs/runbooks/remote-access.md, step 2.",
        )
    return config


def tags(config: Mapping[str, Any]) -> list[str]:
    return list(config.get("tags", DEFAULT_TAGS))


# --- where one-off keys come from (overlay/keys.py, overlay/providers.py) -------------


def key_source(config: Mapping[str, Any], **kw):
    """The configured provider's key source."""
    return provider(config, **kw).keys()


def provider(config: Mapping[str, Any], **options: Any):
    from fleetledger.overlay import providers

    return providers.get(config, **options)


# --- joining, leaving, status ----------------------------------------------------------


NODE_ID_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")


@dataclass
class Joined:
    ship: str
    name: str | None
    addresses: list[str]
    messages: list[str]
    node_id: str | None = None


def join(ctl: ShipCtl, ship: Ship, config: Mapping[str, Any], keys) -> Joined:
    argv = provider(config).join_args(ship)
    key = keys.key(ship)
    result = ctl.call("overlay", *argv, input=(key + "\n").encode("ascii"), timeout=900)
    overlay = result.data.get("overlay") or {}
    node_id = overlay.get("node_id")
    return Joined(ship.name, overlay.get("name"), list(overlay.get("addresses", [])),
                  list(result.messages),
                  node_id if isinstance(node_id, str) and NODE_ID_RE.match(node_id) else None)


def status(ctl: ShipCtl) -> dict | None:
    return ctl.call("overlay", "status").data.get("overlay")


def leave(ctl: ShipCtl) -> list[str]:
    return list(ctl.call("overlay", "leave", timeout=300).messages)


def names_with(ship: Ship, overlay_name: str | None) -> list[str]:
    """The overlay name goes first: it works on the LAN and off it, and a computer that
    is not on the overlay fails to resolve it at once and moves on to the LAN name."""
    names = [n for n in ship.names if n != overlay_name]
    return [overlay_name, *names] if overlay_name else names


def names_without(ship: Ship, overlay_name: str | None) -> list[str]:
    names = [n for n in ship.names if n != overlay_name]
    return names or list(ship.names)


def policy(config: Mapping[str, Any]) -> str:
    """The access rules for Ships (providers.policy_doc). Paste them into the admin
    console's access controls, or let `overlay setup` merge them (opt-in)."""
    return provider(config).policy()


def before_destroy(app, inventory: Inventory, ship: Ship) -> None:
    """Before a Ship is destroyed: leave the overlay, then take its device off it, so it
    does not linger there. Neither step stops the destroy."""
    if not inventory.overlay:
        return
    current = None
    try:
        ctl = ShipCtl(app.ssh_factory(inventory), ship)
        current = status(ctl)
        if current and current.get("joined"):
            for message in leave(ctl):
                app.note(f"{ship.name}: {message}")
    except (FleetError, ShipCtlError) as exc:
        app.note(f"{ship.name}: could not leave the overlay ({exc.message}).")
    try:
        options = {"api_factory": app.overlay_api} if getattr(app, "overlay_api", None) else {}
        removal = provider(inventory.overlay, **options).remove(ship, current)
    except FleetError as exc:
        app.note(f"{ship.name}: could not remove its overlay device ({exc.message}); "
                 "remove it in your overlay's admin console.")
        return
    for message in removal.messages:
        app.note(f"{ship.name}: {message}")
