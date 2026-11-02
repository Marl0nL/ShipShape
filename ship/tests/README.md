# ship/tests

Unit tests for the Ship track (pytest; `pytest.ini` already lists this folder).
Excluded from the tooling bundle.

`conftest.py` builds a sandbox Ship per test: a temporary root with `ship.toml`,
the agent's home and a fake `/proc`, plus `fakes/fake.py` installed as every
command ship-ctl runs (`herdr`, `claude`, `gh`, `gcloud`, `apt-get`, `npm`, ...).
A test sets the fakes' behaviour with `ship.set(...)` and reads what they were
asked to do with `ship.calls(...)`. Every JSON document is checked against the
contract schemas with `jsonschema`.
