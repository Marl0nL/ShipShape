# Ship runtime contract

What every provisioned Ship looks like, so FleetLedger can rely on it without
reading Ship-track code. The Ship track (W4 ship-provision) makes this true; the
Fleet track (W8 core, W9 concierge, W10 CLI) depends on nothing beyond it.

Version 1. Paths and names below are fixed; anything not listed is internal to
the Ship track and may change without notice.

## Users

| User | Purpose | Login | sudo |
|---|---|---|---|
| `fleet` | FleetLedger's admin account | SSH, only with the key from `ships.toml` `fleet.admin_key` (its `.pub`) | `NOPASSWD: ALL`, written by cloud-init from the seed (`/etc/sudoers.d/90-cloud-init-users`) |
| `agent` | Runs herdr and every agent; owns all sign-ins | SSH, with the keys in `ships.toml` `fleet.agent_keys` | none, unless `ship.toml` `agent.sudo = true` (`/etc/sudoers.d/91-shipshape-agent`) |

- No passwords; root login disabled. UIDs are assigned at first boot: resolve users by name.
- `agent` has lingering on (`loginctl enable-linger agent`), so its user services
  start at boot with nobody logged in.
- Home folders: `/home/fleet`, `/home/agent`.

## SSH server

`/etc/ssh/sshd_config.d/10-shipshape.conf` sets:

```
HostKey /etc/ssh/ssh_host_ed25519_key
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers fleet agent
AllowAgentForwarding no
```

The only host key is the ed25519 key from the seed (see `seed.md`). No other host
key types exist, so the key FleetLedger pinned is the only one a client can see.

## Paths

| Path | Owner, mode | What it is |
|---|---|---|
| `/etc/shipshape/ship.toml` | root, 0644 | The Ship's settings (`ship.toml.schema.json`). Changed only by `ship-ctl set` or a new seed. |
| `/opt/shipshape/releases/<tooling_version>-<commit12>/` | root, 0755 | Unpacked tooling bundles (`seed.md`). |
| `/opt/shipshape/current` | symlink | The active release. |
| `/usr/local/bin/ship-ctl` | symlink | `-> /opt/shipshape/current/bin/ship-ctl` |
| `/usr/local/bin/ship-axi` | symlink or wrapper | The ship-axi command (W7). |
| `/opt/shipshape/shim/claude` | root, 0755 | Resume shim; first on herdr's PATH (`settings.md`). |
| `/opt/shipshape/hooks/mode-ledger` | root, 0755 | Mode-ledger Claude hook (`settings.md`). |
| `/usr/local/bin/herdr` | root, 0755 | Pinned herdr from `versions.toml`. |
| `/var/lib/shipshape/` | root, 0755 | Ship state. Contents are internal, except the reserved names below. |
| `/var/lib/shipshape/bundle.tar.gz` | root, 0600 | The last bundle applied. |
| `/run/shipshape/status.json` | root, 0644 | Status cache: the same document as `ship-ctl status --json`, rewritten at least every 60 seconds. |
| `/var/log/shipshape/provision.log` | root, 0640 | Output of first boot and every converge (also in the journal). |
| `/etc/claude-code/managed-settings.json` | root, 0644 | Claude Code managed settings, written only by `ship-ctl apply`. |

Agent home (`/home/agent`), owned by `agent`:

| Path | What it is |
|---|---|
| `~/.local/bin/claude` | Claude Code native launcher. Never replaced: self-updates rely on it. |
| `~/.claude/settings.json` | Claude user settings. ship-ctl manages only the keys listed in `settings.md`. |
| `~/.claude/.credentials.json` | Claude sign-in, mode 0600. FleetLedger never reads it. |
| `~/.config/gcloud/` | gcloud user and ADC credentials. Never read by FleetLedger. |
| `~/.config/gh/hosts.yml` | GitHub CLI token, mode 0600. Never read by FleetLedger. |
| `~/.config/herdr/config.toml` | herdr config; ship-ctl manages the keys listed in `settings.md`. |
| `~/.local/state/shipshape/modes.json` | Mode ledger: Claude session id to permission mode (`settings.md`). |
| `~/.local` | npm global prefix: packages in `~/.local/lib/node_modules`, commands in `~/.local/bin`. |
| `~/work/` | Default root for project folders. |

