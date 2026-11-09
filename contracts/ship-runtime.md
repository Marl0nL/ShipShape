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

With an `[owner]` in ship.toml, ship-ctl's own drop-in (the one it writes for
certificates) adds `AllowUsers <owner>` (sshd adds AllowUsers lines up) and
`AuthorizedKeysFile .ssh/authorized_keys /etc/ssh/shipshape/keys/%u`, so your own
login is let in beside `fleet` and `agent`, and no one else is. Certificates log in
to the owner login with principals of its own (`owner:all`, `owner:tag:<tag>`,
`owner:ship:<name>`, in `/etc/ssh/shipshape/principals/<owner>`), so a machine
trusted only to attach to the agent (`agent:*`) cannot log in as you.

The only host key is the ed25519 key from the seed (see `seed.md`). No other host
key types exist, so the key FleetLedger pinned is the only one a client can see.

From phase 3 the Ship can also carry SSH certificates from FleetLedger's
certificate authority, installed only by `ship-ctl ssh-trust` (`ship-ctl.md`):

| Path | Owner, mode | What it is |
|---|---|---|
| `/etc/ssh/ssh_host_ed25519_key-cert.pub` | root, 0644 | Host certificate for the pinned key, listing every name the Ship is reached by. |
| `/etc/ssh/shipshape/user-ca.pub` | root, 0644 | User CA public keys; certificates they sign may log in as `agent` only. |
| `/etc/ssh/shipshape/principals/agent` | root, 0644 | `agent:all`, `agent:tag:<tag>` per tag, `agent:ship:<name>` (from `ship.toml`). No file exists for `fleet`. |
| `/etc/ssh/shipshape/revoked-keys` | root, 0644 | Revocation list (KRL); applies to every user. |
| `/etc/ssh/sshd_config.d/20-shipshape-ca.conf` | root, 0644 | `HostCertificate`, `TrustedUserCAKeys`, `AuthorizedPrincipalsFile`, `RevokedKeys`, each only when its file exists. |

Plain keys keep working: `fleet` logs in with the admin key and `agent` with the
keys from the seed, certificates or not. A Ship without certificates behaves
exactly as in phase 1.

## Paths

