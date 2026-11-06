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
ship-fleet migrate push you@new-computer    # move it there over SSH, then check every Ship

# Or, run from the new computer (where your keyring is unlocked):
ssh you@old-computer ship-fleet migrate send | ship-fleet migrate receive

# On the new computer, at any time later:
ship-fleet migrate verify                   # does the fleet work from here?
```

`push` and `receive` change nothing on the old computer. Afterwards **both copies
can act on the fleet: use only one of them** (two FleetLedgers acting at once can
undo each other's changes).

## Before you start

On the **new** computer:

1. Install FleetLedger from a ShipShape checkout **at the same commit** as the old
   computer's (`git -C <checkout> rev-parse HEAD` on both). `receive` refuses
   another commit: `ship-fleet update` packs `ship/` from the checkout, so an older
   one would roll Ships back.
2. Have no FleetLedger there yet. `receive` never writes over one: it refuses if
   `~/.config/fleetledger` or `~/.local/share/fleetledger` holds anything.
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
| `remote-ui.json` | the remote web UI setting ([remote-ui.md](remote-ui.md)): this computer's own name on the tailnet. Turn it on again on the new computer if you want it there |
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

`push` returns what `receive` returned: 0 when the new computer has the fleet and
reached every Ship; 5 when the fleet is written there but a check failed (fix it,
for example the network path to a Ship, then `ship-fleet migrate verify` there;
until then keep using the old copy); anything else when nothing was written there.

## Afterwards

- **Use only one copy.** The old copy is unchanged and can still act on the fleet;
  two FleetLedgers acting at once can undo each other's changes (an older checkout
  rolling Ships back, a stale inventory un-revoking a machine).
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
  it wrote, keyring items included.
- **"already has a FleetLedger"**: that computer has one. Move its files aside (or
  point `FLEETLEDGER_CONFIG_DIR` and `FLEETLEDGER_DATA_DIR` at empty folders).
- **"Not writing over files already here"**: a key of that name is already in the
  new computer's `~/.ssh` and is a different key. Rename or move it, then run the
  move again. The same key copied there before is fine.
- **"ssh could not log in"**: log in once with plain `ssh you@new-computer`, check
  its host key, then try again. Use `-o Port=2222` (repeatable) for ssh options,
  and `--ship-fleet ~/.local/bin/ship-fleet` if `ship-fleet` is not on its PATH.
