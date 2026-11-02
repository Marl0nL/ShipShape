# fleetledger/: the manager (`ship-fleet`)

Owned by the **Fleet track** (W3, W8, W9, W10). A Python package installed with
pipx; the command is `ship-fleet`. It runs on your own computer and reaches Ships
only over SSH, as `contracts/ship-runtime.md` describes.

Planned modules (the Fleet track may refine the inside freely):

| Module | Work item | Purpose |
|---|---|---|
| `fleetledger/seed/` | W3 | host keys, `ship.toml` rendering, the cloud-init template, bundle packing, seed ISOs (`contracts/seed.md`) |
| `fleetledger/inventory.py`, `store.py` | W8 | `ships.toml` (`contracts/ships.toml.schema.json`), tags, expected accounts |
| `fleetledger/ssh.py` | W8 | system OpenSSH, FleetLedger's own known-hosts file, connection reuse |
| `fleetledger/status.py` | W8 | polling `ship-ctl status --json` (`contracts/ship-status.schema.json`) |
| `fleetledger/drivers/` | W8 | the hypervisor driver interface; `manual`, and `proxmox` as a test helper (finished in phase 4) |
| `fleetledger/concierge/` | W9 | sign-in engine and `providers/*.toml` |
| `fleetledger/cli.py` | W10 | the `ship-fleet` commands |

Tests live in `fleetledger/tests/` (pytest; `pytest.ini` lists the folder).

The Fleet track builds against `contracts/` and never edits `ship/`. It reads
`ship/tools/*.toml` and `ship/profiles/*.toml` (for `ship-fleet tools` and
`--profile`) and packs `ship/` into bundles, but does not change them.