| Path | Owner, mode | What it is |
|---|---|---|
| `/etc/shipshape/ship.toml` | root, 0644 | The Ship's settings (`ship.toml.schema.json`). Changed only by `ship-ctl set` or a new seed. |
| `/opt/shipshape/releases/<tooling_version>-<commit12>/` | root, 0755 | Unpacked tooling bundles (`seed.md`). |
| `/opt/shipshape/current` | symlink | The active release. |
| `/usr/local/bin/ship-ctl` | symlink | `-> /opt/shipshape/current/bin/ship-ctl` |
| `/usr/local/bin/ship-axi` | symlink or wrapper | The ship-axi command (W7). |
| `/opt/shipshape/hooks/mode-ledger` | root, 0755 | Mode-ledger Claude hook (`settings.md`). |
| `/opt/shipshape/bin/agent-launch` | root, 0755 | Starts Claude Code with the Ship's PATH, npm prefix and catalogue environment; every agent on a herdr-core Ship starts through it (`ship-ctl.md`, "On herdr-core Ships"). |
| `/usr/local/bin/herdr` | root, 0755 | Pinned herdr from `versions.toml`. |
| `/usr/local/bin/herdr-core`, `/usr/local/bin/hc` (symlink) | root, 0755 | Pinned herdr-core from `versions.toml`, installed beside herdr; root-owned so it cannot update itself past the pin. Nothing runs it yet. |
| `/var/lib/shipshape/` | root, 0755 | Ship state. Contents are internal, except the reserved names below. |
| `/var/lib/shipshape/bundle.tar.gz` | root, 0600 | The last bundle applied. |
| `/var/lib/shipshape/agents.json` | root, 0644 | The agent record: the Claude agents the Ship keeps running and resumes after a boot (`agent-record.schema.json`, `ship-ctl.md` "The agent record"). Written only by ship-ctl. |
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
| `~/inbox/` | The file inbox, mode 0700: files a person sends with `ship-fleet put` (or the web UI's Inbox tab) for the agents, each 0600 and never executable (`ship-ctl inbox`). Separate from `~/work/`, so a file sent is never inside a git checkout by accident. |
| `~/.local/state/shipshape/incoming/` | Internal, mode 0700: where `ship-ctl inbox put` writes a file until every byte is there, so `~/inbox/` never shows a half-written one. |

Phase 4 and 5 add:

| Path | Owner, mode | What it is |
|---|---|---|
| `/etc/shipshape/image.json` | root, 0644 | Present on Ships made from a baked image: its manifest (`image-manifest.schema.json`). |
| `/etc/ssh/sshd_config.d/20-shipshape-listen.conf` | root, 0644 | Present only when `network.ssh_listen = "overlay"` (`settings.md`). |
| `/var/lib/shipshape/overlay.json` | root, 0600 | Internal: the overlay record (`ship-ctl overlay`). Holds no key. |

`tailscaled` is installed only on Ships that joined the overlay, and is disabled
and stopped again when a Ship leaves.

Reserved, not created yet: `/var/lib/shipshape/outbox/` and
`/var/lib/shipshape/inbox/` (ship-axi requests and messages, collected by
FleetLedger over SSH).

`/var/lib/shipshape/net/` (root, 0700) holds the network visibility store that
`ship-netwatch` writes and `ship-ctl net` reads; its files are internal.

## Services

| Unit | Scope | Purpose |
|---|---|---|
| `herdr.service` | agent user | `herdr server`, restarted on failure. Its PATH starts with `~/.local/bin`. It restores its layout at start and resumes no agent itself. Enabled unless ship.toml says `runtime = "core"`. |
| `ship-core.service` | agent user | On Ships with `runtime = "core"`, instead of `herdr.service`: runs `hc server start` once at boot, with the Ship's environment. The core then runs as a user unit of its own; the status refresh relaunches the recorded agents. |
| `ship-status.timer`, `ship-status.service` | system | Refresh `/run/shipshape/status.json` every 30 seconds, and react to sign-ins: run tools waiting on a newly signed-in service, and `ship-ctl autostart` once Claude becomes signed in (`ship-ctl.md`). Each refresh also keeps the agent record in step: the first of a boot resumes every recorded agent, later ones start again an agent that crashed. |
| `ship-netwatch.service` | system | Network visibility collector (phase 3): records outbound connections and the DNS names they came from into `/var/lib/shipshape/net/`. Root, with `ProtectHome=yes`, so it cannot read or write any home folder. Observes only; blocks nothing. |
| `vnstat.service` | system | Ubuntu's vnstat daemon: per-interface byte counters for the totals. |
| `ship-lan-guard-early.service` | system | One-shot before `network-pre.target`: loads the stored LAN guard rules, so no interface is up without them (`settings.md`, "LAN guard"). Enabled only while `network.lan_guard` is on. |
| `ship-lan-guard.service` | system | One-shot once the network is online: `ship-ctl lan-guard` finds the gateways and DNS servers again and reloads the rules. Enabled only while `network.lan_guard` is on. |

Also present: `/etc/systemd/logind.conf.d/50-shipshape.conf`
(`InhibitDelayMaxSec=30`, so herdr can save its layout on shutdown) and
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
| Reboot | `sudo -n systemctl reboot` (a clean reboot, never `--force`) |
| Reboot past a shutdown lock (`ship-fleet reboot --force`) | `sudo -n systemctl reboot --check-inhibitors=no`: still clean, and delay locks (herdr's) still get their 30 s; only block locks are overridden |
| Why a reboot was refused | `python3 -I -` as fleet, without sudo, with the script `fleetledger/inhibitors.py` `PROBE` on stdin: logind's `ListInhibitors` (busctl), then `/proc` and `loginctl show-session` for each holder |

After a sign-in FleetLedger triggers nothing: within a minute the Ship finishes
the tools that were waiting on it and, for Claude, starts its autostart agents.

An optional read-only poller key may be installed for `fleet` with
`restrict,command="sudo -n /usr/local/bin/ship-ctl status --json"`.

FleetLedger never reads the agent's credential files, never forwards an SSH
agent, and runs nothing as root except through ship-ctl and the reboot commands
above.

### Shutdown locks

logind (systemd 257 and later; the Ship image has 259) refuses a reboot, root's
included, while any program holds a *block* lock on shutdown, unless the reboot
overrides it. The only such lock on a Ship is APT's ("APT is installing or
removing packages"): `apt`, `apt-get` and `unattended-upgrade` hold it while
they install or remove packages, until the last of their own processes ends. An
`apt` still going in an interactive login holds every reboot off until it ends
or that session is closed, including while it waits at a question (a changed
configuration file, a needrestart prompt) or runs its after-install hooks, when
it can look finished. herdr holds a *delay* lock: it never refuses, it only
delays the shutdown until herdr has saved (at most `InhibitDelayMaxSec`). Claude
Code holds none on Linux. When a reboot is refused FleetLedger reads the locks
as above and says what holds each one and where (the process, the user, the
login session or service), with the next step.

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
