# Build tracks: who owns what

Phase 1 is built by two tracks working in parallel. They never edit each other's
directories; everything they share is written down in `contracts/`.

## Ownership

| Path | Owner | Notes |
|---|---|---|
| `ship/**` | Ship track | W4 provision, W5 ship-ctl, W6 catalogue and profiles, W7 ship-axi |
| `fleetledger/**` | Fleet track | W3 seed generator, W8 core, W9 concierge, W10 CLI |
| `contracts/**` | shared | change only in a contract PR (below) |
| `versions.toml` | shared | change only in its own PR |
| `scripts/`, `.github/`, `tests/contracts/`, `tests/scripts/`, `pytest.ini`, `ruff.toml`, `requirements-dev.txt` | shared | tooling; change only in its own PR |
| `tests/e2e/` | both | W11, after the tracks land |
| `docs/` | shared | each track may add its own pages; edit shared pages in their own PR |

## What each track builds against

| Interface | Ship track | Fleet track |
|---|---|---|
| `contracts/seed.md` | consumes the seed and bundle (bootstrap hands over to `ship-provision`) | produces seeds and bundles |
| `contracts/ship.toml.schema.json`, `settings.md` | reads and applies `ship.toml`; `ship-ctl set` rewrites it | writes the first `ship.toml` into each seed |
| `contracts/ship-runtime.md` | makes it true | relies on it (users, sudo, paths, SSH entry points) |
| `contracts/ship-ctl.md` and its two schemas | implements `ship-ctl` | calls `ship-ctl` over SSH |
| `contracts/tool-recipe.schema.json`, `profile.schema.json`, `tools.md` | writes and runs recipes and profiles | reads them to list and validate |
| `contracts/ships.toml.schema.json` | (not used) | owns the inventory |
| `versions.toml` | installs exactly these versions | reads the base image and tooling version |

The Fleet track keeps its internal interfaces (hypervisor drivers, concierge
providers) inside `fleetledger/`; the Ship track never depends on them.

## Working rules

1. Run `scripts/check` before every push; CI runs the same script.
2. Need something the contracts do not cover? Open a contract PR that touches only
   `contracts/` (plus its examples and tests), and wait for it to merge before
   building on it. Do not work around a gap inside your own directory.
3. Keep tests in your own directory (`ship/tests/`, `fleetledger/tests/`).
4. Follow the public repo rule in `docs/hygiene.md` in every file, commit message,
   PR and issue.
