# ship/bin

`ship-ctl` (W5): the only way FleetLedger inspects or changes a Ship. Installed as
`/usr/local/bin/ship-ctl -> /opt/shipshape/current/bin/ship-ctl`. Its commands,
JSON output and exit codes are fixed by `contracts/ship-ctl.md`,
`contracts/ship-status.schema.json` and `contracts/ship-ctl-result.schema.json`.

The entry point is a few lines; the code is the `shipctl` package in `../lib/`
(Python standard library only).

| Module | What it does |
|---|---|
| `cli.py` | argument parsing, the JSON envelope, exit codes, `set`, `version`, `refresh` |
| `status.py` | the status document: parallel probes, each with a 5 s timeout |
| `apply.py` | ship.toml -> managed settings, agent settings, herdr config, sudoers, avahi |
| `autostart.py` | starts `[[autostart]]` agents that are not running |
| `naming.py` | the Claude session name for each agent (`<prefix>-<agent>`): the one place to change how it is applied |
| `tools.py` | the catalogue runner (`contracts/tools.md`) |
| `converge.py` | switches to a new tooling bundle, rolling back on failure |
| `logs.py` | `ship-ctl logs SOURCE` |
| `schema.py` | a small JSON Schema validator (Ships have no `jsonschema`) |
| `tomlw.py` | TOML writing, and in-place key edits for herdr's config |

## Beyond the contract

- `ship-ctl refresh` (internal): run by `ship-status.service` every 30 seconds.
  It writes `/run/shipshape/status.json`, then reacts to sign-ins: recipes
  waiting on a service that just became `ok` are finished, and Claude becoming
  `ok` runs `autostart` once. Previous states live in `/run/shipshape/`, so the
  first refresh after a boot compares against "not signed in".
- After a reboot autostart waits until `ship-resume` has recorded this boot (or
  10 minutes of uptime), so it never races herdr's own resume. It recognises an
  agent herdr resumed by its name, or by the Claude session or pane it recorded,
  and puts the name back if herdr lost it.

Internal state (not a contract; may change): `/var/lib/shipshape/provision.json`
(written by ship-provision), `tools.json`, `autostart.json`; agent-side
`~/.local/state/shipshape/last-resume.json` (written by ship-resume) and the mode
ledger.

## Testing without a Ship

`SHIPSHAPE_ROOT=/some/dir` makes ship-ctl treat that folder as `/`: no root check,
no switching to the agent user, no chown. `SHIPSHAPE_BUNDLE` points it at a
bundle folder. `ship/tests/` uses both, with fake `herdr`, `claude`, `gh`,
`gcloud`, `apt-get`, `npm` and friends on PATH, so the tests never touch real
tools. Do not run sandbox mode with real tools on PATH: agent commands would run
as you, against your own herdr and sign-ins.
