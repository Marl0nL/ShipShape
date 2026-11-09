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
| 1 | The command ran and failed | `provision-failed`, `tool-failed`, `invalid-bundle`, `bundle-checksum`, `herdr-unavailable`, `agent-busy`, `agent-failed`, `invalid-input`, `overlay-failed`, `overlay-not-joined`, `internal` |
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

FleetLedger ignores a top-level block it does not know, so a Ship updated past
the FleetLedger reading it (say, from a second computer on an older checkout)
still shows; every block it does know is still checked against the schema. New
fields therefore go in a new optional top-level block, or are optional inside
one: a field added inside an existing block still breaks an older FleetLedger.

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
| `auth.github` | `gh auth status --hostname github.com` as `agent`; `account` is the login it reports; `scopes` the token's scopes it lists (`Token scopes:`), sorted, or null when it lists none. |
| `herdr` | `herdr --version` and `herdr agent list` as `agent`. `mode` comes from the mode ledger for the agent's session, else from the agent record. `last_resume` is the agent record's last resume after a boot, with `method` `api-fallback` (ship-ctl started the recorded agents again through herdr's API); until a Ship's first boot on tooling with the record, the one ship-resume recorded (`hidden-client`). |
| `agent_record` | The agent record (see "The agent record" below): each recorded agent's name, folder, mode, whether it is an `[[autostart]]` agent, its state (`running`, `starting`, `failed`, `stopped`, or `unknown` while herdr does not answer), failed tries this boot, and the last boot's resume (`method` `ship-ctl`). Session ids are not reported. Optional in the schema: Ships with older tooling leave it out. |
| `claude` | `claude --version` as `agent`, plus the values in `ship.toml`. |
| `tools` | The last recipe run for each id in `ship.toml` `[tools]` (and its profiles). |
| `host` | `/proc`, the root filesystem, `/var/run/reboot-required`; `updates_pending` from the last apt check (null if unknown). |
| `net` | The `ship-netwatch` summary over the last 24 hours (see `ship-ctl net` below): totals from vnstat, `new_destinations` = destinations whose `first_seen` is inside the window, newest first, at most 50. `null` when the collector is not installed. |
| `ssh` | `/etc/ssh/ssh_host_ed25519_key-cert.pub` (read with `ssh-keygen -L`), the user CA keys, the agent principals file and the revocation list that `ssh-trust` installed (see below). |
| `settings` | `ship.toml` with its defaults filled in: the values `set` manages (tags, time zone, the Claude settings, agent sudo, LAN guard and its allowed addresses, SSH listen, OS-update reboots, the extra GitHub scopes), the owner login's name, and the `[[autostart]]` agents (name, folder, mode, repo). Optional in the schema: Ships with older tooling leave it out, and FleetLedger then shows their settings as unknown. |
| `overlay` | `null` when the overlay tooling is absent; otherwise the overlay record (see `overlay` below) plus `tailscale status --json` (`BackendState`, `Self.DNSName`, `Self.TailscaleIPs`, `Self.Tags`). |

Attention codes the Ship emits: `agent-waiting` (an agent is `blocked`),
`signin-missing`, `signin-expiring`, `signin-expired`, `unit-failed`,
`reboot-required`, `herdr-restart-pending`, `provision-failed`, `tool-waiting`,
`tool-failed`, `signin-scopes-differ` (GitHub is signed in, and its token lacks an
extra scope `github.scopes` chooses, or has a catalogue scope it no longer chooses;
`settings.md`, "Extra GitHub scopes"; for a scope it has but does not choose, the message says to tick it to keep it before it says that signing in again drops it), `host-cert-expiring` (the host certificate ends within 14 days),
`host-cert-expired`, `net-collector-down` (ship-netwatch installed but not
running), `overlay-down` (joined, but the overlay is not `running`),
`agent-relaunch-failed` (a recorded agent stopped and did not start again after 3
tries this boot; subject: the agent).
FleetLedger adds its own on top (`signin-wrong-account` from the tag check,
`behind-tooling` from `versions.toml`, `tags-differ` from the inventory,
`host-cert-missing` and `ssh-trust-behind` from its certificate authority); it
shows unknown codes as they are.

`herdr-restart-pending`: the running herdr server is not the installed
`/usr/local/bin/herdr` (a converge replaced the binary; the server keeps running
the old one). A reboot switches to the new one and resumes every agent in its
mode; `ship-fleet update` does that for you unless told not to.

### `ship-ctl apply [--dry-run] [--json]`

Renders everything that derives from `ship.toml` (listed in `settings.md`) and
writes only what changed. Idempotent. `changed` lists the files written; with
`--dry-run` nothing is written and `changed` lists what would be.

### `ship-ctl set KEY=VALUE... [--json]` and `ship-ctl set --file - [--json]`

Changes `ship.toml`, validates the whole file against the schema, writes it
atomically, then runs `apply`.

- `KEY=VALUE` keys: `ship.tags` (comma-separated), `ship.timezone`, `claude.remote_control`,
  `claude.default_mode`, `claude.allow_bypass`, `claude.update_channel`,
  `claude.session_prefix`, `agent.sudo`, `network.lan_guard`,
  `network.lan_guard_allow` (comma-separated; empty clears it),
  `updates.auto_reboot`, `updates.reboot_time`, `network.ssh_listen`, `owner.name`,
  `owner.keys` (comma-separated public keys), `github.scopes` (comma-separated, from
  the catalogue; empty for none: `settings.md`, "Extra GitHub scopes"). Booleans are
  `true` or `false`.
- `autostart.add=NAME:CWD[:MODE]` adds an `[[autostart]]` agent (refused if one has
  that name), and `autostart.add=NAME:github:OWNER/REPO[:MODE]` adds one that works
  in a GitHub repo, cloned into `~/work/REPO` first (its `repo`); `autostart.remove=NAME` drops one (refused if none has it; a running
  agent keeps running until it is stopped or the Ship reboots). Run `autostart`
  afterwards to start a new one at once.
- `network.ssh_listen=overlay` is refused with `overlay-not-joined` (exit 1, no
  change) unless the Ship has joined the overlay and knows its overlay addresses.
- `--file -` reads a complete replacement `ship.toml` on stdin. Its `ship.name`
  and `ship.id` must match the current file. Use it for anything else (autostart
  entries, tools, profile options).
- Exit 4 and no change if the result would be invalid.

### `ship-ctl autostart [--json]`

Starts every `[[autostart]]` agent that is not running. First the agent record is
brought up to date and every recorded agent that should run and does not is started
again, continuing its session (after a boot, that is the resume: "The agent record"
below). Then each entry that is still not running, and that the record is not
starting again, starts afresh in a new workspace labelled with its name, in its own
mode, with `herdr agent start <name> --kind claude`, then gets its prompt, or "Get
oriented and ready to work." if the entry has none (a chosen default: every
autostart agent starts with a first prompt), and goes into the record. An entry
exited on purpose (it is `stopped` in the record) starts afresh. It does nothing
while the Ship is `provisioning` or `needs-sign-in`.

