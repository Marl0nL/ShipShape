# The vault: every secret, unlocked once per boot

FleetLedger needs a few secrets to manage the fleet:

- the passphrases of the keys it looks after (its admin key, which logs in to every
  Ship, and the host and user CA keys);
- the Proxmox API token;
- the Tailscale OAuth client secret.

On a desktop they sit in the OS keyring, which your login unlocks. An always-on
computer with no desktop session (a small VM) has no such keyring, and until now the
only choice there was a `0600` file for each, beside the encrypted keys. Then any
copy of that disk (a backup, a snapshot, a stolen image) opens the whole fleet.

The **vault** is FleetLedger's own keyring: one encrypted file, `vault.json`, holding
every one of those secrets. It is unlocked **once after each boot**, with **one
passphrase**, from the web UI or over ssh. Until then FleetLedger is read-only, and
your Ships keep running. One unlock opens every secret: there is never one unlock
per key.

It is off unless you turn it on. On a desktop with a keyring, nothing changes unless
you choose it.

```sh
ship-fleet vault init          # make it (you choose the passphrase), move every secret in
ship-fleet unlock              # once after each boot (over ssh is fine; or Unlock in the web UI)
ship-fleet vault status        # off, locked or unlocked, and what it holds
ship-fleet vault lock          # lock it now
ship-fleet vault passphrase    # change the passphrase
ship-fleet vault recovery      # a new recovery code (the old one stops working)
ship-fleet vault off           # move every secret back to the OS keyring (or files)
```

## What is encrypted with what

| What | Encrypted with | Where |
|---|---|---|
| Each secret (a key's passphrase, the Proxmox token, the OAuth secret) | the **data key**: 32 random bytes, made once at `vault init` (Fernet: AES-128-CBC with a random IV, then HMAC-SHA256 over everything) | `vault.json`, beside `ships.toml` (0600) |
| The data key | a key derived from **your passphrase** by scrypt (N=2^17, r=8, p=1: 128 MiB of memory and about a second per try; a fresh random salt each time it is written) | `vault.json`, the "passphrase" slot |
| The data key, again | a key derived the same way from the **recovery code** (160 random bits, shown once) | `vault.json`, the "recovery" slot |
| The managed SSH keys (admin, host CA, user CA) | OpenSSH's own key encryption, with a random 256-bit passphrase each, kept in the vault | `~/.ssh/shipshape_*` |
| The data key, while unlocked | nothing: it is kept in RAM only | `$XDG_RUNTIME_DIR/fleetledger/vault/key` (0600, in a 0700 folder, on a tmpfs) |

The cryptography is the `cryptography` package's: scrypt and Fernet, used as they
are. FleetLedger adds no cipher, mode or padding of its own. Why these:

- **Fernet** is a whole authenticated-encryption recipe, not a toolkit: a random IV
  per message, encrypt-then-MAC, constant-time checks. Nothing is left to get wrong.
  A file changed by even one bit fails to open.
- **scrypt** is a memory-hard KDF, as used by age and others. Its cost is written in
  the file (within fixed bounds, so a file edited to ask for a huge cost is refused,
  not run), so it can be raised later without a new format.
- **One data key under two slots** (as LUKS does) is what makes the recovery code and
  passphrase changes cheap: changing the passphrase rewraps 32 bytes, not every
  secret.

## When it is locked, and when unlocked

- **At every boot it is locked.** The unlocked data key lives only in
  `$XDG_RUNTIME_DIR`, which is RAM (tmpfs) and gone at power-off. FleetLedger refuses
  to unlock into a folder that is not in RAM.
- **Restarting FleetLedger keeps it unlocked** (an update, `ship-fleet service
  restart`). The key stays in RAM until a reboot or `ship-fleet vault lock`: **one
  unlock per boot**.
- **Every FleetLedger process shares the one unlock**: the service, the web UI, and
  commands over ssh read the same key from RAM. `vault lock` deletes it, so all of
  them stop reading secrets at once. It also takes the managed keys out of ssh-agent.

**Locked, FleetLedger is read-only.**

- The web UI shows every page from the last known state, with a banner and an
  **Unlock** button. Every change is refused except unlocking (and signing in, pairing
  and revoking devices). The poller stops polling Ships, since it cannot log in to
  them.
- On the command line, every command but `unlock`, `vault`, `doctor`, `ui` and
  `service` refuses and says to unlock. `--offline` reads (`ship-fleet ls --offline`)
  still work.
- **Your Ships keep running.** They do not depend on FleetLedger. Agents resume,
  Remote Control works, and your laptop's own certificate still attaches until it
  lapses (7 days by default).

**Unlocking** takes the passphrase in a terminal (`ship-fleet unlock`, typed, never
echoed) or in the web UI's Unlock page (a password field, posted with the page's CSRF
token, never echoed or logged). Both run the same function:

