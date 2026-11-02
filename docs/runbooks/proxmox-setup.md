# Runbook: set up Proxmox for Ships (once)

What FleetLedger needs from a Proxmox VE server, and nothing more: one pool,
one small storage for seed ISOs, one bridge, a template VM, and an API token
scoped to those. About 15 minutes, as root on the Proxmox host. Nothing here
needs repeating per Ship.

The names below (`shipshape`, `shipshape-seeds`, `local-lvm`, `vmbr0`, VMID
`9000`) are examples: use your own and put them in `ships.toml` afterwards.

## 1. Pool, seed storage, roles, user and token

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

for who in "--users shipshape@pve" "--tokens shipshape@pve!build"; do
  pveum acl modify /pool/shipshape               $who --roles ShipShapeVM
  pveum acl modify /storage/shipshape-seeds      $who --roles ShipShapeSeeds
  pveum acl modify /storage/local-lvm            $who --roles ShipShapeDisks
  pveum acl modify /sdn/zones/localnetwork/vmbr0 $who --roles PVESDNUser
done
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

From Ubuntu's signed cloud image, at the build pinned in the repo's
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
  --cores 4 --cpu host --net0 virtio,bridge=vmbr0 --scsihw virtio-scsi-pci \
  --scsi0 local-lvm:0,import-from=$PWD/$IMG \
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
disk_storage = "local-lvm"
bridge = "vmbr0"
token_id = "shipshape@pve!build"
token_file = "~/.config/fleetledger/proxmox-token"
tls_fingerprint = "AB:CD:...:89"     # the SHA-256 fingerprint above
vmid_range = [9100, 9199]           # VMIDs FleetLedger may use
```

The token expires after 90 days: make a new one with the same `pveum user token
add` and the two `acl modify --tokens` lines, and replace the file.
