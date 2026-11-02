"""The overlay network (phase 5): reach Ships from your other machines off the LAN.

Ships join a Tailscale tailnet (or a Headscale server) after they boot. For each
join FleetLedger gets a one-off auth key, either pasted by you (read without
echo) or minted through Tailscale's API with a scoped OAuth client, and hands it
to the Ship over SSH on stdin (`ship-ctl overlay join`). Keys never go into
seeds, command lines, logs, the inventory or the audit log, and FleetLedger
keeps no copy. OpenSSH with the pinned host key stays the only way in, so the
overlay adds a route, not a new kind of trust.
"""

from __future__ import annotations

import getpass
import json
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fleetledger import paths
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Inventory, Ship
from fleetledger.shipctl import ShipCtl, ShipCtlError

API = "https://api.tailscale.com/api/v2"
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,500}$")
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


# --- where one-off keys come from ----------------------------------------------------


def check_key(key: str) -> str:
    key = key.strip()
    if not KEY_RE.match(key):
        raise FleetError("That is not an auth key (one line of letters, digits, - and _).")
    return key


class PromptKeys:
    """You paste a one-off key; it is read without echo and used once."""

    def __init__(self, config: Mapping[str, Any], *,
                 ask: Callable[[str], str] | None = None,
                 isatty: Callable[[], bool] = lambda: sys.stdin.isatty()) -> None:
        self.config = config
        self.ask = ask or (lambda prompt: getpass.getpass(prompt, stream=sys.stderr))
        self.isatty = isatty

    def key(self, ship: Ship) -> str:
        wanted = ", ".join(tags(self.config))
        if not self.isatty():
            raise FleetError(
                "Pasting an auth key needs a terminal.",
                hint="Run it in a terminal, or set key_source = \"tailscale-api\" in [overlay].",
            )
        where = ("your Headscale server (headscale preauthkeys create)"
                 if self.config.get("login_server") else
                 "the Tailscale admin console, Settings > Keys")
        return check_key(self.ask(
            f"One-off auth key for {ship.name} from {where}: single use, pre-approved, "
            f"tags {wanted}. It is not shown: "
        ))


Opener = Callable[[urllib.request.Request, float], Any]


def _open(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 (https only)


def read_secret(path_value: str) -> str:
    path = paths.expand(path_value)
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise ConfigError(f"{path} does not exist.") from exc
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise ConfigError(f"{path} is readable by others; chmod 600 it.")
    secret = path.read_text(encoding="utf-8").strip()
    if not secret or "\n" in secret:
        raise ConfigError(f"{path} must hold only the OAuth client secret.")
    return secret


class TailscaleAPIKeys:
    """Mint a single-use, pre-approved, tagged key per Ship with a scoped OAuth client.

    The OAuth client needs only the auth_keys write scope, for the Ships' tags. Its
    secret lives in a 0600 file; neither it, the access token nor the minted key is
    ever printed, logged or put in an exception.
    """

    def __init__(self, config: Mapping[str, Any], *, opener: Opener = _open) -> None:
        self.config = config
        self.api = config["api"]
        self.opener = opener
        self._token: str | None = None

    def _call(self, request: urllib.request.Request, what: str) -> dict:
        try:
            with self.opener(request, 30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                body = json.loads(exc.read() or b"{}")
                detail = str(body.get("message", ""))[:200] if isinstance(body, dict) else ""
            except (ValueError, OSError):
                pass
            raise FleetError(f"Tailscale API: {what} failed ({exc.code})"
                             + (f": {detail}" if detail else "") + ".") from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise FleetError(f"Tailscale API: cannot reach it for {what} ({reason}).") from None
        try:
            doc = json.loads(raw)
        except ValueError:
            raise FleetError(f"Tailscale API: {what} did not answer JSON.") from None
        if not isinstance(doc, dict):
            raise FleetError(f"Tailscale API: {what} gave an unexpected answer.")
        return doc

    def _access_token(self) -> str:
        if self._token is None:
            body = urllib.parse.urlencode({
                "client_id": self.api["oauth_client_id"],
                "client_secret": read_secret(self.api["oauth_secret_file"]),
                "grant_type": "client_credentials",
            }).encode()
            request = urllib.request.Request(f"{API}/oauth/token", data=body, method="POST")
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
            token = self._call(request, "the OAuth sign-in").get("access_token")
            if not isinstance(token, str) or not token:
                raise FleetError("Tailscale API: the OAuth sign-in returned no access token.")
            self._token = token
        return self._token

    def key(self, ship: Ship) -> str:
        tailnet = urllib.parse.quote(self.api.get("tailnet", "-"), safe="-.@")
        body = json.dumps({
            "capabilities": {"devices": {"create": {
                "reusable": False,
                "ephemeral": False,
                "preauthorized": True,
                "tags": tags(self.config),
            }}},
            "expirySeconds": int(self.api.get("key_expiry_s", 600)),
            "description": f"ShipShape {ship.name}"[:50],
        }).encode()
        request = urllib.request.Request(f"{API}/tailnet/{tailnet}/keys", data=body,
                                         method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self._access_token()}")
        key = self._call(request, "making a key").get("key")
        if not isinstance(key, str):
            raise FleetError("Tailscale API: no key in the answer.")
        return check_key(key)


def key_source(config: Mapping[str, Any], **kw) -> PromptKeys | TailscaleAPIKeys:
    if config.get("key_source", "prompt") == "tailscale-api":
        return TailscaleAPIKeys(config, opener=kw.get("opener", _open))
    return PromptKeys(config, ask=kw.get("ask"), isatty=kw.get("isatty", sys.stdin.isatty))


# --- joining, leaving, status ----------------------------------------------------------


@dataclass
class Joined:
    ship: str
    name: str | None
    addresses: list[str]
    messages: list[str]


def join(ctl: ShipCtl, ship: Ship, config: Mapping[str, Any], keys) -> Joined:
    argv = ["join", "--hostname", ship.name]
    for tag in tags(config):
        argv += ["--tag", tag]
    if config.get("login_server"):
        argv += ["--login-server", config["login_server"]]
    key = keys.key(ship)
    result = ctl.call("overlay", *argv, input=(key + "\n").encode("ascii"), timeout=900)
    overlay = result.data.get("overlay") or {}
    return Joined(ship.name, overlay.get("name"), list(overlay.get("addresses", [])),
                  list(result.messages))


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
    """A tailnet policy (access rules) for Ships: your devices may reach the Ships' tags
    on port 22, and nothing else is allowed, so a Ship can open no connection to your
    devices or to another Ship. Paste it into the admin console's access controls."""
    ship_tags = tags(config)
    doc = {
        "tagOwners": {tag: ["autogroup:admin"] for tag in ship_tags},
        "acls": [
            {"action": "accept", "src": ["autogroup:member"],
             "dst": [f"{tag}:22" for tag in ship_tags]},
        ],
        "ssh": [],
        "tests": [
            {"src": ship_tags[0], "deny": [f"{tag}:22" for tag in ship_tags]},
        ],
    }
    return json.dumps(doc, indent=2) + "\n"


def before_destroy(app, inventory: Inventory, ship: Ship) -> None:
    """Leave the overlay before a Ship is destroyed, so it does not linger there."""
    if not inventory.overlay:
        return
    try:
        ctl = ShipCtl(app.ssh_factory(inventory), ship)
        current = status(ctl)
        if current and current.get("joined"):
            for message in leave(ctl):
                app.note(f"{ship.name}: {message}")
    except (FleetError, ShipCtlError) as exc:
        app.note(f"{ship.name}: could not leave the overlay ({exc.message}); remove it in "
                 "your overlay's admin console.")
