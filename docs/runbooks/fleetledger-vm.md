# Runbook: FleetLedger on its own small VM

Move FleetLedger off your desktop onto a small, always-on VM on the same Proxmox host
as the Ships. You reach it from your laptop, desktop and phone through the web UI
(Tailscale Serve) and over ssh. Its secrets sit in one vault that you unlock once
after each boot. **Your Ships keep working throughout, with nothing changed on any of
them:** the same admin key, the same CA, the same pins and revocation list move with
the fleet (`ship-fleet migrate`).

The pieces this uses, each with its own page:

- the service: [fleetledger-service.md](fleetledger-service.md);
- the vault: [../vault.md](../vault.md);
- the remote web UI: [remote-ui.md](remote-ui.md);
- the move: [move-fleetledger.md](move-fleetledger.md).

```sh
# On the desktop (FleetLedger as it is today):
ship-fleet appliance seed --name fleetledger --vmid 120
# ... create the VM with the printed qm steps, then, once it has booted:
ssh vmadmin@fleetledger.local    # Tailscale: install, join as tag:fleetledger
ssh fleetledger@fleetledger.local ship-fleet vault init
ship-fleet migrate push fleetledger@fleetledger.local
# On the VM, from then on:
ssh fleetledger@fleetledger ship-fleet unlock    # once after each boot (or Unlock in the web UI)
```

## What it is, and what keeps it safe

**The VM.** It runs the stock Ubuntu cloud image pinned in `versions.toml` for Ships,
checked by its sha256, plus a seed that FleetLedger writes. 2 vCPUs, 4 GB of memory
and 40 GB of disk are plenty (more disk if you build images there). It has two users,
and your keys are the only way in to either:

- `fleetledger` runs FleetLedger and has no sudo;
- `vmadmin` has sudo, for the system itself (Tailscale, updates).

There are no passwords and root logins are off. The seed:

- clones this ShipShape checkout at **this very commit** (migrate needs both
  computers on the same commit) and installs it;
