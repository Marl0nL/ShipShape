# Security

Proportionate, not heavy: one way in (SSH), identities known in advance, secrets
left where they are created, and nothing that needs rework when access widens
from the LAN to other machines or cloud VMs.

## Transport and identity

- **SSH is the only way in.** Ships run no other listening service apart from
  the optional mDNS announcement. sshd takes keys only: no passwords, no keyboard
  interactive, no root login, only the users `fleet` and `agent`, no agent
  forwarding (`contracts/ship-runtime.md`).
- **Host keys are known before first boot.** FleetLedger generates each Ship's
  ed25519 host key, pins the public half in `ships.toml`, and puts the private
  half only into that Ship's seed. Cloud-init removes the image's own host keys,
  so the pinned key is the only one the Ship has. There is no trust-on-first-use
  and no unverified `ssh-keyscan` for Ships FleetLedger creates.
- **Every connection checks the pin.** FleetLedger writes its own
  `known_hosts` from the inventory and connects with
  `HostKeyAlias=<ship name>`, `StrictHostKeyChecking=yes` and ed25519 only, so
  the pinned key is checked whichever of the Ship's names or addresses is
  dialled. A different key stops the command at once; FleetLedger does not try
  the Ship's other names. Shared connections are keyed on the pinned key, so a
  re-pin never reuses a connection checked against the old one.
- **Ships say who they are.** A status document is used only if its Ship name and
  id match the inventory.
- **Adopting** a Ship FleetLedger did not create is the one place a key comes
  from the network. `ship-fleet adopt` shows the fingerprint and records nothing
  until you confirm it matches the one cloud-init printed on the VM console (or
  you pass it with `--fingerprint`). mDNS announcements are hints only.
- **Certificates (phase 3).** FleetLedger is a small SSH certificate authority
  with two keys on your computer: a host CA and a user CA. The host CA signs
  each Ship's *pinned* host key, taken from the inventory and never from the
  network, for every name the Ship is reached by. The Ship accepts a
  certificate only for its own key, and FleetLedger then proves the Ship
  answers a client that trusts only the CA. So the move from pinned keys to
  certificates never trusts an unverified key. Your other machines need one
  `@cert-authority` line, limited to the Ships' names.
- **Short-lived user certificates.** The user CA certifies your machines' own
  keys (`ship-fleet trust`), for a lifetime (168 hours by default) and a scope:
  all Ships, a tag, or one Ship. On each Ship, sshd accepts certificates for
  `agent` only: only agent has an `AuthorizedPrincipalsFile`, so no certificate
  can log in as `fleet`. A certificate allows a terminal and nothing else (no
  forwarding). `ship-fleet untrust` revokes a machine on every Ship through a
  key revocation list. Narrowing a machine's scope revokes, by serial, the
  certificates it was given for the wider scope. The Ship refuses any list that would revoke fleet's own
  key, so FleetLedger cannot lock itself out. Every change on the Ship is
  checked with `sshd -t` and rolled back if sshd would refuse it.
  See [runbooks/trust-machines.md](runbooks/trust-machines.md).
- **Two keys, two users.** The fleet admin key logs in only as `fleet` (with
  `IdentitiesOnly`). Your own key logs in only as `agent`, for attaching. Never
  forward your SSH agent to a Ship: FleetLedger turns forwarding off on every
  connection, and Ships use their own GitHub sign-in instead.

## Least privilege

- **FleetLedger holds no service tokens.** It has SSH keys, the Proxmox API token
  and the inventory, nothing for Claude, Google or GitHub.
- **The Proxmox token reaches one pool, one seed storage, the VM disk storage
  and one bridge** ([runbooks/proxmox-setup.md](runbooks/proxmox-setup.md)). Its
  file must be mode 0600 or FleetLedger refuses it. The API's TLS certificate is
  pinned by SHA-256, and the pin is checked before the token is sent.
- **Agents run as `agent` with no sudo** unless a Ship turns it on
  (`--agent-sudo`). They can read their own sign-ins and, with Claude Code's
  native installer, replace their own Claude binary; neither reaches beyond
  that Ship.
- **Root-owned, out of agents' reach:** Claude Code's managed settings (Remote
  Control, the bypass policy), herdr's binary, `ship.toml`, the resume shim,
  and the network visibility collector and its records ([network.md](network.md)).
  The collector runs with `ProtectHome=yes` and never touches a home folder.
- **Bypass is opt-in per Ship.** Without `allow_bypass`, managed settings disable
  bypass mode outright.
- **The LAN guard is opt-in per Ship** (`lan_guard=true`): an nftables table that
  refuses new outgoing connections to private and overlay ranges except the
  gateway, DNS and `lan_guard_allow`. It is visibility's counterpart, not a
  sandbox: an agent with sudo can remove it, so pair it with sudo off. Incoming
  SSH (FleetLedger, herdr attach) is never affected. At boot the stored rules
  load before the network comes up, so agents never start with the LAN open.

