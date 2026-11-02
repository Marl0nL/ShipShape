"""Hypervisor drivers (see base.py for the interface)."""

from __future__ import annotations

from fleetledger.drivers.base import (
    Driver,
    ImageFile,
    NotSupported,
    Outcome,
    PowerState,
    PublishedImage,
    SeedFile,
    Snapshot,
)
from fleetledger.errors import ConfigError
from fleetledger.inventory import Inventory

__all__ = [
    "Driver",
    "ImageFile",
    "NotSupported",
    "Outcome",
    "PowerState",
    "PublishedImage",
    "SeedFile",
    "Snapshot",
    "get_driver",
]


def get_driver(inventory: Inventory, driver_id: str, *, ubuntu_release: str = "26.04") -> Driver:
    proxmox = inventory.driver_config("proxmox")
    if driver_id == "manual":
        from fleetledger.drivers.manual import ManualDriver

        return ManualDriver(proxmox, ubuntu_release=ubuntu_release)
    if driver_id == "proxmox":
        if not proxmox:
            raise ConfigError(
                "The proxmox driver needs a [drivers.proxmox] section in ships.toml.",
                hint="See contracts/examples/ships.toml/basic.toml.",
            )
        from fleetledger.drivers.proxmox import ProxmoxDriver

        return ProxmoxDriver(proxmox)
    raise ConfigError(f"Unknown driver '{driver_id}'.")
