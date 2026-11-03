<h1 align="center">ShipShape</h1>

<p align="center"><em>Virtual machines for coding agents ("Ships"), and a tool to run a fleet of them ("FleetLedger").</em></p>

> **Status: phase 1.** Ships boot, provision themselves and resume their agents;
> FleetLedger creates, enrols, shows and signs them in. The previous Docker-based
> design's code is kept by the tag `container-stack-final`, and its last state
> in this repo (the `legacy/` folder) by commit `cdeecd7`; its notes are in
> `docs/archive/`.

## What it is

- **Ships** are Ubuntu VMs, created from Ubuntu's own cloud image plus a small
  seed. They boot herdr, resume their Claude Code agents on their own after a
  reboot, and can make every Claude session available through Remote Control.
- **FleetLedger** (`ship-fleet`) runs on your computer. It creates Ships, shows
  their state, and signs them in to Claude Code, gcloud and GitHub by relaying
  each tool's no-browser sign-in, so tokens never leave the Ship.

See `docs/architecture.md` and `docs/security.md`.

## Quick start

```sh
fleetledger/install.sh                 # installs ship-fleet with pipx
ship-fleet setup                       # guided: keys, inventory, CA, Proxmox, overlay, a first Ship
```

That is all. `setup` asks a few questions, skips whatever is already done, and
can be run again at any time. For Proxmox it needs only the address and an API
token; anything missing on the host comes back as the exact commands to run
there. Then, day to day:

```sh
ship-fleet new ship-02 --tag work      # seed, VM, provision, enrol
ship-fleet login ship-02 all           # Claude Code, gcloud + ADC, GitHub
ship-fleet ls
ship-fleet ui                          # the same in your browser (this computer only)
ship-fleet doctor                      # what is set up, what is missing, the next command
```

New here? Start with [set up your own fleet](docs/guide/your-own-fleet.md).

Runbooks: [set up Proxmox](docs/runbooks/proxmox-setup.md) (once),
[create a Ship](docs/runbooks/create-ship.md),
[baked images](docs/runbooks/images.md),
[access from outside the LAN](docs/runbooks/remote-access.md),
[sign it in](docs/runbooks/sign-in.md),
[attach from other machines](docs/runbooks/trust-machines.md) (SSH certificates, herdr --remote),
[the web UI](docs/web-ui.md),
[retire the old stack](docs/runbooks/retire-old-stack.md).

## Layout

| Path | What it is |
|---|---|
| `ship/` | everything that runs on a Ship |
| `fleetledger/` | the `ship-fleet` manager (Python) |
| `contracts/` | schemas, examples and docs both halves build against |
| `versions.toml` | pinned versions, checksums and signing keys |
| `scripts/check` | lint, tests and the public-repo hygiene check (what CI runs) |
| `docs/` | architecture, security, runbooks, build tracks, hygiene, archived design notes |
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
