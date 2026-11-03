# Runbook: set up Proxmox for Ships (once)

What FleetLedger needs from a Proxmox VE server, and nothing more: one pool,
one small storage for seed ISOs, one bridge, a template VM, and an API token
scoped to those. About 15 minutes, as root on the Proxmox host. Nothing here
needs repeating per Ship.

The pool, seed storage, user and VMID (`shipshape`, `shipshape-seeds`,
`shipshape@pve`, `9000`) are suggestions; the VM disk storage and the bridge are
whatever your host has. Put the names you use in `ships.toml` afterwards.

## 1. Pool, seed storage, roles, user and token

First set the two names that differ per host (`pvesm status` lists the
storages; the bridge is the one your LAN is on, often `vmbr0`):

```sh
DISK_STORAGE=CHANGE-ME     # where VM disks go, e.g. local-lvm or local-zfs
BRIDGE=CHANGE-ME           # e.g. vmbr0
pvesm status --storage "$DISK_STORAGE" && ip link show "$BRIDGE" >/dev/null && echo names-ok
```

Then, still as root:

```sh
pveum pool add shipshape --comment "ShipShape Ships"
mkdir -p /var/lib/shipshape-seeds && chmod 700 /var/lib/shipshape-seeds
pvesm add dir shipshape-seeds --path /var/lib/shipshape-seeds --content iso

pveum role add ShipShapeVM --privs "VM.Allocate VM.Clone VM.Audit \
  VM.PowerMgmt VM.Config.Disk VM.Config.CDROM VM.Config.CPU \
  VM.Config.Memory VM.Config.Network VM.Config.Options VM.GuestAgent.Audit \
  Pool.Audit"
pveum role add ShipShapeSeeds --privs "Datastore.Audit \
  Datastore.AllocateTemplate Datastore.Allocate"
pveum role add ShipShapeDisks --privs "Datastore.Audit Datastore.AllocateSpace"

pveum user add shipshape@pve --comment "ShipShape, API only"
pveum user token add shipshape@pve build --privsep 1 \
  --expire $(date -d '+90 days' +%s)      # prints the secret once: keep it for step 3

# The same grants for the user and for its token (a privilege-separated token
# gets only the overlap). Keep the token id in single quotes: in an interactive
# shell an exclamation mark inside double quotes starts a history expansion.
pveum acl modify /pool/shipshape                   --users shipshape@pve --roles ShipShapeVM
pveum acl modify /storage/shipshape-seeds          --users shipshape@pve --roles ShipShapeSeeds
pveum acl modify "/storage/$DISK_STORAGE"          --users shipshape@pve --roles ShipShapeDisks
pveum acl modify "/sdn/zones/localnetwork/$BRIDGE" --users shipshape@pve --roles PVESDNUser
pveum acl modify /pool/shipshape                   --tokens 'shipshape@pve!build' --roles ShipShapeVM
pveum acl modify /storage/shipshape-seeds          --tokens 'shipshape@pve!build' --roles ShipShapeSeeds
pveum acl modify "/storage/$DISK_STORAGE"          --tokens 'shipshape@pve!build' --roles ShipShapeDisks
pveum acl modify "/sdn/zones/localnetwork/$BRIDGE" --tokens 'shipshape@pve!build' --roles PVESDNUser

pveum acl list | grep shipshape                    # eight lines: four paths, user and token
pveum user token permissions shipshape@pve build   # review: nothing beyond these
```

Why each piece:

- The seed storage is separate because a seed ISO holds the new Ship's private
  host key until it enrols; the right to delete seeds then reaches nothing else.
- The token is privilege-separated, so it holds the overlap of the user's and
  its own grants. It needs `Pool.Audit` to list the pool, and the guest-agent
  right to learn each Ship's address.
- No root, no SSH to the host, and no web login is needed after this.

## 2. The template VM

