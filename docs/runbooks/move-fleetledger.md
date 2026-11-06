# Move FleetLedger to another computer

`ship-fleet migrate` moves your whole FleetLedger, with everything it needs to
manage the fleet, from one computer to another: from your desktop to a laptop for a
week, say, or onto a small always-on VM. **Your Ships keep working from the new
computer with nothing changed on them, and no Ship is adopted again.**

That works because a Ship trusts FleetLedger only through the fleet admin key its
seed put in `/home/fleet/.ssh/authorized_keys` (no address rules, nothing about the
computer FleetLedger runs on), and FleetLedger trusts each Ship through the host
key pinned in its own `ships.toml`. Move the same admin key, the same pins and the
same certificate authority, and nothing on a Ship can tell the difference.

## In short

```sh
# On the old computer:
ship-fleet migrate check                    # what would move, what stays, what stops it
ship-fleet migrate push you@new-computer    # move it, check every Ship, hand over

# Two weeks later, once you are happy, on the old computer:
ship-fleet migrate wipe                     # delete the old copy's keys and secrets
```

**Only one copy acts on the fleet at a time.** Two FleetLedgers acting at once undo
each other's changes: an older checkout's `update` rolls Ships back, and a stale
inventory pushes a revocation list that un-revokes machines. So `push` hands over
in steps:

1. The new computer receives the fleet **on standby**: it has everything, but
   refuses to act on the fleet.
2. It checks every key, secret and Ship (read-only).
3. Only then does the old copy **retire**: it refuses to act on the fleet from
   then on, and keeps its files for a rollback.
4. The new copy becomes **active**, with the next **epoch** (a number in
   `ships.toml` that goes up at every hand-over).

Retiring comes before activating, so a hand-over cut short (the network drops)
leaves no copy acting, never two. Nothing changes on any Ship at any step.

## Before you start

On the **new** computer:

1. Install FleetLedger from a ShipShape checkout **at the same commit** as the old
   computer's (`git -C <checkout> rev-parse HEAD` on both). `receive` refuses
   another commit: `ship-fleet update` packs `ship/` from the checkout, so an older
   one would roll Ships back.
2. Have no FleetLedger there yet. `receive` never writes over one: it refuses if
   `~/.config/fleetledger` or `~/.local/share/fleetledger` holds anything, except a
   copy on standby from an earlier move (replaced: it never acted), or the marker a
   wiped copy leaves (see "Wipe the old copy").
3. Let it reach the Ships the way the old computer does: the same LAN (with
   `libnss-mdns` for `.local` names), or the overlay network for Ships with
   `ssh_listen=overlay` (the tailnet policy must let it reach `tag:ship:22`).
4. Let the old computer log in to it over SSH, and **check its host key** once
   with plain `ssh you@new-computer` (compare the fingerprint). `push` connects
   with `StrictHostKeyChecking=yes`, so a host key it does not already know stops
   it.

On the **old** computer, `ship-fleet migrate check` must say "Ready to move". It
refuses while:

- another `ship-fleet` runs there: the web UI, or a command in another terminal (a
  sign-in, say). Stop it first; while a move runs, no other `ship-fleet` can start;
- a Ship is not enrolled yet (its seed holds its private host key): finish it with
  `ship-fleet status <ship> --wait`, or destroy it;
- a secret cannot be read (a locked keyring: unlock it);
- the checkout has uncommitted changes in `ship/`, `versions.toml`, `contracts/` or
  `fleetledger/` (commit and push them, so the new computer can check that commit
  out);
- a file it does not know is in FleetLedger's folders. It will not guess whether
  such a file matters: move it out of the folder, then check again.

## What moves, and what stays

`check` lists every file and secret with where it comes from, and every file that
stays with the reason. In short:

