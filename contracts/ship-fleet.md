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
| `doctor --next --json` | one document | `shipshape.next-steps/1` |
| `about [--check] --json` | one document | `shipshape.fleetledger-about/1` |

`--offline` asks no Ship: each Ship's last fetched status. Every `--offline` read, and
`about`, `doctor --next` and `signins --accounts`, answer while FleetLedger's vault is
locked; the others are refused then (stderr says how to unlock, exit 1).

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
| `machines` | Per machine, untrusted ones too: `scope` (`all`, `tag:TAG`, `ship:NAME`), `valid_before` (its last certificate's end), `revoked`, `fingerprint`, `state` (`ok`, `expiring`, `lapsed`, `none`, `revoked`) and `this_computer` (the FleetLedger computer, which renews its own). |

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