1. derive the slot key from the passphrase with scrypt;
2. open the data key;
3. check that it opens the secrets;
4. keep it in RAM.

A wrong passphrase is counted, also in RAM, and tries are taken one at a time (across
every process), so a burst of them can neither slip past the count nor run scrypt side
by side. After 3 wrong tries each further try from the web waits 10 seconds, doubling,
up to 15 minutes, on top of scrypt's own second. `ship-fleet unlock` in a terminal of
FleetLedger's own user is not held to that wait (that user could clear the count
anyway), so a lost phone guessing at the web page cannot keep you out; its tries are in
the audit log with its Tailscale login, and `ship-fleet ui revoke` ends them. Each
unlock, wrong try and lock is in the audit log (with the device's Tailscale login
when it came from the web).

Unlocking from a paired remote device does not need a fresh pairing, unlike destroy
or rollback. The passphrase is itself a stronger proof than how recently the browser
paired. Asking for both would mean an ssh session to re-pair after every reboot.

## Threat model

| Who or what | Gets | Why |
|---|---|---|
| **A copy of the disk**: a Proxmox backup, a snapshot without RAM, a stolen image, a copied `~/.config` | **nothing usable** | every secret is in `vault.json` under scrypt and Fernet, the SSH keys are encrypted with passphrases that are only in the vault, and the unlocked key was never on disk |
| The same, plus the passphrase or the recovery code | everything | that is what they are for: keep them as you would the fleet itself (a password manager) |
| An old backup, after you changed the passphrase | opens with the **old** passphrase | the old `vault.json` keeps its old slot. If an old passphrase leaked, the secrets themselves are exposed: rotate them (below) |
| **A snapshot or backup that includes RAM** (Proxmox "Include RAM", suspend to disk, hibernation) | everything, while it was unlocked | RAM holds the data key. Take snapshots without RAM. Proxmox's vzdump backups copy disks, not RAM |
| **Swap** | possibly the data key | RAM pages can be written to swap. The appliance runs without swap; `vault status` and `doctor` warn when swap is on |
| Someone running commands **as FleetLedger's user** (or root) **while unlocked** | everything | they can read the RAM key, as they could read an unlocked desktop keyring. The vault protects data at rest, not a compromised running machine |
| The same, **while locked** | the encrypted file, and the chance to watch for your next unlock | with root on the VM, assume the next passphrase typed there is theirs |
| A **paired browser** (stolen laptop, compromised phone) | the fleet while unlocked, as before ([remote-ui.md](runbooks/remote-ui.md)); **not** the secrets themselves; while locked, only guesses at the passphrase, slowed as above | |
| **Another local user** | nothing | every file is 0600 in a 0700 folder |
| **A Ship** | nothing | Ships never reach FleetLedger |

What the vault does not cover, on purpose:

- **A Ship's seed, until it enrols.** A new Ship's seed ISO carries that Ship's
  private host key, in `~/.local/share/fleetledger/seeds/` (and on the hypervisor's
  ISO storage) until the Ship enrols. It is deleted once the Ship enrols.
- **The computer's own identity.** Its sshd host keys and its Tailscale node key are
  in a backup too. A restored copy could join the tailnet as this computer and reach
  Ships' port 22, but could not log in: that needs the admin key or a user
  certificate, both locked in the vault.
- **Files you send to Ships**, while they wait in the upload spool.
- **Your own key**, if FleetLedger made it and this computer attaches with it: its
  passphrase is in the vault like the others, so it opens only while unlocked.

## Rotation

- **The passphrase:** `ship-fleet vault passphrase`. It asks for the current passphrase
  (or `--recovery` and the recovery code), then the new one twice. Only the
  passphrase slot is written again (a fresh salt). The data key, the secrets and the
  recovery code stay as they were. Backups taken before still open with the old
  passphrase.
- **The recovery code:** `ship-fleet vault recovery` (asks for the passphrase) shows a
  new one once. The old one stops working, in this `vault.json` only. If it (or `vault
  init`) is cut short before the code is shown, the passphrase still works but you have
  no code you know of: run `ship-fleet vault recovery` again.