| Moves | How |
|---|---|
| The inventory, `ships.toml`: every Ship and its pinned host key, tags, settings, trusted machines with their issued certificates and revocations, `[ca]` with its old user CA keys and revoked host keys, `[drivers.proxmox]`, `[overlay]`, `[image.*]` | whole; only the key paths and the secret stores are rewritten for the new computer, and `receive` checks that nothing else changed |
| The fleet admin key and the host and user CA keys (both halves) | the same files, at the same place under the new computer's `~/.ssh` (or FleetLedger's folders) |
| Your keys for Ships (`fleet.agent_keys`) | the `.pub` only, into `~/.config/fleetledger/agent-keys/` |
| The passphrases FleetLedger keeps for its keys, the Proxmox token, the OAuth secret | stored again in the new computer's own store (below) |
| The audit log, last status, charts and Activity history, failed Ship attempts, network summaries, the revocation list, trust bundles | as they are (the history through SQLite, so the copy is consistent) |

| Stays | Why |
|---|---|
| A key of your own that FleetLedger made (`~/.ssh/shipshape_ed25519`) | its private half and passphrase stay on your machines; the new computer never attaches as you. Its `.pub` moves |
| `this-computer.json` | which machine entry is the old computer: a fact about that computer. The old computer stays a trusted machine like any other |
| `remote-ui.json` | the remote web UI setting ([remote-ui.md](remote-ui.md)): the old computer's own name on the tailnet and its Serve port. Opening the UI to your other devices is yours to turn on, on each computer, so the move never does it: `receive` prints the exact command for the new computer, with the same logins (the allow-list) |
| The remote web UI's paired browsers and pairing links (`~/.local/share/fleetledger/web/`) | bound to the old computer's address: pair each device again with the new one (`ship-fleet ui link`) |
| `known_hosts` | rebuilt from the pins in `ships.toml` |
| A part-finished Proxmox setup, the inbox upload spool, Ubuntu's cached cloud image, locks | nothing the fleet needs |
| Baked image files | unless `--with-images` (they are large); a published image still serves new Ships. `check` says which images would then be unpublishable |

Nothing is written to a file at rest on the way: the move is one stream through
SSH (or a pipe), checked member by member against the SHA-256 sums its manifest
sends first.

## Where the secrets go on the new computer

Every secret is stored again in **the new computer's own store**, under names it
works out itself: the OS keyring when one is unlocked there, else 0600 files in
`~/.config/fleetledger/`. Choose with `--store keyring` or `--store file`
(`push --store file` for a headless VM). Asked for and unusable, the keyring is
refused, never quietly swapped for files. A passphrase's keyring item depends on
the key's full path, so moving to a different home folder is fine.

Keys that keep a passphrase of your own move encrypted; load them on the new
computer with `ssh-add` before FleetLedger can use them (`verify` says so).

## The check afterwards

`receive` (and `verify` at any time) checks, without changing anything on any Ship:

- every key opens (each managed key with its passphrase from the new store);
- `ship-fleet doctor`'s checks;
- the Proxmox token works through the pinned certificate, and the OAuth client
  still has the scopes it needs;
- the revocation list is the same bytes as on the old computer;
- every Ship answers on its pinned host key with the moved admin key (a read-only
  `ship-ctl status`), and its host certificate and revocation list match this CA;
- the time zone (new Ships copy it): a different one is a warning.

`push` exits:

| Exit | Means |
|---|---|
| 0 | the new computer is the active FleetLedger; this copy is retired |
| 5 | the fleet is on standby there, but a check failed (say, the network path to a Ship), so **this copy is still the active one**. Fix it, then push again: it replaces the copy on standby |
| 6 | this copy retired, but the new one did not become active: **no copy acts on the fleet**. Run `ship-fleet migrate activate` there, or `ship-fleet migrate unretire` here |
| other | nothing was written there; nothing changed here |

`push --no-cutover` stops after step 2: a trial copy on standby, with this copy
still the active one. Push again (without it) to move for real.

## Handing over by hand

From the new computer, where your keyring is unlocked, the move is a pipe:

```sh
ssh you@old-computer ship-fleet migrate send | ship-fleet migrate receive
```

That leaves the new copy on standby and the old one active. Hand over in the same
order push does: first, on the old computer, `ship-fleet migrate retire --to
new-computer`; then, on the new one, `ship-fleet migrate activate` (it checks every
Ship again first). `ship-fleet migrate verify` checks a copy at any time, on
standby or active.