In the same shell as step 1 (it uses `$DISK_STORAGE` and `$BRIDGE`), from
Ubuntu's signed cloud image, at the build pinned in the repo's
`versions.toml` `[ubuntu]` (`base_url`, `signing_key`, and the image's `sha256`).
Check the signature with `gpgv` against that one key, then the checksum against
both `SHA256SUMS` and the pin:

```sh
cd /var/lib/vz/template/iso
U=https://cloud-images.ubuntu.com/releases/resolute/release-20261016   # [ubuntu].base_url
KEY=D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81                           # [ubuntu].signing_key
IMG=ubuntu-26.04-server-cloudimg-amd64.img
wget $U/$IMG $U/SHA256SUMS $U/SHA256SUMS.gpg
curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x$KEY" \
  | gpg --dearmor > ubuntu-cloud.gpg
gpg --show-keys --with-colons ubuntu-cloud.gpg | grep -q "^fpr:::::::::$KEY:" && echo key-ok
gpgv --keyring ./ubuntu-cloud.gpg SHA256SUMS.gpg SHA256SUMS
sha256sum -c --ignore-missing SHA256SUMS
echo "5645d0318ad21f643ebb1ac4567514620e9893380b07b0201fda163baa5b7eb5  $IMG" | sha256sum -c
                                                    # [ubuntu.images.amd64].sha256
qm create 9000 --name shipshape-base --pool shipshape --memory 8192 \
  --cores 4 --cpu host --net0 "virtio,bridge=$BRIDGE" --scsihw virtio-scsi-pci \
  --scsi0 "$DISK_STORAGE:0,import-from=$PWD/$IMG" \
  --boot order=scsi0 --serial0 socket --vga serial0 --agent enabled=1
qm template 9000
```

The template has no seed and no Proxmox cloud-init drive: each Ship gets its own
seed ISO on a CD-ROM (`contracts/seed.md`). Its disk must be `scsi0` and smaller
than the Ships you will make (FleetLedger grows it, never shrinks it).

## 3. On your computer

```sh
install -d -m 700 ~/.config/fleetledger
( umask 077; cat > ~/.config/fleetledger/proxmox-token )   # paste the secret, then Ctrl-D
```

Never paste the secret into a chat, an issue or a command line.

Get the API certificate's fingerprint on the Proxmox host, so FleetLedger can pin
it:

```sh
openssl x509 -in /etc/pve/local/pve-ssl.pem -noout -fingerprint -sha256   # on the host
```

Then add the driver to `~/.config/fleetledger/ships.toml` (see
[create-ship.md](create-ship.md) for the rest of that file):

```toml
[drivers.proxmox]
api_url = "https://pve.example.com:8006/api2/json"
node = "pve"
pool = "shipshape"
template_vmid = 9000
seed_storage = "shipshape-seeds"
disk_storage = "local-lvm"           # your $DISK_STORAGE
bridge = "vmbr0"                     # your $BRIDGE
token_id = "shipshape@pve!build"
token_file = "~/.config/fleetledger/proxmox-token"
tls_fingerprint = "AB:CD:...:89"     # the SHA-256 fingerprint above
vmid_range = [9100, 9199]           # VMIDs FleetLedger may use
```

The token expires after 90 days: make a new one with the same `pveum user token
add` and the two `acl modify --tokens` lines, and replace the file.

## 4. Snapshots and baked images (phase 4, optional)

Two additions to the same token, nothing else. Both are optional: without them
`ship-fleet snapshot` and `ship-fleet image publish` fail with a permission
error, and everything else works as before.

Snapshots need two more VM rights on the pool (`VM.Snapshot` to take and delete,
`VM.Snapshot.Rollback` to roll back). Adding them to the role reaches both the
user and the token, which already have the role on `/pool/shipshape`:

```sh
pveum role modify ShipShapeVM --append 1 --privs "VM.Snapshot VM.Snapshot.Rollback"
```

Publishing baked images through the API needs one more storage, used only for
uploading them (content type `import`; Proxmox VE 8.4 or newer). An image holds
no secrets, but keeping it apart keeps the token's delete right off every other
storage. The same seed-storage role covers upload, delete and reading it:

```sh
mkdir -p /var/lib/shipshape-images && chmod 700 /var/lib/shipshape-images
pvesm add dir shipshape-images --path /var/lib/shipshape-images --content import
pveum acl modify /storage/shipshape-images --users shipshape@pve --roles ShipShapeSeeds
pveum acl modify /storage/shipshape-images --tokens 'shipshape@pve!build' --roles ShipShapeSeeds
pveum user token permissions shipshape@pve build   # review again
```

and in `ships.toml`:

```toml
[drivers.proxmox]
# ...
image_storage = "shipshape-images"
```

What the token then does with an image: it uploads the qcow2 to
`shipshape-images`, creates a VM in the pool whose disk imports it
(`virtio0: <disk storage>:0,import-from=shipshape-images:import/<file>`), turns
that VM into a template, and deletes the upload. Creating the VM uses rights the
role already has (`VM.Allocate`, `VM.Config.*` on the pool,
`Datastore.AllocateSpace` on the disk storage, the bridge). Unlike the stock
template it has a virtio disk rather than a SCSI controller, and no serial
console: choosing either would need `VM.Config.HWType`, which the role
deliberately leaves out.

If you would rather not give these rights, publish images by hand instead:
`ship-fleet image publish <id> --driver manual` prints the `qm` commands to run
on the host, and `--template-vmid` records the result. See [images.md](images.md).
