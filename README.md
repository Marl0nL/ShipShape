<h1 align="center">ShipShape</h1>

<p align="center"><em>Virtual machines for coding agents ("Ships"), and a tool to run a fleet of them ("FleetLedger").</em></p>

> **Status: rebuild in progress.** The repo is moving from a locked-down Docker
> sandbox to VM-based Ships. This commit lays down the layout, the contracts both
> halves build against, and the checks. The previous design is in `legacy/` and
> `docs/archive/`.

## What it is

- **Ships** are Ubuntu VMs, created from Ubuntu's own cloud image plus a small
  seed. They boot herdr, resume their Claude Code agents on their own after a
  reboot, and can make every Claude session available through Remote Control.
- **FleetLedger** (`ship-fleet`) runs on your computer. It creates Ships, shows
  their state, and signs them in to Claude Code, gcloud and GitHub by relaying
  each tool's no-browser sign-in, so tokens never leave the Ship.

See `docs/architecture.md`.

## Layout

| Path | What it is |
|---|---|
| `ship/` | everything that runs on a Ship |
| `fleetledger/` | the `ship-fleet` manager (Python) |
| `contracts/` | schemas, examples and docs both halves build against |
| `versions.toml` | pinned versions, checksums and signing keys |
| `scripts/check` | lint, tests and the public-repo hygiene check (what CI runs) |
| `docs/` | architecture, build tracks, hygiene, archived design notes |
| `legacy/` | the previous container stack (frozen, to be removed) |

## Development

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
scripts/check
```

`scripts/check` needs `shellcheck` on your PATH. It is exactly what CI runs.

Before contributing, read `docs/tracks.md` (who owns which directory) and
`docs/hygiene.md` (nothing personal may be committed: this is a public repo).
