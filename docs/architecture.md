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

## How the two parts work together

```
your computer                                   Proxmox (or any hypervisor)
+---------------------------+                   +-------------------------------+
| ship-fleet (FleetLedger)  |  seed ISO + API   |  template VM (Ubuntu image)   |
|  ships.toml: names, tags, | ----------------> |  Ship = clone + seed CD-ROM   |
|  pinned host keys         |                   +-------------------------------+
|  seeds, concierge, status |        SSH (pinned host key, admin key as fleet)
|                           | <==============================================> Ship
+---------------------------+                    sudo ship-ctl status|set|logs|...
                                                 sudo -iu agent <sign-in tool>
```

- **Creating a Ship.** `ship-fleet new` makes the host key, renders the Ship's
  `ship.toml` from the inventory, packs `ship/` from the checkout into a tooling
  bundle, and writes all of it into a seed ISO (`contracts/seed.md`). A driver
  boots a VM from the base image with the seed attached. On first boot
  cloud-init installs the host key and users, unpacks the bundle and runs
  `ship-provision`.
- **Enrolment.** FleetLedger connects with the key it pinned, waits for
  provisioning to finish, then ejects and deletes the seed and records the Ship
  as enrolled.
- **Status.** `ship-ctl status --json` returns one document
  (`contracts/ship-status.schema.json`). FleetLedger checks it is the Ship it
  expects, adds what only it knows (wrong-account sign-ins, Ships behind on
  tooling), and shows it in `ship-fleet ls` and `status`.
- **Sign-in.** The login concierge runs each tool's own no-browser sign-in on the
  Ship in a terminal over SSH, relays the link and the code, and checks the
  account against the Ship's tags. The Ship notices the sign-in by itself and
  starts its autostart agents.
- **Changes.** `ship-fleet set`, `tag` and `tools` call `ship-ctl` on the Ship,
  which validates and applies them; FleetLedger keeps the inventory in step.

Ships never depend on FleetLedger to keep running: a Ship boots, resumes its
agents and serves Remote Control with FleetLedger switched off.

More: [security.md](security.md), and the runbooks in [runbooks/](runbooks/):
[setting up Proxmox](runbooks/proxmox-setup.md),
[creating a Ship](runbooks/create-ship.md),
[signing in](runbooks/sign-in.md),
[retiring the old stack](runbooks/retire-old-stack.md).

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
| `docs/security.md`, `docs/runbooks/` | security model; how to set up, create, sign in and retire |
| `tests/e2e/` | end-to-end tests on a real hypervisor pool, and the guided acceptance run |
| `legacy/`, `docs/archive/` | the previous container-based design |
