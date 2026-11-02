# ship-ctl contract

`ship-ctl` is the only way FleetLedger changes or inspects a Ship. The Ship track
implements it (W5, Python standard library only); the Fleet track calls it over
SSH as described in `ship-runtime.md`.

Version 1.

## General rules

- Path: `/usr/local/bin/ship-ctl`. Every command except `version` and `--help`
  needs root (FleetLedger runs `sudo -n ship-ctl ...`).
- `--json` prints exactly one JSON document on stdout and nothing else:
  - `status` prints a status document (`ship-status.schema.json`), or a result
    with `"command": "status"` if it cannot produce one;
  - every other command prints a result (`ship-ctl-result.schema.json`).
- Without `--json` the output is for people; its format is not a contract.
- Diagnostics go to stderr and the journal, never into the JSON.
- Never print a token, password or private key, in any mode.

Exit codes:

| Code | Meaning | `error.code` |
|---|---|---|
| 0 | Success | (null) |
| 1 | The command ran and failed | `provision-failed`, `tool-failed`, `invalid-bundle`, `bundle-checksum`, `herdr-unavailable`, `internal` |
| 2 | Bad arguments | `usage` |
| 3 | Not running as root | `not-root` |
| 4 | `ship.toml` (or the replacement given) fails the schema | `invalid-ship-toml` |

## Commands

### `ship-ctl status --json [--write PATH]`

Prints the status document. Exit 0 whenever a valid document is printed, even if
the Ship is unhealthy: health is in the document, not the exit code.

If no valid status document can be produced, it prints a result document instead
(`"command": "status"`, `"ok": false`, `error.code` `internal`) and exits 1; when
not run as root it prints the same with `not-root` and exits 3. FleetLedger tells
the two shapes apart by their `schema` value.

- Finishes within 15 seconds. Each probe has its own timeout (5 seconds at most);
  a failed probe sets its fields to `unknown`/`null` and adds a line to `errors`.
- `--write PATH` also writes the document atomically to PATH (mode 0644). The
  status timer uses it for `/run/shipshape/status.json`.

How fields are worked out:

| Field | Source |
|---|---|
| `ship.bundle_commit` | `commit` from the installed `bundle.json`; the value `unknown` becomes `null`. |
| `state` | First match: `provisioning` if `provision.result` is `running` or `never`, or cloud-init is running; `needs-sign-in` if `auth.claude.state` is `missing`, `expired` or `error`; `running` if herdr lists at least one agent; `ready` if the herdr server is running; else `unknown`. |
| `auth.claude` | `claude auth status --json` as `agent`: `loggedIn` gives `ok` or `missing`; `account` is the email; `detail` is the plan or method. |
| `auth.gcloud` | `gcloud auth list --filter=status:ACTIVE --format="value(account)"` as `agent`. |
| `auth.gcloud_adc` | `gcloud auth application-default print-access-token` as `agent`, output discarded: exit 0 gives `ok`. `account` may be null. |
| `auth.github` | `gh auth status --hostname github.com` as `agent`; `account` is the login it reports. |
| `herdr` | `herdr status server` and `herdr agent list` as `agent`. `mode` comes from the mode ledger. `last_resume` comes from ship-resume's record. |
| `claude` | `claude --version` as `agent`, plus the values in `ship.toml`. |
| `tools` | The last recipe run for each id in `ship.toml` `[tools]` (and its profiles). |
| `host` | `/proc`, the root filesystem, `/var/run/reboot-required`; `updates_pending` from the last apt check (null if unknown). |
| `net` | `null` until the phase 3 collector exists. |

Attention codes the Ship emits: `agent-waiting` (an agent is `blocked`),
`signin-missing`, `signin-expiring`, `signin-expired`, `unit-failed`,
`reboot-required`, `provision-failed`, `tool-waiting`, `tool-failed`. FleetLedger
adds its own on top (`signin-wrong-account` from the tag check, `behind-tooling`
from `versions.toml`); it shows unknown codes as they are.

