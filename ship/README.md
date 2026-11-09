# ship/: everything that runs on a Ship

Owned by the **Ship track** (W4 to W7). Everything here is packed into the
tooling bundle that seeds and updates carry to each Ship (`contracts/seed.md`),
except `tests/`.

| Folder | Work item | What goes here |
|---|---|---|
| `provision/` | W4 | `ship-provision`, the idempotent setup script |
| `files/` | W4, W5 | systemd units, drop-ins, the mode-ledger hook (`hooks/mode_ledger.py`) |
| `bin/` | W5 | `ship-ctl` (entry point) |
| `lib/shipctl/` | W5 | the `ship-ctl` code (standard library only) |
| `schemas/` | W5 | copies of the contract schemas ship-ctl validates against (bundles hold only `ship/`; a test keeps them identical) |
| `tools/` | W6 | catalogue recipes (`<id>.toml`) |
| `profiles/` | W6 | profiles (`<id>.toml`) and their setup scripts |
| `axi/` | W7 | `ship-axi` |
| `skills/ship/` | W7 | the `ship` Claude skill |
| `image/` | phase 4 | `bake`: makes a baked base image by running `ship-provision --bake` inside a copy of Ubuntu's cloud image (`contracts/image.md`); not used on Ships |
| `tests/` | all | Ship-track unit tests (not bundled) |

The Ship track builds against `contracts/` and never edits `fleetledger/`.
Contract changes go in their own PR (`contracts/README.md`).
