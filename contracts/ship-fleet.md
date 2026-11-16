# ship-fleet output other programs read

`ship-fleet` is FleetLedger's command line. Most of what it prints is for people, and
its format is not a contract. The documents below are: another program (a panel in a
terminal app, say, over `ssh <FleetLedger computer> ship-fleet ...`) may read them,
and they change only as `README.md` says (an optional field added keeps the version;
anything else bumps it).

Each `--json` prints exactly one JSON document on stdout and nothing else.

## `ship-fleet about [--check] [--json]`

FleetLedger itself, on the computer it runs on. Schema:
`fleetledger-about.schema.json` (`"schema": "shipshape.fleetledger-about/1"`);
examples in `examples/fleetledger-about/`.

| Field | What it is |
|---|---|
| `version` | FleetLedger's package version (`ship-fleet --version`). |
| `checkout` | The ShipShape checkout FleetLedger runs from: its folder, `commit` (full id), `branch` (null when detached, as on FleetLedger's own VM) and `dirty` (uncommitted changes to tracked files, which `self-update` refuses). |
| `upstream` | Whether `origin/main` has a newer commit. Looked at **only with `--check`**, which runs one `git fetch --tags --quiet origin` in the checkout (the fetch `ship-fleet self-update` makes, to the same remote) and compares. Without `--check`, `checked` is false and the rest null: nothing reaches the network. `newer`: `ref` is ahead of the checkout; `behind`: commits on `ref` the checkout lacks. |
| `ui` | The web UI. `local`: this computer's address (`http://127.0.0.1:PORT`), from the running UI, else the remote UI's port, else the service unit's; `remote`: the Tailscale Serve address when the remote UI is on. `running`: a `ship-fleet ui` run by this user answers on its control socket; `as_service`, `jobs` (how many are running) and `stopping` are what it says, null when none runs. |
| `service` | The systemd user unit (`ship-fleet service`): `installed`, and systemd's own words for it (`active`: `systemctl --user is-active`; `enabled`: `is-enabled`), null when not installed or systemd did not answer. |
| `vault` | `state`: `off` (no vault: secrets are in the OS keyring or 0600 files), `locked` (FleetLedger is read-only until `ship-fleet unlock`), `unlocked`, or null when it cannot be read. |
| `backup` | `scheduled`: a daily timer is installed (`ship-fleet backup schedule`). `last`: the last `ship-fleet backup now`'s result here (when, whether it worked, the folder, why it failed), or null. That is the last attempt, so `last_made` says when the newest backup in that folder was made (from its file name; null when there is none or the folder is not there just now). |
| `copy` | Which copy of the fleet this is (`ship-fleet migrate`): `active`, `standby`, `retired`, `unreadable` (a marker FleetLedger cannot read), or `none` (no fleet here yet). |
| `self_update` | The command that updates FleetLedger (`ship-fleet self-update`, run on the FleetLedger computer as its user) and the ref it moves to by default. |
| `unread` | What could not be read, and why (`what`: `checkout`, `upstream`, `service`, `remote ui`, `vault`, `backups` or `copy`). The rest of the document is still true. |

It only reads. It never unlocks, updates, restarts, or writes a setting, and it runs
when other commands refuse: while the vault is locked, on a copy on standby or
retired, and while `ship-fleet migrate` moves the fleet (it holds no in-use lock, so
it never holds up a move either). It exits 0 whenever it could print the document,
`unread` saying what it could not see; a broken command line exits 2 (argparse).

It reads no secret and reaches no Ship. What it asks is cheap: a few local `git`
commands (`status` with `--no-optional-locks`, so it never rewrites the index),
`systemctl --user is-active` and `is-enabled` when the unit is installed, one request
to the running UI's control socket (`web/control.py`), FleetLedger's own state files,
and a listing of the last backup's folder. `--check` adds the one fetch.
