import pytest
from fleetledger.drivers import SeedFile, get_driver
from fleetledger.drivers.manual import ManualDriver
from fleetledger.drivers.proxmox import ProxmoxDriver
from fleetledger.drivers.proxmox_api import ProxmoxError
from fleetledger.errors import ConfigError, FleetError
from fleetledger.inventory import Ship

PX = {
    "api_url": "https://pve.example.com:8006/api2/json",
    "node": "pve",
    "pool": "shipshape",
    "template_vmid": 9000,
    "seed_storage": "shipshape-seeds",
    "disk_storage": "local-lvm",
    "bridge": "vmbr0",
    "token_id": "shipshape@pve!build",
    "token_file": "~/.config/fleetledger/proxmox-token",
    "tls_fingerprint": ":".join(["AB"] * 32),
    "vmid_range": [9100, 9102],
}
SIZE = {"cores": 4, "memory_mb": 8192, "disk_gb": 40}


def ship(**ref):
    return Ship(
        name="ship-03",
        id="0b9f6c1e-7d7a-4d4c-8f39-2f1d7e1f0c11",
        driver="proxmox",
        names=("ship-03.local",),
        host_key="ssh-ed25519 AAAA",
        driver_ref=ref,
    )


SEED = SeedFile(path=None, instance_id="ship-03-0a1b2c3d")


class FakeAPI:
    """Records calls; every mutating call returns a task id that finishes OK."""

    def __init__(self, used=(), fail_on=None, vm_status="running", vm_name="ship-03", vms=None,
                 snapshots=()):
        self.vm_name = vm_name  # every VM not in vms: a name, None (gone) or "403"
        self.calls = []
        self.used = set(used)
        self.fail_on = fail_on
        self.vm_status = vm_status
        # vmid -> (name, is template); any other VM is ship-03, the test Ship.
        self.vms = vms or {}
        self.snaps = list(snapshots)
        self.tasks = 0

    def _task(self, method, path, params):
        self.calls.append((method, path, params))
        if self.fail_on and self.fail_on in path:
            raise ProxmoxError(f"{path} failed", status=500)
        self.tasks += 1
        return f"UPID:pve:{self.tasks}"

    def get(self, path, **params):
        self.calls.append(("GET", path, params))
        if path == "/cluster/nextid":
            if params.get("vmid") in self.used:
                raise ProxmoxError("in use", status=400)
            return params.get("vmid", 100)
        if path.endswith("/status/current"):
            return {"status": self.vm_status}
        if path.endswith("/config"):
            vmid = int(path.split("/")[-2])
            name, template = self.vms.get(vmid, (self.vm_name, False)) or (None, False)
            if name is None:
                raise ProxmoxError(f"Configuration file '{vmid}.conf' does not exist", status=500)
            if name == "403":
                raise ProxmoxError("Permission check failed (VM.Audit)", status=403)
            return {"name": name, "template": 1} if template else {"name": name}
        if path.endswith("/snapshot"):
            return [*self.snaps, {"name": "current", "description": "You are here!"}]
        if path.endswith("/agent/network-get-interfaces"):
            return {
                "result": [
                    {"name": "lo", "ip-addresses": [{"ip-address-type": "ipv4",
                                                     "ip-address": "127.0.0.1"}]},
                    {"name": "eth0", "ip-addresses": [
                        {"ip-address-type": "ipv4", "ip-address": "192.0.2.21"},
                        {"ip-address-type": "ipv6", "ip-address": "2001:db8::21"},
                    ]},
                ]
            }
        raise AssertionError(path)

    def post(self, path, **params):
        return self._task("POST", path, params)

    def put(self, path, **params):
        return self._task("PUT", path, params)

    def delete(self, path, **params):
        return self._task("DELETE", path, params)

    def upload(self, node, storage, name, data, content):
        return self._task("UPLOAD", f"/nodes/{node}/storage/{storage}/upload",
                          {"name": name, "data": data, "content": content})

    def upload_file(self, node, storage, name, path, content, sha256):
        return self._task("UPLOAD", f"/nodes/{node}/storage/{storage}/upload",
                          {"name": name, "path": path, "content": content, "sha256": sha256})

    def wait_task(self, node, upid, timeout=600):
        self.calls.append(("WAIT", upid, {}))


def mutating(api):
    return [(m, p, kw) for m, p, kw in api.calls if m not in ("GET", "WAIT")]