- installs the service unit, without starting it;
- puts `ship-fleet` on the PATH a plain ssh command gets, so `ssh fleetledger@<vm>
  ship-fleet ...` works as written (ssh runs a command with no login shell, and pipx's
  `~/.local/bin` is only on a login shell's PATH): `/usr/local/bin/ship-fleet`,
  root-owned, runs the `fleetledger` user's own install and refuses anyone else, so
  `vmadmin` never runs code that `fleetledger` can change;
- turns lingering on, so the service starts at boot;
- lets a reboot wait for running jobs (up to 25 minutes, inside the 30 systemd gives a
  reboot before forcing it; [fleetledger-service.md](fleetledger-service.md));
- turns swap off.

**Outside the Ship pool.** The `qm` steps put the VM in **no pool**. FleetLedger's
Proxmox token only reaches its pool, so it cannot see, let alone destroy, its own VM.
That is the strongest guard there is.

**`[fleet] self`.** The seed also records the VM in the inventory, and every layer
then refuses to act on it:
- no Ship may have its name, VM id or host key (the inventory refuses);
- the Proxmox driver refuses every call on its VM id, whatever the token can see;
- the manual driver prints no step for it.

So FleetLedger can never destroy, resize, stop, snapshot or `update` itself, and fleet
actions (`--tag`, `all`) only ever pick Ships. `ship-fleet doctor` checks that the
token cannot see the VM. Its VM id must be outside the Ships' range
(`drivers.proxmox.vmid_range`, 9100–9199 by default); the seed refuses one inside.

**Its own host key, pinned.** The seed carries the VM's ssh host key, made on your
desktop. `[fleet] self` pins it, so `ship-fleet migrate push` to the VM checks it by
itself, and the seed prints the `known_hosts` line for your own ssh. The seed ISO
holds that private host key: delete it once the VM has booted (below).

**Its tailnet identity: `tag:fleetledger`.** It needs Tailscale because Ships with
`ssh_listen=overlay` only take logins that arrive on the overlay. Tagged devices do
not expire. Once `[fleet] self` is set, `ship-fleet overlay policy` prints two more
rules beside the Ships':
- `tag:fleetledger` may reach the Ships on port 22;
- your own devices (`autogroup:member`) may reach it on 22 (ssh, unlock) and 443 (the
  web UI).

A `tests` entry proves a Ship cannot reach it. Ships still get no destinations at all.

## 1. On the desktop: the seed

```sh
ship-fleet appliance seed --name fleetledger --vmid 120
```

Options:
- `--host NAME`: names to reach it by (default `fleetledger.local`; add its tailnet
  name later with `--force`, or just use it);
- `--key PUB`: who may log in (default your keys for Ships, `fleet.agent_keys`);
- `--repo URL`: the https address to clone ShipShape from (default this checkout's
  `origin`; the commit must be pushed);
- `--port N`: the web UI's port (8765).

It writes `~/.local/share/fleetledger/seeds/appliance-fleetledger.iso` (0600), records
`[fleet] self`, and prints the steps.

## 2. On Proxmox: the VM

Upload the ISO to your ISO storage, then run the printed steps as root on the host. In
short:

```sh
wget -O /var/tmp/<image> <pinned Ubuntu cloud image>
echo '<pinned sha256>  /var/tmp/<image>' | sha256sum -c
cat /sys/module/kvm_intel/parameters/nested 2>/dev/null || cat /sys/module/kvm_amd/parameters/nested
qm create 120 --name fleetledger --memory 4096 --cores 2 --cpu host --ostype l26 \
  --net0 virtio,bridge=<bridge> --scsihw virtio-scsi-pci \
  --scsi0 <disk-storage>:0,import-from=/var/tmp/<image> \
  --ide2 <iso-storage>:iso/appliance-fleetledger.iso,media=cdrom \
  --boot order=scsi0 --serial0 socket --vga serial0 --onboot 1
qm resize 120 scsi0 40G
qm start 120
```

**Image builds and nested virtualisation (`--cpu host`).** Baking Ship images
(`ship-fleet image build`) wants KVM:

- **If the `nested` line says `Y` or `1`**, keep `--cpu host`: the VM builds images
  itself, quickly. `ship-fleet doctor` on the VM says whether it has KVM.
- **If not**, use `--cpu x86-64-v2-AES` and build images on your desktop instead,
  where the retired copy may still build an image file (nothing else):

  ```sh
  ship-fleet image build --out ~/images                 # on the desktop
  scp ~/images/<id>.qcow2 ~/images/<id>.json fleetledger@fleetledger:
  ssh fleetledger@fleetledger ship-fleet image import <id>.qcow2
  ```

  `import` checks the file against its manifest's sha256 and records it as if built
  there. Then `image publish` and `image use` as ever ([images.md](images.md)).

Watch the first boot on the serial console (`qm terminal 120`): cloud-init installs
packages and FleetLedger, which takes a few minutes. Then:

```sh
qm set 120 --delete ide2                       # on the host: the seed's CD is no longer needed
pvesm free <iso-storage>:iso/appliance-fleetledger.iso
rm ~/.local/share/fleetledger/seeds/appliance-fleetledger.iso   # on the desktop
```

Delete the seed in both places: it holds the VM's private host key. `ship-fleet migrate`
refuses to move while it is still there.

Add the printed line to `~/.ssh/known_hosts` on each computer you ssh from, then
`ssh vmadmin@fleetledger.local` must log in without asking about the host key.

## 3. On the VM: Tailscale and the vault

As `vmadmin` (the documented installer, then join as the tag):

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --advertise-tags=tag:fleetledger
sudo tailscale set --operator=fleetledger      # so FleetLedger may check and run Serve
```

In the admin console, paste the rules `ship-fleet overlay policy` prints on the desktop
(they include `tag:fleetledger`'s), and give the VM a bland name: its HTTPS
certificate makes the name public ([remote-ui.md](remote-ui.md)).

As `fleetledger`, turn the vault on. It is empty for now; the move fills it:

```sh
ssh -t fleetledger@fleetledger ship-fleet vault init
```

Choose a passphrase you will type once after each boot. Keep the **recovery code** it
shows apart from the passphrase (a password manager): it is the way back if the
passphrase is lost ([../vault.md](../vault.md#recovery-if-the-passphrase-is-lost)).

## 4. Move the fleet

On the desktop, stop FleetLedger there (Ctrl-C the UI, or `systemctl --user stop
fleetledger`), then:

```sh
ship-fleet migrate check
ship-fleet migrate push fleetledger@fleetledger
```

Push checks the VM's pinned host key by itself. It streams everything into `migrate
receive` on the VM, which:
- stores every secret in the VM's vault (no file, no OS keyring);
- reaches every Ship read-only, over the LAN or the tailnet;
- hands over: the desktop retires, the VM activates.

Nothing on any Ship changes. Details, rollback and what moves:
[move-fleetledger.md](move-fleetledger.md).

## 5. On the VM: the service and the web UI

```sh
ssh fleetledger@fleetledger
tailscale serve --bg 127.0.0.1:8765
ship-fleet ui remote https://fleetledger.<tailnet>.ts.net --port 8765 --login you@example.com
systemctl --user start fleetledger
ship-fleet ui link --name laptop      # pair the laptop; --qr for the phone
```

From now on the VM runs FleetLedger, always on. The desktop is an ordinary trusted
machine: renew its certificate from the web UI (Download bundle) like the laptop's.
After two weeks, once you are happy: `ship-fleet migrate wipe` on the desktop.

## Every boot: unlock, once

A boot (a Proxmox update, a power cut) leaves the vault locked. FleetLedger then shows
the last known state and changes nothing, and your Ships keep running. Unlock it once,
for every secret, until the next boot:

- the web UI's **Unlock** button, on a paired browser; or
- `ssh -t fleetledger@fleetledger ship-fleet unlock`.

Restarts of FleetLedger itself (`ship-fleet service restart`, `self-update`) keep it
unlocked.

## Backups

- **Proxmox backups of the VM** (vzdump or PBS) are safe to keep anywhere: with the
  vault, the disk holds nothing usable without the passphrase. Take snapshots
  **without RAM**: RAM holds the unlocked key.
- **FleetLedger's own encrypted backup**, daily, somewhere else:

  ```sh
  ship-fleet backup schedule --to /mnt/nas/fleetledger --at 03:30 --keep 14
  ship-fleet backup now --to /mnt/nas/fleetledger       # one now
  ```

  Each backup is the whole fleet (what `migrate` would move), sealed under the vault:
  it opens with the vault's passphrase or recovery code as they were when it was made,
  and nothing else. It runs only while the vault is unlocked.

  The folder must be off the VM's own disk, a NAS mount say: a backup on the same disk
  goes with it. `backup` refuses a folder on this computer's own disk, which is also
  what an unmounted share looks like, so a share that failed to mount gets no backup
  rather than a misleading one. The timer's unit waits for the mount
  (`RequiresMountsFor`). `--same-disk` keeps them here anyway, if you pull the files to
  your desktop yourself (`scp fleetledger@fleetledger:/path/*.flbackup
  ~/fleetledger-backups/`).

  Every backup's result is kept: `ship-fleet doctor` and Settings ▸ Health show the
  last one, and warn when it failed or is over two days old. A backup skips what would
  only stop a move (a Ship still being made and its seed, a file FleetLedger does not
  know) and lists it; it still refuses on a handed-over copy, a secret it cannot read,
  or uncommitted changes in the checkout (a restore needs a commit to match).

**Disaster: the VM is gone.** Restore the latest backup onto a new VM. The backup must
match a ShipShape commit you can check out; the seed clones the commit you run it from.

1. **A seed for the new VM.** Your desktop's copy is retired, so it makes no seed for
   the fleet; make one from an empty FleetLedger home on it (it records nothing):

   ```sh
   FLEETLEDGER_CONFIG_DIR=$(mktemp -d) FLEETLEDGER_DATA_DIR=$(mktemp -d) \
     ship-fleet appliance seed --name fleetledger --vmid 121 --key ~/.ssh/id_ed25519.pub
   ```

   Then steps 2 and 3 above: the `qm` steps, the first boot, deleting the seed,
   Tailscale.
2. **The vault first**, on the new VM, so the secrets land in it and not in 0600 files
   on its disk: `ship-fleet vault init` (it starts empty).
3. **Restore**, on the new VM:

   ```sh
   ship-fleet migrate import fleetledger-<when>-e<epoch>.flbackup     # asks the vault passphrase the backup was made with
   ship-fleet migrate activate
   ship-fleet appliance here --vmid 121       # the restored inventory still names the old VM
   systemctl --user start fleetledger
   ```

   `import` checks every Ship read-only before anything acts, exactly as `receive`
   does. A Ship that was still being made when the backup ran (its seed is not in the
   backup) and never came up is a warning, not a failure: once this copy is active,
   finish it (`ship-fleet status NAME --wait`) or destroy it. `activate` raises the epoch, so an older state is refused here. `appliance
   here` records this VM (its hostname, its own sshd host key, the VM id you give) as
   `[fleet] self`, in place of the old one.

On your desktop instead (no new VM): the same `migrate import` and `activate` in a
home with no FleetLedger; its OS keyring takes the secrets.

**If the old VM comes back**, it is locked (it rebooted), and must step aside before
anyone unlocks it. On it, as `fleetledger`:

```sh
systemctl --user stop fleetledger      # its service started at boot and holds FleetLedger's lock
ship-fleet migrate retire --to <the new VM> --yes      # works while locked
```

## Updates

```sh
ship-fleet self-update               # to origin/main; --to v1.2 for a tag
```

It refuses uncommitted changes, and refuses going back unless `--force`. It
reinstalls only if the install itself changed, and restarts the service, which lets
running jobs finish first. **No Ship changes:** FleetLedger's checkout is also the
next Ship tooling, so roll it out when you choose, with `ship-fleet update`
([update-fleet.md](update-fleet.md)).

System updates are `vmadmin`'s: `sudo apt upgrade`, and a reboot when one is needed
(then unlock).

**A VM seeded before `/usr/local/bin/ship-fleet` came in** answers `ship-fleet: command
not found` to a plain `ssh fleetledger@fleetledger ship-fleet ...`. Once it has
self-updated past that, add the file as `vmadmin` (read it first: it is short):

```sh
sudo install -o root -g root -m 0755 \
  /home/fleetledger/ShipShape/fleetledger/fleetledger/seed/appliance-ship-fleet.sh \
  /usr/local/bin/ship-fleet
```

**Testing unmerged ShipShape changes:**
- keep a separate dev fleet (its own inventory and test Ships) on your desktop; or
- check a branch out on the VM (`ship-fleet self-update --to origin/my-branch --force`)
  and back.

## When FleetLedger is down

The Ships keep running, resume their agents and serve Remote Control. Your laptop and
desktop still attach with their certificates until those lapse (7 days). What waits
for FleetLedger to come back:
- making and destroying Ships;
- the sign-in concierge (you can still sign in by hand as `agent`);
- certificate renewals;
- the dashboard.

## Checking it by hand, once

CI cannot run a VM managing its own host, so once, after the move:

1. `ship-fleet doctor` on the VM: every check ok, including "FleetLedger's own VM: out
   of the token's reach" and, with `--cpu host`, "image builds: KVM here".
2. `ship-fleet appliance show`: the VM id and pinned host key are right.
3. Reboot the VM (`sudo reboot` as `vmadmin`). Afterwards the web UI says FleetLedger is
   locked, every page still shows the fleet, and `ship-fleet ls --offline` works. Then
   unlock from the web UI: Ships are checked again within a minute.
4. `ship-fleet backup now --to /tmp/check --same-disk`, then on your desktop, in an empty
   FleetLedger home (`FLEETLEDGER_CONFIG_DIR` and `FLEETLEDGER_DATA_DIR` set to empty
   folders), `ship-fleet migrate import` it with the passphrase: every Ship verified,
   on standby. Delete that home afterwards.
