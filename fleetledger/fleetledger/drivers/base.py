"""The hypervisor driver interface.

FleetLedger talks to hypervisors only through this, so hypervisor code never
leaks into the rest. A driver either does each step itself (proxmox) or tells
you exactly what to do (manual); both report back the same way.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from fleetledger.errors import FleetError
from fleetledger.inventory import Ship


class NotSupported(FleetError):
    """The driver cannot do this."""


@dataclass(frozen=True)
class SeedFile:
    """A seed ISO ready to attach: the local file and its instance id."""

    path: Path
    instance_id: str

    @property
    def file_name(self) -> str:
        return f"{self.instance_id}.iso"


@dataclass(frozen=True)
class Outcome:
    """What a driver did, or (manual) what you need to do."""

    done: bool
    messages: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    driver_ref: Mapping[str, str | int] = field(default_factory=dict)


@dataclass(frozen=True)
class PowerState:
    state: str  # running | stopped | unknown
    addresses: list[str] = field(default_factory=list)


class Driver(ABC):
    id: str

    @abstractmethod
    def create(self, ship: Ship, seed: SeedFile, size: Mapping[str, int]) -> Outcome:
        """Make the VM from the base image, attach the seed, start it.

        driver_ref in the outcome is stored in ships.toml; it includes
        seed = <instance id> until enrolment finishes.
        """

    @abstractmethod
    def start(self, ship: Ship) -> Outcome: ...

    @abstractmethod
    def stop(self, ship: Ship) -> Outcome: ...

    @abstractmethod
    def reboot(self, ship: Ship) -> Outcome: ...

    @abstractmethod
    def destroy(self, ship: Ship) -> Outcome: ...

    @abstractmethod
    def status(self, ship: Ship) -> PowerState: ...

    @abstractmethod
    def finish_enrolment(self, ship: Ship) -> Outcome:
        """Detach the seed CD-ROM and delete the uploaded ISO (contracts/seed.md)."""

    def host_key_fingerprints(self, ship: Ship) -> list[str] | None:
        """Fingerprints the hypervisor can vouch for, or None if it cannot."""
        return None

    def snapshot(self, ship: Ship, name: str) -> Outcome:
        raise NotSupported(f"The {self.id} driver cannot take snapshots.")
