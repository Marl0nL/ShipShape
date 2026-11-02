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
| `claude.remote_control = "off"` | `"remoteControlAtStartup": false` |
| `claude.remote_control = "blocked"` | `"remoteControlAtStartup": false` and `"disableRemoteControl": true` |
| `claude.default_mode` | `"permissions": { "defaultMode": ... }` |
| `claude.allow_bypass = false` | `"permissions": { "disableBypassPermissionsMode": "disable" }` |
| `claude.update_channel` | `"autoUpdatesChannel"`, plus `"minimumVersion"` from `versions.toml` `[claude].minimum_version` |
| `claude.session_prefix` (default: the Ship's name) | `"env": { "CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX": ... }` |

Never set on a Ship: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` (it turns off
Remote Control), `DISABLE_AUTOUPDATER` or `DISABLE_UPDATES` (the native installer
must update itself), `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` (sign-in is
the agent's own `claude auth login`).

### Agent user files

ship-ctl merges only these keys and leaves everything else alone (herdr's
integration and agents edit these files too):

| File | Keys |
|---|---|
| `~/.claude/settings.json` | `skipDangerousModePermissionPrompt` (true exactly when `allow_bypass` is true); the mode-ledger `UserPromptSubmit` hook entry |
| `~/.claude.json` | `projects["<cwd>"].hasTrustDialogAccepted = true` for every autostart `cwd` |
| `~/.config/herdr/config.toml` | `onboarding = false`; `[update] version_check = false`; `[session] resume_agents_on_restore = true` |

### System

| ship.toml | Effect |
|---|---|
| `agent.sudo` | `/etc/sudoers.d/91-shipshape-agent` present (`NOPASSWD: ALL`) or absent |
| `network.lan_guard`, `lan_guard_allow` | nftables table `inet shipshape_lan_guard` present or absent (phase 2) |
| `tools`, `profiles`, `profile_options` | `ship-ctl tools run` (see `tools.md`) |
| `autostart` | used by `ship-ctl autostart`: at boot, once Claude becomes signed in, and on demand |

## Permission modes across reboots

Claude Code restores a resumed session's permission mode, except bypass, which it
drops. herdr resumes each session with a bare `claude --resume <id>`. The Ship
covers the gap:

- A `UserPromptSubmit` hook records the session's mode in
  `~/.local/state/shipshape/modes.json`:

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