def test_proxmox_create(tmp_path):
    api = FakeAPI(used={9100})
    driver = ProxmoxDriver(PX, api=api, read_seed=lambda s: b"ISO")
    outcome = driver.create(ship(), SEED, SIZE)
    assert outcome.done and outcome.driver_ref == {"vmid": 9101, "seed": "ship-03-0a1b2c3d"}
    steps = mutating(api)
    assert steps[0] == ("UPLOAD", "/nodes/pve/storage/shipshape-seeds/upload",
                        {"name": "ship-03-0a1b2c3d.iso", "data": b"ISO", "content": "iso"})
    assert steps[1] == ("POST", "/nodes/pve/qemu/9000/clone", {
        "newid": 9101, "name": "ship-03", "pool": "shipshape", "full": True,
        "storage": "local-lvm",
    })
    assert steps[2] == ("POST", "/nodes/pve/qemu/9101/config", {
        "ide2": "shipshape-seeds:iso/ship-03-0a1b2c3d.iso,media=cdrom",
        "cores": 4, "memory": 8192, "net0": "virtio,bridge=vmbr0", "agent": "enabled=1",
    })
    assert steps[3] == ("PUT", "/nodes/pve/qemu/9101/resize", {"disk": "scsi0", "size": "40G"})
    assert steps[4] == ("POST", "/nodes/pve/qemu/9101/status/start", {})
    # Every task was waited for.
    assert sum(1 for c in api.calls if c[0] == "WAIT") == api.tasks


def test_proxmox_create_cleans_up_on_failure():
    api = FakeAPI(fail_on="/resize", vm_status="stopped")
    driver = ProxmoxDriver(PX, api=api, read_seed=lambda s: b"ISO")
    with pytest.raises(ProxmoxError):
        driver.create(ship(vmid=9102), SEED, SIZE)
    tail = mutating(api)[-2:]
    assert tail[0][:2] == ("DELETE", "/nodes/pve/qemu/9102")
    assert tail[0][2] == {"purge": True, "destroy-unreferenced-disks": True}
    volume = "shipshape-seeds:iso/ship-03-0a1b2c3d.iso"
    assert tail[1][:2] == ("DELETE", f"/nodes/pve/storage/shipshape-seeds/content/{volume}")


def test_proxmox_vmid_range_exhausted():
    driver = ProxmoxDriver(PX, api=FakeAPI(used={9100, 9101, 9102}))
    with pytest.raises(FleetError, match="No free VMID"):
        driver.allocate_vmid()


class AgentStartingAPI(FakeAPI):
    def __init__(self, answer):
        super().__init__()
        self.answer = answer

    def get(self, path, **params):
        if path.endswith("/agent/network-get-interfaces"):
            self.calls.append(("GET", path, params))
            return self.answer
        return super().get(path, **params)


@pytest.mark.parametrize("answer", [
    None,  # `data: null` while the guest agent starts (phase 0)
    {},
    {"result": None},
    {"result": [{"name": "eth0"}]},
    {"result": [{"name": "eth0", "ip-addresses": None}]},
    {"result": {"unexpected": 1}},
    {"result": 5},
    {"result": [{"name": "eth0", "ip-addresses": [{"ip-address-type": "ipv4"}]}]},
])
def test_proxmox_status_guest_agent_not_ready_is_pending(answer):
    power = ProxmoxDriver(PX, api=AgentStartingAPI(answer)).status(ship(vmid=9101))
    assert power.state == "running" and power.addresses == []


def test_proxmox_status_and_enrolment():
    api = FakeAPI()
    driver = ProxmoxDriver(PX, api=api)
    power = driver.status(ship(vmid=9101))
    assert power.state == "running" and power.addresses == ["192.0.2.21"]
    outcome = driver.finish_enrolment(ship(vmid=9101, seed="ship-03-0a1b2c3d"))
    assert outcome.done
    assert mutating(api) == [
        ("POST", "/nodes/pve/qemu/9101/config", {"ide2": "none,media=cdrom"}),
        ("DELETE",
         "/nodes/pve/storage/shipshape-seeds/content/shipshape-seeds:iso/ship-03-0a1b2c3d.iso",
         {}),
    ]