The Ship runs it on its own: at the first status refresh after a boot, and once
whenever Claude becomes signed in (see "Reacting to sign-ins" below). FleetLedger
never needs to call it; calling it anyway is harmless.

An entry with a `repo` is cloned into its `cwd` first, as the agent, once (into a
folder that is empty or not there yet). A repo that cannot be cloned before the
GitHub sign-in (private, or not there) leaves that agent unstarted, with the
attention item `agent-repo-waiting`; it is retried when GitHub becomes signed in
(see "Reacting to sign-ins"). The status refresh clones each entry's repo as soon
as it can, without waiting for Claude (a public repo needs no sign-in), so the
folder is ready when the agent starts. A clone that fails while signed in is
`agent-repo-failed`. When github.com cannot be reached at all (no network or DNS
yet), the item says so, not that a sign-in is missing, and the clone is tried again
at every status refresh. An agent removed from `[[autostart]]` takes its repo state
and attention item with it. Other agents start regardless.

`data`: `{ "started": [names], "skipped": [{ "name": ..., "reason": ... }] }`. A
`reason` is `running`, `resuming` (the record is starting it again),
`relaunch-failed` (the record gave up on it this boot: `agent-relaunch-failed`),
`resume-pending`
(this boot's resume waits for DNS), `provisioning`, `needs-sign-in`, or why its repo
or its start failed.

### `ship-ctl agent restart (NAME... | --all) [--mode MODE] [--fresh] [--force] [--json]`

Swaps Claude agents for ones in another permission mode, in place. A running
session keeps its mode across a resume, so a new `default_mode` (or an
`[[autostart]]` mode) reaches only agents started afterwards; this is how a running
one adopts it. NAME is an agent's name in the agent record (the same as its herdr
name; the record is brought up to date first, so an agent started by hand counts
too) or a herdr agent name. `--all` is every Claude agent herdr lists with a name
(one without a name is skipped, with a reason).

