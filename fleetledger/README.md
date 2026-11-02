# fleetledger/: the manager (`ship-fleet`)

Owned by the **Fleet track** (W3, W8, W9, W10). A Python package installed with
pipx; the command is `ship-fleet`. It runs on your own computer and reaches Ships
only over SSH, as `contracts/ship-runtime.md` describes.

## Modules

The seed generator, inventory and catalogue reader are built; the rows from
`ssh.py` down land in follow-up PRs.

| Module | Work item | Purpose |
|---|---|---|
| `fleetledger/seed/` | W3 | host keys, `ship.toml` rendering, the cloud-init files, bundle packing, seed ISOs (`contracts/seed.md`) |
| `fleetledger/inventory.py`, `store.py` | W8 | `ships.toml` (`contracts/ships.toml.schema.json`), tags, expected accounts |
| `fleetledger/catalogue.py` | W3, W10 | reads recipes and profiles from `ship/tools/` and `ship/profiles/` (`contracts/tools.md`) |
| `fleetledger/repo.py`, `schemas.py`, `paths.py` | all | the checkout FleetLedger runs from, contract validation, local folders |
| `fleetledger/ssh.py` | W8 | system OpenSSH, FleetLedger's own known-hosts file, connection reuse |
| `fleetledger/status.py` | W8 | polling `ship-ctl status --json` (`contracts/ship-status.schema.json`) |
| `fleetledger/drivers/` | W8 | the hypervisor driver interface; `manual`, and `proxmox` as a test helper (finished in phase 4) |
| `fleetledger/concierge/` | W9 | sign-in engine and `providers/*.toml` |
| `fleetledger/cli.py` | W10 | the `ship-fleet` commands |

Tests live in `fleetledger/tests/` (pytest; `pytest.ini` lists the folder). They
use throwaway keys, a temporary FleetLedger home and a small fake checkout; they
never touch your real config, keys or network.

## Files on your computer

Nothing personal lives in the repo (`docs/hygiene.md`). FleetLedger keeps:

| Path | What it is |
|---|---|
| `~/.config/fleetledger/ships.toml` | the inventory: Ships, pinned host keys, tags, expected accounts (mode 0600) |
| `~/.config/fleetledger/known_hosts` | FleetLedger's own known-hosts file, written from the inventory |
| `~/.local/share/fleetledger/seeds/` | seed ISOs (folder 0700, files 0600) until each Ship enrols |

`FLEETLEDGER_CONFIG_DIR`, `FLEETLEDGER_DATA_DIR` and `FLEETLEDGER_RUNTIME_DIR`
override these (the XDG variables are honoured too). `SHIPSHAPE_REPO` points
FleetLedger at a different checkout.

## The inventory

`ships.toml` is the single source of truth; the schema and a full example are in
`contracts/`. FleetLedger edits it with `tomlkit`, so your comments and layout
survive. Every change takes a lock, is validated against the schema plus a few
rules the schema cannot express (no two Ships share a name, id or host key), and
is written atomically. An invalid change leaves the file untouched.

Expected accounts come from the Ship's tags (`[tag.<tag>]`). `gcloud_adc`
follows `gcloud`. Account names compare case-insensitively. If two of a Ship's
tags name different accounts for one service, every account is reported as wrong
until you fix the tags.

## Seeds (W3)

`fleetledger.seed.make_seed(inventory, ship, repo)` writes one ISO per seed:

1. **ship.toml** from the inventory, exactly as `contracts/settings.md` maps it:
   tags fall back to `defaults.tags`; `claude` is `defaults.claude` overlaid with
   the Ship's own, with `session_prefix` defaulting to the Ship's name; tools are
   the Ship's, else `defaults.tools`, else the catalogue's `default = true`
   recipes; profile options are copied from `[profile_options]` and checked
   against the profile (a missing required option stops). The result is
   validated against the schema before anything is written.
2. **The tooling bundle**: `ship/` from this checkout (tracked and untracked but
   not ignored files; never `ship/tests/`, `node_modules/`, caches or symlinks),
   plus `bundle.json` and `versions.toml`. It records the commit and whether
   `ship/` or `versions.toml` had uncommitted changes. Entries are sorted, owned
   by root and timestamped from `created_at`, so packing is reproducible.
   `ship/provision/ship-provision` and `ship/bin/ship-ctl` must exist and be
   executable; the bundle must stay under 5 MiB.
3. **A new ed25519 host key** from `ssh-keygen`, made in a private temporary
   folder that is removed straight away. The private half exists only inside the
   ISO; the caller pins the public half in `ships.toml`.
4. **cloud-init files**: `user-data` with exactly the contract's top-level keys
   and the contract's bootstrap script, `meta-data` with a fresh
   `<name>-<8 hex>` instance id, and DHCP `network-config`. Public keys come
   from `fleet.admin_key` + `.pub` and `fleet.agent_keys`; a file that holds a
   private key is refused.
5. **The ISO**: ISO 9660 + Joliet + Rock Ridge, label `CIDATA`, built with
   pycdlib (no host tools), written with mode 0600 as
   `seeds/<instance id>.iso`.

The tests check each step against the contract text itself (the bootstrap script
and `network-config` are compared with `contracts/seed.md`), run that bootstrap
script on a real bundle in a temporary folder, and read the ISO back.
