# ship/tests

Unit tests for the Ship track (pytest; `pytest.ini` already lists this folder).
Test modules are named `test_ship_*.py`: pytest imports test modules by basename,
so they must not clash with `fleetledger/tests/`.
Excluded from the tooling bundle.

`conftest.py` builds a sandbox Ship per test: a temporary root with `ship.toml`,
the agent's home and a fake `/proc`, plus `fakes/fake.py` installed as every
command ship-ctl runs (`herdr`, `claude`, `gh`, `gcloud`, `apt-get`, `npm`, ...).
A test sets the fakes' behaviour with `ship.set(...)` and reads what they were
asked to do with `ship.calls(...)`. Every JSON document is checked against the
contract schemas with `jsonschema`.

The fake herdr keeps panes as well as agents, refuses to start an agent in a pane
that is missing or busy, and runs the real mode-ledger hook for each launch
(SessionStart) and `/exit` (SessionEnd). `ship.reboot()` is a new boot as ship-ctl
sees it (herdr back with every pane restored empty under its old id),
`ship.crash(name)` kills an agent's claude, and `ship.exit(name)` exits it.
`test_ship_record.py` covers the agent record, resume, the crash policy and
moving a Ship from older tooling onto the record.