## A retired copy

A retired copy refuses every command but `ship-fleet migrate` and `doctor`, the web
UI included, with a line saying when it retired and where the fleet went. Its files
stay as they were, so you can roll back. Until it is wiped, the old computer is as
sensitive as before: it still holds the admin and CA keys.

**Roll back** (within the two weeks): on the new computer `ship-fleet migrate retire
--to old-computer`, then on the old one `ship-fleet migrate unretire`. The old copy
acts again with an epoch above any handed over. What the new copy changed in the
meantime (a new Ship, a trusted machine) does not come back that way; to bring it,
wipe the old copy (`ship-fleet migrate wipe --now`) and move the fleet back with
`push` from the new computer instead.

**The epoch** guards against an old state coming back: a computer that handed the
fleet on at epoch 3 refuses a fleet from epoch 2 or before (an old backup, a copy
forgotten on standby somewhere), even after it is wiped.

## Wipe the old copy

Two weeks after it retired, delete the old copy's keys and secrets:

```sh
ship-fleet migrate wipe        # lists what goes and what stays, then asks
```

Earlier than that it refuses (it is kept for a rollback) unless you add `--now`; an
active copy is never wiped. It deletes:

- the fleet admin key and the CA keys that FleetLedger looks after, and their
  passphrases (keyring items or files);
- the Proxmox token and the OAuth secret;
- the inventory, `this-computer.json`, and everything else in
  `~/.config/fleetledger` and `~/.local/share/fleetledger`.

It keeps, and says so: keys you look after yourself (delete them yourself unless
something else uses them), and your own key that FleetLedger made, with its
passphrase, because the old computer still attaches to Ships with it. Afterwards
only `~/.config/fleetledger/retired.json` stays (no secrets), so that an older copy
of the fleet is refused here. A copy on standby can be wiped at once.

## Afterwards

- **The old computer is now a trusted machine like any other.** If it was "this
  computer" (trusted with your own key, so `ssh <ship>` worked there), its bundle
  in `~/.ssh/shipshape/` keeps working until its certificate lapses (7 days by
  default). Renew it from the new computer, as for any machine
  ([trust-machines.md](trust-machines.md), "Renew"):
  `ship-fleet trust <machine> --print-cert | ssh old-computer 'cat > ~/.ssh/shipshape/<machine>-cert.pub'`.
- **Do not `untrust` the old computer.** Its key is one of your keys for Ships, and
  the revocation list applies to every login: untrusting it would lock your own key
  out of every Ship.

## If something goes wrong

- **A move cut short** (the network dropped, Ctrl-C): nothing was written on the
  new computer; run it again. A receive that fails while writing takes back what
  it wrote, keyring items included. One killed outright while writing (a power
  cut) cannot: if the next run says "already has a FleetLedger", move
  `~/.config/fleetledger` and `~/.local/share/fleetledger` aside there and run it
  again.
- **Nothing else starts on the old computer during a push**, the web UI included,
  until the new computer has checked every Ship; a large fleet takes a while.
- **"This FleetLedger was retired"** or **"is on standby"**: this copy does not act
  on the fleet; the line says where it went or came from. Use the active copy, or
  hand over as above.
- **"has a retired FleetLedger"** (when moving the fleet back): wipe that copy first
  (`ship-fleet migrate wipe --now`), then push again.
- **"an older state"**: the fleet coming in is older than one this computer handed
  on. Move the fleet from the active copy instead.
- **"already has a FleetLedger"**: that computer has one. Move its files aside (or
  point `FLEETLEDGER_CONFIG_DIR` and `FLEETLEDGER_DATA_DIR` at empty folders).
- **"Not writing over files already here"**: a key of that name is already in the
  new computer's `~/.ssh` and is a different key. Rename or move it, then run the
  move again. The same key copied there before is fine.
- **"ssh could not log in"**: log in once with plain `ssh you@new-computer`, check
  its host key, then try again. Use `-o Port=2222` (repeatable) for ssh options,
  and `--ship-fleet ~/.local/bin/ship-fleet` if `ship-fleet` is not on its PATH.