- **Mode:** `--mode`, else the mode the agent would start in now: its
  `[[autostart]]` entry's `mode`, else `claude.default_mode` (for an agent that
  is not running: the mode it was in, below).
  `bypassPermissions` on a Ship whose `claude.allow_bypass` is false is refused
  (`invalid-input`) before any agent is touched; every agent's mode is checked
  first.
- **Gracefully, one agent at a time.** An agent herdr reports as `working` (or
  `unknown`: its foreground is not known) is left alone unless `--force`;
  forced, its turn is interrupted with Escape and the messages say so. An agent
  waiting at a question (`blocked`) has it dismissed with Escape, and the
  messages say so. `idle` and `done` agents are simply stopped.
- **Stopping:** Ctrl+C (clears a half-typed line), then `/exit` typed with
  `herdr agent prompt`; ship-ctl waits up to 30 seconds for herdr to see no agent
  in the pane, then tries Ctrl+C twice. An agent that still does not exit is
  left running as it was.
- **Starting again:** `herdr agent start <name> --kind claude --pane <the same
  pane>`, so the same workspace, folder and name, with the mode's flag
  (`--permission-mode MODE`, or `--dangerously-skip-permissions`) and
  `--resume <its session>`: the conversation continues. The session is the one
  herdr reports, else the record's. `--fresh` starts a new conversation instead,
  with `--name` as autostart names it, and an agent that was given a first prompt
  (an `[[autostart]]` one) gets it again. An agent whose session is known to
  neither can only be restarted `--fresh`. Remote Control comes back by itself
  (managed settings; a resumed session reconnects to its own Remote Control
  session), and session names survive the resume. One that stopped but did not
  start again is started by the next status refresh, as after a crash.
- **A recorded agent that is not running** (it stopped, or did not start again
  after a crash: `failed`) is started again from the record: its folder and
  session, in its old pane if that is a free shell in its folder, else in a new
  workspace. Without `--mode` it comes back in the mode it was in (its session's
  in the mode ledger, else the record's; `default_mode` where that is bypass and
  bypass is no longer allowed). That clears `agent-relaunch-failed`.
