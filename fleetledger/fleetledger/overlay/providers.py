"""Overlay providers: one interface, so other overlays can be added.

A provider is what FleetLedger needs to know about one overlay network. It does
not run anything on a Ship by itself: joining and leaving go through
`ship-ctl overlay` (contracts/ship-ctl.md), which today drives the tailscale
client on the Ship. docs/overlay-providers.md explains how to add one (NetBird,
ZeroTier, plain WireGuard, ...), Ship side included.

The interface:

- keys(): where one-off join keys come from (KeySource.key(ship) -> str);
- join_args(ship): the `ship-ctl overlay join` arguments (never the key);
- policy(): the access rules to give the overlay, as text;
- remove(ship, live): take a destroyed Ship's device off the overlay, by identity
  only, or say how;
- check(fleet): provider-side checks of the overlay itself (`overlay check`).
"""

from __future__ import annotations

import json
import sys
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from fleetledger.errors import FleetError
from fleetledger.inventory import Ship
from fleetledger.overlay import keys as keymod
from fleetledger.overlay.tsapi import TailscaleAPI, default_opener

# Whether destroy removes a Ship's tailnet device by default (overlay.api.remove_devices).
# Off: the stored secret then only mints keys (devices:core is tailnet-wide).
REMOVE_DEVICES_DEFAULT = False


class KeySource(Protocol):
    def key(self, ship: Ship) -> str: ...


@dataclass(frozen=True)
class Check:
    """One result of `ship-fleet overlay check`."""

    name: str
    state: str  # ok | fail | warn | skip
    detail: str = ""


@dataclass
class Removal:
    done: bool
    messages: list[str] = field(default_factory=list)


class OverlayProvider(ABC):
    id: str

    def __init__(self, config: Mapping[str, Any], **options: Any) -> None:
        self.config = config
        self.options = options

    @property
    def tags(self) -> list[str]:
        return list(self.config.get("tags", keymod.DEFAULT_TAGS))

    @abstractmethod
    def keys(self) -> KeySource: ...

    def join_args(self, ship: Ship) -> list[str]:
        args = ["join", "--hostname", ship.name]
        for tag in self.tags:
            args += ["--tag", tag]
        if self.config.get("login_server"):
            args += ["--login-server", self.config["login_server"]]
        return args

    def policy(self) -> str:
        """Your devices may reach the Ships' tags on port 22, and nothing else is
        allowed, so a Ship can open no connection to your devices or another Ship."""
        return json.dumps(policy_doc(self.tags), indent=2) + "\n"

    @abstractmethod
    def remove(self, ship: Ship, live: Mapping[str, Any] | None = None) -> Removal:
        """live: the Ship's overlay status just before it left, if it could be read."""

    def check(self, fleet: Iterable[Ship]) -> list[Check]:
        return []


def policy_doc(tags: list[str]) -> dict:
    return {
        "tagOwners": {tag: ["autogroup:admin"] for tag in tags},
        "acls": [
            {"action": "accept", "src": ["autogroup:member"],
             "dst": [f"{tag}:22" for tag in tags]},
        ],
        "ssh": [],
        "tests": [
            {"src": tags[0], "deny": [f"{tag}:22" for tag in tags]},
        ],
    }


# --- Tailscale (hosted) ----------------------------------------------------------------


