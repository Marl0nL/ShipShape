"""Proxmox VE driver.

Uses only the scoped API token from ships.toml [drivers.proxmox]
(docs/runbooks/proxmox-setup.md): uploads the seed ISO to the seed-only
storage, full-clones a template (the stock one, or a published baked image)
into the pool, attaches the seed as a CD-ROM, sizes the VM, starts it, reads
addresses from the guest agent, takes and rolls back snapshots, publishes baked
images as templates, and destroys VMs again. It never needs root, SSH to the
host, or the web UI.

Every destructive call first checks the VM's name: FleetLedger only ever stops,
rolls back or destroys a VM whose name is the Ship's (or, for an image, the
template's), never a VMID alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from fleetledger.drivers.base import (
    Driver,
    ImageFile,
    Outcome,
    PowerState,
    PublishedImage,
    SeedFile,
    Snapshot,
    check_snapshot_name,
)
from fleetledger.drivers.proxmox_api import ProxmoxAPI, ProxmoxError
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Ship

BOOT_DISK = "scsi0"  # the template's disk (plan: "The template, once")
SEED_DRIVE = "ide2"
IMAGE_PREFIX = "shipshape-img-"

# A published image's template: the same hardware as the stock template
# (docs/runbooks/proxmox-setup.md), minus the serial console, which would need
# VM.Config.HWType. create() sets cores, memory, network and the seed per Ship.
TEMPLATE_HW = {
    "ostype": "l26",
    "cores": 2,
    "memory": 2048,
    "cpu": "host",
    "scsihw": "virtio-scsi-pci",
    "boot": f"order={BOOT_DISK}",
    "agent": "enabled=1",
}


class NotOurs(FleetError):
    """The VMID holds a VM FleetLedger did not make for this Ship or image."""


GONE = re.compile(r"does not exist|no such", re.IGNORECASE)


def is_gone(exc: ProxmoxError) -> bool:
    """The VM or volume is already gone. Proxmox answers 500 for most failures
    (locked, storage offline, in use), so the message decides, not the status."""
    return exc.status in (404, 500) and bool(GONE.search(exc.message))


def seed_volume(storage: str, instance_id: str) -> str:
    return f"{storage}:iso/{instance_id}.iso"


def image_file_name(image_id: str) -> str:
    return f"shipshape-{image_id}.qcow2"


def image_volume(storage: str, image_id: str) -> str:
    return f"{storage}:import/{image_file_name(image_id)}"


def template_name(image_id: str) -> str:
    return IMAGE_PREFIX + image_id


class ProxmoxDriver(Driver):
    id = "proxmox"

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        api: ProxmoxAPI | None = None,
        read_seed: Callable[[SeedFile], bytes] = lambda seed: seed.path.read_bytes(),
    ) -> None:
        self.config = config
        self.api = api or ProxmoxAPI(config)
        self.node = config["node"]
        self._read_seed = read_seed

    # --- helpers ----------------------------------------------------------
    def _vm(self, vmid: int | str) -> str:
        return f"/nodes/{self.node}/qemu/{vmid}"

    def _vmid(self, ship: Ship) -> int:
        try:
            return int(ship.driver_ref["vmid"])
        except (KeyError, ValueError) as exc:
            raise FleetError(f"{ship.name} has no Proxmox vmid in ships.toml driver_ref.") from exc

    def _task(self, upid: Any, timeout: float = 600) -> None:
        if isinstance(upid, str) and upid.startswith("UPID:"):
            self.api.wait_task(self.node, upid, timeout=timeout)

    def allocate_vmid(self) -> int:
        """The first free VMID in vmid_range (or the cluster's next free id)."""
        first, last = self.config.get("vmid_range", [None, None])
        if first is None:
            return int(self.api.get("/cluster/nextid"))
        for vmid in range(first, last + 1):
            try:
                self.api.get("/cluster/nextid", vmid=vmid)
            except ProxmoxError as exc:
                if exc.status == 400:  # in use
                    continue
                raise
            return vmid
        raise FleetError(f"No free VMID in vmid_range {first}-{last}.")

    # --- interface --------------------------------------------------------
    def create(
        self,
        ship: Ship,
        seed: SeedFile,
        size: Mapping[str, int],
        image: PublishedImage | None = None,
    ) -> Outcome:
        cfg = self.config
        template = int(image.ref["template_vmid"]) if image else int(cfg["template_vmid"])
        if image is not None and not self.check_name(
            template, template_name(image.id), template=True
        ):
            raise FleetError(f"Image {image.id}'s template VM {template} does not exist on "
                             f"{self.node}.", hint=f"Publish it again: ship-fleet image publish "
                             f"{image.id}")
        vmid = int(ship.driver_ref["vmid"]) if "vmid" in ship.driver_ref else self.allocate_vmid()
        volume = seed_volume(cfg["seed_storage"], seed.instance_id)
        messages: list[str] = []
        uploaded = cloned = False
        try:
            self._task(
                self.api.upload(
                    self.node, cfg["seed_storage"], seed.file_name, self._read_seed(seed), "iso"
                )
            )
            uploaded = True
            messages.append(f"Uploaded the seed as {volume}.")
            self._task(
                self.api.post(
                    self._vm(template) + "/clone",
                    newid=vmid,
                    name=ship.name,
                    pool=cfg["pool"],
                    full=True,
                    storage=cfg["disk_storage"],
                ),
                timeout=1200,
            )
            cloned = True
            label = f"image {image.id}" if image else "the stock template"
            messages.append(f"Cloned {label} (VM {template}) to VM {vmid}.")
            self._task(  # POST config is asynchronous: it returns a task
                self.api.post(
                    self._vm(vmid) + "/config",
                    **{SEED_DRIVE: f"{volume},media=cdrom"},
                    cores=size["cores"],
                    memory=size["memory_mb"],
                    net0=f"virtio,bridge={cfg['bridge']}",
                    agent="enabled=1",
                )
            )
            self._task(
                self.api.put(self._vm(vmid) + "/resize", disk=BOOT_DISK, size=f"{size['disk_gb']}G")
            )
            self._task(self.api.post(self._vm(vmid) + "/status/start"))
            messages.append(f"Started VM {vmid}.")
        except BaseException:
            # Leave nothing behind from a half-made Ship.
            if cloned:
                self._destroy_vm(vmid, ship.name)
            if uploaded:
                self._delete_volume(volume)
            raise
        return Outcome(
            done=True,
            messages=messages,
            driver_ref={"vmid": vmid, "seed": seed.instance_id},
        )

    def _checked_vm(self, ship: Ship) -> str:
        """The API path of the Ship's VM, after checking it is that Ship."""
        return self._vm(self._owned(ship))

    def start(self, ship: Ship) -> Outcome:
        self._task(self.api.post(self._checked_vm(ship) + "/status/start"))
        return Outcome(done=True, messages=[f"Started {ship.name}."])

    def stop(self, ship: Ship) -> Outcome:
        # ACPI shutdown, so herdr can save its state (logind inhibitor).
        self._task(self.api.post(self._checked_vm(ship) + "/status/shutdown", timeout=180))
        return Outcome(done=True, messages=[f"Shut down {ship.name}."])

    def reboot(self, ship: Ship) -> Outcome:
        self._task(self.api.post(self._checked_vm(ship) + "/status/reboot"))
        return Outcome(done=True, messages=[f"Rebooted {ship.name}."])

    def destroy(self, ship: Ship) -> Outcome:
        vmid = self._vmid(ship)
        gone = self._destroy_vm(vmid, ship.name)
        messages = [f"VM {vmid} was already gone." if gone else f"Destroyed VM {vmid}."]
        if "seed" in ship.driver_ref:
            self._delete_volume(self._ship_seed(ship))
            messages.append("Deleted its seed ISO.")
        return Outcome(done=True, messages=messages)

    def status(self, ship: Ship) -> PowerState:
        vm = self._vm(self._vmid(ship))
        current = self.api.get(vm + "/status/current")
        state = current.get("status", "unknown")
        addresses: list[str] = []
        if state == "running":
            try:
                answer = self.api.get(vm + "/agent/network-get-interfaces")
            except ProxmoxError:
                answer = None  # guest agent not up yet
            # While the guest agent starts, Proxmox answers `data: null`: not yet.
            ifaces = answer.get("result") if isinstance(answer, dict) else None
            for iface in ifaces if isinstance(ifaces, list) else []:
                if not isinstance(iface, dict) or iface.get("name") == "lo":
                    continue
                for addr in iface.get("ip-addresses") or []:
                    if addr.get("ip-address-type") == "ipv4" and addr.get("ip-address"):
                        addresses.append(addr["ip-address"])
        return PowerState(state if state in ("running", "stopped") else "unknown", addresses)

    def finish_enrolment(self, ship: Ship) -> Outcome:
        vmid = self._owned(ship)
        # Eject the media: applied at once on a running VM (removing an IDE
        # drive would only be a pending change until the next power cycle).
        self._task(self.api.post(self._vm(vmid) + "/config", **{SEED_DRIVE: "none,media=cdrom"}))
        messages = [f"Ejected the seed CD-ROM from VM {vmid}."]
        if "seed" in ship.driver_ref:
            self._delete_volume(self._ship_seed(ship))
            messages.append("Deleted the uploaded seed ISO.")
        return Outcome(done=True, messages=messages)

    # --- snapshots ----------------------------------------------------------
    # Needs VM.Snapshot (take, delete) and VM.Snapshot.Rollback (rollback) on the
    # pool: see docs/runbooks/proxmox-setup.md. Disk only, never RAM (vmstate), so
    # no memory contents end up on the host's storage.
    def snapshot(self, ship: Ship, name: str, description: str = "") -> Outcome:
        check_snapshot_name(name)
        vmid = self._owned(ship)
        self._task(
            self.api.post(self._vm(vmid) + "/snapshot", snapname=name,
                          description=description or None, vmstate=False),
            timeout=1200,
        )
        return Outcome(done=True, messages=[f"Took snapshot {name} of {ship.name} (VM {vmid})."])

    def snapshots(self, ship: Ship) -> list[Snapshot]:
        vmid = self._owned(ship)
        found = []
        for snap in self.api.get(self._vm(vmid) + "/snapshot") or []:
            if not isinstance(snap, dict) or snap.get("name") in (None, "current"):
                continue
            when = snap.get("snaptime")
            created = (datetime.fromtimestamp(int(when), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                       if isinstance(when, int | str) and str(when).isdigit() else None)
            found.append(Snapshot(name=snap["name"], created=created,
                                  description=str(snap.get("description") or "").strip(),
                                  parent=snap.get("parent")))
        return sorted(found, key=lambda s: (s.created or "", s.name))

    def rollback(self, ship: Ship, name: str) -> Outcome:
        check_snapshot_name(name)
        vmid = self._owned(ship)
        if name not in {s.name for s in self.snapshots(ship)}:
            raise FleetError(f"{ship.name} has no snapshot named {name}.")
        vm = self._vm(vmid)
        messages = []
        if self.api.get(vm + "/status/current").get("status") == "running":
            # A clean shutdown first, so herdr saves its state; rollback would
            # otherwise stop the VM like a power cut.
            self._task(self.api.post(vm + "/status/shutdown", timeout=180), timeout=240)
            messages.append(f"Shut down {ship.name}.")
        self._task(self.api.post(f"{vm}/snapshot/{name}/rollback", start=True), timeout=1200)
        messages.append(f"Rolled {ship.name} back to {name} and started it.")
        return Outcome(done=True, messages=messages)

    def delete_snapshot(self, ship: Ship, name: str) -> Outcome:
        check_snapshot_name(name)
        vmid = self._owned(ship)
        try:
            self._task(self.api.delete(f"{self._vm(vmid)}/snapshot/{name}"), timeout=1200)
        except ProxmoxError as exc:
            if not is_gone(exc):
                raise
            return Outcome(done=True, messages=[f"{ship.name} has no snapshot {name}."])
        return Outcome(done=True, messages=[f"Deleted snapshot {name} of {ship.name}."])

    # --- baked images ---------------------------------------------------------
    # Upload the qcow2 to image_storage (content type import), create a VM in the
    # pool that imports it as its disk, make that VM a template, and delete the
    # upload. Ships are full clones, so a template can go once no new Ship needs it.
    def publish_image(self, image: ImageFile) -> Outcome:
        cfg = self.config
        storage = cfg.get("image_storage")
        if not storage:
            raise ConfigError(
                "Publishing an image needs drivers.proxmox.image_storage: a storage with the "
                "import content type (docs/runbooks/proxmox-setup.md, \"Baked images\").",
                hint=f"Or publish it by hand: ship-fleet image publish {image.id} "
                "--driver manual",
            )
        name = template_name(image.id)
        volume = image_volume(storage, image.id)
        vmid = self.allocate_vmid()
        uploaded = created = False
        try:
            self._task(
                self.api.upload_file(self.node, storage, image_file_name(image.id), image.path,
                                     "import", sha256=image.sha256),
                timeout=1800,
            )
            uploaded = True
            self._task(
                self.api.post(
                    f"/nodes/{self.node}/qemu",
                    vmid=vmid,
                    name=name,
                    pool=cfg["pool"],
                    description=f"ShipShape image {image.id} (sha256 {image.sha256})",
                    net0=f"virtio,bridge={cfg['bridge']}",
                    **{BOOT_DISK: f"{cfg['disk_storage']}:0,import-from={volume}"},
                    **TEMPLATE_HW,
                ),
                timeout=1800,
            )
            created = True
            self._task(self.api.post(self._vm(vmid) + "/template"), timeout=600)
        except BaseException:
            if created:
                self._destroy_vm(vmid, name)
            if uploaded:
                self._delete_volume(volume)
            raise
        self._delete_volume(volume)  # the template has its own copy of the disk
        return Outcome(
            done=True,
            messages=[f"Published image {image.id} as template VM {vmid} ({name})."],
            driver_ref={"template_vmid": vmid},
        )

    def remove_image(self, image: PublishedImage) -> Outcome:
        vmid = int(image.ref["template_vmid"])
        gone = self._destroy_vm(vmid, template_name(image.id), template=True)
        text = f"Template VM {vmid} was already gone." if gone else f"Deleted template VM {vmid}."
        return Outcome(done=True, messages=[text])

    # --- clean-up ---------------------------------------------------------
    def _ship_seed(self, ship: Ship) -> str:
        return seed_volume(self.config["seed_storage"], str(ship.driver_ref["seed"]))

    def check_name(self, vmid: int, name: str, *, template: bool = False) -> bool:
        """True if VM vmid is the VM called name; False if it is already gone.

        VMIDs are reused once freed, so a VMID recorded earlier may now belong to
        another VM. Anything that changes, removes or clones a VM checks its name
        (and whether it is a template) first, and refuses a VM with another name,
        the wrong kind, or one the token cannot see.
        """
        try:
            config = self.api.get(self._vm(vmid) + "/config")
        except ProxmoxError as exc:
            if is_gone(exc):
                return False
            if exc.status != 403:
                raise
            # The token sees VMs only through the pool, and a destroyed VM leaves
            # it, so a VMID that is simply free also answers 403. Ask whether the
            # number is free (open to every user); taken means hidden, not ours.
            try:
                self.api.get("/cluster/nextid", vmid=vmid)
            except ProxmoxError as taken:
                if taken.status == 400:
                    raise NotOurs(
                        f"VM {vmid} is not visible to the token (not in the pool?); "
                        f"not touching it as {name}."
                    ) from exc
                raise
            return False
        config = config if isinstance(config, dict) else {}
        actual, is_template = config.get("name"), bool(config.get("template"))
        if actual != name:
            raise NotOurs(
                f"VM {vmid} is now '{actual}', not {name} (its VMID was reused); "
                "not touching it.",
                hint="Check ships.toml: the Ship's driver_ref.vmid or the image's "
                "template_vmid may be stale.",
            )
        if is_template != template:
            kind = "a template" if is_template else "not a template"
            raise NotOurs(f"VM {vmid} ({name}) is {kind}; not touching it.")
        return True

    def _owned(self, ship: Ship) -> int:
        vmid = self._vmid(ship)
        if not self.check_name(vmid, ship.name):
            raise FleetError(f"VM {vmid} ({ship.name}) no longer exists.")
        return vmid

    def _destroy_vm(self, vmid: int, expected: str, *, template: bool = False) -> bool:
        """Stop and destroy a VM after checking its name. True if it was already gone."""
        vm = self._vm(vmid)
        try:
            if not self.check_name(vmid, expected, template=template):
                return True
            if self.api.get(vm + "/status/current").get("status") == "running":
                self._task(self.api.post(vm + "/status/stop"))
        except ProxmoxError as exc:
            if not is_gone(exc):
                raise
            return True
        try:
            self._task(self.api.delete(vm, purge=True, **{"destroy-unreferenced-disks": True}))
        except ProxmoxError as exc:
            if not is_gone(exc):
                raise
            return True
        return False

    def _delete_volume(self, volume: str) -> None:
        storage = volume.split(":", 1)[0]
        try:
            self._task(self.api.delete(f"/nodes/{self.node}/storage/{storage}/content/{volume}"))
        except ProxmoxError as exc:
            if not is_gone(exc):
                raise
