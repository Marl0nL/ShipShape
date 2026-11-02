# fleetledger/: the manager (`ship-fleet`)

Owned by the **Fleet track** (W3, W8, W9, W10). A Python package installed with
pipx; the command is `ship-fleet`. It runs on your own computer and reaches Ships
only over SSH, as `contracts/ship-runtime.md` describes.

## Modules

Everything above `concierge/` is built; the concierge and the CLI land in a
follow-up PR.

| Module | Work item | Purpose |
|---|---|---|
| `fleetledger/seed/` | W3 | host keys, `ship.toml` rendering, the cloud-init files, bundle packing, seed ISOs (`contracts/seed.md`) |
| `fleetledger/inventory.py`, `store.py` | W8 | `ships.toml` (`contracts/ships.toml.schema.json`), tags, expected accounts |
| `fleetledger/catalogue.py` | W3, W10 | reads recipes and profiles from `ship/tools/` and `ship/profiles/` (`contracts/tools.md`) |
| `fleetledger/repo.py`, `schemas.py`, `paths.py` | all | the checkout FleetLedger runs from, contract validation, local folders |
| `fleetledger/ssh.py` | W8 | system OpenSSH, FleetLedger's own known-hosts file, connection reuse |
| `fleetledger/status.py`, `shipctl.py` | W8 | polling `ship-ctl status --json` (`contracts/ship-status.schema.json`) and calling the other ship-ctl commands |
| `fleetledger/drivers/` | W8 | the hypervisor driver interface; `manual`, and `proxmox` as a test helper (finished in phase 4) |
| `fleetledger/enrol.py` | W8 | from a booted seed to an enrolled Ship (`contracts/seed.md`) |
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
`contracts/`. FleetLedger edits it with `tomlkit`, so comments and layout
elsewhere in the file survive its edits. A nested section it changes (such as
a Ship's `claude` settings or `driver_ref`) is rewritten as an inline table, and
comments inside that section are lost. Every change takes a lock, is validated against the schema plus a few
rules the schema cannot express (no two Ships share a name, id or host key), and
is written atomically. An invalid change leaves the file untouched.

A Ship without a `tags` entry has `defaults.tags`; `tags = []` means no tags.
Expected accounts come from those tags (`[tag.<tag>]`). `gcloud_adc`
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

   Tools follow `contracts/settings.md` exactly: a list replaces, it does not
   add. A Ship's own `tools` wins outright; without one, `defaults.tools` wins;
   only when neither exists do the catalogue's `default = true` recipes apply.
   So a Ship with `tools = ["tasks-axi"]` gets only `tasks-axi`; list the
   defaults too if you want them.
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

## Reaching Ships (W8)

`ssh.py` runs the system's `ssh`, so your SSH config and agent apply. FleetLedger
adds only options that make each connection safe and quick:

- **Pinned host keys, never learned.** `known_hosts` is rewritten from
  `ships.toml` (one line per Ship, keyed by Ship name), and every connection uses
  `HostKeyAlias=<ship name>`, `StrictHostKeyChecking=yes` and
  `HostKeyAlgorithms=ssh-ed25519`. Whichever of the Ship's names is dialled, its
  key must be the pinned one. A different key raises `HostKeyMismatch` straight
  away; FleetLedger does not try the Ship's other names, because that is a
  security event, not a network one.
- **One key per user.** The fleet admin key (`IdentitiesOnly`) logs in as
  `fleet`. Agent, X11 and port forwarding are always off.
- **Names in order.** Names that cannot be reached are skipped; the first one
  that answers is used.
- **Shared connections.** `ControlMaster` sockets live in the runtime folder and
  persist for 60 seconds. If that folder's path is too long for a Unix socket,
  sharing is turned off rather than failing.

Remote commands follow `contracts/ship-runtime.md`: `sudo -n
/usr/local/bin/ship-ctl ... --json` for ship-ctl, and `sudo -n -iu agent -- bash
-c '<command>'` for anything that must run as the agent.

## Status (W8)

`status.fetch` asks one Ship for `ship-ctl status --json`; `status.poll` asks
many in parallel (8 at a time). FleetLedger only trusts a status document if
three things hold: it validates against the contract, it is a status document
(not a failed result), and its `ship.name` and `ship.id` match the inventory.
Otherwise the Ship is shown with a problem (`unreachable`, `host-key`,
`identity`, `invalid`, `error`) instead of its claims. Before `ship-ctl` is
installed (first boot), `cloud-init status --format json` stands in.

On top of the Ship's own attention items, FleetLedger adds:

| Code | When |
|---|---|
| `signin-wrong-account` | a signed-in service's account is not the one the Ship's tags expect |
| `behind-tooling` | the Ship's tooling is older than `versions.toml` `[shipshape].tooling` |
| `tags-differ` | the Ship's own tags differ from the inventory's |

The last good document per Ship is cached at
`~/.local/share/fleetledger/status/<ship>.json` (0600).

`shipctl.ShipCtl` runs every other ship-ctl command and checks the result
against `ship-ctl-result.schema.json`. Before its first call to a Ship it runs
`ship-ctl version` and refuses any contract major version it does not know.

## Hypervisor drivers (W8)

`drivers/base.py` is the interface: `create`, `start`, `stop`, `reboot`,
`destroy`, `status`, `finish_enrolment`, and optionally
`host_key_fingerprints` and `snapshot`. Every call returns an `Outcome`, which
is either done or a list of instructions for you, so the CLI treats both drivers
alike.

- **manual** prints the steps. With a `[drivers.proxmox]` section in
  `ships.toml` they are exact `qm` and `pvesm` commands for that server;
  otherwise they are generic and work for any hypervisor.
- **proxmox** is the phase 1 test helper; phase 4 finishes it. It uses only the
  scoped API token: it uploads the seed ISO (Proxmox checks a SHA-256 the
  client sends with it), full-clones the template into the pool, attaches the
  seed on `ide2`, sets cores, memory and bridge, grows `scsi0`, starts the VM,
  and waits for every Proxmox task. It reads addresses from the guest agent and
  destroys the VM again. If creation fails part-way, it removes what it made.
  The client pins the SHA-256 fingerprint of the API's TLS certificate and
  checks it before the token is sent. The token is read from its file (which
  must be 0600) for each request and never appears in errors.

## Enrolment (W8)

`enrol.enrol` waits for `provision.result` to be `ok` (retrying while the Ship is
still booting, and stopping at once on a wrong host key). It then has the driver
detach the seed CD-ROM and delete the uploaded ISO, deletes FleetLedger's own
copy of the seed, and records `ship.<name>.enrolled`. With the manual driver the
detach-and-delete steps are printed, and the seed's name stays in `driver_ref`
so `destroy` can remind you of it.

## Live test against Proxmox

`tests/test_live_proxmox.py` runs only when `FLEETLEDGER_LIVE_PROXMOX` names a
`ships.toml` with a `[drivers.proxmox]` section. It creates a Ship from a seed
with a stub `ship-provision`, reaches it over SSH with the pinned key, enrols it,
checks that an impostor key is refused, destroys it, and checks that nothing of
its own is left in the pool. It never runs in CI.

```sh
FLEETLEDGER_LIVE_PROXMOX=~/.config/fleetledger/ships.toml \
  python -m pytest fleetledger/tests/test_live_proxmox.py -v
```

The real-sshd tests in `tests/test_ssh.py` need an `sshd` binary. They use
`sshd` from `PATH`, `/usr/sbin/sshd`, or `FLEETLEDGER_TEST_SSHD`, and are skipped
if none is found.
