"""Live check of the Proxmox test helper (W8): create a Ship, reach it, destroy it.

Skipped unless FLEETLEDGER_LIVE_PROXMOX names a ships.toml with a
[drivers.proxmox] section (the scoped token from the plan's "Access needed").
Nothing else is taken from that file: keys are throwaway, made by the test.
It never runs in CI.

What it does, with the token alone:
  seed (stub ship-provision that installs the guest agent) -> upload ISO ->
  full clone into the pool -> attach seed, size, start -> guest agent address ->
  SSH as fleet with the pinned host key -> enrol (detach + delete the ISO) ->
  an impostor key is refused -> destroy -> nothing of ours left in the pool.
"""

from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

import pytest
from fleetledger.drivers.base import SeedFile
from fleetledger.drivers.proxmox import ProxmoxDriver, seed_volume
from fleetledger.enrol import enrol
from fleetledger.inventory import Ship, new_ship_id, now_timestamp
from fleetledger.seed import make_seed
from fleetledger.ssh import SSH, HostKeyMismatch
from fleetledger.store import ship_entry

LIVE = os.environ.get("FLEETLEDGER_LIVE_PROXMOX")
pytestmark = pytest.mark.skipif(not LIVE, reason="set FLEETLEDGER_LIVE_PROXMOX to run")

STUB_PROVISION = """#!/usr/bin/env bash
# Test stub for ship-provision: just enough for FleetLedger to find the VM.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q qemu-guest-agent
systemctl start qemu-guest-agent
"""


def test_create_reach_enrol_destroy(store, fake_repo, home):
    px = tomllib.loads(Path(LIVE).expanduser().read_text())["drivers"]["proxmox"]
    with store.edit() as ed:
        ed.doc["drivers"] = {"proxmox": px}
        ed.doc["defaults"]["tools"] = []
    (fake_repo.ship_dir / "provision" / "ship-provision").write_text(STUB_PROVISION)

    name = f"ship-live-{os.getpid() % 10000}"
    draft = Ship(name=name, id=new_ship_id(), driver="proxmox", names=(f"{name}.local",),
                 host_key="ssh-ed25519 AAAA", tags=("work",), created=now_timestamp())
    seed = make_seed(store.load(), draft, fake_repo)
    driver = ProxmoxDriver(px)
    outcome = driver.create(draft, SeedFile(seed.path, seed.instance_id),
                            {"cores": 2, "memory_mb": 4096, "disk_gb": 20})
    vmid = outcome.driver_ref["vmid"]
    ship = Ship(**{**draft.__dict__, "host_key": seed.host_public_key,
                   "driver_ref": dict(outcome.driver_ref)})
    with store.edit() as ed:
        ed.add_ship(name, ship_entry(ship))
    try:
        address = None
        for _ in range(90):  # up to 15 minutes for boot + apt
            addresses = driver.status(ship).addresses
            if addresses:
                address = addresses[0]
                break
            time.sleep(10)
        assert address, "no address from the guest agent"
        with store.edit() as ed:
            ed.set_field(name, "names", [address])
        ship = store.load().ship(name)

        result = enrol(store, SSH(store.load()), driver, ship, timeout=900, interval=10)
        assert result.view.provision_result == "ok"
        assert store.load().ship(name).enrolled
        assert not seed.path.exists()
        contents = driver.api.get(f"/nodes/{px['node']}/storage/{px['seed_storage']}/content")
        assert seed_volume(px["seed_storage"], seed.instance_id) not in {
            c["volid"] for c in contents
        }

        # Pin some other key: the real Ship must now be refused.
        other = home / "keys" / "you.pub"
        with store.edit() as ed:
            ed.set_field(name, "host_key", other.read_text().strip())
        with pytest.raises(HostKeyMismatch):
            SSH(store.load()).run(store.load().ship(name), ["true"])
    finally:
        driver.destroy(Ship(**{**ship.__dict__, "driver_ref": {"vmid": vmid,
                                                               "seed": seed.instance_id}}))
    members = driver.api.get(f"/pools/{px['pool']}").get("members", [])
    assert vmid not in {m.get("vmid") for m in members}