- **The secrets themselves:** if the passphrase leaked together with a backup, or the
  machine was compromised while unlocked, a new passphrase does not help. Rotate what
  the vault guards:
  - make a new Proxmox token (`ship-fleet setup --redo hypervisor`) and delete the old
    one in Proxmox;
  - make a new OAuth client (`ship-fleet overlay setup --force`) and revoke the old one;
  - for the CA and admin keys, see [trust-machines.md](runbooks/trust-machines.md) and
    [security.md](security.md).

## Recovery if the passphrase is lost

In order:

1. **The recovery code.** Unlock with it, then set a new passphrase:

   ```sh
   ship-fleet unlock --recovery
   ship-fleet vault passphrase --recovery    # asks the code again, then the new passphrase
   ```

2. **The copy you moved from**, if it is still retired but not wiped (two weeks after
   a `ship-fleet migrate`). There, `ship-fleet migrate unretire`, then move the fleet
   back to the VM with `ship-fleet migrate push`. The VM gets a new vault with a new
   passphrase.
3. **None of these.** The secrets are gone. Your Ships keep running, but nothing can
   log in to them as FleetLedger. Rebuilding control means:
   - putting a new admin key on every Ship by hand, from its console;
   - a new CA: `ship-fleet ca sign` on every Ship, and trusting every machine again;
   - a new Proxmox token and OAuth client;
   - adopting each Ship again.

   Keep the recovery code somewhere safe and apart from the passphrase.

A copy handed over by `ship-fleet migrate` (on standby, or retired) refuses almost
everything, but `unlock` and `vault` still work there: after a reboot, a copy waiting
to be activated, unretired or wiped must be able to open its vault first.

## Turning it on, on the desktop or a VM

`ship-fleet vault init`, in a terminal, as the user FleetLedger runs as:

1. checks the runtime folder is in RAM;
2. asks for a passphrase twice (at least 12 characters; a few words is easiest);
3. reads every secret `ships.toml` names from where it is now (OS keyring or `0600`
   files). If any cannot be read, nothing changes;
4. writes `vault.json` with them, unlocked;
5. points `ships.toml` at the vault (`store = "keyring"`: the vault is this computer's
   keyring now);
6. reads each secret back through FleetLedger's own readers;
7. only then deletes the old copies (the OS keyring items, the files). If anything
   fails, or you press Ctrl-C, before `ships.toml` is written, the new vault is taken
   back: nothing changed, and `vault init` can simply run again. From that write on,
   the vault stays (it already holds every secret, and may be the only copy of some),
   and old copies may be left behind: `vault status` lists them, and `vault init`
   again finishes the move (it moves in what is not in yet, and deletes each old copy
   that holds exactly what the vault does; one that differs is only reported);
8. shows the **recovery code** once, and asks you to type its last group to show you
   have it.

On a new computer with no fleet yet (the appliance, before `ship-fleet migrate`), the
vault starts empty. `migrate receive` then stores each secret straight into it.

While the vault is on, a secret never goes to a `0600` file: setup, overlay setup and
`migrate --store file` refuse, and a secret setup finds in a `0600` file (one you put
there by hand) goes into the vault, and the file goes. `ship-fleet vault off
[--store keyring|file]` moves everything back out, in the same order: read, write and
read back, point, delete. A write that fails before `ships.toml` names the files
takes back what it wrote and leaves the vault as it was; from that write on the files
stay, since `ships.toml` reads them. A file already holding the very value is kept as
it is, so a `vault off` cut short at any point can simply run again. The vault must be
unlocked for that.

`ships.toml` says `keyring` for a secret in the vault, because the vault is this
computer's keyring for FleetLedger. Which keyring that is, is a fact about the
computer, not the fleet, so `ship-fleet migrate` stores each secret again in the new
computer's own store.

## Files

| Path | What |
|---|---|
| `~/.config/fleetledger/vault.json` | the vault: slots and encrypted secrets (0600). Its presence turns the vault on. Without the passphrase it tells only its size, scrypt's cost and when each part was last written (a Fernet token carries its time in the clear) |
| `$XDG_RUNTIME_DIR/fleetledger/vault/key` | the unlocked data key (0600, RAM only) |
| `$XDG_RUNTIME_DIR/fleetledger/vault/tries.json` | the count of wrong tries (RAM only) |
| `$XDG_RUNTIME_DIR/fleetledger/vault/.lock` | one writer at a time |
