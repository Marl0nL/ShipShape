"""Seed generator (W3): everything a new Ship needs on its first boot.

make_seed() builds a Ship's host key, ship.toml, tooling bundle and cloud-init
files, and writes them as one seed ISO (contracts/seed.md). The private host
key exists only inside that ISO; FleetLedger keeps the public half in
ships.toml and the ISO itself at mode 0600 until the Ship has enrolled.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fleetledger import paths
from fleetledger.catalogue import Catalogue
from fleetledger.inventory import Inventory, Ship
from fleetledger.repo import Repo
from fleetledger.seed import bundle as bundle_mod
from fleetledger.seed import cloudinit, hostkey, iso, shiptoml
from fleetledger.seed.shiptoml import Autostart, SeedOptions

__all__ = ["Autostart", "Seed", "SeedOptions", "make_seed"]

@dataclass(frozen=True)
class Seed:
    path: Path
    instance_id: str
    host_public_key: str
    host_fingerprint: str
    ship_toml: str
    bundle_release: str



def make_seed(
    inventory: Inventory,
    ship: Ship,
    repo: Repo,
    options: SeedOptions | None = None,
    *,
    out_dir: Path | None = None,
) -> Seed:
    """Build and write a seed ISO for a Ship, with a new host key.

    The caller pins the returned public half in ships.toml; the private half
    exists only inside the ISO.
    """
    catalogue = Catalogue.from_repo(repo)
    doc = shiptoml.build(inventory, ship, catalogue, options, repo.contracts)
    ship_toml = shiptoml.render(doc)

    admin_keys = cloudinit.read_public_keys(
        inventory.admin_public_key_path, "the fleet admin public key"
    )
    agent_keys: list[str] = []
    for path in inventory.agent_key_paths:
        agent_keys += cloudinit.read_public_keys(path, "an agent key")

    tooling = bundle_mod.pack(repo)
    key = hostkey.generate(ship.name)
    instance_id = cloudinit.new_instance_id(ship.name)
    files = {
        "user-data": cloudinit.user_data(
            ship.name, key, admin_keys, agent_keys, ship_toml, tooling
        ),
        "meta-data": cloudinit.meta_data(ship.name, instance_id),
        "network-config": cloudinit.network_config(),
    }
    folder = paths.private_dir(out_dir or paths.seeds_dir())
    path = folder / f"{instance_id}.iso"
    iso.write(files, path)
    return Seed(
        path=path,
        instance_id=instance_id,
        host_public_key=key.public,
        host_fingerprint=key.fingerprint,
        ship_toml=ship_toml,
        bundle_release=tooling.release,
    )
