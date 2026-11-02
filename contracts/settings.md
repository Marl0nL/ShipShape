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
| `claude.remote_control = "on"` | `"remoteControlAtStartup": true` |
| `claude.remote_control = "off"` | `"remoteControlAtStartup": false` and `"disableRemoteControl": true` |
| `claude.remote_control = "blocked"` | the same as `"off"` (an older name, still accepted) |
| `claude.default_mode` | `"permissions": { "defaultMode": ... }` |
| `claude.allow_bypass = false` | `"permissions": { "disableBypassPermissionsMode": "disable" }` |
| `claude.update_channel` | `"autoUpdatesChannel"`, plus `"minimumVersion"` from `versions.toml` `[claude].minimum_version` |
| `claude.session_prefix` (default: the Ship's name) | `"env": { "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX": ... }` (a fallback only; see "Session names") |

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
| `~/.claude/settings.json` | `skipDangerousModePermissionPrompt` (true exactly when `allow_bypass` is true); the mode-ledger hook entries for `UserPromptSubmit` and `SessionStart` (command `/opt/shipshape/hooks/mode-ledger`) |
| `~/.claude.json` | `hasCompletedOnboarding = true` (no first-run screens in unattended launches); `projects["<cwd>"].hasTrustDialogAccepted = true` for every autostart `cwd` |
| `~/.config/herdr/config.toml` | `onboarding = false`; `[update] version_check = false`; `[session] resume_agents_on_restore = true` |

### System

| ship.toml | Effect |
|---|---|
| `ship.name`, `ship.id` | `/etc/avahi/services/shipshape.service`: the LAN announcement (`ship-runtime.md`) |
| `ship.name`, `ship.tags` | `/etc/ssh/shipshape/principals/agent`: which user certificates may log in as `agent` (`agent:all`, `agent:tag:<tag>`, `agent:ship:<name>`; `ship-ctl.md`) |
| `agent.sudo` | `/etc/sudoers.d/91-shipshape-agent` present (`NOPASSWD: ALL`) or absent |
| `network.lan_guard`, `lan_guard_allow` | nftables table `inet shipshape_lan_guard` present or absent, loaded at boot by `ship-lan-guard.service` (see "LAN guard") |
| `updates.auto_reboot`, `updates.reboot_time` | `/etc/apt/apt.conf.d/52shipshape-unattended-upgrades`: `Unattended-Upgrade::Automatic-Reboot` and `Automatic-Reboot-Time` (see "OS updates") |
| `tools`, `profiles`, `profile_options` | `ship-ctl tools run` (see `tools.md`) |
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
Ship's firewall is touched. `ship-ctl apply` loads or removes the table and
enables or disables `ship-lan-guard.service`, which reloads it at every boot.

## OS updates

Ubuntu's unattended-upgrades stays on (security updates every day, as Ubuntu
ships it). `ship-ctl apply` writes
`/etc/apt/apt.conf.d/52shipshape-unattended-upgrades` in full:

```
Unattended-Upgrade::Automatic-Reboot "<updates.auto_reboot>";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "<updates.reboot_time>";
```

With `auto_reboot = false` (the default) a needed reboot shows as
`reboot-required` attention, and `ship-fleet reboot` does it when you choose.
An automatic reboot is a clean `shutdown -r`, so herdr saves its state and
resumes every agent in its mode, as after any reboot.

## Permission modes across reboots

Claude Code restores a resumed session's permission mode, except bypass, which it
drops. herdr resumes each session with a bare `claude --resume <id>`. The Ship
covers the gap:

- A hook records the session's mode in `~/.local/state/shipshape/modes.json`. On
  `UserPromptSubmit` it records the `permission_mode` Claude reports. On
  `SessionStart` it reads the mode from the launching `claude` process's own flags
  (`--dangerously-skip-permissions` or `--permission-mode`), so an agent started in
  bypass but never prompted is covered too; a launch without flags records nothing. The ledger:

  ```json
  { "schema": "shipshape.modes/1",
    "sessions": { "<session id>": { "mode": "bypassPermissions", "updated_at": "2026-01-01T00:00:00Z" } } }
  ```

- `/opt/shipshape/shim/claude`, first on herdr's PATH, passes every launch
  straight to `~/.local/bin/claude`. The one exception is `--resume <id>` for a
  session the ledger lists as `bypassPermissions` on a Ship with
  `allow_bypass = true`: there it adds `--dangerously-skip-permissions`.
- The status document reports each agent's `mode` from the ledger.

## How ships.toml becomes a Ship's ship.toml

The seed generator builds `ship.toml` like this, then validates it before writing
the seed:

| ship.toml | From ships.toml |
|---|---|
| `ship.name` | the `[ship.<name>]` key |
| `ship.id` | `ship.<name>.id` (a new random UUID when the Ship is created) |
| `ship.tags` | `ship.<name>.tags`, else `defaults.tags` |
| `claude` | `defaults.claude` overlaid with `ship.<name>.claude`; `session_prefix` defaults to the name |
| `tools.install` | `ship.<name>.tools`, else `defaults.tools` |
| `tools.profiles` | `ship.<name>.profiles`, else `defaults.profiles` |
| `profile_options` | for each profile: `profile_options.<profile>`. A missing required option stops seed creation with a clear message. |
| `autostart`, `agent`, `network` | from the `ship-fleet new` options; absent by default |

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

