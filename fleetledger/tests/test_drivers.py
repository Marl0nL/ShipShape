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

    def __init__(self, used=(), fail_on=None, vm_status="running"):
        self.calls = []
        self.used = set(used)
        self.fail_on = fail_on
        self.vm_status = vm_status
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
