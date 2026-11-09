"""The manual driver: FleetLedger prints the steps, you run them.

It works for any hypervisor. When ships.toml has a [drivers.proxmox] section
the steps are exact `qm` and `pvesm` commands for that server (run as root on
the Proxmox host); otherwise they are generic.
"""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from typing import Any

from fleetledger.drivers.base import (
    Driver,
    ImageFile,
    Outcome,
    PowerState,
    PublishedImage,
    SeedFile,
    check_snapshot_name,
)
from fleetledger.drivers.proxmox import seed_volume, template_name
from fleetledger.inventory import Ship

GENERIC = (
    "Create a VM from the Ubuntu {release} cloud image ({cores} vCPUs, {memory_mb} MB memory, "
    "{disk_gb} GB disk, bridged onto your LAN with DHCP), attach {seed} as a CD-ROM, and start "
    "it. cloud-init finds the seed by its CIDATA label."
)


class ManualDriver(Driver):
    id = "manual"

    def __init__(
        self, proxmox: Mapping[str, Any] | None = None, *, ubuntu_release: str = "26.04",
        protected: int | None = None,
    ) -> None:
        self.px = proxmox
        self.release = ubuntu_release
        self.protected = protected  # FleetLedger's own VM id ([fleet] self): no step for it

    def _vmid(self, ship: Ship) -> str:
        from fleetledger.appliance import refuse_self

        refuse_self(ship.driver_ref.get("vmid"), self.protected)
        return str(ship.driver_ref.get("vmid", "<vmid>"))

    def _volume(self, instance_id: str) -> str:
        storage = self.px["seed_storage"] if self.px else "<seed-storage>"
        return seed_volume(storage, instance_id)

    def _free_seed(self, ship: Ship) -> str:
        return shlex.join(["pvesm", "free", self._volume(str(ship.driver_ref["seed"]))])

    def create(
        self,
        ship: Ship,
        seed: SeedFile,
        size: Mapping[str, int],
        image: PublishedImage | None = None,
    ) -> Outcome:
        ref: dict[str, str | int] = {"seed": seed.instance_id}
        if "vmid" in ship.driver_ref:
            ref["vmid"] = ship.driver_ref["vmid"]
        if not self.px:
            text = GENERIC.format(release=self.release, seed=seed.path, **size)
            if image is not None:
                text = text.replace(f"the Ubuntu {self.release} cloud image",
                                    f"the baked image {image.id}")
            return Outcome(done=False, instructions=[text], driver_ref=ref)
        px, vmid = self.px, self._vmid(ship)
        template = str(image.ref.get("template_vmid")) if image else str(px["template_vmid"])
        volume = self._volume(seed.instance_id)
        steps = [
            f"# 1. Upload {seed.path} to the {px['seed_storage']} storage as an ISO image named "
            f"{seed.file_name} (web UI: {px['seed_storage']} > ISO Images > Upload).",
            "# 2. On the Proxmox host, as root:",
            shlex.join(
                [
                    "qm", "clone", template, vmid, "--name", ship.name,
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

    def snapshot(self, ship: Ship, name: str, description: str = "") -> Outcome:
        check_snapshot_name(name)
        if self.px:
            return Outcome(done=False, instructions=[
                shlex.join(["qm", "snapshot", self._vmid(ship), name])])
        return Outcome(done=False, instructions=[
            f"Take a disk snapshot of {ship.name}'s VM named {name}."])

    def rollback(self, ship: Ship, name: str) -> Outcome:
        check_snapshot_name(name)
        if self.px:
            vmid = self._vmid(ship)
            return Outcome(done=False, instructions=[
                shlex.join(["qm", "shutdown", vmid]),
                shlex.join(["qm", "rollback", vmid, name, "--start", "1"])])
        return Outcome(done=False, instructions=[
            f"Shut {ship.name}'s VM down, roll it back to {name} and start it."])

    def delete_snapshot(self, ship: Ship, name: str) -> Outcome:
        check_snapshot_name(name)
        if self.px:
            return Outcome(done=False, instructions=[
                shlex.join(["qm", "delsnapshot", self._vmid(ship), name])])
        return Outcome(done=False, instructions=[f"Delete snapshot {name} of {ship.name}'s VM."])

    def publish_image(self, image: ImageFile) -> Outcome:
        """Print how to turn the qcow2 into a template; record it afterwards with
        `ship-fleet image publish <id> --template-vmid N`."""
        if not self.px:
            return Outcome(done=False, instructions=[
                f"Import {image.path} as a VM template on your hypervisor (sha256 "
                f"{image.sha256}), then record it: ship-fleet image publish {image.id} "
                "--driver manual --template-vmid <id>"])
        px = self.px
        q = shlex.quote
        remote = f"/var/tmp/{image.path.name}"
        return Outcome(done=False, instructions=[
            f"# 1. Copy {image.path} to the Proxmox host as {remote}.",
            "# 2. On the host, as root, with VMID set to a free VM id (e.g. VMID=9050):",
            f"echo {q(image.sha256 + '  ' + remote)} | sha256sum -c",
            f'qm create "$VMID" --name {q(template_name(image.id))} --pool {q(px["pool"])} '
            "--memory 2048 --cores 2 --cpu host --ostype l26 "
            f"--net0 {q('virtio,bridge=' + px['bridge'])} --scsihw virtio-scsi-pci "
            f"--scsi0 {q(px['disk_storage'] + ':0,import-from=' + remote)} "
            "--boot order=scsi0 --agent enabled=1",
            'qm template "$VMID"',
            f"rm {q(remote)}",
            f"# 3. Here: ship-fleet image publish {image.id} --driver manual "
            "--template-vmid <the VMID>",
        ])

    def remove_image(self, image: PublishedImage) -> Outcome:
        vmid = str(image.ref.get("template_vmid", "<vmid>"))
        if self.px:
            return Outcome(done=False, instructions=[
                shlex.join(["qm", "destroy", vmid, "--purge"])])
        return Outcome(done=False, instructions=[f"Delete the template made from {image.id}."])

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
