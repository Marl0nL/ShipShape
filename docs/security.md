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

## Off the LAN: the overlay network (phase 5)

Ships can join a Tailscale tailnet (or a Headscale server) so you reach them from
your other machines without forwarding router ports
([runbooks/remote-access.md](runbooks/remote-access.md)).

- **It adds a route, not a way in.** OpenSSH with the pinned host key and key-only
  logins stays the only way into a Ship. Tailscale SSH is never turned on.
- **Access rules give Ships no destinations.** The policy FleetLedger prints
  (`ship-fleet overlay policy`) lets your devices reach `tag:ship` on port 22 and
  allows nothing else, so a Ship cannot open a connection to your laptop or to
  another Ship over the overlay. A policy test guards the Ship-to-Ship rule.
- **One-off keys, after boot, over SSH.** Each join uses a single-use,
  pre-approved, tagged key that expires in minutes, pasted (read without echo) or
  minted by FleetLedger with an OAuth client. It reaches the Ship on stdin over
  SSH and exists there only in a root-only file under `/run` while `tailscale up`
  runs. Keys are never in seeds, cloud metadata, command lines, logs, the
  inventory or status, and nothing keeps a copy.
- **The OAuth client is as narrow as Tailscale allows.** By default it has only
  `auth_keys`: it can mint `tag:ship` keys and nothing else. Two opt-in levels
  add more: device removal on destroy (`devices:core`, which Tailscale grants over
  the whole tailnet: tags do not limit it, so a leaked secret could then remove
  or re-tag any device, yours included; FleetLedger itself only removes a device it can tie to the Ship by
  its recorded node id, never by hostname alone), and policy management
  (`policy_file`: FleetLedger only adds the Ships' rules, after a diff and your
  yes, but the secret could change any rule). `ship-fleet overlay
  setup` checks the client's scopes before it saves the secret (0600, never in
  `ships.toml`), and `overlay check` keeps verifying the rules
  ([guide/your-own-fleet.md](guide/your-own-fleet.md) compares the choices).
- **Cloud Ships** will need another first-contact path:
  [design/cloud-first-contact.md](design/cloud-first-contact.md).
- **Nothing runs until a Ship joins.** `tailscale` is installed (from its pinned,
  signature-checked repo) only by `ship-ctl overlay join`, with log upload to
  Tailscale turned off; it is stopped and disabled again on `leave`.
- **LAN logins can be turned off** per Ship (`ssh_listen=overlay`): sshd then
  denies every login that does not arrive on loopback or the Ship's own overlay
  addresses. ship-ctl refuses this until the Ship has joined, and turns it back
  off before leaving, so these commands cannot lock a Ship out.

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
| Overlay OAuth client secret (optional) | your computer: `~/.config/fleetledger/` (0600) | made once by you; can only mint `tag:ship` keys |
| Overlay join key | nowhere for long: the Ship's `/run` (tmpfs, root-only) while it joins | pasted, or minted per join; single use, minutes |
| Ship's overlay node key | the Ship: Tailscale's state in `/var/lib/tailscale` (root) | `tailscale up` |
| Your own key | your machines | public half put on `agent` by the seed |

Never in: the base image, seeds (apart from the Ship's own host key), FleetLedger
logs, shell history, command lines, or the repo.

### Baked images and snapshots

- A baked image holds no secrets: no users, keys, host keys, seeds or sign-ins
  (`contracts/image.md`). It is made only from Ubuntu's signed cloud image and
  the pinned packages, and its bake ends by removing cloud-init state, the
  machine id and any host keys, so two Ships from one image share nothing that
  identifies them. Its sha256 is recorded and checked before every publish.
- A snapshot is a copy of a Ship's disk, sign-ins included. FleetLedger takes
  disk-only snapshots (never memory), but treat them like the Ship: rolling back
  can restore a sign-in you have since revoked.
- FleetLedger only stops, rolls back or deletes a VM after checking its name is
  the Ship's (or, for an image, the template's), never by VM id alone.

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

## The web UI

`ship-fleet ui` listens on `127.0.0.1` only, admits a browser through a one-time
link swapped for a cookie, refuses foreign `Host` headers (DNS rebinding), and
needs a CSRF token and an exact `Origin` on every change and on the live
terminal's WebSocket. Pasted codes follow the concierge's rules above; request
logging is off. Details: [web-ui.md](web-ui.md#security).

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
| A leaked seed, snapshot or backup | impersonate a Ship (seed); reuse sign-ins (snapshot) | seeds deleted at enrolment and kept 0600 until then; disk-only snapshots, treated as sensitive; baked images hold no secrets; re-seed and `adopt --replace` (which also lists the old host key as revoked for trusted machines); host certificates last 90 days; sign Ships out from FleetLedger |
| A stale VM id in the inventory | destroy or roll back someone else's VM | every destructive driver call checks the VM's name first; the token reaches only the pool |
| Your computer is compromised | full control of the fleet and the Proxmox pool | passphrase-protected admin and CA keys in ssh-agent; scoped Proxmox token; no service tokens to steal |
| A trusted machine is lost or compromised | attach to the Ships in its scope as agent | short certificate lifetimes; scope per tag or Ship; `ship-fleet untrust` revokes it everywhere; certificates never reach `fleet` and allow no forwarding |
| Supply chain | a tampered tool on every Ship | Claude installed after checking the signed release manifest; signed apt repos with pinned keys; herdr pinned by checksum; recipes pin versions; verified Ubuntu images |
| Someone on your overlay network | reach a Ship from anywhere | access rules allow only your devices, only port 22; logins still need your key and the pinned host key |
| A compromised Ship on the overlay | reach your laptop or other Ships | the access rules give Ships no destinations; with `ssh_listen=overlay` the LAN is closed too |
| A leaked overlay key or OAuth secret | add a machine to your tailnet | join keys are single use and expire in minutes; the OAuth client can only mint `tag:ship` keys, which get no destinations; revoke it in the admin console |
| Your Claude accounts | anyone in one can drive that account's Ships through Remote Control | strong sign-in on your Claude accounts; Remote Control off per Ship where not needed (`ship-fleet set <ship> remote_control=off`, see [runbooks/sign-in.md](runbooks/sign-in.md#remote-control)) |