class TailscaleProvider(OverlayProvider):
    """Tailscale's hosted service. Keys are pasted, or minted with an OAuth client."""

    id = "tailscale"

    @property
    def uses_api(self) -> bool:
        return self.config.get("key_source", "prompt") == "tailscale-api"

    def api(self) -> TailscaleAPI:
        if not self.uses_api:
            raise FleetError("This needs the Tailscale API (key_source = \"tailscale-api\").")
        cached = self.options.get("_api")
        if cached is None:
            factory = self.options.get("api_factory")
            cached = factory(self.config["api"]) if factory else TailscaleAPI(
                self.config["api"], opener=self.options.get("opener", default_opener))
            self.options["_api"] = cached
        return cached

    def keys(self) -> KeySource:
        if self.uses_api:
            return APIKeys(self.api(), self.tags, int(self.config["api"].get("key_expiry_s", 600)))
        return keymod.PromptKeys(self.config, ask=self.options.get("ask"),
                                 isatty=self.options.get("isatty", sys.stdin.isatty))

    @property
    def removes_devices(self) -> bool:
        """Whether destroy removes the Ship's device through the API. One default, here:
        Tailscale's devices:core is tailnet-wide, so this decides what a leaked
        secret could do (docs/overlay-providers.md)."""
        if not self.uses_api:
            return False
        return bool(self.config["api"].get("remove_devices", REMOVE_DEVICES_DEFAULT))

    def find_devices(self, ship: Ship, live: Mapping[str, Any] | None = None
                     ) -> tuple[list[dict], str]:
        """The Ship's device(s), by identity; (matches, how they were matched).

        Strongest first: the node id (recorded at join, or reported by the Ship now);
        else the tailnet name recorded at join or the Ship's live overlay addresses.
        Never the hostname alone: anyone can name a machine after a Ship. Only
        devices carrying one of the Ships' tags are ever considered.
        """
        devices = [d for d in self.api().devices()
                   if set(d.get("tags") or []) & set(self.tags)]
        live = live or {}
        node = live.get("node_id") or ship.overlay_node
        if node:
            return [d for d in devices if node in (d.get("nodeId"), d.get("id"))], "node id"
        names = {n for n in ship.names if "." in n} | (
            {str(live["name"])} if live.get("name") else set())
        addresses = set(live.get("addresses") or [])
        found = [d for d in devices
                 if str(d.get("name") or "").rstrip(".") in names
                 or addresses & set(d.get("addresses") or [])]
        return found, "tailnet name or address"

    def remove(self, ship: Ship, live: Mapping[str, Any] | None = None) -> Removal:
        if not self.removes_devices:
            why = ("FleetLedger has no API access with pasted keys" if not self.uses_api else
                   "device removal is off: overlay.api.remove_devices")
            live = live or {}
            shown = live.get("name") or next((n for n in ship.names if "." in n and not
                                              n.endswith(".local")), ship.name)
            node = live.get("node_id") or ship.overlay_node
            ident = f", node id {node}" if node else ""
            return Removal(False, [
                f"Remove {ship.name}'s device from your tailnet: admin console > Machines > "
                f"{shown}{ident} > ... > Remove ({why}).",
            ])
        devices, how = self.find_devices(ship, live)
        if not devices:
            namesakes = [d for d in self.api().devices() if d.get("hostname") == ship.name]
            if namesakes:
                return Removal(False, [
                    f"A device named {ship.name} is on the tailnet, but FleetLedger cannot "
                    "tell it is this Ship's (no recorded node id or tailnet name matches); "
                    "not removing it. Remove it in the admin console if it is."])
            return Removal(True, [f"{ship.name} has no device on the tailnet."])
        if len(devices) > 1:
            names = ", ".join(str(d.get("name")) for d in devices)
            return Removal(False, [f"Several tailnet devices match {ship.name} ({names}); "
                                   "not removing any. Remove the right one in the admin console."])
        device = devices[0]
        device_id = str(device.get("nodeId") or device.get("id") or "")
        self.api().delete_device(device_id)
        return Removal(True, [f"Removed {device.get('name') or ship.name} from the tailnet "
                              f"(matched by {how})."])

    def write_policy(self, text: str, etag: str | None) -> None:
        """Change the tailnet policy: only with overlay.api.manage_policy (opt-in)."""
        if not (self.uses_api and self.config["api"].get("manage_policy", False)):
            raise FleetError("FleetLedger may not change the tailnet policy: "
                             "overlay.api.manage_policy is off.")
        self.api().set_policy(text, etag)


class APIKeys:
    """Mint one single-use, pre-approved, tagged key per join."""

    def __init__(self, api: TailscaleAPI, tags: list[str], expiry_s: int) -> None:
        self.api, self.tags, self.expiry_s = api, tags, expiry_s

    def key(self, ship: Ship) -> str:
        return keymod.check_key(self.api.create_key(self.tags, self.expiry_s,
                                                    f"ShipShape {ship.name}"))


# --- Headscale (your own server) ---------------------------------------------------------


class HeadscaleProvider(OverlayProvider):
    """Your own Headscale server: keys are pasted; FleetLedger has no server access."""

    id = "headscale"

    def keys(self) -> KeySource:
        return keymod.PromptKeys(self.config, ask=self.options.get("ask"),
                                 isatty=self.options.get("isatty", sys.stdin.isatty))

    def remove(self, ship: Ship, live: Mapping[str, Any] | None = None) -> Removal:
        return Removal(False, [
            f"Remove {ship.name} from Headscale, on its server: headscale nodes list "
            f"(find {ship.name}), then headscale nodes delete --identifier <id>.",
        ])


PROVIDERS: dict[str, Callable[..., OverlayProvider]] = {
    "tailscale": TailscaleProvider,
    "headscale": HeadscaleProvider,
}


def get(config: Mapping[str, Any], **options: Any) -> OverlayProvider:
    make = PROVIDERS.get(config.get("provider", "tailscale"))
    if make is None:
        raise FleetError(f"Unknown overlay provider {config.get('provider')!r}.")
    return make(config, **options)
