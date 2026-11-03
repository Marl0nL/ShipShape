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
- remove(ship): take a destroyed Ship's device off the overlay, or say how;
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

DEFAULT_TAGS = ("tag:ship",)


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
        return list(self.config.get("tags", DEFAULT_TAGS))

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
    def remove(self, ship: Ship) -> Removal: ...

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

    def find_devices(self, ship: Ship) -> list[dict]:
        """The Ship's device(s): tagged with one of its tags, and named as the Ship."""
        found = []
        for device in self.api().devices():
            tags = set(device.get("tags") or [])
            if not tags & set(self.tags):
                continue
            name = str(device.get("name") or "").rstrip(".")
            if name in ship.names or device.get("hostname") == ship.name or \
                    name.split(".", 1)[0] == ship.name:
                found.append(device)
        return found

    def remove(self, ship: Ship) -> Removal:
        if not self.uses_api:
            return Removal(False, [
                f"Remove {ship.name} from your tailnet: admin console > Machines > "
                f"{ship.name} > Remove (FleetLedger has no API access with pasted keys).",
            ])
        devices = self.find_devices(ship)
        if not devices:
            return Removal(True, [f"{ship.name} has no device on the tailnet."])
        if len(devices) > 1:
            names = ", ".join(str(d.get("name")) for d in devices)
            return Removal(False, [f"Several tailnet devices match {ship.name} ({names}); "
                                   "not removing any. Remove the right one in the admin console."])
        device = devices[0]
        device_id = str(device.get("nodeId") or device.get("id") or "")
        self.api().delete_device(device_id)
        return Removal(True, [f"Removed {device.get('name') or ship.name} from the tailnet."])


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

    def remove(self, ship: Ship) -> Removal:
        return Removal(False, [
            f"Remove {ship.name} from Headscale, on its server: headscale nodes list "
            f"(find {ship.name}), then headscale nodes delete --identifier <id>.",
        ])


PROVIDERS: dict[str, Callable[..., OverlayProvider]] = {
    "tailscale": TailscaleProvider,
    "headscale": HeadscaleProvider,
}


def get(config: Mapping[str, Any], **options: Any) -> OverlayProvider:
    try:
        return PROVIDERS[config.get("provider", "tailscale")](config, **options)
    except KeyError:
        raise FleetError(f"Unknown overlay provider {config.get('provider')!r}.") from None