Reserved, not created in phase 1: `/var/lib/shipshape/outbox/` and
`/var/lib/shipshape/inbox/` (ship-axi requests and messages, collected by
FleetLedger over SSH), and `/var/lib/shipshape/net/` (network visibility, phase 3).

## Services

| Unit | Scope | Purpose |
|---|---|---|
| `herdr.service` | agent user | `herdr server`, restarted on failure. Its PATH starts with `/opt/shipshape/shim`, then `~/.local/bin`. |
| `ship-resume.service` | agent user | One-shot after herdr: attaches a hidden client so herdr resumes agents, detaches, and records that this boot's resume is done. `agent` cannot run ship-ctl, so autostart follows from `ship-status` below, which waits for that record. |
| `ship-status.timer`, `ship-status.service` | system | Refresh `/run/shipshape/status.json` every 30 seconds, and react to sign-ins: run tools waiting on a newly signed-in service, and `ship-ctl autostart` once Claude becomes signed in (`ship-ctl.md`). |

Also present: `/etc/systemd/logind.conf.d/50-shipshape.conf`
(`InhibitDelayMaxSec=30`, so herdr can save on shutdown) and
`/etc/systemd/journald.conf.d/50-shipshape.conf` (`Storage=persistent`).

FleetLedger reads logs with `ship-ctl logs` (see `ship-ctl.md`), never by unit
name, so unit internals can change.

## Commands on the agent's PATH

`bash`, `git`, `curl`, `jq`, `tmux`, `python3`, `node`, `npm`, `uv`, `gh`,
`gcloud`, `claude`, `herdr`, `ship-axi`, plus whatever the installed recipes add.
Every login shell for `agent` has `~/.local/bin` first on its PATH.

## How FleetLedger reaches a Ship

All access is SSH as `fleet`, with `BatchMode=yes`, FleetLedger's own known-hosts
file, and agent forwarding off.

| Purpose | Command |
|---|---|
| Status poll | `sudo -n /usr/local/bin/ship-ctl status --json` |
| Any other ship-ctl command | `sudo -n /usr/local/bin/ship-ctl <command> ... --json` |
| Sign-ins (concierge) | `ssh -tt fleet@<ship> sudo -n -iu agent -- <command>` (a login shell, so `HOME` and `PATH` are the agent's) |
| Update tooling | upload the bundle into `/home/fleet/incoming/`, then `sudo -n /usr/local/bin/ship-ctl converge --bundle <path> --json` |
| Reboot | `sudo -n systemctl reboot` (never a forced reboot) |

After a sign-in FleetLedger triggers nothing: within a minute the Ship finishes
the tools that were waiting on it and, for Claude, starts its autostart agents.

An optional read-only poller key may be installed for `fleet` with
`restrict,command="sudo -n /usr/local/bin/ship-ctl status --json"`.

FleetLedger never reads the agent's credential files, never forwards an SSH
agent, and runs nothing as root except through ship-ctl and the reboot command
above.

Agents themselves read `/run/shipshape/status.json` (through ship-axi) and never
need ship-ctl.

## LAN announcement

avahi announces one DNS-SD service so `ship-fleet scan` can list Ships on the LAN:

- type `_shipshape._tcp`, port 22, instance name: the Ship's name;
- host: the Ship's mDNS name (`<ship name>.local`);
- TXT records `schema=1`, `name=<ship name>`, `id=<ship id>`.

Announcements are hints only. FleetLedger trusts a Ship solely through its pinned
host key; `ship-fleet adopt` shows the key's fingerprint for you to compare with
the one cloud-init prints on the VM console before anything is recorded.