def test_proxmox_destroy_stops_first():
    api = FakeAPI(vm_status="running")
    ProxmoxDriver(PX, api=api).destroy(ship(vmid=9101))
    assert [c[:2] for c in mutating(api)] == [
        ("POST", "/nodes/pve/qemu/9101/status/stop"),
        ("DELETE", "/nodes/pve/qemu/9101"),
    ]
    with pytest.raises(FleetError, match="no Proxmox vmid"):
        ProxmoxDriver(PX, api=api).destroy(ship())


def test_manual_with_proxmox_settings(tmp_path):
    seed = SeedFile(path=tmp_path / "ship-03-0a1b2c3d.iso", instance_id="ship-03-0a1b2c3d")
    driver = ManualDriver(PX)
    outcome = driver.create(ship(vmid=9105), seed, SIZE)
    assert not outcome.done
    assert outcome.driver_ref == {"seed": "ship-03-0a1b2c3d", "vmid": 9105}
    text = "\n".join(outcome.instructions)
    assert "qm clone 9000 9105 --name ship-03 --pool shipshape --full --storage local-lvm" in text
    assert "--ide2 shipshape-seeds:iso/ship-03-0a1b2c3d.iso,media=cdrom" in text
    assert "qm resize 9105 scsi0 40G" in text and "qm start 9105" in text
    enrol = driver.finish_enrolment(ship(vmid=9105, seed="ship-03-0a1b2c3d"))
    assert enrol.instructions == [
        "qm set 9105 --delete ide2",
        "pvesm free shipshape-seeds:iso/ship-03-0a1b2c3d.iso",
    ]
    assert "qm destroy 9105" in "\n".join(driver.destroy(ship(vmid=9105)).instructions)


def test_manual_generic(tmp_path):
    seed = SeedFile(path=tmp_path / "s.iso", instance_id="ship-03-0a1b2c3d")
    outcome = ManualDriver(None).create(ship(), seed, SIZE)
    assert "<vmid>" not in outcome.instructions[0]
    assert "CIDATA" in outcome.instructions[0] and "26.04" in outcome.instructions[0]
    assert "private host key" in ManualDriver(None).finish_enrolment(ship()).instructions[0]
    assert ManualDriver(None).status(ship()).state == "unknown"


def test_get_driver(store):
    inv = store.load()
    assert get_driver(inv, "manual").id == "manual"
    with pytest.raises(ConfigError, match="drivers.proxmox"):
        get_driver(inv, "proxmox")


class FailingAPI(FakeAPI):
    """Every DELETE (and the status lookup) fails with a given Proxmox message."""

    def __init__(self, message, **kw):
        super().__init__(**kw)
        self.message = message

    def get(self, path, **params):
        if path.endswith("/status/current") and "status" in self.message:
            raise ProxmoxError(self.message, status=500)
        return super().get(path, **params)

    def delete(self, path, **params):
        self.calls.append(("DELETE", path, params))
        raise ProxmoxError(self.message, status=500)


@pytest.mark.parametrize("message", [
    "Proxmox DELETE failed: 500 can't lock file '/var/lock/qemu-server/lock-9101.conf'",
    "Proxmox DELETE failed: 500 storage 'shipshape-seeds' is not online",
    "Proxmox GET failed: 500 status lookup failed: VM is locked (clone)",
])
def test_other_server_errors_are_not_taken_as_gone(message):
    """Only "does not exist" means gone: the seed ISO holds the private host key."""
    driver = ProxmoxDriver(PX, api=FailingAPI(message, vm_status="stopped"))
    with pytest.raises(ProxmoxError):
        driver.destroy(ship(vmid=9101))
    with pytest.raises(ProxmoxError):
        driver.finish_enrolment(ship(vmid=9101, seed="ship-03-0a1b2c3d"))


def test_already_gone_is_fine():
    api = FailingAPI("Proxmox DELETE failed: 500 Configuration file "
                     "'nodes/pve/qemu-server/9101.conf' does not exist", vm_status="stopped")
    assert ProxmoxDriver(PX, api=api).destroy(ship(vmid=9101, seed="ship-03-0a1b2c3d")).done
    api = FailingAPI("Proxmox DELETE failed: 500 volume 'shipshape-seeds:iso/x.iso' "
                     "does not exist")
    assert ProxmoxDriver(PX, api=api).finish_enrolment(ship(vmid=9101, seed="x")).done


