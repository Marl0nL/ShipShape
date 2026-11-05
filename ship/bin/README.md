# ship/bin

`ship-ctl` (W5): the only way FleetLedger inspects or changes a Ship. Installed as
`/usr/local/bin/ship-ctl -> /opt/shipshape/current/bin/ship-ctl`. Its commands,
JSON output and exit codes are fixed by `contracts/ship-ctl.md`,
`contracts/ship-status.schema.json` and `contracts/ship-ctl-result.schema.json`.

The entry point is a few lines; the code is the `shipctl` package in `../lib/`
(Python standard library only).

| Module | What it does |
|---|---|
| `cli.py` | argument parsing, the JSON envelope, exit codes, `set`, `version`, `refresh`, `lan-guard` |
| `status.py` | the status document: parallel probes, each with a 5 s timeout |
| `apply.py` | ship.toml -> managed settings, agent settings, herdr config, sudoers, avahi, the unattended-upgrades reboot policy |
| `languard.py` | the LAN guard: renders, checks (`nft -c`) and loads the `inet shipshape_lan_guard` table, enables its boot unit |
| `autostart.py` | starts `[[autostart]]` agents that are not running |
| `notice.py` | `notify-signin`: tells the `[[autostart]]` agents, and no others, that a sign-in has completed |
| `naming.py` | the Claude session name for each agent (`<prefix>-<agent>`): the one place to change how it is applied |
| `tools.py` | the catalogue runner (`contracts/tools.md`) |
| `converge.py` | switches to a new tooling bundle, rolling back on failure |
| `logs.py` | `ship-ctl logs SOURCE` |
| `net.py` | `ship-ctl net` and status's `net` section: summarises what ship-netwatch recorded (`contracts/net-summary.schema.json`) |
| `sshtrust.py` | `ship-ctl ssh-trust`: installs FleetLedger's SSH certificates and CA keys |
| `overlay.py` | `ship-ctl overlay join|leave|status` (phase 5): the auth key from stdin via a root-only file in `/run`, `tailscale up`, and the sshd listen policy (`network.ssh_listen`) |
| `schema.py` | a small JSON Schema validator (Ships have no `jsonschema`) |
| `tomlw.py` | TOML writing, and in-place key edits for herdr's config |
| `shiptoml.py` | reading, checking and writing `ship.toml` |
| `context.py`, `paths.py`, `runner.py` | what every command gets: the paths (moved under `SHIPSHAPE_ROOT` in tests), a command runner, pinned versions, a clock |
| `homefs.py`, `fsutil.py` | safe reads and writes in the agent's home (below); atomic writes elsewhere |

## Beyond the contract

- `ship-ctl refresh` (internal): run by `ship-status.service` every 30 seconds.
  It writes `/run/shipshape/status.json`, then reacts to sign-ins: recipes
  waiting on a service that just became `ok` are finished, and Claude becoming
  `ok` runs `autostart` once. Previous states live in `/run/shipshape/`, so the
  first refresh after a boot compares against "not signed in".
- `ship-ctl lan-guard` (internal): run by `ship-lan-guard.service` once the
  network is online (`ship-lan-guard-early.service` has already loaded the
  stored rules before the network came up). It
  finds the default gateways (`ip -j route`) and DNS servers
  (`/run/systemd/resolve/resolv.conf`) again and reloads the table, or removes
  it when the guard is off. Every address is parsed and printed back by Python's
  `ipaddress` before it is written into the rules, so nothing else reaches nft.
  A DHCP change of gateway during a boot is picked up at the next boot or apply.
- `herdr-restart-pending`: status compares the inode of each running
  `herdr server` (agent's processes only, `/proc/<pid>/exe`) with
  `/usr/local/bin/herdr`.
- After a reboot autostart waits until `ship-resume` has recorded this boot (or
  10 minutes of uptime), so it never races herdr's own resume. It recognises an
  agent herdr resumed by its name, or by the Claude session or pane it recorded,
  and puts the name back if herdr lost it.

Internal state (not a contract; may change): `/var/lib/shipshape/provision.json`
(written by ship-provision), `tools.json`, `autostart.json`, `autostart-repos.json`,
`profiles.json`, `overlay.json`, `net/state.json`; the previous sign-in states in
`/run/shipshape/`; agent-side
`~/.local/state/shipshape/last-resume.json` (written by ship-resume) and the mode
ledger.

## Files in the agent's home

ship-ctl runs as root but edits files the `agent` user owns, and the agent can
put a symlink anywhere in its home. Everything ship-ctl reads or writes there goes
through `homefs.py`: paths are opened one folder at a time with `O_NOFOLLOW`, a
symlink anywhere on the way is refused (and reported in `messages`), and writes
replace the file by `renameat`. Inside a folder ship-ctl owns outright (a skill
copied by `tools`), stray symlinks are removed rather than followed.
`ship/tests/test_ship_symlinks.py` plants links for each case.

## Check on a real Ship (W11)

The tests use fakes, so these need a live Ship:

- `ship-ctl logs herdr` and `logs resume` return lines (journal fields
  `_SYSTEMD_USER_UNIT=` plus `_UID=<agent uid>`, read as root).
- `herdr agent list` JSON matches what `status.py` reads (`name`, `agent`,
  `agent_status`, `pane_id`, `agent_session.value`), including after a reboot.
- `claude auth status --json` signed in gives `email` and `subscriptionType`;
  `gh auth status` wording still yields the login.
- Autostart sessions show `<prefix>-<agent>` names in the Claude app
  (`claude --name`; phase 0 saw names survive `claude --resume`).

## Testing without a Ship

`SHIPSHAPE_ROOT=/some/dir` makes ship-ctl treat that folder as `/`: no root check,
no switching to the agent user, no chown. `SHIPSHAPE_BUNDLE` points it at a
bundle folder. `ship/tests/` uses both, with fake `herdr`, `claude`, `gh`,
`gcloud`, `apt-get`, `npm` and friends on PATH, so the tests never touch real
tools. Do not run sandbox mode with real tools on PATH: agent commands would run
as you, against your own herdr and sign-ins.
