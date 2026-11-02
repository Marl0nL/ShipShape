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
| 1 | The command ran and failed | `provision-failed`, `tool-failed`, `invalid-bundle`, `bundle-checksum`, `herdr-unavailable`, `invalid-input`, `internal` |
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
| `net` | The `ship-netwatch` summary over the last 24 hours (see `ship-ctl net` below): totals from vnstat, `new_destinations` = destinations whose `first_seen` is inside the window, newest first, at most 50. `null` when the collector is not installed. |
| `ssh` | `/etc/ssh/ssh_host_ed25519_key-cert.pub` (read with `ssh-keygen -L`), the user CA keys, the agent principals file and the revocation list that `ssh-trust` installed (see below). |

Attention codes the Ship emits: `agent-waiting` (an agent is `blocked`),
`signin-missing`, `signin-expiring`, `signin-expired`, `unit-failed`,
`reboot-required`, `provision-failed`, `tool-waiting`, `tool-failed`,
`host-cert-expiring` (the host certificate ends within 14 days),
`host-cert-expired`, `net-collector-down` (ship-netwatch installed but not
running). FleetLedger adds its own on top (`signin-wrong-account` from the tag
check, `behind-tooling` from `versions.toml`, `host-cert-missing` and
`ssh-trust-behind` from its certificate authority); it shows unknown codes as
they are.

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
`herdr agent start <name> --kind claude`, then sends its prompt, or "Get oriented
and ready to work." if the entry has none (a chosen default: every autostart
agent starts with a first prompt). It does nothing
while the Ship is `provisioning` or `needs-sign-in`.

The Ship runs it on its own: at boot (the first status refresh after
`ship-resume.service` has finished) and once whenever Claude becomes signed in
(see "Reacting to sign-ins" below). FleetLedger never needs to call it; calling
it anyway is harmless.

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

### `ship-ctl ssh-trust --file - [--json]`

Installs what FleetLedger's SSH certificate authority issues for this Ship. Reads
one `ssh-trust` document (`ssh-trust.schema.json`) on stdin; each field present
replaces what the Ship has, fields left out are kept.

- `host_certificate`: written to `/etc/ssh/ssh_host_ed25519_key-cert.pub` only if
  `ssh-keygen -L` reads it as a *host* certificate whose public key is exactly
  `/etc/ssh/ssh_host_ed25519_key.pub`, whose principals include the Ship's name,
  and which has not expired. The Ship never takes a host key from this document:
  the key it certifies is the one already pinned.
- `user_ca_keys`: written to `/etc/ssh/shipshape/user-ca.pub`. An empty list
  removes it, which turns certificate logins off.
- `revoked_keys`: the KRL, written to `/etc/ssh/shipshape/revoked-keys` only if
  `ssh-keygen -Q` can read it and it revokes none of the keys in `fleet`'s
  `authorized_keys` (a list that locked FleetLedger out could not be undone over
  SSH).

Then it renders `/etc/ssh/sshd_config.d/20-shipshape-ca.conf` (below), checks the
whole configuration with `sshd -t`, and reloads `ssh`. If `sshd -t` fails, every
file goes back to what it was and the command exits 1 (`invalid-input`); the
running sshd is never left with a configuration it would refuse.

`20-shipshape-ca.conf` has a line only for each file that exists:

```
HostCertificate /etc/ssh/ssh_host_ed25519_key-cert.pub
TrustedUserCAKeys /etc/ssh/shipshape/user-ca.pub
AuthorizedPrincipalsFile /etc/ssh/shipshape/principals/%u
RevokedKeys /etc/ssh/shipshape/revoked-keys
```

`AuthorizedPrincipalsFile` is always present once a user CA is. Only
`/etc/ssh/shipshape/principals/agent` exists, so a certificate can log in as
`agent` and never as `fleet` (the admin key stays a plain key in `fleet`'s
`authorized_keys`). The agent file lists `agent:all`, `agent:tag:<tag>` for each
tag in `ship.toml`, and `agent:ship:<name>`; `ship-ctl apply` keeps it in step
with `ship.toml` (so `ship-ctl set ship.tags=...` changes which certificates
work). Certificates are accepted for at most what their validity says; a lapsed
or revoked one is refused by sshd itself.

Errors: a document that fails the schema, or a certificate that fails the checks
above, exits 1 with `invalid-input` and changes nothing. Never prints a key.

`data`:

```json
{
  "host_certificate": { "serial": 4242, "key_id": "...", "principals": ["ship-01", "ship-01.local"],
                        "valid_after": "...Z", "valid_before": "...Z", "ca_fingerprint": "SHA256:..." },
  "user_ca": ["SHA256:..."],
  "agent_principals": ["agent:all", "agent:tag:work", "agent:ship:ship-01"],
  "revoked_keys_sha256": "<64 hex>"
}
```

(the same shape as the status document's `ssh`).

### `ship-ctl net [--json]`

The network visibility summary: which programs reached which destinations, how
often and roughly how much, over the last 7 days, plus interface totals. `data`
is a `net-summary` document (`net-summary.schema.json`). Works whatever state the
collector is in: when it is stopped the last saved summary is reported, when it
is not installed `collector.state` is `unavailable` and the lists are empty.

How it is collected (`ship-netwatch.service`, root, read-only towards everything
except `/var/lib/shipshape/net/`):

- connections from `tcpconnect-libbpf` and bytes from `tcplife-libbpf`
  (Ubuntu's `libbpf-tools`: precompiled, so nothing is compiled on the Ship;
  source `ebpf`); if they cannot run, `ss` is polled every 5 seconds instead
  (source `ss`: short connections can be missed and there are no byte counts);
- names from `resolvectl monitor --json=short`: each connection's address is
  matched to the DNS answer it came from, else the address itself is the
  destination (`kind: address`);
- totals from `vnstat --json`.

Names and program names come from the Ship and could have been chosen by an agent,
so ship-netwatch keeps only well-formed host names and replaces any other
character in a program name with `_`. It is visibility, not control: an agent
can still use its own DNS (DNS over HTTPS) or bare addresses, and with sudo it can
stop the collector.

### `ship-ctl version [--json]`

Needs no root. `data`:

```json
{
  "tooling": "0.1.0",
  "commit": "0123456789ab",
  "contracts": { "ship-status": 1, "ship-toml": 1, "ship-ctl-result": 1, "bundle": 1, "seed": 1, "runtime": 1,
                 "ssh-trust": 1, "net-summary": 1 }
}
```

FleetLedger checks `contracts` before using a Ship and refuses (with a clear
message) any major version it does not know. A Ship without `ssh-trust` or
`net-summary` predates phase 3: FleetLedger skips certificates and network
visibility for it until it is updated.
