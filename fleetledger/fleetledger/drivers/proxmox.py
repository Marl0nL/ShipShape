"""Proxmox VE driver, phase 1: the test helper (phase 4 finishes it).

Uses only the scoped API token from ships.toml [drivers.proxmox] (plan,
"Access needed"): uploads the seed ISO to the seed-only storage, full-clones
the template into the pool, attaches the seed as a CD-ROM, sizes the VM,
starts it, reads addresses from the guest agent, and destroys it again. It
never needs root, SSH to the host, or the web UI.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fleetledger.drivers.base import Driver, Outcome, PowerState, SeedFile
from fleetledger.drivers.proxmox_api import ProxmoxAPI, ProxmoxError
from fleetledger.errors import FleetError
from fleetledger.inventory import Ship

BOOT_DISK = "scsi0"  # the template's disk (plan: "The template, once")
SEED_DRIVE = "ide2"


def seed_volume(storage: str, instance_id: str) -> str:
    return f"{storage}:iso/{instance_id}.iso"


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
    def create(self, ship: Ship, seed: SeedFile, size: Mapping[str, int]) -> Outcome:
        cfg = self.config
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
                    self._vm(cfg["template_vmid"]) + "/clone",
                    newid=vmid,
                    name=ship.name,
                    pool=cfg["pool"],
                    full=True,
                    storage=cfg["disk_storage"],
                ),
                timeout=1200,
            )
            cloned = True
            messages.append(f"Cloned template {cfg['template_vmid']} to VM {vmid}.")
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
                self._destroy_vm(vmid)
            if uploaded:
                self._delete_volume(volume)
            raise
        return Outcome(
            done=True,
            messages=messages,
            driver_ref={"vmid": vmid, "seed": seed.instance_id},
        )

    def start(self, ship: Ship) -> Outcome:
        self._task(self.api.post(self._vm(self._vmid(ship)) + "/status/start"))
        return Outcome(done=True, messages=[f"Started {ship.name}."])

    def stop(self, ship: Ship) -> Outcome:
        # ACPI shutdown, so herdr can save its state (logind inhibitor).
        self._task(self.api.post(self._vm(self._vmid(ship)) + "/status/shutdown", timeout=180))
        return Outcome(done=True, messages=[f"Shut down {ship.name}."])

    def reboot(self, ship: Ship) -> Outcome:
        self._task(self.api.post(self._vm(self._vmid(ship)) + "/status/reboot"))
        return Outcome(done=True, messages=[f"Rebooted {ship.name}."])

    def destroy(self, ship: Ship) -> Outcome:
        vmid = self._vmid(ship)
        self._destroy_vm(vmid)
        messages = [f"Destroyed VM {vmid}."]
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
                ifaces = self.api.get(vm + "/agent/network-get-interfaces").get("result", [])
            except ProxmoxError:
                ifaces = []  # guest agent not up yet
            for iface in ifaces:
                if iface.get("name") == "lo":
                    continue
                for addr in iface.get("ip-addresses", []):
                    if addr.get("ip-address-type") == "ipv4":
                        addresses.append(addr["ip-address"])
        return PowerState(state if state in ("running", "stopped") else "unknown", addresses)

    def finish_enrolment(self, ship: Ship) -> Outcome:
        vmid = self._vmid(ship)
        self._task(self.api.post(self._vm(vmid) + "/config", delete=SEED_DRIVE))
        messages = [f"Detached the seed CD-ROM from VM {vmid}."]
        if "seed" in ship.driver_ref:
            self._delete_volume(self._ship_seed(ship))
            messages.append("Deleted the uploaded seed ISO.")
        return Outcome(done=True, messages=messages)

    # --- clean-up ---------------------------------------------------------
    def _ship_seed(self, ship: Ship) -> str:
        return seed_volume(self.config["seed_storage"], str(ship.driver_ref["seed"]))

    def _destroy_vm(self, vmid: int) -> None:
        vm = self._vm(vmid)
        try:
            if self.api.get(vm + "/status/current").get("status") == "running":
                self._task(self.api.post(vm + "/status/stop"))
        except ProxmoxError as exc:
            if exc.status != 500:  # 500: no such VM
                raise
            return
        self._task(self.api.delete(vm, purge=True, **{"destroy-unreferenced-disks": True}))

    def _delete_volume(self, volume: str) -> None:
        storage = volume.split(":", 1)[0]
        try:
            self._task(self.api.delete(f"/nodes/{self.node}/storage/{storage}/content/{volume}"))
        except ProxmoxError as exc:
            if exc.status not in (404, 500):  # already gone
                raise