@pytest.mark.parametrize("other", ["someone-else", "ship-03-old", "ship-030", "ship-0"])
def test_destroy_refuses_a_reused_vmid(other):
    """A VMID recorded earlier may now belong to another VM: never destroy by number alone."""
    api = FakeAPI(vm_name=other)
    with pytest.raises(FleetError, match=f"now '{other}', not ship-03"):
        ProxmoxDriver(PX, api=api).destroy(ship(vmid=9101))
    for action in ("start", "stop", "reboot"):
        with pytest.raises(FleetError, match="not ship-03"):
            getattr(ProxmoxDriver(PX, api=api), action)(ship(vmid=9101))
    with pytest.raises(FleetError, match="not ship-03"):
        ProxmoxDriver(PX, api=api).finish_enrolment(ship(vmid=9101, seed="ship-03-0a1b2c3d"))
    assert mutating(api) == []


def test_destroy_refuses_a_vm_the_token_cannot_see():
    api = FakeAPI(vm_name="403", used={9101})  # 403, and the number is taken: hidden
    with pytest.raises(FleetError, match="not visible to the token"):
        ProxmoxDriver(PX, api=api).destroy(ship(vmid=9101))
    assert mutating(api) == []


def test_destroy_of_a_vm_already_gone():
    api = FakeAPI(vm_name=None)
    assert ProxmoxDriver(PX, api=api).destroy(ship(vmid=9101)).done
    assert [c for c in mutating(api) if "/qemu/" in c[1]] == []


def test_freed_vmid_counts_as_gone():
    """Outside the pool a freed VMID answers 403 too; /cluster/nextid says it is free."""
    api = FakeAPI(vm_name="403")  # 9101 not in use
    driver = ProxmoxDriver(PX, api=api)
    assert driver.destroy(ship(vmid=9101)).done
    assert ("GET", "/cluster/nextid", {"vmid": 9101}) in api.calls
    assert [c for c in mutating(api) if "/qemu/" in c[1]] == []
    with pytest.raises(FleetError, match="no longer exists"):
        driver.start(ship(vmid=9101))


def test_power_actions_check_the_name_first():
    api = FakeAPI()
    driver = ProxmoxDriver(PX, api=api)
    driver.stop(ship(vmid=9101))
    gets = [c[1] for c in api.calls if c[0] == "GET"]
    assert gets[0] == "/nodes/pve/qemu/9101/config"
    assert mutating(api)[-1][:2] == ("POST", "/nodes/pve/qemu/9101/status/shutdown")


# --- phase 4: name checks, images, snapshots ----------------------------------

from pathlib import Path  # noqa: E402

from fleetledger.drivers import ImageFile, NotSupported, PublishedImage  # noqa: E402
from fleetledger.drivers.proxmox import NotOurs  # noqa: E402

IMG = "26.04-20261102-3f9a1c2e"


def test_destroy_refuses_a_vm_with_another_name():
    """A stale vmid must never destroy someone else's VM: the name decides."""
    api = FakeAPI(vms={9101: ("prod-db", False)})
    with pytest.raises(NotOurs, match="prod-db"):
        ProxmoxDriver(PX, api=api).destroy(ship(vmid=9101, seed="ship-03-0a1b2c3d"))
    assert mutating(api) == []


def test_destroy_refuses_a_template_named_like_the_ship():
    api = FakeAPI(vms={9101: ("ship-03", True)})
    with pytest.raises(NotOurs):
        ProxmoxDriver(PX, api=api).destroy(ship(vmid=9101))
    assert mutating(api) == []


def test_destroy_of_a_vm_already_gone_still_deletes_the_seed():
    api = FakeAPI(vms={9101: None})
    outcome = ProxmoxDriver(PX, api=api).destroy(ship(vmid=9101, seed="ship-03-0a1b2c3d"))
    assert outcome.done and "already gone" in outcome.messages[0]
    assert [c[:2] for c in mutating(api)] == [
        ("DELETE", "/nodes/pve/storage/shipshape-seeds/content/"
                   "shipshape-seeds:iso/ship-03-0a1b2c3d.iso"),
    ]


@pytest.mark.parametrize("verb", ["start", "stop", "reboot"])
def test_power_actions_check_the_name(verb):
    api = FakeAPI(vms={9101: ("other", False)})
    with pytest.raises(NotOurs):
        getattr(ProxmoxDriver(PX, api=api), verb)(ship(vmid=9101))
    assert mutating(api) == []


