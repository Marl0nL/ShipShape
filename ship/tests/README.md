# ship/tests

Unit tests for the Ship track (pytest; `pytest.ini` already lists this folder).
Test modules are named `test_ship_*.py`: pytest imports test modules by basename,
so they must not clash with `fleetledger/tests/`.
Excluded from the tooling bundle.

`conftest.py` builds a sandbox Ship per test: a temporary root with `ship.toml`,
the agent's home and a fake `/proc`, plus `fakes/fake.py` installed as every
command ship-ctl runs (`hc`, `herdr`, `claude`, `gh`, `gcloud`, `apt-get`, `npm`, ...).
A test sets the fakes' behaviour with `ship.set(...)` and reads what they were
asked to do with `ship.calls(...)`. Every JSON document is checked against the
contract schemas with `jsonschema`. Every test also runs without the
`HERDR_*`, `HC_*` and `SHIPSHAPE_*` settings of the pane pytest was started in
(`clean_env`), so a sandbox command never reaches a real herdr session.

A sandbox Ship is a herdr-core Ship (`ship.toml` `runtime = "core"`; `write_toml`
adds it to a ship.toml that names no runtime). Its agents are terminals of the
fake `hc`, which keeps them under their address and name, runs the real
mode-ledger hook for each launch (SessionStart) and `/exit` (SessionEnd), and
refuses what the real one refuses (a second terminal by a name, typing into a
blocked agent). `ship.reboot()` is a new boot as ship-ctl sees it (every kept
terminal back under its address without its program until its new shell starts),
`ship.crash(name)` kills an agent's claude, and `ship.exit(name)` exits it;
`ship.agent`, `ship.args`, `ship.prompts` and `ship.set_state` read and set the
agents. `test_ship_record.py` covers the agent record, resume and the crash
policy; `test_ship_core.py` the core runtime itself.

`ship.use_herdr()` makes the Ship a classic herdr Ship, whose agents run in the
fake herdr (panes as well as agents; it refuses to start an agent in a pane that
is missing or busy). Only `test_ship_herdr_legacy.py` (what only classic herdr
does, including moving a Ship from older tooling onto the record) and
`test_ship_runtime_switch.py` (which needs both runtimes) use it.
