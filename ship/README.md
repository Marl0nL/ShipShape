# ship/: everything that runs on a Ship

Owned by the **Ship track** (W4 to W7). Everything here is packed into the
tooling bundle that seeds and updates carry to each Ship (`contracts/seed.md`),
except `tests/`.

| Folder | Work item | What goes here |
|---|---|---|
| `provision/` | W4 | `ship-provision`, the idempotent setup script |
| `files/` | W4 | systemd units, drop-ins, the resume shim, the mode-ledger hook |
| `bin/` | W5 | `ship-ctl` |
| `tools/` | W6 | catalogue recipes (`<id>.toml`) |
| `profiles/` | W6 | profiles (`<id>.toml`) and their setup scripts |
| `axi/` | W7 | `ship-axi` |
| `skills/ship/` | W7 | the `ship` Claude skill |
| `tests/` | all | Ship-track unit tests (not bundled) |

The Ship track builds against `contracts/` and never edits `fleetledger/`.
Contract changes go in their own PR (`contracts/README.md`).
