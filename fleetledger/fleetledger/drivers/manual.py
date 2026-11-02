"""The manual driver: FleetLedger prints the steps, you run them.

It works for any hypervisor. When ships.toml has a [drivers.proxmox] section
the steps are exact `qm` and `pvesm` commands for that server (run as root on
the Proxmox host); otherwise they are generic.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

from fleetledger.drivers.base import Driver, Outcome, PowerState, SeedFile
from fleetledger.inventory import Ship

GENERIC = (
    "Create a VM from the Ubuntu {release} cloud image ({cores} vCPUs, {memory_mb} MB memory, "
    "{disk_gb} GB disk, bridged onto your LAN with DHCP), attach {seed} as a CD-ROM, and start "
    "it. cloud-init finds the seed by its CIDATA label."
)


class ManualDriver(Driver):
    id = "manual"

    def __init__(
        self, proxmox: Mapping[str, Any] | None = None, *, ubuntu_release: str = "26.04"
    ) -> None:
        self.px = proxmox
        self.release = ubuntu_release

    def _vmid(self, ship: Ship) -> str:
        return str(ship.driver_ref.get("vmid", "<vmid>"))

    def _volume(self, instance_id: str) -> str:
        storage = self.px["seed_storage"] if self.px else "<seed-storage>"
        return f"{storage}:iso/{instance_id}.iso"

    def _free_seed(self, ship: Ship) -> str:
        return shlex.join(["pvesm", "free", self._volume(str(ship.driver_ref["seed"]))])

    def create(self, ship: Ship, seed: SeedFile, size: Mapping[str, int]) -> Outcome:
        ref: dict[str, str | int] = {"seed": seed.instance_id}
        if "vmid" in ship.driver_ref:
            ref["vmid"] = ship.driver_ref["vmid"]
        if not self.px:
            text = GENERIC.format(release=self.release, seed=seed.path, **size)
            return Outcome(done=False, instructions=[text], driver_ref=ref)
        px, vmid = self.px, self._vmid(ship)
        volume = self._volume(seed.instance_id)
        steps = [
            f"# 1. Upload {seed.path} to the {px['seed_storage']} storage as an ISO image named "
            f"{seed.file_name} (web UI: {px['seed_storage']} > ISO Images > Upload).",
            "# 2. On the Proxmox host, as root:",
            shlex.join(
                [
                    "qm", "clone", str(px["template_vmid"]), vmid, "--name", ship.name,
                    "--pool", px["pool"], "--full", "--storage", px["disk_storage"],
                ]
            ),
            shlex.join(
                [
                    "qm", "set", vmid, "--ide2", f"{volume},media=cdrom",
                    "--cores", str(size["cores"]), "--memory", str(size["memory_mb"]),
                    "--net0", f"virtio,bridge={px['bridge']}",
                ]
            ),
            shlex.join(["qm", "resize", vmid, "scsi0", f"{size['disk_gb']}G"]),
            shlex.join(["qm", "start", vmid]),
        ]
        return Outcome(done=False, instructions=steps, driver_ref=ref)

    def _simple(self, ship: Ship, verb: str) -> Outcome:
        if self.px:
            return Outcome(done=False, instructions=[shlex.join(["qm", verb, self._vmid(ship)])])
        return Outcome(done=False, instructions=[f"{verb.capitalize()} {ship.name}'s VM."])

    def start(self, ship: Ship) -> Outcome:
        return self._simple(ship, "start")

    def stop(self, ship: Ship) -> Outcome:
        return self._simple(ship, "shutdown")

    def reboot(self, ship: Ship) -> Outcome:
        return self._simple(ship, "reboot")

    def destroy(self, ship: Ship) -> Outcome:
        if self.px:
            vmid = self._vmid(ship)
            steps = [
                shlex.join(["qm", "stop", vmid]),
                shlex.join(["qm", "destroy", vmid, "--purge", "--destroy-unreferenced-disks"]),
            ]
            if "seed" in ship.driver_ref:
                steps.append(self._free_seed(ship))
            return Outcome(done=False, instructions=steps)
        return Outcome(done=False, instructions=[f"Delete {ship.name}'s VM and its disks."])

    def status(self, ship: Ship) -> PowerState:
        return PowerState("unknown")

    def finish_enrolment(self, ship: Ship) -> Outcome:
        if not self.px:
            return Outcome(
                done=False,
                instructions=[
                    f"Detach the seed CD-ROM from {ship.name}'s VM and delete the seed ISO "
                    "from your hypervisor's storage: it holds the Ship's private host key."
                ],
            )
        steps = [shlex.join(["qm", "set", self._vmid(ship), "--delete", "ide2"])]
        if "seed" in ship.driver_ref:
            steps.append(self._free_seed(ship))
        return Outcome(done=False, instructions=steps)
