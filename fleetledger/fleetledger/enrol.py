"""Enrolment: from a booted seed to a Ship in the inventory (contracts/seed.md).

1. Connect as fleet with the host key FleetLedger generated (already pinned).
2. Poll status until provisioning has finished (ok or failed).
3. Detach the seed CD-ROM and delete the uploaded ISO through the driver,
   delete FleetLedger's own copy, and record ship.<name>.enrolled.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from fleetledger import paths
from fleetledger.drivers import Driver
from fleetledger.errors import FleetError
from fleetledger.inventory import Inventory, Ship, now_timestamp
from fleetledger.ssh import SSH, HostKeyMismatch
from fleetledger.status import ShipView, fetch
from fleetledger.store import Store


@dataclass
class Enrolment:
    view: ShipView
    messages: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)


def wait_for_provisioning(
    ssh: SSH,
    inventory: Inventory,
    ship: Ship,
    *,
    timeout: float = 1800,
    interval: float = 15,
    on_update: Callable[[ShipView], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ShipView:
    """Poll until provisioning is ok or failed. A Ship still booting is unreachable;
    that is expected and retried. A wrong host key stops at once."""
    deadline = clock() + timeout
    while True:
        view = fetch(ssh, inventory, ship)
        if on_update:
            on_update(view)
        if view.problem == "host-key":
            raise HostKeyMismatch(view.message or "Host key mismatch.")
        if view.problem == "identity":
            raise FleetError(view.message or "Wrong Ship.")
        if view.provision_result in ("ok", "failed"):
            return view
        if clock() + interval > deadline:
            raise FleetError(
                f"{ship.name} did not finish provisioning within {timeout / 60:.0f} minutes "
                f"(last state: {view.state}).",
                hint=f"Check the VM console, then `ship-fleet logs {ship.name} provision`.",
            )
        sleep(interval)


def local_seed(ship: Ship):
    seed = ship.driver_ref.get("seed")
    return paths.seeds_dir() / f"{seed}.iso" if seed else None


def enrol(
    store: Store,
    ssh: SSH,
    driver: Driver,
    ship: Ship,
    **wait_options,
) -> Enrolment:
    view = wait_for_provisioning(ssh, store.load(), ship, **wait_options)
    if view.provision_result == "failed":
        raise FleetError(
            f"Provisioning failed on {ship.name}; it is not enrolled.",
            hint=f"See `ship-fleet logs {ship.name} provision`.",
        )
    outcome = driver.finish_enrolment(ship)
    seed_copy = local_seed(ship)
    if seed_copy is not None:
        seed_copy.unlink(missing_ok=True)
    with store.edit() as ed:
        ed.mark_enrolled(ship.name, now_timestamp())
        if outcome.done:  # manual: keep the name so `destroy` can remind you of the ISO
            ed.set_nested(ship.name, "driver_ref", "seed", None)
    messages = [*outcome.messages, f"{ship.name} is enrolled."]
    return Enrolment(view=view, messages=messages, instructions=list(outcome.instructions))
