"""One-off join keys pasted by you: read without echo, checked, used once."""

from __future__ import annotations

import getpass
import re
import sys
from collections.abc import Callable, Mapping
from typing import Any

from fleetledger.errors import FleetError
from fleetledger.inventory import Ship

KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16,500}$")


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
        wanted = ", ".join(self.config.get("tags", ("tag:ship",)))
        if not self.isatty():
            raise FleetError(
                "Pasting an auth key needs a terminal.",
                hint="Run it in a terminal, or set key_source = \"tailscale-api\" in [overlay].",
            )
        headscale = self.config.get("provider") == "headscale" or self.config.get("login_server")
        where = ("your Headscale server (headscale preauthkeys create)" if headscale else
                 "the Tailscale admin console, Settings > Keys")
        return check_key(self.ask(
            f"One-off auth key for {ship.name} from {where}: single use, pre-approved, "
            f"tags {wanted}. It is not shown: "
        ))