- **Recorded:** the mode ledger and the agent record get the session in its new
  mode, so the status document reports it, `ship-fleet reboot` expects it, and the
  next boot resumes it in that mode (`settings.md`, "Permission modes across
  reboots").

`data`: `{ "restarted": [{ "name", "mode", "previous_mode" (from the ledger, or
null), "conversation": "continued" | "fresh", "was": <its herdr state, or its
record state when it was not running> }], "skipped": [{ "name", "reason" }] }`;
`changed` lists the agents restarted.
Errors, with `data` still saying what did happen: `agent-busy` when only busy
agents were left alone (run again with `--force`, or when they are idle),
`agent-failed` when an agent did not exit, did not start again, or has no known
session; `herdr-unavailable` when herdr does not answer; `invalid-input` for a
name neither the record nor herdr has; `agent-busy` too when another ship-ctl held
the record for 60 seconds (see "The agent record").

Doing it by hand, on the Ship as `agent` (attach with herdr and go to its pane):
exit the agent (`/exit`), then in the same pane run
`claude --permission-mode MODE --continue` (or `--dangerously-skip-permissions`
for bypass, or `--resume <session id>` for one session of several in that folder).
The mode-ledger hook records the new mode as the session starts (it reads the
launch flags), and the next status refresh gives the agent its name back and
records it, so a reboot keeps it.

### `ship-ctl agent forget NAME... [--json]`

Drops recorded agents that are not running from the agent record: they are not
resumed at boot and not started again (and `agent-relaunch-failed` goes). A running
agent is refused (`invalid-input`): exit it, and it leaves the record by itself (an
`[[autostart]]` agent stays, as `stopped`, and starts afresh at the next boot;
removing it from `[[autostart]]` lets it go). While herdr does not answer nothing
is forgotten (`herdr-unavailable`): whether the agent runs is not known.
`data`: `{ "forgotten": [names] }`.

### The agent record

ShipShape keeps its own record of the Claude agents a Ship runs,
`/var/lib/shipshape/agents.json` (root, `agent-record.schema.json`): each agent's
name, folder, Claude session id, permission mode, the first prompt autostart gave
it, where it last ran, and whether it should be running. The record, not herdr,
decides what comes back after a boot: herdr's own resume is off
(`resume_agents_on_restore = false`, `settings.md`), and herdr restores only its
layout, each pane a shell in its folder under its old id.

- **Written** whenever ship-ctl starts, restarts or forgets an agent, and kept in
  step at every status refresh: a live Claude agent that reports a session and is
  not recorded (started by hand, or by a firstmate) is picked up, named after its
  folder if it has no herdr name (`herdr agent rename`); a recorded one whose
  session changed (`/clear`, or a hand restart in its pane) is followed, and its
  mode follows the mode ledger. `ship-ctl apply` (so every converge) records what
  runs too, which is how a Ship updated from tooling before the record keeps its
  agents across the reboot that follows: they come back in the modes they ran in.
- **Resume after a boot:** the first status refresh of a boot (once Claude is
  signed in, and DNS answers, at most 10 minutes) starts every recorded agent that
  should run with `herdr agent start <name> --kind claude --pane <pane> --
  <mode flag> --resume <session>`: in the pane herdr restored for it when that is a
  free shell in its folder, else in a new workspace labelled with its name. Its
  mode is the ledger's for that session, else the record's (`bypassPermissions`
  only where `allow_bypass` is true; elsewhere `default_mode`). Then the record
  notes this boot's resume (`agent_record.resume`, `herdr.last_resume`), and
  notices may be typed again.
- **Crash policy:** a recorded agent that stops in the middle of a boot is started
  again, with `--resume`, by the next status refresh, in its own pane. It counts
  as stopped on purpose, and is not started again, when Claude reported that it
  was exited (its `SessionEnd` hook said `prompt_input_exit` or `logout`, kept in
  the mode ledger) or its pane is gone, holds another agent, or is in another
  folder now. A session the mode-ledger hook never saw start (it began before the
  Ship was updated to this tooling, so it runs without the `SessionEnd` hook) is
  left stopped too, as it was before: its exit could not be told from a crash.
  After the next boot every agent runs under the new hooks. An `[[autostart]]`
  agent stopped on purpose stays in the record, `stopped`, and starts afresh at the
  next boot; any other (one removed from `[[autostart]]` too) leaves the record.
  A start that fails, and an agent that stops within 10 minutes of starting (its
  transcript too big to load, a crash on start), is a failed try; the tries reset
  once it has run 10 minutes. After 3 failed tries in one boot the agent is left
  alone (`failed`, attention `agent-relaunch-failed`) until `agent restart`,
  `agent forget` or the next boot. So an agent that keeps dying is started again at
  most twice, never in a loop. An agent stopped any other way than an exit (killed,
  `herdr agent stop`) counts as crashed and is started again.
- **One at a time:** ship-ctl holds a lock while it starts, stops or records agents;
  a status refresh that finds it held leaves the record for the next refresh.
  `autostart`, `agent restart` and `agent forget` wait for it at most 60 seconds
  (a boot's resume can hold it for a while), then fail with `agent-busy`, saying
  why; nothing has been touched.

### `ship-ctl notify-signin SERVICE [--json]`

Tells the Ship's core agents that a sign-in has completed. SERVICE is `claude`,
`gcloud`, `gcloud-adc` or `github`. FleetLedger runs it once a sign-in it ran
(`ship-fleet login`, the web UI's concierge) checks out with the expected
account, because agents started before that sign-in may already have found it
missing.

- **Core agents only:** the `[[autostart]]` entries, matched to live herdr agents
  through the agent record. No other agent is ever messaged (a Ship running a
  firstmate may have many worker agents).
- **One line**, typed with `herdr agent prompt`:
  `ShipShape: <service> is now signed in as <account>. Re-check it if you need it.`
  The account is read on the Ship from the service's own status probe and cut
  down to letters, digits and `@._+-`. It carries nothing else.
- **Typed only into an agent herdr reports as `idle`, `working` or `done`.**
  Claude Code queues the line while a turn is running. Skipped, with a reason
  in `data`:
  - an entry with no live agent (`not-running`);
  - an agent waiting at a permission prompt (`waiting-for-permission`), since
    typed text and Enter could answer it;
  - an agent in any other state, such as `unknown` (`not-ready`): its foreground
    is not known;
  - every entry while this boot's resume is not done yet (`resume-pending`): the
    first status refresh of a boot does it.
- **Recorded** in the Ship's journal (`ship-ctl logs system`, tag `ship-ctl`).
- A service that is not signed in sends nothing.

`data`: `{ "service": ..., "account": ... or null, "sent": [names], "skipped":
[{ "name": ..., "reason": ... }] }`.

### Reacting to sign-ins

The Ship reacts to sign-ins by itself (below). The only thing FleetLedger adds
after a sign-in it ran is `notify-signin`, to tell the core agents.

Each time the status timer refreshes `/run/shipshape/status.json` (at
least every 60 seconds), ship-ctl compares every `auth` service's state with the
previous refresh:

- a service that changed to `ok`: run `tools run` for the recipes waiting on it;
- `claude` changed to `ok`: then run `autostart` once;
- `github` changed to `ok`: clone the repos that waited for it, then start just
  those agents (never one you stopped).

This is triggered by the change, not by the state, so an `[[autostart]]` agent
you exit later is not started afresh until the next boot or the next time Claude
goes from signed out to signed in. (Every refresh also keeps the agent record in
step and starts again a recorded agent that crashed: "The agent record".) It works however the sign-in happened: through the concierge or by
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

Converge never restarts herdr or reboots: agents keep running. When it replaced
the herdr binary, the status document reports `herdr-restart-pending` until the
next reboot. Its `apply` records every running agent in the agent record first, so
the next reboot brings them back in their modes.

### `ship-ctl tools list|run|add|remove [ID...] [--json]`

- `list`: `data` is `{ "installed": <same shape as status tools>, "available": [recipe ids in the current bundle] }`.
- `run [ID...]`: installs, or finishes setting up, the given recipes (default:
  everything `ship.toml` asks for). Recipes waiting on a sign-in stay `waiting`;
  that is not a failure.
- `add ID...` / `remove ID...`: edit `[tools].install`, then `run`. `remove`
  stops managing the tool; it does not uninstall it in phase 1.

### `ship-ctl grow-root [--json]`

Grows the partition that holds `/` to fill its disk (`growpart`), then its ext4
filesystem (`resize2fs`, always, so a partition grown earlier without its
filesystem is caught up), online. FleetLedger runs it after it grows a Ship's disk
through the hypervisor (`ship-fleet resize --disk-gb`). Nothing to grow is not an
error: `changed` is empty. `data`: `{ "device": "/dev/sda1", "grown": true|false }`.

### `ship-ctl logs SOURCE [--lines N] [--json]`

SOURCE is one of `provision`, `herdr`, `resume`, `tools`, `status`, `system`;
default 200 lines. `resume` is the agent record's lines from the status refresh:
agents resumed after a boot, started again after a crash, or let go. With `--json`, `data` is `{ "lines": [strings] }`. Log lines
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
`/etc/ssh/shipshape/principals/agent` exists, and, with an `[owner]`,
`/etc/ssh/shipshape/principals/<owner>`, so a certificate can log in as `agent` or
as you and never as `fleet` (the admin key stays a plain key in `fleet`'s
`authorized_keys`). The agent file lists `agent:all`, `agent:tag:<tag>` for each
tag in `ship.toml`, and `agent:ship:<name>`; the owner's file lists the same with
`owner:` instead, so each login takes only certificates issued for it.
`ship-ctl apply` keeps both in step with `ship.toml` (so `ship-ctl set
ship.tags=...` changes which certificates work). Certificates are accepted for at most what their validity says; a lapsed
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
  destination (`kind: address`), written canonically (an IPv4-mapped IPv6
  address as plain IPv4, no zone index such as `%eth0`);
- totals from `vnstat --json`.

Names and program names come from the Ship and could have been chosen by an agent,
so ship-netwatch keeps only well-formed host names and replaces any other
character in a program name with `_`. It is visibility, not control: an agent
can still use its own DNS (DNS over HTTPS) or bare addresses, and with sudo it can
stop the collector.

### `ship-ctl overlay join|leave|status [--json]`

The overlay network (phase 5): access to the Ship from your other machines off the
LAN. Only Tailscale (or Headscale, through `--login-server`) is supported.

- `join --hostname NAME --tag TAG... [--login-server URL] --json`: reads a one-off
  auth key from **stdin** (one line, at most 512 bytes). The key never appears on a
  command line, in a log, in the result or on disk beyond a root-only file under
  `/run/shipshape/` that exists only while `tailscale up` runs and is removed
  afterwards. It installs the `tailscale` package from the pinned repo if it is
  missing, enables `tailscaled` (with log upload off), and runs `tailscale up
  --auth-key=file:<that file> --hostname NAME --advertise-tags TAG,...
  --ssh=false --accept-dns=false --accept-routes=false --reset`. It then records
  the Ship as joined (internal state) and returns the overlay in `data.overlay`
  (same shape as the status document's `overlay`). Joining again replaces the
  node's login with the new key. Fails with `overlay-failed` if `tailscale up`
  fails or the overlay is not `running` within 60 seconds; the key file is
  removed either way.
- `leave --json`: sets `network.ssh_listen` back to `any` first (so sshd answers
  on the LAN again), runs `tailscale logout`, then stops and disables
  `tailscaled`. Leaving a Ship that never joined changes nothing.
- `status --json`: `data.overlay` as above.

Tailscale SSH is never turned on: OpenSSH with the pinned host key stays the only
way in, over the overlay as on the LAN.

### `ship-ctl inbox list|put|remove|notify [--json]`

The file inbox: `~/inbox/` (agent, 0700), where a person drops files for the
Ship's agents (`ship-fleet put`, the web UI's Inbox tab). FleetLedger streams each
file over its usual SSH login; the Ship opens nothing new for it. ship-ctl runs as
root inside a home the agent controls, so every folder and file is opened without
following a symlink (as `apply` writes agent files); a symlink where `~/inbox` or
the staging folder should be is refused (`invalid-input`) and left alone.

- `put --name NAME --size BYTES --json`: reads exactly BYTES from **stdin** into a
  new file (0600, owned by agent) in `~/.local/state/shipshape/incoming/`, syncs
  it, then links it into `~/inbox/` and removes the staging name, so an agent only
  ever sees a complete file. NAME is cleaned first: only its last path part, NFC,
  letters and digits of any script plus ` ._-+,@()[]#%=~` (any other character
  becomes `_`; control and format characters such as bidi overrides are dropped),
  no leading dot, dash, `~` or `#`, at most 200 bytes (the extension kept), `file`
  if nothing is left. A name already taken gets `-2`, `-3` ... before its
  extension, the part after the last dot if it is at most 16 bytes, a `.tar.*` pair
  kept together (`report v1.2-2.pdf`, `archive-2.tar.gz`, `README-2`): nothing is
  ever overwritten, even when two files
  arrive at once (linkat refuses an existing name). Refused before a byte is read
  (`invalid-input`): a file over 256 MiB, or one that would leave less than 1 GiB
  free on the home's filesystem. Fewer or more bytes than BYTES keeps nothing
  (`invalid-input`). Files never get an execute bit; nothing reads, runs or
  unpacks them. Each `put` first removes staging `.part` files untouched for an
  hour (a transfer cut off by a kill or a reboot), before it checks free space. `data`: `{ "name": stored name, "given": NAME as sent, "size",
  "path", "renamed": bool }`; `changed` is the path. Recorded in the journal
  (`ship-ctl logs system`, tag `ship-ctl`).
- `list --json`: `data`: `{ "path": "/home/agent/inbox", "exists", "files": [{
  "name", "size", "modified" (UTC) }], "others": count of entries that are not
  plain files (folders, links) or whose names are not UTF-8, "truncated", "max_bytes",
  "free_bytes" }`, newest first, at most 500 files. Folders are not entered.
- `remove NAME... --json`: removes plain files with exactly these names from
  `~/inbox/` (no `/`). `data`: `{ "removed": [...], "missing": [...], "refused":
  [names that are not plain files] }`. Missing is not an error: an agent may have
  moved the file already.
- `notify --count N --json`: tells the core (`[[autostart]]`) agents, exactly as
  `notify-signin` does (the same agents, the same states typed into, the same
  skips, recorded the same way), with one line that carries a count and no file
  name: `ShipShape: N new files in your inbox (~/inbox), sent by the person.
  Handle them as the ship skill says when your work needs them.` `data`: `{
  "count", "sent", "skipped" }`.

The `ship` skill tells agents what the inbox is for and how to treat what is in it
(untrusted input; move a file out once it is dealt with).

### `ship-ctl version [--json]`

Needs no root. `data`:

```json
{
  "tooling": "0.1.0",
  "commit": "0123456789ab",
  "contracts": { "ship-status": 1, "ship-toml": 1, "ship-ctl-result": 1, "bundle": 1, "seed": 1, "runtime": 1,
                 "ssh-trust": 1, "net-summary": 1, "inbox": 1 }
}
```

FleetLedger checks `contracts` before using a Ship and refuses (with a clear
message) any major version it does not know. A Ship without `ssh-trust` or
`net-summary` predates phase 3: FleetLedger skips certificates and network
visibility for it until it is updated. A Ship without `inbox` predates the file
inbox: `ship-fleet put` says to update it first.
