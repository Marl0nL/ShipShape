"""Live checks of the phase 4 Proxmox driver: name checks, snapshots, published images.

Skipped unless FLEETLEDGER_LIVE_PROXMOX names a ships.toml with a [drivers.proxmox]
section (the scoped token; docs/runbooks/proxmox-setup.md). Never runs in CI.

- Name checks and destroy need only the phase 1 rights.
- Snapshots need VM.Snapshot and VM.Snapshot.Rollback (runbook section 4); without
  them the snapshot test is skipped with the permission error Proxmox gave.
- Publishing needs drivers.proxmox.image_storage and FLEETLEDGER_LIVE_IMAGE, the
  path to a baked qcow2 (`ship-fleet image build`).

Every VM is named ss-p45-live4-* (the builder's task id), and each test destroys what it made.
"""

from __future__ import annotations

import hashlib
import os
import time
import tomllib
from pathlib import Path

import pytest
from fleetledger.drivers.base import ImageFile, PublishedImage, SeedFile
from fleetledger.drivers.proxmox import NotOurs, ProxmoxDriver, template_name
from fleetledger.drivers.proxmox_api import ProxmoxError
from fleetledger.inventory import Ship, new_ship_id, now_timestamp
from fleetledger.seed import make_seed

LIVE = os.environ.get("FLEETLEDGER_LIVE_PROXMOX")
pytestmark = pytest.mark.skipif(not LIVE, reason="set FLEETLEDGER_LIVE_PROXMOX to run")
SIZE = {"cores": 2, "memory_mb": 4096, "disk_gb": 20}

# Test stub for ship-provision: the guest agent, so Proxmox can shut the VM down
# cleanly and report its address (a real Ship gets it from ship-provision).
STUB_PROVISION = """#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q qemu-guest-agent
systemctl start qemu-guest-agent
"""


@pytest.fixture
def stub_repo(fake_repo):
    (fake_repo.ship_dir / "provision" / "ship-provision").write_text(STUB_PROVISION)
    return fake_repo


@pytest.fixture
def px():
    return tomllib.loads(Path(LIVE).expanduser().read_text())["drivers"]["proxmox"]


@pytest.fixture
def made(store, stub_repo, px):
    """A Ship VM cloned from the stock template; its seed only installs the guest agent."""
    driver = ProxmoxDriver(px)
    name = f"ss-p45-live4-{os.getpid() % 10000}"
    draft = Ship(name=name, id=new_ship_id(), driver="proxmox", names=(f"{name}.local",),
                 host_key="ssh-ed25519 AAAA", tags=("work",), created=now_timestamp())
    seed = make_seed(store.load(), draft, stub_repo)
    outcome = driver.create(draft, SeedFile(seed.path, seed.instance_id), SIZE)
    ship = Ship(**{**draft.__dict__, "host_key": seed.host_public_key,
                   "driver_ref": dict(outcome.driver_ref)})
    yield driver, ship
    driver.destroy(ship)
    members = driver.api.get(f"/pools/{px['pool']}").get("members", [])
    assert ship.driver_ref["vmid"] not in {m.get("vmid") for m in members}


def test_name_check_refuses_another_vm(made):
    driver, ship = made
    impostor = Ship(**{**ship.__dict__, "name": "not-this-ship"})
    for call in (driver.destroy, driver.stop, driver.reboot):
        with pytest.raises(NotOurs):
            call(impostor)
    assert driver.status(ship).state in ("running", "stopped")


def test_snapshot_take_list_rollback_delete(made):
    driver, ship = made
    try:
        driver.snapshot(ship, "live4-first", "phase 4 live test")
    except ProxmoxError as exc:
        if exc.status == 403:
            pytest.skip(f"the token lacks snapshot rights (runbook section 4): {exc.message}")
        raise
    assert "live4-first" in [s.name for s in driver.snapshots(ship)]
    for _ in range(60):  # a clean shutdown needs the guest agent up: let it boot
        if driver.status(ship).addresses:
            break
        time.sleep(10)
    else:
        pytest.fail("the guest agent never reported an address")
    assert driver.rollback(ship, "live4-first").done
    assert driver.status(ship).state == "running"
    driver.delete_snapshot(ship, "live4-first")
    assert "live4-first" not in [s.name for s in driver.snapshots(ship)]


def test_publish_create_remove_image(store, fake_repo, px):
    path = os.environ.get("FLEETLEDGER_LIVE_IMAGE")
    if not path or not px.get("image_storage"):
        pytest.skip("set FLEETLEDGER_LIVE_IMAGE and drivers.proxmox.image_storage")
    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    image_id = "26.04-20260101-" + digest[:8]
    driver = ProxmoxDriver(px)
    published = driver.publish_image(ImageFile(image_id, path, digest, path.stat().st_size))
    image = PublishedImage(image_id, dict(published.driver_ref))
    try:
        config = driver.api.get(f"/nodes/{px['node']}/qemu/{image.ref['template_vmid']}/config")
        assert config["name"] == template_name(image_id) and config.get("template")
        name = f"ss-p45-live4-img-{os.getpid() % 1000}"
        draft = Ship(name=name, id=new_ship_id(), driver="proxmox", names=(f"{name}.local",),
                     host_key="ssh-ed25519 AAAA", created=now_timestamp())
        seed = make_seed(store.load(), draft, fake_repo)
        outcome = driver.create(draft, SeedFile(seed.path, seed.instance_id), SIZE, image)
        ship = Ship(**{**draft.__dict__, "driver_ref": dict(outcome.driver_ref)})
        driver.destroy(ship)
    finally:
        driver.remove_image(image)
