<h1 align="center">ShipShape</h1>

<p align="center"><em>Virtual machines for coding agents ("Ships"), and a tool to run a fleet of them ("FleetLedger").</em></p>

> **Status:** Ships boot, provision themselves and resume their agents;
> FleetLedger creates, signs in, updates and watches them, from the terminal or
> a local web UI. The previous Docker-based design's code is kept by the tag
> `container-stack-final`, and its last state in this repo (the `legacy/`
> folder) by commit `cdeecd7`; its notes are in `docs/archive/`.

## What it is

- **Ships** are Ubuntu VMs, created from Ubuntu's own cloud image plus a small
  seed. They boot herdr, resume their Claude Code agents on their own after a
  reboot, and can make every Claude session available through Remote Control.
- **FleetLedger** (`ship-fleet`) runs on your computer. It creates Ships, shows
  their state, and signs them in to Claude Code, gcloud and GitHub by relaying
  each tool's no-browser sign-in, so tokens never leave the Ship.

See `docs/architecture.md` and `docs/security.md`.

## Quick start

New here? [Set up your own fleet](docs/guide/your-own-fleet.md) walks through
every step. In short:

```sh
fleetledger/install.sh                 # installs ship-fleet with pipx
ship-fleet setup                       # opens the setup page in your browser
```

The setup page (on `127.0.0.1`, this computer only) first asks whether this is
a **new fleet**, where FleetLedger makes and looks after every key it needs, or
whether to **connect to keys you already have**. Then it asks only for what you
alone can give: where Ships run (Proxmox, or manual), which of your SSH keys may
log in to Ships, optionally a Tailscale OAuth client, and a first Ship. Each step
shows done or next; stop whenever you like and come back. No browser on that
computer? `ship-fleet setup --text` asks the same in the terminal. Then, day to day:

```sh
ship-fleet new ship-02 --tag work      # seed, VM, provision, enrol
ship-fleet login ship-02 all           # Claude Code, gcloud + ADC, GitHub
ship-fleet ls
ship-fleet ui                          # the same in your browser (this computer only)
ship-fleet doctor                      # what is set up, what is missing, the next command
```

Runbooks: [set up Proxmox](docs/runbooks/proxmox-setup.md) (once),
[create a Ship](docs/runbooks/create-ship.md),
[baked images](docs/runbooks/images.md),
[access from outside the LAN](docs/runbooks/remote-access.md),
[the web UI from your laptop and phone](docs/runbooks/remote-ui.md) (Tailscale Serve),
[sign it in](docs/runbooks/sign-in.md),
[attach from other machines](docs/runbooks/trust-machines.md) (SSH certificates, hc; herdr --remote for classic Ships),
[update the fleet](docs/runbooks/update-fleet.md) (and switch running agents to a new
permission mode: `ship-fleet agent restart`),
[move a Ship between herdr and herdr-core](docs/runbooks/switch-runtime.md) (`ship-fleet
runtime`, with an automatic rollback),
[send files to its agents](docs/runbooks/send-files.md) (`ship-fleet put`, or drop them on
the web UI's Inbox tab),
[the web UI](docs/web-ui.md),
[run it as a service](docs/runbooks/fleetledger-service.md) (always on, no terminal:
`ship-fleet service install`),
[FleetLedger on its own VM](docs/runbooks/fleetledger-vm.md) (`ship-fleet appliance
seed`, encrypted backups, `self-update`),
[move FleetLedger to another computer](docs/runbooks/move-fleetledger.md) (`ship-fleet
migrate`: the Ships keep working, nothing adopted again),
[the vault](docs/vault.md) (every secret in one encrypted file, unlocked once per boot:
`ship-fleet vault init`, `ship-fleet unlock`),
[retire the old stack](docs/runbooks/retire-old-stack.md).

## Layout

| Path | What it is |
|---|---|
| `ship/` | everything that runs on a Ship |
| `fleetledger/` | the `ship-fleet` manager (Python) |
| `contracts/` | schemas, examples and docs both halves build against |
| `versions.toml` | pinned versions, checksums and signing keys |
| `scripts/check` | lint, tests and the public-repo hygiene check (what CI runs) |
| `docs/` | the first-fleet guide, web UI, architecture, security, network, runbooks, build tracks, hygiene, design and archived notes |
| `tests/e2e/` | end-to-end tests on a real hypervisor pool, and the guided acceptance run |

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
scripts/check
```

`scripts/check` needs `shellcheck` on your PATH. It is exactly what CI runs.

Before contributing, read `docs/tracks.md` (who owns which directory) and
`docs/hygiene.md` (nothing personal may be committed: this is a public repo).
