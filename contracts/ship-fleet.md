# ship-fleet output other programs read

`ship-fleet` is FleetLedger's command line. Most of what it prints is for people, and
its format is not a contract. The documents below are: another program (a panel in a
terminal app, say, over `ssh <FleetLedger computer> ship-fleet ...`) may read them,
and they change only as `README.md` says (an optional field added keeps the version;
anything else bumps it).

Each `--json` prints exactly one JSON document on stdout and nothing else (`attention
--watch` prints one per poll); what went wrong goes to stderr. A command that lists
prints a JSON list, `[]` when there is nothing to list (no Ships yet, say).

## Versions

Every document names its shape in a `schema` field, `"shipshape.<contract>/<version>"`:
`<contract>` is its schema's file name in this folder less `.schema.json`, and
`<version>` a whole number. A list carries it on each entry (an empty list has no
shape to read). A reader checks it first and refuses a document, or an entry, whose
`schema` it does not know, or that has none (a FleetLedger older than the document).
An optional field added keeps the version; anything else bumps it, as `README.md` says.

| Command | Prints | `schema` |
|---|---|---|
| `ls [--tag TAG] [--offline] --json` | a list, one per Ship | `shipshape.fleetledger-ship/1` |
| `status NAME --json` | one Ship | `shipshape.fleetledger-ship/1` |
| `attention [--tag TAG] [--offline] --json` | a list | `shipshape.fleetledger-attention/1` |
| `signins [--tag TAG] [--service S] [--group G] [--offline] --json` | a list | `shipshape.fleetledger-signin/1` |
| `signins --accounts [--tag TAG] --json` | one document | `shipshape.fleetledger-tag-accounts/1` |
| `ca show --json` | one document | `shipshape.fleetledger-ca/1` |
| `doctor [--offline] --json` | one document | `shipshape.fleetledger-doctor/1` |
| `doctor --next --json` | one document | `shipshape.next-steps/1` |
| `snapshot ls NAME --json` | a list | `shipshape.fleetledger-snapshot/1` |
| `about [--check] --json` | one document | `shipshape.fleetledger-about/1` |
| `machine-bundle NAME` (always JSON) | one document | `shipshape.machine-bundle/1` |
| `machine-pair` (the pairing key's forced command; always JSON) | one document | `shipshape.machine-pair/1` |
| `keys ls --json` | a list, one per key line | `shipshape.fleetledger-key/1` |
| `looks show --json` | one document | `shipshape.fleetledger-looks/1` |

`--offline` asks no Ship: each Ship's last fetched status. Every `--offline` read, and
`about`, `doctor` (with `--json` or `--next`), `signins --accounts`, `keys ls`, `looks show`, `machine-bundle` (which then signs
nothing) and `machine-pair` (a request waits; approving it does not), answer while FleetLedger's vault is locked; the others are refused then (stderr says how to unlock, exit 1).

## `ship-fleet ls --json` and `ship-fleet status NAME --json`

One Ship as FleetLedger sees it: `ls` lists every Ship in the inventory (with `--tag`,
those with it) in its order, `status` prints one. Schema:
`fleetledger-ship.schema.json`; examples in `examples/fleetledger-ship/`.

| Field | What it is |
|---|---|
| `name`, `tags` | The Ship and its tags in the inventory (`defaults.tags` counting). |
| `state` | The status document's (`provisioning`, `needs-sign-in`, `ready`, `running`, `unknown`); `not-enrolled` (in the inventory, never enrolled, not answering); else the problem. |
| `problem`, `message` | Why there is no status, and in words: `unreachable`, `host-key` (another host key answered), `identity` (it says it is another Ship), `invalid` (its status breaks the contract), `error` (ship-ctl failed), `unknown` (offline, never fetched). Both null when it answered. |
| `host` | The name it answered on; null offline, and when nothing answered or the host key did not match. |
| `attention` | Its attention items (`code`, `message`, `subject`): the Ship's own and FleetLedger's about it. `attention --json` ranks them with the fleet's. |
| `status` | The Ship's status document (`ship-status.schema.json`), fetched now or, offline, the last one; null with a problem. |
| `cloud_init` | cloud-init's own status JSON while ship-ctl is not installed yet; else null. |

Without `--offline`, `ls` asks every Ship at once and `status` one (and renews this
computer's certificate in its last day, saying so on stderr); `status` exits 1 when
the Ship has a problem, after printing it.

## `ship-fleet attention --json`

What needs you, most urgent first (`high`, `medium`, `low`), the Ships' items before
the fleet's within each severity. Schema: `fleetledger-attention.schema.json`; examples
in `examples/fleetledger-attention/`.

| Field | What it is |
|---|---|
| `ship` | The Ship it is about; null for an item about the fleet. |
| `code` | A Ship's attention code (`ship-status.schema.json`), FleetLedger's about a Ship, a `problem` word or `not-enrolled`, or a fleet item's code. Show unknown codes as they are. |
| `message`, `fix` | What it is, and the next step where the message does not say it (empty then; never empty for a fleet item). |
| `subject` | What it is about within the Ship (an agent, a service id, a unit), or the fleet item's subject. |
| `severity` | `high`, `medium` or `low`. |

The fleet's own items, read from FleetLedger's files only (nothing is issued or asked).
`vault-locked` is listed whatever the Ships; the others only when at least one Ship is
listed (with `--tag`, one with that tag): with none, an unlocked FleetLedger prints `[]`.

| `code` | `subject` | When |
|---|---|---|
| `vault-locked` (high) | `vault` | FleetLedger's vault is locked (after every boot, or `ship-fleet vault lock`). While it is, a Ship never fetched gets no item of its own. |
| `machine-cert-expiring` (medium), `machine-cert-lapsed` (high) | the machine's name | A trusted machine that reaches one of the Ships listed is in its certificate's last day, or past it. The FleetLedger computer itself, which renews its own, only once lapsed. |
| `machine-refresh-stopped` (medium) | the machine's name | A trusted machine that refreshes itself (it has asked `ship-fleet machine-bundle` at least once) and reaches one of the Ships listed has not asked for 4 days, while its certificate is not due yet. |
| `machine-refresh-refused` (medium) | the machine's name | Such a machine was refused its bundle for 30 days without asking; in place of its certificate's item. |
| `proxmox-token-expiring` (medium), `proxmox-token-lapsed` (high) | the token's id | FleetLedger's Proxmox API token is in its last 14 days, or past them (`token_expires` in `ships.toml`). |

## `ship-fleet signins --json`

Every sign-in on every Ship, the rows of the web UI's Sign-ins page in the same order:
what needs you, then what is under way, degraded, healthy, and not used. With a Ship
named, its rows only. Schema: `fleetledger-signin.schema.json`; examples in
`examples/fleetledger-signin/`.

| Field | What it is |
|---|---|
| `ship`, `tags` | The Ship and its tags. |
| `service`, `provider` | The status document's auth key (`claude`, `gcloud`, `gcloud_adc`, `github`, ...) and the service `ship-fleet login` takes for it (`gcloud-adc`). |
| `group`, `rank` | `action`, `underway`, `degraded`, `healthy` or `off`, and the urgency within it (the list is already in order). |
| `state`, `level`, `message` | A short word (`ok`, `expiring`, `expired`, `wrong account`, `scopes differ`, `missing`, `error`, `unknown`, `off`), `critical`, `warning`, `notice` or null, and a sentence. |
| `since`, `expires_at` | Since when it has been as it is (or when it expired), and when an expiring one goes. |
| `account` | The account it is signed in as, as the tool reports it. Never a credential. |
| `answers` | The Ship answered (offline: last answered), so signing in or out can work. |
| `drops`, `keep` | `scopes differ`: the GitHub scopes signing in again would drop, and the choice that keeps them. |
| `fix` | The command that resolves it; empty when healthy. |
| `flow`, `batch`, `deadline`, `note` | The web UI's sign-ins in progress: always null from the command line. |

## `ship-fleet ca show --json`

The certificate authority, each Ship's host certificate as its last status showed
it, and every machine trusted to attach (`ship-fleet trust`). Read from `ships.toml`
and the status cache: it asks no Ship and issues nothing. Without a CA (`ship-fleet ca
init`) it prints nothing on stdout and exits 2. Schema: `fleetledger-ca.schema.json`; examples in
`examples/fleetledger-ca/`.

| Field | What it is |
|---|---|
| `host_ca`, `user_ca` | Fingerprints of the key that signs Ships' host certificates and of the user CA keys Ships accept (the current one first). |
| `ships` | Per Ship: `certificate_valid_before`, `principals`, `needs` (why `ca sign` would sign it again, or null), `trust_behind` (its CA keys or revocation list are old) and `seen` (a status was ever fetched). |
| `machines` | Per machine, untrusted ones too: `scope` (`all`, `tag:TAG`, `ship:NAME`), `logins` (what its certificates log in as: `agent`, `owner` or both; `ship-fleet trust NAME --logins`), `valid_before` (its last certificate's end), `revoked`, `fingerprint`, `state` (`ok`, `expiring`, `lapsed`, `none`, `revoked`), `this_computer` (the FleetLedger computer, which renews its own), and for `machine-bundle`: `last_refreshed` (when it last asked; null: never), `idle_limit`, `idle_refused` (when it was refused for being idle, or null) and `looks` (its bundle carries the fleet's Desk looks: `ship-fleet trust NAME --looks on`); on FleetLedger's own VM only, `bundle_key` for a trusted machine: `installed`, `own-line` (its key has a line of yours in `authorized_keys`, so FleetLedger adds none) or `missing` (see "The bundle keys" under `machine-bundle`). |
| `pairing` | Pairing by code (`ship-fleet pair`, below): `on`, and `pending`, the computers waiting (none while off), each with `id`, `host` (the name it gives itself: its own claim), `fingerprint` (its key's), `source` (the address sshd saw, or null), `asked` and `expires`. Never its code. Added in this version; a FleetLedger from before prints none. |

No two entries that are not `revoked` share a key (`trust NAME --key` refuses one
already listed; `trust --this-computer` reuses the live entry with its key, or adds a
new one when only revoked entries have it). So a machine finds its own live entry, and
the name it is trusted under, as the one entry with its key's `fingerprint` and
`revoked` null: `ssh-keygen -lf <its .pub>` prints the same `SHA256:...`. Revoked
entries with that fingerprint may be there too; none means it is not trusted now.

## `ship-fleet doctor [--offline] --json`

Every check `ship-fleet doctor` prints, in its order: `{"schema", "items"}`. Schema:
`fleetledger-doctor.schema.json`; examples in `examples/fleetledger-doctor/`. It only
reads, as the text form does: `--offline` skips the Proxmox API, and it answers while
the vault is locked and on a copy on standby or retired. It exits as the text form
does, after printing: 1 when any item is `missing` or `fail`, else 0.

| Field | What it is |
|---|---|
| `key` | Which check (below). |
| `subject` | For a check made once per machine, Ship or key, its name (`fleet.machine-certificate`, `fleet.machine-refresh-refused`, `fleet.machine-refresh-stopped`: the machine; `ships.not-enrolled`: the Ship; `fleet.managed-keys` when it fails: the key file's path); else null. `key` and `subject` together name one item. |
| `area`, `title` | The text form's AREA and CHECK columns. |
| `state` | `ok`, `info` (fine, with a next step), `warn`, `missing`, `fail`, `skip` (not checked, or optional and not set up). |
| `detail`, `fix` | What it found, and the exact command (or step) that fixes it, empty when none. The text form lists `fix` under Next for `info`, `warn`, `missing` and `fail`. |

| `key` | What it checks |
|---|---|
| `computer.python`, `computer.pythonpath`, `computer.openssh`, `computer.shipshape-checkout` | This computer: Python 3.12 or newer, no empty `PYTHONPATH` entry (listed only when there is one), OpenSSH's tools, the checkout FleetLedger runs from. |
| `computer.vault`, `computer.vault-key-in-ram`, `computer.swap` | FleetLedger's vault, when it is on: locked or unlocked, its key's folder in RAM, swap. |
| `computer.backups` | The last `ship-fleet backup`, when there has been one or one is scheduled. |
| `computer.image-builds`, `computer.ship-fleet-wrapper`, `computer.sshd-auth-info` | On FleetLedger's own VM only: KVM for image builds; the root-owned `/usr/local/bin/ship-fleet` a plain `ssh` command needs (listed only when it is missing or cannot be run); and sshd's `ExposeAuthInfo`, optional, listed (`info`, with the one command as `vmadmin`) only when it is surely off. |
| `fleet.this-copy` | Which copy of the fleet this is (`ship-fleet migrate`). |
| `fleet.inventory`, `fleet.admin-key`, `fleet.ssh-agent`, `fleet.your-keys-for-ships`, `fleet.managed-keys`, `fleet.certificate-authority` | `ships.toml`, FleetLedger's keys and the CA. `fleet.managed-keys` is one `ok` item, or one `fail` per key FleetLedger looks after that is gone or whose passphrase it cannot read (`subject`). |
| `fleet.trusted-machines`, `fleet.machine-certificate`, `fleet.machine-refresh-refused`, `fleet.machine-refresh-stopped` | Trusted machines: one `ok` item, or a `warn` per machine (`subject`) for each of `ship-fleet attention`'s machine items: its certificate in its last day or lapsed, its bundle refresh refused for being idle, or its refreshes stopped. |
| `fleet.bundle-keys` | On FleetLedger's own VM, or wherever FleetLedger's block is in `~/.ssh/authorized_keys`: whether the block holds exactly one forced line per trusted machine ("The bundle keys" under `machine-bundle`). `fail` for a line edited by hand or duplicated, a stale line, a line that is no machine's, broken markers, or a file others may write; `missing` for a machine with no line yet. Fix: `ship-fleet trust --sync-keys` (or `chmod 600`, or a hand edit on a copy that is not the active one). Not listed elsewhere. |
| `fleet.pairing` | Pairing by code (`ship-fleet pair`), listed only while it is on or the pairing key has a line anywhere: `ok` with its one line in FleetLedger's block (and how many computers wait); `missing` for no line while on, or on where no computer can ask (not FleetLedger's own VM, or not the copy that acts); `fail` for a line changed by hand or there twice, a line while pairing is off, or a line of yours for the pairing key outside the block (it lets anybody in with whatever that line allows). Fix: `ship-fleet trust --sync-keys`, `ship-fleet pair off`, or deleting your line. |
| `hypervisor.driver`, `hypervisor.proxmox`, `hypervisor.proxmox-settings`, `hypervisor.proxmox-token`, `hypervisor.proxmox-api`, `hypervisor.template`, `hypervisor.fleetledgers-own-vm` | Where Ships run. |
| `overlay.overlay`, `overlay.oauth-secret` | The overlay network. |
| `ships.ships`, `ships.not-enrolled` | The Ships: how many, and one `warn` per Ship (`subject`) not enrolled yet. |

## `ship-fleet snapshot ls NAME --json`

The Ship's disk snapshots as its driver lists them, in the text form's order (Proxmox:
oldest first, any with no time before them); `[]` when it has none. Each entry is what
the text form shows, nothing more. Schema: `fleetledger-snapshot.schema.json`; examples
in `examples/fleetledger-snapshot/`.

| Field | What it is |
|---|---|
| `ship` | The Ship named. |
| `name` | The snapshot's name, as `snapshot rollback` and `snapshot rm` take it. One made outside FleetLedger is listed as the hypervisor names it; those two refuse a name that is not a letter followed by 1 to 39 letters, digits, `-` or `_` (or is `current`). |
| `taken` | When it was taken (UTC, RFC 3339, Z); null when the hypervisor does not say. |
| `note` | The description stored with it (`snapshot take --note`); empty when none. |

It asks the hypervisor, never the Ship. A driver that cannot list snapshots (`manual`)
or a hypervisor that fails: nothing on stdout, one line on stderr (`ship-fleet: ...`,
and a hint on the next where there is one), exit 1; a Ship not in the inventory, or a
`ships.toml` whose driver settings are missing or unknown, the same but exit 2.
`--json` belongs to `ls` only: `take`, `rollback` and `rm` refuse it, exit 1.

## `ship-fleet doctor --next --json`

What is left after first-run setup, one step per entry in a fixed order. It reads
FleetLedger's files only and contacts nothing. Schema: `next-steps.schema.json`;
examples in `examples/next-steps/`.

## `ship-fleet about [--check] [--json]`

FleetLedger itself, on the computer it runs on. Schema:
`fleetledger-about.schema.json` (`"schema": "shipshape.fleetledger-about/1"`);
examples in `examples/fleetledger-about/`.

| Field | What it is |
|---|---|
| `version` | FleetLedger's package version (`ship-fleet --version`). |
| `checkout` | The ShipShape checkout FleetLedger runs from: its folder, `commit` (full id), `branch` (null when detached, as on FleetLedger's own VM) and `dirty` (uncommitted changes to tracked files, which `self-update` refuses). |
| `upstream` | Whether `origin/main` has a newer commit. Looked at **only with `--check`**, which runs one `git fetch --tags --quiet origin` in the checkout (the fetch `ship-fleet self-update` makes, to the same remote) and compares. Without `--check`, `checked` is false and the rest null: nothing reaches the network. `newer`: `ref` is ahead of the checkout; `behind`: commits on `ref` the checkout lacks. |
| `ui` | The web UI. `local`: this computer's address (`http://127.0.0.1:PORT`), from the running UI, else the remote UI's port, else the service unit's; `remote`: the Tailscale Serve address when the remote UI is on. `running`: a `ship-fleet ui` run by this user answers on its control socket; `as_service`, `jobs` (how many are running) and `stopping` are what it says, null when none runs. |
| `service` | The systemd user unit (`ship-fleet service`): `installed`, and systemd's own words for it (`active`: `systemctl --user is-active`; `enabled`: `is-enabled`), null when not installed or systemd did not answer. |
| `vault` | `state`: `off` (no vault: secrets are in the OS keyring or 0600 files), `locked` (FleetLedger is read-only until `ship-fleet unlock`), `unlocked`, or null when it cannot be read. |
| `backup` | `scheduled`: a daily timer is installed (`ship-fleet backup schedule`). `last`: the last `ship-fleet backup now`'s result here (when, whether it worked, the folder, why it failed), or null. That is the last attempt, so `last_made` says when the newest backup in that folder was made (from its file name; null when there is none or the folder is not there just now). |
| `copy` | Which copy of the fleet this is (`ship-fleet migrate`): `active`, `standby`, `retired`, `unreadable` (a marker FleetLedger cannot read), or `none` (no fleet here yet). |
| `self_update` | The command that updates FleetLedger (`ship-fleet self-update`, run on the FleetLedger computer as its user) and the ref it moves to by default. |
| `unread` | What could not be read, and why (`what`: `checkout`, `upstream`, `service`, `remote ui`, `vault`, `backups` or `copy`). The rest of the document is still true. |

It only reads. It never unlocks, updates, restarts, or writes a setting, and it runs
when other commands refuse: while the vault is locked, on a copy on standby or
retired, and while `ship-fleet migrate` moves the fleet (it holds no in-use lock, so
it never holds up a move either). It exits 0 whenever it could print the document,
`unread` saying what it could not see; a broken command line exits 2 (argparse).

It reads no secret and reaches no Ship. What it asks is cheap: a few local `git`
commands (`status` with `--no-optional-locks`, so it never rewrites the index),
`systemctl --user is-active` and `is-enabled` when the unit is installed, one request
to the running UI's control socket (`web/control.py`), FleetLedger's own state files,
and a listing of the last backup's folder. `--check` adds the one fetch.

## `ship-fleet signins --accounts [--tag TAG] [--json]`

The account each tag's Ships must sign in as, per sign-in: `[tag.<tag>]` in
`ships.toml`, which FleetLedger compares with the signed-in account after every
sign-in and on every status poll (a Ship signed in as another shows
`signin-wrong-account`). Schema: `fleetledger-tag-accounts.schema.json`
(`"schema": "shipshape.fleetledger-tag-accounts/1"`); examples in
`examples/fleetledger-tag-accounts/`.

| Field | What it is |
|---|---|
| `signins` | Every sign-in a tag may name an account for, in FleetLedger's order (its sign-in registry, `ship/signins/`): `claude`, `gcloud`, `github`, then any others. `gcloud_adc` follows `gcloud` and is not one of them. |
| `tags` | Every tag that names accounts or that a Ship has (`defaults.tags` counting), by name. Each: `tag`, `accounts` (sign-in id to account name; a sign-in left out has no expectation from this tag) and `ships` (the Ships with that tag). With `--tag`, that tag only, or none when no Ship has it and it names no accounts. |
| `conflicts` | Ships whose tags expect different accounts for one sign-in (`ship`, `signin`, `accounts`): that sign-in shows as the wrong account whichever it is signed in as. With `--tag`, only Ships with that tag. |

The same command changes them, one tag at a time:

```sh
ship-fleet signins --tag work --set-account claude=work@example.com --set-account github=example-user
ship-fleet signins --tag work --clear-account gcloud
```

A change is checked as a hand edit of `ships.toml` is (a known sign-in id, a tag
name as `ships.toml.schema.json` has it, an account that is not empty) and refused
when the account has spaces or looks like a token: only account names are kept,
never a credential. It is written as every other change (locked, validated, mode
0600, comments kept); a tag left with no accounts loses its table.

It only reads `ships.toml`: it reaches no Ship, and the listing runs while the vault
is locked (a change does not). Tag accounts never reach a Ship (`settings.md`).

## `ship-fleet machine-bundle NAME`

One trusted machine's own bundle, for that machine to keep itself current over ssh:
its certificate, its known_hosts and SSH config, its `hc` machines, and a file of the
fleet's Ships with their tags. Schema: `machine-bundle.schema.json`
(`"schema": "shipshape.machine-bundle/1"`); examples in `examples/machine-bundle/`.
Always JSON; there is no text form and no option.

| Field | What it is |
|---|---|
| `machine` | NAME, the trusted machine (`ships.toml` `machine.<name>`). |
| `at` | When FleetLedger made the document. |
| `certificate` | `line`: its user certificate, as `<machine>-cert.pub` holds it; `valid_before`: when it lapses; `renewed`: this call signed it; `not_renewed`: why a certificate that was due was not signed (`code` `vault-locked` or `sign-failed`, and `why`), else null. `line` is null only when FleetLedger has no valid certificate for it and could not sign one. |
| `scope`, `logins` | What its certificates open: which Ships, and which logins on them. |
| `ssh` | `user`: the login its SSH config uses (agent when it may log in as the agent, else your own). |
| `known_hosts` | Its known_hosts lines for Ships: one `@cert-authority` line naming every name of every Ship listed (none while there is none), then `@revoked *` per revoked host key. |
| `fleetledger` | FleetLedger's own VM (`[fleet] self`): `name`, and its host key as a `known_hosts` line, so the machine keeps the pin of the computer it asks. Null when FleetLedger records no VM of its own. |
| `ships` | Every Ship the machine's scope reaches, and only those: `name` (its `Host` alias and `hc` machine name), `tags`, `runtime` (`core` or `herdr`, from its last status), `host_names` (`lan`: the first of its non-overlay names that resolves on the FleetLedger computer; `overlay`: its overlay name, or null) and `host_certificate` (its last status shows one). |
| `idle_limit` | `on`: after `days` without asking, the machine is refused until allowed again. |
| `refresher` | The refresher script, base64 (optional: absent from a FleetLedger older than it). Only the first-install line `ship-fleet trust` prints decodes it; see "The refresher on a trusted machine" below. |
| `looks` | Only for a machine opted in (`ship-fleet trust NAME --looks on`), right after `at`: the fleet's Desk looks (`ship-fleet looks`, below), as `digest` (the `sha256` `looks show` prints) and `files` (each `name` as the format names it, `size`, and `data`, base64, in name order); or null when FleetLedger keeps none. Absent for every other machine, so its bundle is exactly as before looks; absent too when the kept folder fails its checks or the bundle would be over 1 MiB with it (the audit line says which). See "The fleet's looks" below. |

Everything but `refresher` is data, and the machine checks every field against the
schema's patterns (and tighter) before writing a file from it. `refresher` is read only
by the line a person pastes once; the refresher itself never reads it. The bundle a
person installs by hand (`ship-fleet trust`, `install.sh`) is unchanged.

**What it does.**

- It answers only for a machine that is trusted and not untrusted. An unknown
  machine and an untrusted one get the same one line on stderr, exit 1, and nothing on
  stdout. So does a name that is not a machine name.
- It signs a new certificate only once the current one is past half its life (with
  the default 7-day certificates, in its last 84 hours), with the code, lifetime and
  record `ship-fleet trust` uses, but never for longer than the machine's last
  certificate was given (`last_cert_hours`: after `ship-fleet trust NAME --hours 2`,
  2 hours again, until a `trust NAME` without `--hours`), ships.toml locked throughout, so two calls at once
  sign one. Otherwise it returns the current one again. A certificate FleetLedger does
  not hold (signed by `trust --print-cert` since), or one for more than the machine may
  have now (its scope or logins narrowed since), counts as none: a new one is signed.
- While the vault is locked it signs nothing and never asks for a passphrase: the
  document comes with the current certificate while it is valid (null once lapsed),
  `not_renewed.code` `vault-locked`, and the rest as ever. Signing never prompts
  (stdin closed, no terminal); a CA key that would need a passphrase gives
  `sign-failed`. Its `why` is a fixed sentence: the reason (ssh-keygen's words, a
  key's path) goes only to the audit log on the FleetLedger computer. So does the
  reason for any other failure (a broken `ships.toml`, say), which the caller sees as
  one fixed line on stderr, exit 1.
- **The idle limit.** It records when the machine asked (`last_refreshed` in
  `ships.toml`, to within five minutes). A machine whose last ask, or the last time
  you allowed it again, is more than 30 days ago is refused, saying so and the one
  command that lets it ask again: `ship-fleet trust NAME --allow-again`. A refusal
  does not count as asking. `ship-fleet trust NAME --idle-limit off` keeps the machine
  trusted until untrusted; `--idle-limit on` (the default) puts the limit back. A
  machine that has never asked, such as one trusted before the limit existed, starts
  its 30 days at its first ask, so the upgrade locks out no machine. FleetLedger's own
  computer (`trust --this-computer`) has no idle limit. Certificate lifetimes do not
  change.
- Every call it answers writes one line to the audit log: the machine, and
  `returned`, `renewed`, `returned, not renewed: <code>` (with the reason for
  `sign-failed`), `refused: <why>` (unknown, untrusted, not a machine name, idle since
  ...) or `failed: <why>`; for a machine opted in to looks, followed by `; looks
  <first 12 hex digits of the digest>`, `; looks: none kept` or `; looks not sent: <why>`.
- On a copy on standby or retired (`ship-fleet migrate`) it refuses, as every command
  but a few does, saying where the fleet is; that copy writes nothing, not even an
  audit line. During a move it refuses until the move ends.

**What it never does.** It takes the machine from its argument alone: it reads
nothing from stdin, never looks at `SSH_ORIGINAL_COMMAND`, runs no shell, and has no
option, so a key whose `authorized_keys` line forces `ship-fleet machine-bundle NAME`
gets NAME's bundle and nothing else: no other machine's, no secret, no Ship outside
NAME's scope. For a machine opted in to looks (`--looks on`), that bundle also carries the
fleet's Desk looks, so that key then reads them too: every file of the kept folder. It writes only NAME's record in `ships.toml` (`last_refreshed`; a new
certificate's serial, date, lifetime and `issued` entry; `idle_refused`), the new certificate
beside NAME's bundle (`~/.local/share/fleetledger/trust/NAME/NAME-cert.pub`), and its
audit line; asked again within five minutes it does not rewrite `ships.toml`.

**The bundle keys.** On FleetLedger's own VM (`[fleet] self` names the computer) and as
the active copy, `ship-fleet trust NAME` gives the machine's key one line in the
FleetLedger user's `~/.ssh/authorized_keys`, inside a block between two marker lines
that FleetLedger alone writes:

```
# >>> FleetLedger: trusted machines' bundle keys (ship-fleet trust writes these) >>>
restrict,command="/home/fleetledger/.local/bin/ship-fleet machine-bundle NAME" ssh-ed25519 AAAA... NAME
# <<< FleetLedger: trusted machines' bundle keys <<<
```

`restrict` with nothing turned back on: no terminal, no forwarding of any kind, no
`~/.ssh/rc`; `command=` runs that one command whatever the client asks for. The line
holds the key's own bytes and the machine's name (a-z, 0-9 and -), nothing from the
key's comment. `untrust` removes the line, and a new line starts the machine's idle
clock (`refresh_allowed`) if it never asked. A key that has a line of its own outside
the block (in `authorized_keys` or `authorized_keys2`: one of your keys) gets none,
because sshd uses the first line that matches a key: your line decides what it may
do, and `ship-fleet ca show` says `own-line`. FleetLedger never changes a line outside
the block (unless you ask it to, with `ship-fleet keys`, below) or the file's mode and
owner, refuses a file with broken markers or a line
in the block that is no machine's, and writes atomically. Elsewhere (a desktop, a copy
on standby or retired) it adds no line; it still removes lines of machines no longer
trusted, and `migrate retire` (and a push's cut-over) empties the block, which
`activate` and `unretire` fill again. `ship-fleet trust --sync-keys` rewrites the
block to match; `ship-fleet doctor` checks it (`bundle keys`).

**The refresher on a trusted machine.** On FleetLedger's own VM, `ship-fleet trust NAME`
prints one line to paste on NAME (for a key with its own line too: your main machine
uses the same refresher, which asks for its bundle by name through its shell). The line
finds NAME's private key under `~/.ssh` by the `.pub` file that holds the trusted key,
pins the VM's host key from `[fleet] self`, fetches the bundle with that key (`ssh -F
none`, `BatchMode`, `IdentitiesOnly`), decodes `refresher`, checks its SHA-256 against the
one the line names, and runs its `install`, which:

- copies it to `~/.ssh/shipshape/refresh` (mode 0700);
- writes `~/.ssh/shipshape/machine.conf`, read line by line and never sourced:
  `name` (the machine's name here), `key` (the private key's path), `user` and `hosts`
  (how to reach FleetLedger, tried in turn), `host_key` (the pinned `ssh-ed25519` key),
  `host_names` (`overlay`, the default: each Ship's overlay name when it has one; or
  `lan`), `hc` (`on`/`off`) and `hc_path`;
- puts `Include ~/.ssh/shipshape/config` at the top of `~/.ssh/config`, as `install.sh`
  does;
- leaves a timer that runs the refresher at login and hourly: a systemd user timer
  (`shipshape-refresh.timer`) on Linux, a launchd agent (`local.shipshape.refresh`) on
  macOS. `--no-timer` at the end of the line leaves none (also `--no-hc`, `--lan`);
  `~/.ssh/shipshape/refresh remove-timer` removes it;
- refreshes, and then, with Herdr here (`herdr-app` on PATH, or its folder) and no Fleet
  panel in it yet, says the one command left: `shipshape-setup --herdr-client`. Not when
  `shipshape-setup` itself runs the install (it sets `SHIPSHAPE_SETUP`).

Run with no arguments (the timer, the Fleet panel after a job), the refresher asks
for `ship-fleet machine-bundle NAME` the same way, checks the whole document, and
refuses it whole if any value is out of place. A Ship whose name is a `Host` of your own
in `~/.ssh/config` is left out (with a warning), so a bundle can never take over one of
your aliases. It then writes, each by rename, only
`NAME-cert.pub`, `known_hosts`, `config` (from its own template: no text from the
bundle becomes a directive), `fleet.json` (the bundle as checked, compact, without
`refresher` and `looks`), `hc-added`, `state.json` and `looks-applied`, all in
`~/.ssh/shipshape`, and, for a bundle with `looks`, the folder `desk-looks` in Herdr's folder
(below). It adds each
herdr-core Ship with a host certificate to `hc` (when the bundle's login is `agent`)
and removes only the `hc` machines it added itself, once their Ship has left the bundle
or stopped running herdr-core. It never replaces itself: only an install you run (the
pasted line, or `shipshape-setup`, each checking the SHA-256 FleetLedger names) puts a new
refresher in place, never the timer or the Fleet panel. Exit 0: applied (a warning,
such as hc missing, goes to stderr and `state.json`). Exit 1: nothing new applied, one
sentence on stderr and in `state.json`: FleetLedger unreachable, the machine refused or
untrusted, a certificate due and not signed (`vault-locked`, `sign-failed`), no
certificate, or a document it refuses. Exit 2: wrong usage. `state.json`:
`refresh-state.schema.json` (`"schema": "shipshape.refresh-state/1"`).

**The fleet's looks.** `ship-fleet trust NAME --looks on|off` (signs nothing; changes only
`ships.toml`'s `looks` for the machine; audited as `trust-looks-on`/`-off`) decides whether
NAME's bundle carries `looks`; off is the default. Refresher 2 (`refresh --version`; the
`refresher` field of `state.json`) reads it; refresher 1 refuses every bundle that has it
(`an unknown field, looks`), keeping its last files, so a machine is opted in only after
the line `ship-fleet trust NAME` prints (or, where NAME's own key has a shell on FleetLedger,
`shipshape-setup`) has put refresher 2 there: FleetLedger cannot tell which
refresher a machine runs, and `trust --looks on` says what to check. Refresher 2:

- checks `looks` whole, with the rest of the bundle, before writing anything: known fields
  only; names as `desk-looks.md` gives them (`looks.json`, `palettes/looks-<id>.json`,
  `logos/<name>.png|.jpg`), none twice, `looks.json` once; each size within its limit and 512
  KiB in all, at most 16 palettes and 16 logos; base64 decoded to exactly its size (`base64
  --decode`, or `-d`); each logo by its first bytes; each JSON file free of control bytes and through its strict
  reader, `looks.json` as `shipshape.desk-looks/1`; and the folder against `digest` (`sha256sum`, or
  `shasum -a 256`). One out of place refuses the bundle whole;
- writes the folder into Herdr's folder (`$HERDR_APP_HOME`, else `~/.config/herdr-app`; never
  made) as `desk-looks`, from a new folder beside it checked again by its digest, by rename;
  with the same digest as the folder there, it writes nothing. It records the digests of the
  last eight folders it wrote in `~/.ssh/shipshape/looks-applied`, and changes or removes a
  `desk-looks` only when the digest of what is there now is one of those: a folder of your own, a link, or its folder
  after you changed it is never touched (a warning says the fleet's were not written). For
  null it removes the folder it wrote;
- with no Herdr folder, or no `base64`, `sha256sum`/`shasum` or `od` to check with, leaves
  the looks (a warning). A bundle without `looks` leaves whatever is there.

Runbook: `docs/runbooks/share-desk-looks.md`.

**Attention.** A machine that has asked at least once refreshes itself, so FleetLedger
says when it stops (fleet-level items in `ship-fleet attention`, the web UI and
`doctor`, one per machine): `machine-refresh-stopped` when it has not asked for 4 days
(more than a long weekend switched off, and past half the 7-day certificate's life, so
a certificate renewed at its last ask still has 3 days); `machine-refresh-refused`
once it was refused for being idle, in place of its certificate's own item; and,
when its certificate is in its last day or lapsed, the existing `machine-cert-*` item,
whose fix then says when it last asked and, while the vault is locked, to unlock it.
A machine that never asked has only the `machine-cert-*` items, as before. Any call
counts as asking, one made by hand included: FleetLedger cannot tell who ran it.

## `ship-fleet machine-setup --json | --package | --installer`

What the installer, `shipshape-setup` (at the checkout's root), reads to set a computer up
as a Herdr client, over an ssh session that has a shell on FleetLedger's own VM. It only
reads, and answers only there, as the copy that acts (it names that VM's host key); off it,
one line on stderr and exit 1. It answers while the vault is locked. A trusted machine's
bundle-only key never reaches it: its forced line runs `machine-bundle`.

`--json`: schema `machine-setup.schema.json` (`"schema": "shipshape.machine-setup/1"`);
examples in `examples/machine-setup/`.

| Field | What it is |
|---|---|
| `fleetledger` | The VM (`[fleet] self`): `name`; `user`, the user FleetLedger runs as (whom the refresher logs in as); `hosts`, its names in the order the refresher tries them (its bare name last); `host_key`, `ssh-ed25519 AAAA...`. |
| `refresher_sha256` | The SHA-256 of the refresher every bundle carries (`refresher`). |
| `package` | `sha256` and `bytes` of what `--package` prints. |
| `machines` | Every trusted machine but those untrusted and FleetLedger's own computer, with an ed25519 key: `name`, `key` (type and base64 only), `access`: `bundle` (FleetLedger's forced line), `shell` (a line of yours, so it asks for its bundle by name) or `none` (no line yet). |

`--package`: a gzipped tar of `shipshape-setup` and `herdr/` less its tests (the install
scripts, the Desk layout and themes, the Desk's mods and the Fleet panel; a link inside
`herdr/` packed as the file it names), the same bytes for the same checkout. `--installer`:
`shipshape-setup` alone, for a computer with no checkout.

**What the installer does with it**, and what it checks: it pins FleetLedger's host key
on first contact (`ssh-keyscan`, shown as a fingerprint to confirm, or
`--host-key-fingerprint`; a key `~/.ssh/known_hosts` already has is used as it is, one it
has differently is refused) and refuses a VM whose `fleetledger.host_key` is not that key.
Every value passes the refresher's own patterns, or the whole document is refused. It is
the trusted machine whose `key` one of its `~/.ssh` key files holds (more than one:
`--name`). When none is and the session has a shell, it shows the `ship-fleet trust` it
would run (a name from its host name, scope `all`, logins `both`, its own new
`~/.ssh/shipshape/id_ed25519`) and runs it after one yes (`--yes`). It fetches the
refresher as the pasted line does (`machine-bundle NAME` with that machine's own key,
FleetLedger's settings ignored, the host key pinned) and installs it only when its SHA-256
is `refresher_sha256`; the package only when its size and SHA-256 are `package`'s.

## `ship-fleet pair` and `ship-fleet machine-pair`

Pairing by code, for a computer with no key that logs in to FleetLedger's own VM: it asks
to be trusted, and you approve it by typing the code it shows. Off by default.

```
ship-fleet pair on | off        let unknown computers ask, or no longer
ship-fleet pair ls              the requests waiting
ship-fleet pair approve [ID]    type its code; choose its name, scope and logins
ship-fleet pair refuse ID       (or --all)
```

**The way in.** While pairing is on, on FleetLedger's own VM and as the active copy,
FleetLedger's block in `~/.ssh/authorized_keys` ("The bundle keys", above) holds one more
line, for the well-known pairing key, whose private half is in `shipshape-setup` for
anyone to read:

```
restrict,command="/home/fleetledger/.local/bin/ship-fleet machine-pair" ssh-ed25519 AAAA... shipshape-pairing
```

`ship-fleet pair off` removes it (and forgets every request); so does every sync where
pairing is off, or the copy is not the active one on its own VM. No new port: the VM's
sshd, keys only, as before. `trust` refuses the pairing key as a machine's key, and
`pair on` refuses while a line of yours for it is outside the block.

**What an unknown computer can do** (`machine-pair`, schema `machine-pair.schema.json`;
examples in `examples/machine-pair/`). It reads `SSH_ORIGINAL_COMMAND` alone (at most 200
bytes of printable ASCII, one fixed pattern per verb; never stdin):

| Command | Answer |
|---|---|
| `request BLOB NAME` | Its own ed25519 key's base64 and the name it gives itself (a machine name: a claim). `pending`, with a random `token` (kept only as its SHA-256), a random `nonce`, the `code` and `expires_in` (600 seconds). The same key asking again replaces its earlier request. |
| `status TOKEN` | `pending` (with `expires_in`), `approved` (with `machine`, `fleetledger`: `user`, `hosts`, `host_key`, and `refresher_sha256`), `refused`, or `unknown` (never filed, withdrawn, or lapsed). |
| `cancel TOKEN` | Withdraws its own request: `unknown`. An approved one stays approved. |

Anything else is `refused` (exit 1), with one fixed sentence; pairing off, or not FleetLedger's
own VM as the active copy, is `off`; a full store is `busy`. No answer names a Ship, another
machine or a count. Bounds: 4 requests waiting at once, 2 from one address, 20 filed in any
hour, each lapsing after 10 minutes; the store (`~/.local/share/fleetledger/pairing/requests.json`,
0600) never holds more, and a file that is not FleetLedger's counts as empty. A request
grants nothing: its key has no line, so it logs in to nothing, and no bundle is made for it.
It writes the store and, for a request filed, one audit line (`pair-request`).

**The code** is the first 8 hex digits of SHA-256 of `shipshape-pair/1:NONCE:BLOB` (no
newline), as a number, modulo 1000000, six digits. The installer works it out itself from
its own key, and stops if FleetLedger's differs. A second computer asking at the same time
has another key and another nonce, so another code.

**Approving** (`pair approve`, in a shell of yours; the Fleet panel opens it in a terminal
tab) shows the request (the name it claims, as a claim; its key's fingerprint; where it
asked from; FleetLedger's own host key, which the computer pinned and shows too), then asks
you to type the code the computer shows: it is never shown on FleetLedger, so it cannot be
copied from there. With no ID, the code finds the one request it belongs to; two requests
with one code are neither approved. Then a name (default: the one it claims, if no machine
has it), scope (`all`) and logins (`both`), and it runs `ship-fleet trust NAME --key
'ssh-ed25519 ...' --scope ... --logins ...` with every rule `trust` has (the bundle-only
forced line, the 30-day idle rule). `--code`, `--name`, `--scope`, `--logins` and `--yes`
answer the questions for a script; the panel never passes them. The vault must be
unlocked (trust signs). Once approved, refused or lapsed, nothing of the request stays but
its token's hash and the word, until it would have lapsed.

A phone is out of scope: it runs no installer, so it never asks (its browser pairs with
the web UI: `ship-fleet ui link`).

## `ship-fleet keys ls --json`

Every key that may log in to this computer as FleetLedger's user: each line with a key
in `~/.ssh/authorized_keys`, then `~/.ssh/authorized_keys2` (sshd reads both by
default), in the order sshd tries them. Comments and blank lines are left out. Schema:
`fleetledger-key.schema.json`; examples in `examples/fleetledger-key/`. It only reads
the two files (and the session's own `SSH_USER_AUTH` file): it reaches no Ship, runs on
a desktop too, and answers while the vault is locked. On a copy on standby or retired
(`ship-fleet migrate`) it refuses, as every command but a few does.

| Field | What it is |
|---|---|
| `file`, `line` | Where the line is. |
| `name` | The line's comment, as written (null: none). `keys name` sets it. Anything a hand edit put there: show it as text. |
| `type`, `fingerprint` | The key's type, and its SHA256 fingerprint as `ssh-keygen -l` prints it (null for a key sshd cannot decode). Never the key itself. |
| `access` | `shell` (no forced command: it may run anything, though options such as `no-pty` still apply), `command` (only `command`), `bundle` (a trusted machine's line in FleetLedger's block, only `machine-bundle <machine>`), `cert-authority` (a CA: any certificate it signs logs in). |
| `machine`, `command` | For `bundle`, the machine; for `command`, the forced command. Null otherwise. |
| `options` | The line's options as written; empty for none. |
| `this_session` | The key the ssh session running the command logged in with, as sshd says (`ExposeAuthInfo yes`, which the appliance seed sets); null when sshd does not say. |
| `changeable` | `keys name` and `keys revoke` may change it: on FleetLedger's own VM, in `authorized_keys`, outside FleetLedger's block, with the block's markers intact. |

The same command changes them, on FleetLedger's own VM only (elsewhere
`authorized_keys` is your own login's, and both refuse, saying so):

```sh
ship-fleet keys name SHA256:... desktop     # its comment only; KEY: a name or fingerprint
ship-fleet keys revoke old-laptop           # removes its line, after you type its name
```

`name` takes letters, digits and `. _ @ + -` (no spaces, quotes or commas), so a name
can never start a new line or add an option, and refuses one another line has. `revoke`
asks for the key's name in a terminal (`--yes` skips that) and refuses, unless
`--allow-lockout` is given: the last line that surely gets a shell; the key this
session logged in with; and, over ssh when sshd does not say which key that is, any
key. Another line counts as a shell you can still log in with only when FleetLedger can
be sure of it: in `authorized_keys` (an sshd may be set to read no other), outside its
block, the first line for its key, with no `from=`, `expiry-time=` or other option that
could keep it out, and a key `ssh-keygen` accepts. A session with no ssh variables at
all (a console, `sudo -u`, a terminal multiplexer started at boot) is not guarded by
the session rule, only by the last-shell one. It
refuses a key with more than one line (one removed would leave it able to log in).
Neither touches a line in FleetLedger's block (`ship-fleet untrust NAME` removes a
machine's), a line in `authorized_keys2`, or a file whose block markers are broken.
Each changes exactly one line, keeps every other byte and the file's mode and owner,
writes atomically under the lock `trust` takes, and gives up rather than write over a
change made since it read the file. Each writes an audit line (`key-name`,
`key-revoke`; service: the key's fingerprint).

**Who ran a command.** Every audit line written by a command run in a terminal carries
`actor_key` (the fingerprint of the one key the ssh session logged in with, from sshd's
`SSH_USER_AUTH` file) and `actor` (the comment of the first line for that key), when
sshd says which key that was; neither when it does not (no `ExposeAuthInfo`, a local
login, a certificate, two keys), and never for the web UI, whatever session started it.
A shell inside `tmux` or `screen` keeps the `SSH_USER_AUTH` of the session that started
it while that session lasts. The name is the first line's for that key, even where sshd
skipped it (a `from=` that did not match) and let the key in through a later one: the
fingerprint is right either way. A `machine-bundle` call names its machine as before.

## `ship-fleet looks`

One looks folder for the Herdr Desk (`desk-looks.md`: palettes, presets, tag looks and
logos), kept by FleetLedger for every computer of yours:

```sh
ship-fleet looks publish ~/.config/herdr-app/desk-looks    # a folder on this computer
tar -cf - -C ~/.config/herdr-app/desk-looks . | ssh fleetledger@HOST ship-fleet looks publish -
ship-fleet looks show [--json]                              # what is kept
ship-fleet looks clear                                      # keep none
```

**Who may publish.** Only someone with a shell on the FleetLedger computer, as for every
`ship-fleet` command: the second line works from a computer whose own key has a shell
there. The keys FleetLedger writes into its `authorized_keys` cannot: a trusted machine's
key runs `machine-bundle NAME` and the pairing key `machine-pair`, whatever the client asks
for, and neither reads stdin or runs another command.

**Checked on the way in, whole.** Every rule of `desk-looks.md` is checked again in
FleetLedger, never trusted from the computer that sent it: known fields only, the id, tag
and colour patterns, what a preset may name (the Desk's own palettes, finishes, types,
densities and fonts, or the folder's palettes), the size and count limits, and each logo by
its first bytes. A few corners are stricter than the Desk: a field named twice in one
object, a field given as `null`, text that is not valid Unicode, and JSON that is not UTF-8
are refused. Files are found by name from `looks.json`, never by listing the folder:

- a folder: each file is opened without following a link (a link anywhere below the folder
  you name is refused), must be a regular file, and is read with a hard limit;
- `-`: at most 1 MiB is read from stdin, then parsed in memory and never extracted. Each
  member must be `.`, `palettes` or `logos` (folders), or a regular file named as the
  format names one (`looks.json`, `palettes/looks-<id>.json`, `logos/<name>.png|.jpg`, a
  leading `./` allowed). A link, hard link, device, FIFO, sparse file, absolute or `..`
  path, any other name (a README, macOS's `._` files: `COPYFILE_DISABLE=1 tar ...` leaves
  them out), a name twice, more than 16 palettes or 16 logos, more than 36 members, a
  file over its limit or more than 512 KiB of files refuses the whole stream. A palette
  or logo looks.json does not name is ignored.

A folder with one thing out of place is refused whole (exit 1, stderr says what), and what
was kept before stays as it was. A folder without `looks.json` is refused: `clear` keeps
none.

**What is kept.** The checked folder, rewritten from its checked values: each JSON file
as compact JSON built from the values that passed (never the sent text; never longer than
it), each logo as it came (a checked picture). It is one file,
`~/.local/share/fleetledger/looks/published.json` (mode 0600), written atomically (a
temporary file, then a rename), with when and from where it came (each a short line of
plain text, or null). Reading it back checks it all again, the where and when included, so a
hand edit shows as a `problem`, never as looks. A tar member that tarfile cannot read
(crafted headers included) is a refusal like any other. Nothing kept is ever
run, or put into a command or a path: file names come from the format's patterns alone.

**`looks show --json`** prints one document (`fleetledger-looks.schema.json`; examples in
`examples/fleetledger-looks/`): whether a folder is kept (`stored`), and for one that is,
`problem` (null, or why it cannot be used), `published_at`, `from` (`how`: `folder` or
`stdin`; the `folder` for one here; the ssh session's `address`; the `actor` and
`actor_key` as the audit log names them), `sha256` (over each file's name, a newline, its
length, a newline and its bytes, in name order: it changes exactly when what a computer
would receive changes), `bytes`, and what it names: `palettes` (`id`, `dark`), `presets`
(ids), `tag_looks` (`tag`, `preset`) and `files` (`name`, `bytes`). Never a file's
contents, labels included.

**Rules.** `show` answers while the vault is locked (it reads one non-secret file);
`publish` and `clear` change FleetLedger's state, so they wait for the unlock, as `toolkit
add` does. A copy on standby or retired (`ship-fleet migrate`) refuses all three. Each
`publish` writes an audit line (`looks-publish`; service: the first 12 hex digits of the
digest, or `-` for one refused), each `clear` that removed something one
(`looks-clear`). `ship-fleet migrate` moves the kept folder to the new computer (that one file, and a
receive refuses any other under `looks/`);
`migrate wipe` deletes it with the rest of FleetLedger's own files.

**Sent to the machines you opt in.** `ship-fleet trust NAME --looks on` puts the kept folder
in NAME's bundle (`machine-bundle`, "The fleet's looks"), for its refresher to write into
Herdr's `desk-looks` there; `looks show` lists those machines ("sent to"). Every other machine's
bundle is as before. A kept folder with a `problem` is sent to none.