### `ship-ctl apply [--dry-run] [--json]`

Renders everything that derives from `ship.toml` (listed in `settings.md`) and
writes only what changed. Idempotent. `changed` lists the files written; with
`--dry-run` nothing is written and `changed` lists what would be.

### `ship-ctl set KEY=VALUE... [--json]` and `ship-ctl set --file - [--json]`

Changes `ship.toml`, validates the whole file against the schema, writes it
atomically, then runs `apply`.

- `KEY=VALUE` keys: `ship.tags` (comma-separated), `claude.remote_control`,
  `claude.default_mode`, `claude.allow_bypass`, `claude.update_channel`,
  `claude.session_prefix`, `agent.sudo`, `network.lan_guard`. Booleans are `true`
  or `false`.
- `--file -` reads a complete replacement `ship.toml` on stdin. Its `ship.name`
  and `ship.id` must match the current file. Use it for anything else (autostart
  entries, tools, profile options).
- Exit 4 and no change if the result would be invalid.

### `ship-ctl autostart [--json]`

Starts every `[[autostart]]` agent that is not running, in its own mode, with
`herdr agent start <name> --kind claude`, then sends its prompt. It does nothing
while the Ship is `provisioning` or `needs-sign-in`.

The Ship runs it on its own: at boot (`ship-resume.service`) and once whenever
Claude becomes signed in (see "Reacting to sign-ins" below). FleetLedger never
needs to call it; calling it anyway is harmless.

`data`: `{ "started": [names], "skipped": [{ "name": ..., "reason": ... }] }`.

### Reacting to sign-ins

The Ship reacts to sign-ins by itself; FleetLedger triggers nothing after a
sign-in. Each time the status timer refreshes `/run/shipshape/status.json` (at
least every 60 seconds), ship-ctl compares every `auth` service's state with the
previous refresh:

- a service that changed to `ok`: run `tools run` for the recipes waiting on it;
- `claude` changed to `ok`: then run `autostart` once.

This is triggered by the change, not by the state, so an agent you stop later is
not restarted until the next boot or the next time Claude goes from signed out
to signed in. It works however the sign-in happened: through the concierge or by
hand over SSH. The first refresh after boot compares against "not signed in", and
autostart is idempotent, so a boot that is already signed in starts each agent
once.

### `ship-ctl converge --bundle PATH [--json]`

Applies a tooling bundle:

1. Check `bundle.json` against `bundle-manifest.schema.json` and the required
   entries (`seed.md`).
2. Unpack it and switch `/opt/shipshape/current` to the new release.
3. Run `ship-provision --converge`, then `apply`, then `tools run`.

If any step fails, `current` goes back to the previous release and the command
exits 1. `data`: `{ "from": release, "to": release }`.

### `ship-ctl tools list|run|add|remove [ID...] [--json]`

- `list`: `data` is `{ "installed": <same shape as status tools>, "available": [recipe ids in the current bundle] }`.
- `run [ID...]`: installs, or finishes setting up, the given recipes (default:
  everything `ship.toml` asks for). Recipes waiting on a sign-in stay `waiting`;
  that is not a failure.
- `add ID...` / `remove ID...`: edit `[tools].install`, then `run`. `remove`
  stops managing the tool; it does not uninstall it in phase 1.

### `ship-ctl logs SOURCE [--lines N] [--json]`

SOURCE is one of `provision`, `herdr`, `resume`, `tools`, `status`, `system`;
default 200 lines. With `--json`, `data` is `{ "lines": [strings] }`. Log lines
are passed through as written; the Ship track keeps secrets out of them.

### `ship-ctl version [--json]`

Needs no root. `data`:

```json
{
  "tooling": "0.1.0",
  "commit": "0123456789ab",
  "contracts": { "ship-status": 1, "ship-toml": 1, "ship-ctl-result": 1, "bundle": 1, "seed": 1, "runtime": 1 }
}
```

FleetLedger checks `contracts` before using a Ship and refuses (with a clear
message) any major version it does not know.
