# Settings contract: ship.toml and ships.toml

Two files, two owners:

- `ship.toml` lives on each Ship (`/etc/shipshape/ship.toml`). The Fleet track
  writes the first version into the seed; afterwards it changes only through
  `ship-ctl set`. The Ship track turns it into real configuration with
  `ship-ctl apply`.
- `ships.toml` is FleetLedger's inventory on your computer. It is local and
  git-ignored, because it holds your accounts and names. Only the Fleet track reads
  or writes it.

Schemas: `ship.toml.schema.json`, `ships.toml.schema.json`. Examples:
`examples/ship.toml/`, `examples/ships.toml/`.

## What `ship-ctl apply` does with ship.toml

### Claude Code managed settings

`/etc/claude-code/managed-settings.json` is written in full on every apply
(root-owned, so agents cannot change it):

| ship.toml | Managed setting |
|---|---|
| `claude.remote_control = "on"` (the default) | `"remoteControlAtStartup": false`; the agents the agent record gives it to get `--remote-control` at launch (see "Remote Control") |
| `claude.remote_control = "all"` | `"remoteControlAtStartup": true` (every session, as "on" was before) |
| `claude.remote_control = "off"` | `"remoteControlAtStartup": false` and `"disableRemoteControl": true` |
| `claude.remote_control = "blocked"` | the same as `"off"` (an older name, still accepted) |
| `claude.default_mode` | `"permissions": { "defaultMode": ... }` |
| `claude.allow_bypass = false` | `"permissions": { "disableBypassPermissionsMode": "disable" }` |
| `claude.update_channel` | `"autoUpdatesChannel"`, plus `"minimumVersion"` from `versions.toml` `[claude].minimum_version` |
| `claude.session_prefix` (default: the Ship's name) | `"env": { "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX": ... }` (a fallback only; see "Session names") |

#### Remote Control

"on" gives Remote Control to the agents the Ship starts and that its agent record
gives it to, and to no other session. The managed `remoteControlAtStartup` false
keeps it off for every session (a user `true`, from `/config`, cannot override a
managed value), and a launch ship-ctl makes (autostart, the resume at boot, a
restart after a crash, `agent restart`, `runtime switch`) ends with
`--remote-control` when the record's `remote_control` is true for that agent
(`ship/lib/shipctl/naming.py`). Claude Code documents the flag as turning it on
for one session even when `remoteControlAtStartup` is false; with `--resume` it
reattaches the session's saved Remote Control session. "on" must not set
`disableRemoteControl`: it refuses the flag too.

Which agents the record gives it to:

- `[[autostart]]` agents: from their first start.
- Adopted agents (started some other way, by a person or by another agent, then
  picked up from the runtime): not, until `ship-ctl agent remote-control NAME`
  (`ship-fleet agent remote-control SHIP NAME`, or the agent list in the web UI)
  turns it on; `--off` turns it off for any agent. The Ship cannot tell reliably
  whether a session it picks up has Remote Control: a person can turn it on or off
  inside the session at any time (`/remote-control`), which leaves no trace the
  Ship can read, so it does not guess.
- Records written before this setting existed: autostart agents on, adopted ones
  off.

The change reaches an agent the next time the Ship starts it (`agent restart`
brings it now, continuing the conversation). A session that had Remote Control
and is resumed by hand reconnects to it. Workers an agent runs as its own child
processes are never recorded and never get it. Updating from a version where "on"
meant every session: running sessions keep theirs until they end; an adopted
agent the Ship starts again comes back without it until it is turned on; "all" is
the old behaviour.

"off" needs `disableRemoteControl`: `remoteControlAtStartup` governs only new
sessions, and a resumed session reconnects to the Remote Control session saved
in its transcript. So "off" and "blocked" now write the same settings. Either
way, a change reaches a running session only when it restarts (a reboot
restarts them all), and the Claude app can take a few minutes to show or drop
sessions.

Never set on a Ship: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` (it turns off
Remote Control), `DISABLE_AUTOUPDATER` or `DISABLE_UPDATES` (the native installer
must update itself), `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (sign-in is
the agent's own `claude auth login`).

#### Session names

Every Claude session `ship-ctl autostart` starts is launched with
`--name <session_prefix>-<agent name>` (when the installed Claude Code has
`--name`). That name is what the Claude app shows, and it survives
`claude --resume`. To name a session started some other way, send it
`/rename <name>`. The prefix variable above only names a session that has no
name and no messages yet, so it is a fallback, not how sessions are named.

### Agent user files

ship-ctl merges only these keys and leaves everything else alone (herdr's
integration and agents edit these files too):

| File | Keys |
|---|---|
| `~/.claude/settings.json` | `skipDangerousModePermissionPrompt` (true exactly when `allow_bypass` is true); the mode-ledger hook entries for `UserPromptSubmit`, `SessionStart` and `SessionEnd` (command `/opt/shipshape/hooks/mode-ledger`) |
| `~/.claude.json` | `hasCompletedOnboarding = true` (no first-run screens in unattended launches); `projects["<cwd>"].hasTrustDialogAccepted = true` for every autostart `cwd` |
| `~/.config/herdr/config.toml` | `onboarding = false`; `[update] version_check = false`; `[session] resume_agents_on_restore = false` (ship-ctl resumes agents from its agent record, `ship-ctl.md`; herdr restores only its layout) |

### System

| ship.toml | Effect |
|---|---|
| `ship.name`, `ship.id` | `/etc/avahi/services/shipshape.service`: the LAN announcement (`ship-runtime.md`) |
| `ship.timezone` | the system time zone (`timedatectl set-timezone`), so `updates.reboot_time`, logs and agents see the same local time |
| `ship.name`, `ship.tags` | `/etc/ssh/shipshape/principals/agent`: which user certificates may log in as `agent` (`agent:all`, `agent:tag:<tag>`, `agent:ship:<name>`; `ship-ctl.md`) |
| `agent.sudo` | `/etc/sudoers.d/91-shipshape-agent` present (`NOPASSWD: ALL`) or absent |
| `network.lan_guard`, `lan_guard_allow` | nftables table `inet shipshape_lan_guard` present or absent, loaded at boot by `ship-lan-guard-early.service` and refreshed by `ship-lan-guard.service` (see "LAN guard") |
| `updates.auto_reboot`, `updates.reboot_time` | `/etc/apt/apt.conf.d/52shipshape-unattended-upgrades`: `Unattended-Upgrade::Automatic-Reboot` and `Automatic-Reboot-Time` (see "OS updates") |
| `network.ssh_listen` | `/etc/ssh/sshd_config.d/20-shipshape-listen.conf`: absent for `any`; for `overlay`, a `Match LocalAddress` block that denies every user on any local address other than loopback and the Ship's own overlay addresses (phase 5, `ship-ctl.md` overlay) |
| `wake.after_boot`, `wake.delay_minutes` | `ship-wake.timer` enabled or disabled, its `OnBootSec` in `/etc/systemd/system/ship-wake.timer.d/50-shipshape.conf` (see "Waking the primary agent after a boot") |
| `tools`, `profiles`, `profile_options` | `ship-ctl tools run` (see `tools.md`) |
| `toolkits` | the toolkit allow-list: written only by `ship-ctl toolkit apply\|remove`, kept by `set --file`; ship-ctl runs nothing from a toolkit not listed (`toolkit.md`) |
| `github.scopes` | nothing on disk: FleetLedger's GitHub sign-in asks for these extra scopes, and `ship-ctl status` compares them with the token's (see "Extra GitHub scopes") |
| `autostart` | used by `ship-ctl autostart`: at boot, once Claude becomes signed in, and on demand |

## LAN guard

When `network.lan_guard = true`, the Ship refuses **new outgoing** connections
to private and local address ranges:

- IPv4: `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`
  (shared address space, also used by overlay networks), `169.254.0.0/16`;
- IPv6: `fc00::/7`, `fe80::/10`.

Always let through: loopback, replies to connections the Ship accepted (so
FleetLedger's SSH and attaching with herdr are never affected), DHCP, ICMPv6,
the default gateways and DNS servers the Ship uses (found again each time the
rules load), and every entry in `lan_guard_allow`. Internet traffic is untouched.
Refused connections get an immediate reject, not a silent drop.

The rules live only in the table `inet shipshape_lan_guard`; nothing else in the
Ship's firewall is touched. `ship-ctl apply` loads or removes the table, keeps a
copy in `/etc/shipshape/lan-guard.nft`, and enables or disables two boot units:

- `ship-lan-guard-early.service` loads that copy before `network-pre.target`, so
  the guard is in place before any interface comes up and before any agent resumes
  agents;
- `ship-lan-guard.service` runs once the network is online, finds the gateways
  and DNS servers again and reloads the table (a Ship that moved networks has
  the old gateway and DNS allowed only until then).

## OS updates

Ubuntu's unattended-upgrades stays on (security updates every day, as Ubuntu
ships it). `ship-ctl apply` writes
`/etc/apt/apt.conf.d/52shipshape-unattended-upgrades` in full:

```
Unattended-Upgrade::Automatic-Reboot "<updates.auto_reboot>";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "<updates.reboot_time>";
```

`reboot_time` is the Ship's own local time: `ship.timezone` sets the Ship's
zone (UTC when absent), and `ship-fleet new` fills it in from the FleetLedger
computer's zone, so `04:00` means four in the morning where you are. Change it
with `ship-fleet set SHIP timezone=Europe/Berlin`.

With `auto_reboot = false` (the default) a needed reboot shows as
`reboot-required` attention, and `ship-fleet reboot` does it when you choose.
An automatic reboot is a clean `shutdown -r`; after it, the Ship resumes every
recorded agent in its mode, as after any reboot (even an unclean one: the agent
record and Claude's transcripts are on disk already).

## Waking the primary agent after a boot

After a reboot every recorded agent comes back in its conversation, but nothing
prompts it, so the monitors, servers and loops it ran in the background stay down
until someone writes to it. With `wake.after_boot = true` (off by default) the
Ship types one line into its **primary agent** `wake.delay_minutes` (default 10)
after each boot:

```
Your Ship rebooted 10 minutes ago. Make sure you've resumed any background processes or work that you should be doing, and review whether subagents or crews may have been interrupted by the reboot too.
```

`wake.text` replaces it: one line, at most 1000 characters, where `{delay}` becomes
the delay ("10 minutes"); `ship-ctl set wake.text=` (empty) goes back to this one.

- **The primary agent** is the one marked with `ship-ctl agent primary NAME` (at
  most one: marking another moves the mark; `--clear` removes it). With none marked,
  it is the first `[[autostart]]` agent the agent record has that should run. The
  status document reports it (`agent_record.primary`, with `marked`). With no
  primary at all, nothing is sent, and the journal says so.
- **When:** `ship-ctl apply` enables `ship-wake.timer` only while `after_boot` is on,
  with `OnBootSec` set to the delay; it first runs after the next boot. The timer
  starts `ship-wake.service`, which runs `ship-ctl wake`.
- **Once per boot:** `ship-ctl wake` keeps this boot's id and outcome in
  `/var/lib/shipshape/wake.json`; a second run in the same boot (a restart of the
  unit, a run by hand, a status refresh) sends nothing. It sends nothing more than
  30 minutes after the time it was meant for (a timer enabled late in a boot fires
  at once), and nothing before it.
- **Notice rules:** as `notify-signin` (`ship-ctl.md`): typed with the runtime's
  prompt only into an agent that is idle, working or done (Claude Code queues it
  mid-turn), never into one at a permission prompt or in a state the runtime cannot
  tell, and not before this boot's resume is done. The runtime cannot see a line
  someone has half typed into the agent's pane. While the agent cannot be typed
  into, or is not running yet, it tries again every minute, six tries in all, then
  gives up for this boot. Each outcome is one line in the Ship's journal
  (`ship-ctl logs system`, `wake: ...`).

## The agent runtime

`runtime` (top level, `herdr` when unset) chooses what runs the Ship's agents.
FleetLedger writes `runtime = "core"` into every new Ship's seed, so new Ships run
herdr-core from their first boot. Unset still means `herdr`: a Ship made before that
has no `runtime` and stays on herdr until it is moved (below).

- `herdr`: the herdr server (`herdr.service`), as Ships have always run.
  **Deprecated:** the next ShipShape release removes classic herdr. A Ship on it
  shows attention `runtime-deprecated` with the command that moves it to
  herdr-core.
- `core`: herdr-core (`hc`, `ship-core.service`). Each agent runs in a kept
  terminal, labelled for the herdr desktop app (one space, `shipshape`, a tab per
  agent), started through `/opt/shipshape/bin/agent-launch`; agents report their
  own state; Claude Code 2.1.295 or newer is needed. ship-provision enables
  `ship-core.service` instead of `herdr.service` at boot; both stay installed, and
  `session.resume_agents_on_restore` stays false either way.

Agents continue their sessions across a change of runtime (the agent record has
each one's session and mode; `ship-ctl.md`, "On herdr-core Ships"), from the next
boot: until then an agent that ran under the other runtime is started by neither. A
live Ship moves to herdr-core with `ship-ctl runtime switch core` (`ship-fleet
runtime SHIP core`; `ship-ctl.md`), which stops the agents first, resumes them on
herdr-core in their modes, and rolls back to herdr on any failure;
`docs/runbooks/switch-runtime.md` has the steps. The switch is one-way: a switch
from herdr-core to herdr is refused, changing nothing (on a Ship still on herdr,
`switch herdr` only puts that runtime right after a switch that stopped part way;
and after a switch from herdr that did not finish, it finishes the way back).

## Permission modes across reboots

Claude Code restores a resumed session's permission mode, except bypass, which it
drops, and a session just restarted in a new mode may not have recorded that mode
in its transcript yet. So ShipShape never relies on it: after a boot (or a crash)
ship-ctl starts each agent of its agent record itself, with `claude --resume <id>`
and that agent's mode flag (`ship-ctl.md`, "The agent record"):

- A hook records the session's mode in `~/.local/state/shipshape/modes.json`. On
  `UserPromptSubmit` it records the `permission_mode` Claude reports. On
  `SessionStart` it reads the mode from the launching `claude` process's own flags
  (`--dangerously-skip-permissions` or `--permission-mode`), so an agent started in
  bypass but never prompted is covered too; a launch without flags records no mode.
  On `SessionEnd` it records how the session ended (Claude's `reason`: `clear`,
  `resume`, `logout`, `prompt_input_exit`, `other`), until the session starts again;
  that tells an agent someone exited from one that crashed. `SessionStart` also
  records `started_at`: the session runs under these hooks, so its end will be
  recorded (Claude Code reads its hooks when a session starts). In a herdr-core
  terminal `SessionStart` records the terminal's address as `terminal`
  (`$HC_TERMINAL_ID`, e.g. `t3`): which session each core terminal runs now. The
  ledger:

  ```json
  { "schema": "shipshape.modes/1",
    "sessions": { "<session id>": { "mode": "bypassPermissions", "updated_at": "2026-01-01T00:00:00Z",
                                    "started_at": "2026-01-01T00:00:00Z",
                                    "ended": { "reason": "prompt_input_exit", "at": "2026-01-01T00:00:00Z" } } } }
  ```

- The agent record keeps each agent's mode, following the ledger. An agent is
  resumed in its session's mode from the ledger, else the record's: for
  `bypassPermissions` that is `--dangerously-skip-permissions`, and only on a Ship
  with `allow_bypass = true` (elsewhere it comes back in `default_mode`); for any
  other mode, `--permission-mode <mode>`.
- The status document reports each agent's `mode` from the ledger, else the record.

A running agent keeps its mode when `default_mode` changes: the new default
applies to agents started afterwards, and a reboot resumes each agent in the mode
it was in. `ship-ctl agent restart` (`ship-fleet agent restart`) swaps a running
agent for one in the new mode, continuing its conversation, and records the new
mode in the ledger and the agent record (`ship-ctl.md`).

Tooling before the agent record had herdr resume its own agents, through a hidden
herdr client (`ship-resume.service`) and a `claude` shim that put the mode flag back
on herdr's bare `claude --resume <id>`. A converge removes both, and records the
running agents first.

## Extra GitHub scopes

gh signs in with its own minimum scopes (`repo`, `read:org`, `gist`). A Ship may
choose extra ones from a short catalogue (the schema's `githubScope`; explained in
`fleetledger/fleetledger/ghscopes.py`): `workflow` (push changes to
`.github/workflows/`), `project` (GitHub Projects), `write:packages` (GitHub
Packages, such as ghcr.io images). Scopes that delete or administer (`delete_repo`,
`admin:*`) are not offered.

- The choice lives in `ship.toml` `github.scopes`, with a copy in `ships.toml`
  `ship.<name>.github_scopes` for the seed; `ship-fleet set SHIP github_scopes=...`
  changes both. The default is none: the sign-in is as it always was.
- FleetLedger's GitHub sign-in asks for exactly the chosen scopes: `gh auth login
  --scopes ...` on a Ship not signed in; on one signed in whose token's scopes differ,
  `gh auth refresh --scopes <missing> --remove-scopes <no longer chosen>`, which keeps
  the account. Only catalogue scopes are added or removed: gh's minimum and scopes
  given by hand stay as they are.
- Changing the choice never changes the token. `ship-ctl status` reports the token's
  scopes (`auth.github.scopes`) and, while they differ from the choice, the attention
  item `signin-scopes-differ` (subject `github`), until you sign in again.

## How ships.toml becomes a Ship's ship.toml

The seed generator builds `ship.toml` like this, then validates it before writing
the seed:

| ship.toml | From ships.toml |
|---|---|
| `ship.name` | the `[ship.<name>]` key |
| `ship.id` | `ship.<name>.id` (a new random UUID when the Ship is created) |
| `ship.tags` | `ship.<name>.tags`, else `defaults.tags` |
| `ship.timezone` | `ship-fleet new --timezone`, else the FleetLedger computer's zone (`TZ`, else `/etc/localtime`), else `UTC` |
| `claude` | `defaults.claude` overlaid with `ship.<name>.claude`; `session_prefix` defaults to the name |
| `tools.install` | `ship.<name>.tools`, else `defaults.tools` |
| `tools.profiles` | `ship.<name>.profiles`, else `defaults.profiles` |
| `profile_options` | for each profile: `profile_options.<profile>`. A missing required option stops seed creation with a clear message. |
| `autostart`, `agent`, `network` | from the `ship-fleet new` options; absent by default |
| `github.scopes` | `ship.<name>.github_scopes` (`ship-fleet new --github-scopes`); absent when there are none |

The `[tag.*]` expected accounts stay in `ships.toml`: they are never copied to a
Ship. FleetLedger compares them with the `account` values in each status document.

## The certificate authority in ships.toml (phase 3)

`[ca]` and `[machine.<name>]` stay on your computer; Ships get only what
`ship-ctl ssh-trust` installs (`ssh-trust.schema.json`):

| ships.toml | On each Ship |
|---|---|
| `ca.host_key` (its `.pub`) | signs `/etc/ssh/ssh_host_ed25519_key-cert.pub` over the pinned `host_key`, with every name in `ship.<name>.names` plus the bare name as principals, valid `ca.host_cert_days` |
| `ca.user_key` (its `.pub`), plus `ca.previous_user_keys` | `/etc/ssh/shipshape/user-ca.pub` |
| `machine.<m>.public_key` of every machine with `revoked` | in the KRL, `/etc/ssh/shipshape/revoked-keys` |
| `machine.<m>.scope` | not sent: it becomes the principals of that machine's certificates (`all` → `agent:all`, `tag:work` → `agent:tag:work`, `ship:ship-01` → `agent:ship:ship-01`) |
| `machine.<m>.issued` entries whose principals the current `scope` no longer covers | their serials, in the same KRL (narrowing a scope revokes the wider certificates still out there) |
| `ca.revoked_host_keys` | not sent: trusted machines get it as their `RevokedHostKeys` list |

Trusted machines get, from `ship-fleet trust <machine>`: their certificate, one
`@cert-authority` line for the host CA limited to the Ships' names, an SSH config
block per Ship (`User agent`, the certificate, `ForwardAgent no`), and each Ship
registered as a herdr saved machine.