## Where each secret lives

| Secret | Lives in | Gets there by |
|---|---|---|
| Claude sign-in | the Ship: `~agent/.claude/.credentials.json` (0600) | the concierge; only the one-time code crosses the network |
| gcloud user and ADC | the Ship: `~agent/.config/gcloud/` | the concierge |
| GitHub token | the Ship: `~agent/.config/gh/hosts.yml` (0600) | device code via the concierge |
| Ship host private key | the Ship: `/etc/ssh`; briefly the seed ISO | generated into the seed; FleetLedger keeps only the public half |
| Fleet admin key | your computer: `~/.ssh`, passphrase, ssh-agent | `ship-fleet init` |
| Host and user CA keys | your computer: `~/.ssh`, passphrase, ssh-agent (FleetLedger signs through the agent and never reads them) | `ship-fleet ca init` |
| A trusted machine's key | that machine only; FleetLedger gets the public half | made there; `ship-fleet trust` certifies it |
| Proxmox API token | your computer: `~/.config/fleetledger/` (0600) | made once by you |
| Your own key | your machines | public half put on `agent` by the seed |

Never in: the base image, seeds (apart from the Ship's own host key), FleetLedger
logs, shell history, command lines, or the repo.

### Seeds

A seed ISO holds the Ship's private host key, so:

- FleetLedger's local copy is mode 0600 in a 0700 folder;
- on Proxmox it goes to a storage used for nothing else;
- at enrolment FleetLedger ejects it and deletes both the uploaded and the local
  copy, and reports success only when Proxmox confirms the ISO is gone;
- with the manual driver, FleetLedger prints the commands to remove it.

The private key is generated by `ssh-keygen` in a private temporary folder that
is removed straight away; it is never written anywhere else.

## The login concierge

- Each tool's own sign-in runs on the Ship, as `agent`, in a terminal over SSH.
- The code you paste is read without echo and typed into that terminal: it is
  never on a command line (so not in `ps` or shell history), in an argument, in
  a log or in an exception. A code pasted after its sign-in window has closed is
  not typed.
- No terminal transcript is kept. On failure a few lines are shown with typed
  codes, URL query strings and long token-like strings removed.
- The audit log (`~/.local/share/fleetledger/audit.log`, 0600) records time,
  Ship, service, outcome and account, nothing else.
- After every sign-in, and on every status poll, the account is compared with
  the one the Ship's tags expect. A mismatch is reported as a wrong account.

## Public repository

Nothing committed may point at a person, their work, machines or network:
`scripts/hygiene.py` checks every file and commit message, with a private
deny-list kept outside the repo ([hygiene.md](hygiene.md)).

## Threat model

| Threat | What it could do | What limits it |
|---|---|---|
| A misbehaving or prompt-injected agent (more so in bypass) | use or leak that Ship's sign-ins; push to repos you can reach; probe your LAN | separate sign-ins per Ship (sign one out without touching others); no sudo; bypass only where allowed; no admin keys or agent forwarding on Ships; the network visibility record (LAN destinations included) for review afterwards; the optional LAN guard (`ship-fleet set SHIP lan_guard=true`) refuses new connections to private addresses other than the gateway and DNS, so Ships of different tags cannot reach each other |
| A Ship on the wrong account | work done with personal credentials, or the reverse | expected accounts per tag, checked after every sign-in and on every poll |
| Someone else on your LAN | fake an mDNS announcement; answer at a Ship's name; try passwords | pinned host keys; key-only SSH; announcements are hints only |
| A leaked seed, snapshot or backup | impersonate a Ship (seed); reuse sign-ins (snapshot) | seeds deleted at enrolment and kept 0600 until then; treat snapshots as sensitive; re-seed and `adopt --replace` (which also lists the old host key as revoked for trusted machines); host certificates last 90 days; sign Ships out from FleetLedger |
| Your computer is compromised | full control of the fleet and the Proxmox pool | passphrase-protected admin and CA keys in ssh-agent; scoped Proxmox token; no service tokens to steal |
| A trusted machine is lost or compromised | attach to the Ships in its scope as agent | short certificate lifetimes; scope per tag or Ship; `ship-fleet untrust` revokes it everywhere; certificates never reach `fleet` and allow no forwarding |
| Supply chain | a tampered tool on every Ship | Claude installed after checking the signed release manifest; signed apt repos with pinned keys; herdr pinned by checksum; recipes pin versions; verified Ubuntu images |
| Your Claude accounts | anyone in one can drive that account's Ships through Remote Control | strong sign-in on your Claude accounts; Remote Control off per Ship where not needed (`ship-fleet set <ship> remote_control=off`, see [runbooks/sign-in.md](runbooks/sign-in.md#remote-control)) |
