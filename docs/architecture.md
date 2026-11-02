# Architecture

ShipShape has two parts that live in this repo and meet only through `contracts/`.

## Ships

A Ship is an Ubuntu 26.04 LTS virtual machine for running coding agents.

- **Built from** Ubuntu's signed cloud image plus a small cloud-init seed ISO that
  FleetLedger writes for each Ship (`contracts/seed.md`). The seed carries the
  Ship's identity, its pinned SSH host key, its settings (`ship.toml`) and the
  tooling bundle. It carries no service tokens.
- **Users:** `fleet` (admin, for FleetLedger) and `agent` (runs herdr and every
  agent, owns every sign-in; no sudo by default).
- **On boot:** herdr runs as a systemd user service, restores its layout, and
  resumes each Claude Code conversation in the mode it was in. A hidden client
  attaches briefly, because herdr resumes agents only once a client is attached.
- **Settings:** `ship.toml` drives Claude Code's managed settings (Remote
  Control, the default permission mode, whether bypass is allowed), herdr's
  config, and the catalogue tools installed for agents.
- **Interface:** `ship-ctl` reports status as one JSON document and applies
  changes (`contracts/ship-ctl.md`).
- **Network:** there is no network control. SSH is the only way in.

## FleetLedger

FleetLedger is a Python tool, `ship-fleet`, that runs on your own computer.

- **Inventory:** it keeps a local inventory (`ships.toml`) of Ships, tags and
  the accounts each tag expects.
- **Seeds:** it writes seeds and, through hypervisor drivers, creates Ships
  (Proxmox first).
- **Status:** it polls each Ship's status over SSH and shows what needs
  attention.
- **Login concierge:** it runs each tool's no-browser sign-in on the Ship
  (Claude Code, gcloud, GitHub), relays the link and the pasted code, and checks
  the signed-in account against the Ship's tag. Tokens stay on the Ship.

## How the repo is organised

| Path | What it is |
|---|---|
| `ship/` | everything that runs on a Ship (Ship track) |
| `fleetledger/` | the manager (Fleet track) |
| `contracts/` | the interfaces between them: schemas, examples, prose |
| `versions.toml` | pinned versions both tracks use |
| `scripts/check` | every check CI runs |
| `docs/tracks.md` | who owns what while phase 1 is built |
| `docs/hygiene.md` | the public repo rule and its check |
| `legacy/`, `docs/archive/` | the previous container-based design |
