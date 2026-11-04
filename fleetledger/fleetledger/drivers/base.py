"""The hypervisor driver interface.

FleetLedger talks to hypervisors only through this, so hypervisor code never
leaks into the rest. A driver either does each step itself (proxmox) or tells
you exactly what to do (manual); both report back the same way.
"""

from __future__ import annotations

import re
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


@dataclass(frozen=True)
class PublishedImage:
    """A baked image ready to create Ships from on this driver (contracts/image.md).

    ref is what publish_image returned, as stored in ships.toml
    image.<id>.published.<driver>.
    """

    id: str
    ref: Mapping[str, str | int]


@dataclass(frozen=True)
class ImageFile:
    """A baked image on this computer, checked against its recorded sha256."""

    id: str
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class Snapshot:
    name: str
    created: str | None  # UTC timestamp, if the hypervisor says
    description: str = ""
    parent: str | None = None


SNAPSHOT_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,39}$")


def check_snapshot_name(name: str) -> str:
    if not SNAPSHOT_NAME.match(name) or name.lower() == "current":
        raise FleetError(
            f"'{name}' is not a usable snapshot name: a letter, then letters, digits, - or _ "
            "(2 to 40 characters), and not 'current'."
        )
    return name


class Driver(ABC):
    id: str

    @abstractmethod
    def create(
        self,
        ship: Ship,
        seed: SeedFile,
        size: Mapping[str, int],
        image: PublishedImage | None = None,
    ) -> Outcome:
        """Make the VM from the base image, attach the seed, start it.

        image: a baked image published to this driver; None for the stock
        template. driver_ref in the outcome is stored in ships.toml; it includes
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

    # --- size (optional) --------------------------------------------------
    def resize(self, ship: Ship, size: Mapping[str, int]) -> Outcome:
        """Change cores, memory_mb and/or disk_gb (a disk only grows). Cores and memory
        apply at the next restart through the hypervisor; the disk grows at once and
        the Ship's filesystem fills it at its next boot."""
        raise NotSupported(f"The {self.id} driver cannot resize a Ship; change its VM on "
                           "your hypervisor, then record the size with ship-fleet resize "
                           "--record-only.")

    # --- snapshots (optional) ---------------------------------------------
    # A snapshot holds the Ship's sign-ins: treat it like the Ship itself.
    def snapshot(self, ship: Ship, name: str, description: str = "") -> Outcome:
        raise NotSupported(f"The {self.id} driver cannot take snapshots.")

    def snapshots(self, ship: Ship) -> list[Snapshot]:
        raise NotSupported(f"The {self.id} driver cannot list snapshots.")

    def rollback(self, ship: Ship, name: str) -> Outcome:
        raise NotSupported(f"The {self.id} driver cannot roll back to a snapshot.")

    def delete_snapshot(self, ship: Ship, name: str) -> Outcome:
        raise NotSupported(f"The {self.id} driver cannot delete snapshots.")

    # --- baked images (optional) --------------------------------------------
    def publish_image(self, image: ImageFile) -> Outcome:
        """Make a baked image something create() can start from.

        driver_ref in the outcome is stored as image.<id>.published.<driver>.
        """
        raise NotSupported(f"The {self.id} driver cannot publish images.")

    def remove_image(self, image: PublishedImage) -> Outcome:
        raise NotSupported(f"The {self.id} driver cannot remove images.")
