"""How you attach to a Ship's agents from a trusted machine, by what runs them.

A Ship runs its agents in herdr's server or in herdr-core (ship.toml
`runtime = "core"`, which every new Ship's seed writes); its status document says
which (`herdr.runtime`, absent from older tooling: herdr). A Ship with no status yet
is taken to run herdr-core: a Ship that has never answered is almost always a new one.
herdr Ships attach with the herdr TUI, `herdr --remote <ship>`.
The herdr TUI cannot attach to herdr-core: core Ships attach with `hc attach
<ship>/<agent>` (or the bare `hc` picker), or in the herdr desktop app.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

HERDR = "herdr"
CORE = "core"

APP_HINT = ("or in the herdr desktop app: add the Ship under Settings > Machines "
            "(its telemetry is on by default; you can turn it off in its settings)")


def runtime_of(doc: Mapping[str, Any] | None) -> str:
    """herdr or core, from a status document. None (no status yet): core, as every new
    Ship runs. A document from older tooling (no herdr.runtime): herdr. Anything else
    a newer Ship might report attaches as herdr does."""
    from fleetledger import status

    if doc is None:
        return CORE
    return CORE if status.runtime_of(doc) == CORE else HERDR


def cached_runtime(name: str) -> str:
    """A Ship's runtime from its last fetched status (none fetched yet: core)."""
    from fleetledger import status

    return runtime_of(status.read_cache(name))


def command(ship: str, runtime: str, agent: str | None = None) -> str:
    """The command that attaches to a Ship's agents from a trusted machine. On core,
    one agent's terminal when it is named, else the bare `hc` picker (every machine's
    terminals)."""
    if runtime == CORE:
        return f"hc attach {ship}/{agent}" if agent else "hc"
    return f"herdr --remote {ship}"


def first_agent(doc: Mapping[str, Any] | None) -> str | None:
    """The first named Claude agent in a status document, for a core Ship's attach."""
    for agent in ((doc or {}).get("herdr") or {}).get("agents") or []:
        if agent.get("kind") == "claude" and agent.get("name"):
            return agent["name"]
    return None


def for_ship(ship: str, doc: Mapping[str, Any] | None, agent: str | None = None) -> str:
    """command() for a Ship by its status document (agent, or its first agent on core)."""
    runtime = runtime_of(doc)
    return command(ship, runtime, agent or (first_agent(doc) if runtime == CORE else None))