def test_create_from_a_published_image_clones_its_template():
    api = FakeAPI()
    driver = ProxmoxDriver(PX, api=api, read_seed=lambda s: b"ISO")
    image = PublishedImage(IMG, {"template_vmid": 9181})
    outcome = driver.create(ship(vmid=9101), SEED, SIZE, image)
    clone = mutating(api)[1]
    assert clone[:2] == ("POST", "/nodes/pve/qemu/9181/clone")
    assert clone[2]["newid"] == 9101 and clone[2]["full"] is True
    assert any(IMG in m for m in outcome.messages)


def test_snapshot_take_list_delete():
    api = FakeAPI(snapshots=[
        {"name": "before-upgrade", "snaptime": 1793000000, "description": "x\n"},
        {"name": "first", "snaptime": 1792000000, "description": ""},
    ])
    driver = ProxmoxDriver(PX, api=api)
    driver.snapshot(ship(vmid=9101), "before-upgrade", "note")
    assert mutating(api)[0] == ("POST", "/nodes/pve/qemu/9101/snapshot",
                                {"snapname": "before-upgrade", "description": "note",
                                 "vmstate": False})
    snaps = driver.snapshots(ship(vmid=9101))
    assert [s.name for s in snaps] == ["first", "before-upgrade"]  # oldest first, no "current"
    assert snaps[0].created == "2026-10-14T17:46:40Z" and snaps[1].description == "x"
    driver.delete_snapshot(ship(vmid=9101), "first")
    assert mutating(api)[-1][:2] == ("DELETE", "/nodes/pve/qemu/9101/snapshot/first")


def test_snapshot_never_saves_memory():
    """vmstate would put the Ship's RAM (sign-ins in use) on the host's storage."""
    api = FakeAPI()
    ProxmoxDriver(PX, api=api).snapshot(ship(vmid=9101), "s1")
    assert mutating(api)[0][2]["vmstate"] is False


@pytest.mark.parametrize("name", ["1st", "a", "current", "has space", "x" * 41, "../etc"])
def test_snapshot_names_are_checked(name):
    api = FakeAPI()
    with pytest.raises(FleetError, match="snapshot name"):
        ProxmoxDriver(PX, api=api).snapshot(ship(vmid=9101), name)
    assert api.calls == []


def test_rollback_shuts_down_cleanly_first():
    api = FakeAPI(vm_status="running", snapshots=[{"name": "s1", "snaptime": 1}])
    outcome = ProxmoxDriver(PX, api=api).rollback(ship(vmid=9101), "s1")
    assert [c[:2] for c in mutating(api)] == [
        ("POST", "/nodes/pve/qemu/9101/status/shutdown"),
        ("POST", "/nodes/pve/qemu/9101/snapshot/s1/rollback"),
    ]
    assert mutating(api)[1][2] == {"start": True}
    assert outcome.done


def test_rollback_to_an_unknown_snapshot_changes_nothing():
    api = FakeAPI(snapshots=[{"name": "s1", "snaptime": 1}])
    with pytest.raises(FleetError, match="no snapshot named s2"):
        ProxmoxDriver(PX, api=api).rollback(ship(vmid=9101), "s2")
    assert mutating(api) == []


def test_snapshots_of_another_vm_are_refused():
    api = FakeAPI(vms={9101: ("other", False)})
    for call in (lambda d: d.snapshot(ship(vmid=9101), "s1"),
                 lambda d: d.rollback(ship(vmid=9101), "s1"),
                 lambda d: d.delete_snapshot(ship(vmid=9101), "s1")):
        with pytest.raises(NotOurs):
            call(ProxmoxDriver(PX, api=api))
    assert mutating(api) == []


def image_file(tmp_path):
    path = tmp_path / f"{IMG}.qcow2"
    path.write_bytes(b"QCOW")
    return ImageFile(IMG, path, "ab" * 32, 4)


