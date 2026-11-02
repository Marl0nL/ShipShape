# fleetledger/: the manager (`ship-fleet`)

```sh
fleetledger/install.sh        # pipx install --editable; rerun after dependency changes
ship-fleet init               # fleet admin key + empty ~/.config/fleetledger/ships.toml
ship-fleet new ship-01 --tag work
ship-fleet status ship-01 --wait
ship-fleet login ship-01 all
ship-fleet ls
ship-fleet attention          # what needs you, across the fleet
ship-fleet update --tag work  # roll this checkout's tooling and pins out
```

Owned by the **Fleet track** (W3, W8, W9, W10). A Python package installed with
pipx; the command is `ship-fleet`. It runs on your own computer and reaches Ships
only over SSH, as `contracts/ship-runtime.md` describes.

## Modules

All of phase 1's Fleet track is built: W3, W8, W9 and W10.

| Module | Work item | Purpose |
|---|---|---|
| `fleetledger/seed/` | W3 | host keys, `ship.toml` rendering, the cloud-init files, bundle packing, seed ISOs (`contracts/seed.md`) |
| `fleetledger/inventory.py`, `store.py` | W8 | `ships.toml` (`contracts/ships.toml.schema.json`), tags, expected accounts |
| `fleetledger/catalogue.py` | W3, W10 | reads recipes and profiles from `ship/tools/` and `ship/profiles/` (`contracts/tools.md`) |
| `fleetledger/repo.py`, `schemas.py`, `paths.py` | all | the checkout FleetLedger runs from, contract validation, local folders |
| `fleetledger/ssh.py` | W8 | system OpenSSH, FleetLedger's own known-hosts file, connection reuse |
| `fleetledger/status.py`, `shipctl.py` | W8 | polling `ship-ctl status --json` (`contracts/ship-status.schema.json`) and calling the other ship-ctl commands |
| `fleetledger/drivers/` | W8, phase 4 | the hypervisor driver interface; `manual` (prints the steps) and `proxmox` (API: create, power, snapshots, image templates, destroy; every destructive call checks the VM's name). All hypervisor-specific code lives here. |
| `fleetledger/images.py` | phase 4 | baked images: fetching and checking Ubuntu's image, ids and manifests, running `ship/image/bake` (`contracts/image.md`) |
| `fleetledger/lifecycle.py` | phase 4 | `ship-fleet destroy`, `snapshot` and `image` |
| `fleetledger/enrol.py` | W8 | from a booted seed to an enrolled Ship (`contracts/seed.md`) |
| `fleetledger/concierge/`, `providers/*.toml` | W9 | the login concierge: terminal engine, providers, account checks, audit log |
| `fleetledger/web/` | phase 2 | the local web UI, `ship-fleet ui` ([docs/web-ui.md](../docs/web-ui.md)) |
| `fleetledger/cli.py`, `commands.py`, `scan.py` | W10 | the `ship-fleet` commands; LAN discovery and host-key fetching for `scan` and `adopt` |
| `fleetledger/pins.py` | phase 2 | `ship-fleet pins`: check and move the `versions.toml` pins with their checksums and signatures |
| `fleetledger/ops.py` | phase 2 | day-2 operations on one Ship or many (update, reboot, settings, tags, tools, logs, Needs attention), shared by the CLI and the web UI |
| `fleetledger/net.py`, `netcmd.py` | phase 3 | network visibility: fetch and cache Ships' summaries, first-seen ledger, the Network tab's data, `ship-fleet net` (`docs/network.md`) |
| `fleetledger/ca.py`, `trustcmd.py` | phase 3 | the SSH certificate authority: host and user certificates, revocation list, trust bundles; `ship-fleet ca`, `trust`, `untrust` (`docs/runbooks/trust-machines.md`) |

Tests live in `fleetledger/tests/` (pytest; `pytest.ini` lists the folder). They
use throwaway keys, a temporary FleetLedger home and a small fake checkout; they
never touch your real config, keys or network.

## Files on your computer

Nothing personal lives in the repo (`docs/hygiene.md`). FleetLedger keeps:

| Path | What it is |
|---|---|
| `~/.config/fleetledger/ships.toml` | the inventory: Ships, pinned host keys, tags, expected accounts (mode 0600) |
| `~/.config/fleetledger/known_hosts` | FleetLedger's own known-hosts file, written from the inventory |
| `~/.local/share/fleetledger/seeds/` | seed ISOs (folder 0700, files 0600) until each Ship enrols |
| `~/.local/share/fleetledger/audit.log` | the concierge's audit log: time, Ship, service, outcome, account (0600) |
| `~/.local/share/fleetledger/status/` | the last good status per Ship (0600), for `ls --offline` |
| `~/.local/share/fleetledger/net/` | the last network summary per Ship and the first-seen ledger (0600) |
| `~/.local/share/fleetledger/ca/` | the current revocation list, cached so its hash stays stable (0600) |
| `~/.local/share/fleetledger/trust/<machine>/` | the last bundle made for a trusted machine (0600) |
| `~/.local/share/fleetledger/images/` | baked images and their manifests (0600); `base/` caches Ubuntu's verified cloud image |

`FLEETLEDGER_CONFIG_DIR`, `FLEETLEDGER_DATA_DIR` and `FLEETLEDGER_RUNTIME_DIR`
override these (the XDG variables are honoured too). `SHIPSHAPE_REPO` points
FleetLedger at a different checkout.

## The inventory

`ships.toml` is the single source of truth; the schema and a full example are in
`contracts/`. FleetLedger edits it with `tomlkit`, so comments and layout
elsewhere in the file survive its edits. A nested section it changes (such as
a Ship's `claude` settings or `driver_ref`) is rewritten as an inline table, and
comments inside that section are lost. Every change takes a lock, is validated against the schema plus a few
rules the schema cannot express (no two Ships share a name, id or host key), and
is written atomically. An invalid change leaves the file untouched.

A Ship without a `tags` entry has `defaults.tags`; `tags = []` means no tags.
Expected accounts come from those tags (`[tag.<tag>]`). `gcloud_adc`
follows `gcloud`. Account names compare case-insensitively. If two of a Ship's
tags name different accounts for one service, every account is reported as wrong
until you fix the tags.

## Seeds (W3)

`fleetledger.seed.make_seed(inventory, ship, repo)` writes one ISO per seed:

1. **ship.toml** from the inventory, exactly as `contracts/settings.md` maps it:
   tags fall back to `defaults.tags`; `claude` is `defaults.claude` overlaid with
   the Ship's own, with `session_prefix` defaulting to the Ship's name; tools are
   the Ship's, else `defaults.tools`, else the catalogue's `default = true`
   recipes; profile options are copied from `[profile_options]` and checked
   against the profile (a missing required option stops). The result is
   validated against the schema before anything is written.

   Tools follow `contracts/settings.md` exactly: a list replaces, it does not
   add. A Ship's own `tools` wins outright; without one, `defaults.tools` wins;
   only when neither exists do the catalogue's `default = true` recipes apply.
   So a Ship with `tools = ["tasks-axi"]` gets only `tasks-axi`; list the
   defaults too if you want them.
2. **The tooling bundle**: `ship/` from this checkout (tracked and untracked but
   not ignored files; never `ship/tests/`, `node_modules/`, caches or symlinks),
   plus `bundle.json` and `versions.toml`. It records the commit and whether
   `ship/` or `versions.toml` had uncommitted changes. Entries are sorted, owned
   by root and timestamped from `created_at`, so packing is reproducible.
   `ship/provision/ship-provision` and `ship/bin/ship-ctl` must exist and be
   executable; the bundle must stay under 5 MiB.
3. **A new ed25519 host key** from `ssh-keygen`, made in a private temporary
   folder that is removed straight away. The private half exists only inside the
   ISO; the caller pins the public half in `ships.toml`.
4. **cloud-init files**: `user-data` with exactly the contract's top-level keys
   and the contract's bootstrap script, `meta-data` with a fresh
   `<name>-<8 hex>` instance id, and DHCP `network-config`. Public keys come
   from `fleet.admin_key` + `.pub` and `fleet.agent_keys`; a file that holds a
   private key is refused.
5. **The ISO**: ISO 9660 + Joliet + Rock Ridge, label `CIDATA`, built with
   pycdlib (no host tools), written with mode 0600 as
   `seeds/<instance id>.iso`.

The tests check each step against the contract text itself (the bootstrap script
and `network-config` are compared with `contracts/seed.md`), run that bootstrap
script on a real bundle in a temporary folder, and read the ISO back.

## Reaching Ships (W8)

`ssh.py` runs the system's `ssh`, so your SSH config and agent apply. FleetLedger
adds only options that make each connection safe and quick:

- **Pinned host keys, never learned.** `known_hosts` is rewritten from
  `ships.toml` (one line per Ship, keyed by Ship name), and every connection uses
  `HostKeyAlias=<ship name>`, `StrictHostKeyChecking=yes` and
  `HostKeyAlgorithms=ssh-ed25519`. Whichever of the Ship's names is dialled, its
  key must be the pinned one. A different key raises `HostKeyMismatch` straight
  away; FleetLedger does not try the Ship's other names, because that is a
  security event, not a network one.
- **One key per user.** The fleet admin key (`IdentitiesOnly`) logs in as
  `fleet`. Agent, X11 and port forwarding are always off.
- **Names in order.** Names that cannot be reached are skipped; the first one
  that answers is used.
- **Shared connections.** `ControlMaster` sockets live in the runtime folder and
  persist for 60 seconds. A shared connection skips the key exchange, so each
  socket's name includes a hash of the pinned key. After a re-pin, the next
  command opens a new connection and is checked against the new key; it never
  reuses one that was checked against the old key. If the folder's path is too
  long for a Unix socket, sharing is turned off rather than failing.

Remote commands follow `contracts/ship-runtime.md`: `sudo -n
/usr/local/bin/ship-ctl ... --json` for ship-ctl, and `sudo -n -iu agent -- bash
-c '<command>'` for anything that must run as the agent.

## Status (W8)

`status.fetch` asks one Ship for `ship-ctl status --json`; `status.poll` asks
many in parallel (8 at a time). FleetLedger only trusts a status document if
three things hold: it validates against the contract, it is a status document
(not a failed result), and its `ship.name` and `ship.id` match the inventory.
Otherwise the Ship is shown with a problem (`unreachable`, `host-key`,
`identity`, `invalid`, `error`) instead of its claims. Before `ship-ctl` is
installed (first boot), `cloud-init status --format json` stands in.

On top of the Ship's own attention items, FleetLedger adds:

| Code | When |
|---|---|
| `signin-wrong-account` | a signed-in service's account is not the one the Ship's tags expect |
| `behind-tooling` | the Ship's tooling is older than `versions.toml` `[shipshape].tooling` |
| `tags-differ` | the Ship's own tags differ from the inventory's |

The last good document per Ship is cached at
`~/.local/share/fleetledger/status/<ship>.json` (0600).

`shipctl.ShipCtl` runs every other ship-ctl command and checks the result
against `ship-ctl-result.schema.json`. Before its first call to a Ship it runs
`ship-ctl version` and refuses any contract major version it does not know.

## Hypervisor drivers (W8)

`drivers/base.py` is the interface: `create`, `start`, `stop`, `reboot`,
`destroy`, `status`, `finish_enrolment`, and optionally
`host_key_fingerprints` and `snapshot`. Every call returns an `Outcome`, which
is either done or a list of instructions for you, so the CLI treats both drivers
alike.

- **manual** prints the steps. With a `[drivers.proxmox]` section in
  `ships.toml` they are exact `qm` and `pvesm` commands for that server;
  otherwise they are generic and work for any hypervisor.
- **proxmox** is the phase 1 test helper; phase 4 finishes it. It uses only the
  scoped API token: it uploads the seed ISO (Proxmox checks a SHA-256 the
  client sends with it), full-clones the template into the pool, attaches the
  seed on `ide2`, sets cores, memory and bridge, grows `scsi0`, starts the VM,
  and waits for every Proxmox task. It reads addresses from the guest agent and
  destroys the VM again. If creation fails part-way, it removes what it made.
  At enrolment it ejects the seed media (applied at once on a running VM) and
  deletes the ISO. A VM or volume counts as already gone only when Proxmox says
  it "does not exist". Proxmox answers HTTP 500 for most failures (locked, in
  use, storage offline), so any other error is raised: a seed ISO holds the
  Ship's private host key and must not be reported deleted when it is not.
  The client pins the SHA-256 fingerprint of the API's TLS certificate and
  checks it before the token is sent. The token is read from its file (which
  must be 0600) for each request and never appears in errors.

## Enrolment (W8)

`enrol.enrol` waits for `provision.result` to be `ok` (retrying while the Ship is
still booting, and stopping at once on a wrong host key or a Ship that says it
is another one). It then has the driver
detach the seed CD-ROM and delete the uploaded ISO, deletes FleetLedger's own
copy of the seed, and records `ship.<name>.enrolled`. With the manual driver the
detach-and-delete steps are printed, and the seed's name stays in `driver_ref`
so `destroy` can remind you of it.

## Live test against Proxmox

`tests/test_live_proxmox.py` runs only when `FLEETLEDGER_LIVE_PROXMOX` names a
`ships.toml` with a `[drivers.proxmox]` section. It creates a Ship from a seed
with a stub `ship-provision`, reaches it over SSH with the pinned key, enrols it,
checks that an impostor key is refused, destroys it, and checks that nothing of
its own is left in the pool. It never runs in CI.

```sh
FLEETLEDGER_LIVE_PROXMOX=~/.config/fleetledger/ships.toml \
  python -m pytest fleetledger/tests/test_live_proxmox.py -v
```

The real-sshd tests in `tests/test_ssh.py` need an `sshd` binary. They use
`sshd` from `PATH`, `/usr/sbin/sshd`, or `FLEETLEDGER_TEST_SSHD`, and are skipped
if none is found.

## The login concierge (W9)

`ship-fleet login ship-01 claude` (or `gcloud`, `gcloud-adc`, `github`, `all`;
or `--tag work` for every Ship with a tag):

1. **Preflight.** A plain SSH call checks the pinned host key and opens the
   shared connection that the sign-in terminal reuses.
2. **The tool's own sign-in, in a terminal.** `ssh -tt fleet@<ship> sudo -n -iu
   agent -- bash -c '<command>'` in a pseudo-terminal 4096 columns wide, so
   links never wrap.
3. **Matching.** The engine reads the output itself and strips escape sequences
   (colours, OSC 8 hyperlinks); a sequence split across two reads waits for its
   other half. It matches the provider's patterns, and accepts a link or code
   only once more output follows it, so a link is never cut short. A provider
   can answer fixed prompts (`[match.send]`), each once. gh runs with
   `GH_PROMPT_DISABLED=1`: its interactive prompts wait for a real terminal's
   cursor report. Every gcloud command runs with
   `CLOUDSDK_CORE_DISABLE_FILE_LOGGING=1`: gcloud's own debug log would
   otherwise store the sign-in code.
4. **You.** FleetLedger shows the link and how long it stays valid, plus the
   code for device flows. Paste-back flows read your code with `getpass`, so it
   is not echoed, and type it into the Ship's terminal. It is never on a command
   line, in `ps`, or in shell history. A code pasted after the sign-in window
   has closed is not typed. Once a code is typed, the engine waits for the
   tool's verdict even if the window closes meanwhile. Device flows show a
   countdown until gh finishes.
5. **Checks.** Each provider's `[[check]]` commands run as agent (for example
   `claude auth status --json`, or `gcloud auth list` plus an ADC token test
   sent to `/dev/null`). The accounts are compared with the Ship's tags, so
   signing a work Ship in to your personal account is reported as
   **WRONG ACCOUNT**, with the `logout` command to undo it.
6. **Audit.** One line per login or logout: time, Ship, service, outcome and the
   account. Nothing else: no codes, links or terminal output.

If a pattern misses (a tool changed its wording), you are offered the live
terminal to finish by hand; the checks still run afterwards. No transcript is
kept: the output buffer lives in memory for the flow only. What is shown on
failure is a few redacted lines (escape sequences stripped first, then typed
codes, URL query strings, email addresses and long token-like strings such as
UUIDs removed).

Providers are one TOML file each in `fleetledger/providers/`: the command, mode
(`paste-back` or `device-code`), timeout, `[match]` patterns (`url`, `code`,
`prompt`, `success`, `failure`, `send`), `[[check]]` entries, `after` commands
and `[logout]`. Adding a service is one file.

The pattern tests replay sessions in a real pseudo-terminal from
`tests/fixtures/concierge/`. The claude, gcloud and github sessions follow the
wording recorded from the real tools in phase 0 spike S5 (example values
only); the rest are variants for the engine's edge cases (see that folder's
README).

## The CLI (W10)

`ship-fleet --help` lists every command; each has its own `--help`.

| Command | What it does |
|---|---|
| `init` | makes the fleet admin key (with a passphrase unless `--no-passphrase`) and an empty inventory |
| `new NAME` | pins a new host key, writes the seed, records the Ship, and has the driver create it. Options: `--tag`, `--mode` (bypass implies `allow_bypass`), `--tools`, `--profile`, `--autostart NAME:CWD[:MODE]`, `--agent-sudo`, size, `--driver manual\|proxmox`. With proxmox it waits and enrols. If the driver fails, the inventory entry and seed are removed. |
| `adopt NAME` | for a Ship FleetLedger did not create (or to re-pin one with `--replace`): fetches its host key and shows the fingerprint for you to compare with the VM console (or checks `--fingerprint`). It then asks the Ship who it is over a connection pinned to that key, and records it. |
| `scan` | lists `_shipshape._tcp` announcements and whether each is in the inventory (hints only) |
| `ls` | one row per Ship: state, agents, the four sign-ins (OK / Missing / Wrong account ...), cpu · memory · disk, and the first note. `--tag`, `--json`, `--offline`. |
| `status NAME` | the detail for one Ship. `--wait` waits for provisioning and finishes enrolment. |
| `login`, `logout` | the concierge, above |
| `set NAME KEY=VALUE...` | `ship-ctl set` with short keys (`remote_control=off`, `default_mode=...`). The inventory is kept in step. `default_mode=bypassPermissions` also sets `allow_bypass=true`. |
| `tag NAME +work -personal` | changes tags in the inventory and on the Ship (`--local` for the inventory only); `none` clears them |
| `tools ls`, `tools add/remove NAME ID...` | the catalogue, and `ship-ctl tools add/remove` |
| `ssh NAME [--as fleet] [-- CMD]` | a shell as agent with your own keys, or as fleet with the admin key; pinned host key either way |
| `logs NAME SOURCE` | `ship-ctl logs` (provision, herdr, resume, tools, status, system) |

Every command is tested in `tests/test_cli.py` against a fake Ship behind a real
`SSH` object, so the command lines are real but nothing leaves the machine.

## Names

Ships are reached by the names in `ships.toml`, tried in order: `<ship>.local`
by default (each Ship announces itself over mDNS). Your computer needs mDNS name
resolution for `.local` names (`libnss-mdns` on Ubuntu), or give Ships a DNS name
with `ship-fleet new --host`. When the proxmox driver brings a Ship up and none
of its names resolves on this computer, `new` adds the address the hypervisor
reports as the last name to try, so creation still finishes. The host key is
pinned either way (`HostKeyAlias`), so an address is as safe to dial as a name.
See also `docs/runbooks/create-ship.md`.

## Day-2 operations (phase 2)

`fleetledger/ops.py` holds every action that changes running Ships, so the CLI
and the web UI behave the same. Each function takes an `ops.Fleet` (the
inventory store and the ways to reach Ships), streams progress through
`on_event(ship, text)`, and returns one `ops.Outcome(ship, ok, summary, details,
data)` per Ship. One Ship failing never stops a batch; a request that is wrong
for every Ship (an unknown setting or tool) is refused before any Ship is touched.

| Command | Function | What it does |
|---|---|---|
| `ship-fleet update SHIP...\|--tag T\|all` | `ops.update` | packs `ship/` and `versions.toml` from this checkout, copies the bundle and its `.sha256` into `/home/fleet/incoming/` (mode 0600), runs `ship-ctl converge`, then checks the Ship reports the new bundle. `--reboot auto` (default) reboots only Ships whose herdr binary changed (`herdr-restart-pending`); `always` and `never` do what they say. A Ship already on a clean bundle is skipped unless `--force`. |
| `ship-fleet reboot SHIP...\|--tag T\|all` | `ops.reboot` | records each agent's name and mode, runs `sudo -n systemctl reboot` over the one name that answered, waits until the Ship answers from a new boot (its uptime is shorter than the wait), then until herdr has resumed. Fails the Ship if any agent is missing or came back in another mode. |
| `ship-fleet set SHIP...\|--tag T\|all KEY=VALUE...` | `ops.set_settings` | `ship-ctl set`; new keys `lan_guard_allow`, `auto_reboot`, `reboot_time` (the Ship's local time), `timezone`. `ship-fleet new` gives each Ship this computer's zone (`--timezone` to choose). |
| `ship-fleet tag SHIP +T -T` | `ops.set_tags` | inventory first, then the Ship (`--local`: inventory only). |
| `ship-fleet tools add\|remove SHIP\|all ID...` or `--tag T ID...` | `ops.change_tools` | `ship-ctl tools add\|remove`. |
| `ship-fleet logs [SHIP...\|--tag T\|all] SOURCE` | `ops.logs`, `ops.fleet_logs` | `ship-ctl logs`; with several Ships each line is prefixed by its Ship. |
| `ship-fleet attention [--tag T] [--watch [S]]` | `ops.attention` | every attention item across the fleet plus unreachable or untrusted Ships, most urgent first (`ops.SEVERITY`). `--watch` polls and prints only new items. |

`update` and `reboot` on more than one Ship ask first (or need `--yes` when not
run from a terminal). `ship-fleet status SHIP` also shows herdr's version and last
resume, the bundle commit, provisioning and pending OS updates.

`ship-fleet pins` (`fleetledger/pins.py`) checks and moves the pins in this
checkout's `versions.toml`:

| Pin | `pins set` checks |
|---|---|
| `herdr`, `uv` | each architecture's asset is downloaded and hashed; the hash must equal the SHA-256 digest GitHub publishes for it |
| an npm package | the exact version exists in the registry |
| `ubuntu BUILD` | `SHA256SUMS` carries a good signature from the pinned key (fetched by fingerprint, fingerprint checked again, `gpgv` with that key alone); the image hashes come from it |

The file is edited with tomlkit (comments stay) and must still validate against
`contracts/versions.schema.json`; nothing is written if any check fails.

How to roll out a new pin: `docs/runbooks/update-fleet.md`.

