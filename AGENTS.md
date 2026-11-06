# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Checks: `PYTHON=.venv/bin/python scripts/check` is exactly what CI runs (shellcheck, ruff, pytest, npm test, public-repo hygiene). The repo is public: read `docs/hygiene.md`; only placeholder names, `example.com`/`example.invalid` and documentation address ranges in code, docs, tests and screenshots.
- Ship code (`ship/`) is tested in a sandbox root with fake `herdr`, `claude`, `gh`, ... (`ship/tests/ship_harness.py`, `ship/tests/fakes/fake.py`); never drive a real herdr from tests. FleetLedger reaches Ships only through `ship-ctl` (`contracts/ship-ctl.md`); schema changes go in both `contracts/` and their copies in `ship/schemas/` (a test keeps them identical).
- UI screenshots: `.venv/bin/python fleetledger/tests/demo_ui.py --port N --home <empty dir>` serves the web UI against made-up Ships in a throwaway FleetLedger home (unbuffered stdout prints the sign-in link; `--remote` adds the remote web UI and made-up paired devices); add new `ship-ctl` verbs to its `make_ssh` when a page needs them. Its sign-ins play the recorded sessions in `fleetledger/tests/fixtures/concierge/` (`demo_spawner`), so one started there waits for you like a real one. PRs embed screenshots from `pr-screenshots/` by raw commit-SHA URL, added in one commit and removed in the next so they never reach `main`.
- New FleetLedger state: a file written under FleetLedger's config or data folders must be taught to `ship-fleet migrate` (`_plan_config`/`_plan_data` in `fleetledger/fleetledger/migrate.py`: move it, or leave it with a reason; `handover.wipe_plan` for what a wipe deletes), or every move stops on it as an unknown file. Tests: the two-computer fixtures in `fleetledger/tests/test_migrate.py`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