def test_publish_image(tmp_path):
    api = FakeAPI(used={9100})
    driver = ProxmoxDriver(dict(PX, image_storage="shipshape-images"), api=api)
    outcome = driver.publish_image(image_file(tmp_path))
    assert outcome.done and outcome.driver_ref == {"template_vmid": 9101}
    steps = mutating(api)
    assert steps[0][:2] == ("UPLOAD", "/nodes/pve/storage/shipshape-images/upload")
    assert steps[0][2]["name"] == f"shipshape-{IMG}.qcow2"
    assert steps[0][2]["content"] == "import" and steps[0][2]["sha256"] == "ab" * 32
    method, path, params = steps[1]
    assert (method, path) == ("POST", "/nodes/pve/qemu")
    assert params["vmid"] == 9101 and params["pool"] == "shipshape"
    assert params["name"] == f"shipshape-img-{IMG}"
    volume = f"shipshape-images:import/shipshape-{IMG}.qcow2"
    assert params["scsi0"] == f"local-lvm:0,import-from={volume}"
    assert params["net0"] == "virtio,bridge=vmbr0" and params["agent"] == "enabled=1"
    # No serial console: that would need VM.Config.HWType, which the token lacks.
    assert "serial0" not in params and "vga" not in params
    assert steps[2][:2] == ("POST", "/nodes/pve/qemu/9101/template")
    assert steps[3][:2] == ("DELETE", "/nodes/pve/storage/shipshape-images/content/"
                                      f"shipshape-images:import/shipshape-{IMG}.qcow2")


def test_publish_image_cleans_up_on_failure(tmp_path):
    api = FakeAPI(fail_on="/template", vms={9100: (f"shipshape-img-{IMG}", False)})
    driver = ProxmoxDriver(dict(PX, image_storage="shipshape-images"), api=api)
    with pytest.raises(ProxmoxError):
        driver.publish_image(image_file(tmp_path))
    tail = [c[:2] for c in mutating(api)[-2:]]
    assert tail == [("DELETE", "/nodes/pve/qemu/9100"),
                    ("DELETE", "/nodes/pve/storage/shipshape-images/content/"
                               f"shipshape-images:import/shipshape-{IMG}.qcow2")]


def test_publish_image_needs_an_image_storage(tmp_path):
    api = FakeAPI()
    with pytest.raises(ConfigError, match="image_storage"):
        ProxmoxDriver(PX, api=api).publish_image(image_file(tmp_path))
    assert mutating(api) == []


def test_remove_image_checks_name_and_template():
    api = FakeAPI(vm_status="stopped", vms={9181: (f"shipshape-img-{IMG}", True)})
    ProxmoxDriver(PX, api=api).remove_image(PublishedImage(IMG, {"template_vmid": 9181}))
    assert [c[:2] for c in mutating(api)] == [("DELETE", "/nodes/pve/qemu/9181")]
    for vms in ({9181: (f"shipshape-img-{IMG}", False)}, {9181: ("ship-03", True)}):
        api = FakeAPI(vm_status="stopped", vms=vms)
        with pytest.raises(NotOurs):
            ProxmoxDriver(PX, api=api).remove_image(PublishedImage(IMG, {"template_vmid": 9181}))
        assert mutating(api) == []


def test_manual_driver_images_and_snapshots(tmp_path):
    driver = ManualDriver(PX)
    seed = SeedFile(path=tmp_path / "s.iso", instance_id="ship-03-0a1b2c3d")
    text = "\n".join(driver.create(ship(vmid=9105), seed, SIZE,
                                   PublishedImage(IMG, {"template_vmid": 9181})).instructions)
    assert "qm clone 9181 9105" in text
    published = "\n".join(driver.publish_image(image_file(tmp_path)).instructions)
    assert "qm template \"$VMID\"" in published and "sha256sum -c" in published
    assert "--template-vmid" in published
    assert driver.snapshot(ship(vmid=9105), "s1").instructions == ["qm snapshot 9105 s1"]
    assert "qm rollback 9105 s1 --start 1" in driver.rollback(ship(vmid=9105), "s1").instructions
    generic = ManualDriver(None)
    assert IMG in generic.create(ship(), seed, SIZE,
                                 PublishedImage(IMG, {"template_vmid": 1})).instructions[0]


def test_drivers_without_snapshots_say_so():
    from fleetledger.drivers.base import Driver

    class Bare(Driver):
        id = "bare"
        create = start = stop = reboot = destroy = status = finish_enrolment = None

    bare = Bare()
    for call in (lambda: bare.snapshot(ship(), "s1"), lambda: bare.snapshots(ship()),
                 lambda: bare.publish_image(ImageFile(IMG, Path("x"), "0" * 64, 1))):
        with pytest.raises(NotSupported):
            call()
