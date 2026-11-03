# Runbook: baked images

A baked image is Ubuntu's verified cloud image with the slow, shared part of
provisioning already done (Ubuntu packages, the gh, gcloud, Node.js and Chrome
apt repositories and packages, herdr, uv). A Ship made from it skips those steps
at first boot, so it reaches `needs-sign-in` in minutes instead of about ten.
Everything per-Ship still comes from its own seed: users, host key, settings,
tooling, Claude Code, npm tools. The image holds no keys and no secrets
(`contracts/image.md`).

Images are optional. Without one, Ships start from the stock template exactly as
before.

## Build one

On your computer, from your ShipShape checkout:

```sh
ship-fleet image build
```

It:

1. downloads Ubuntu's cloud image at the build pinned in `versions.toml`
   (`[ubuntu]`), once, and checks it: `SHA256SUMS` must be signed by exactly the
   pinned key (checked with `gpgv` against that one key), and the image's sha256
   must match both `SHA256SUMS` and the pin;
2. packs the tooling bundle from this checkout;
3. runs `ship/image/bake`, which provisions a copy of the image offline with the
   same `ship-provision` every Ship runs (`--bake`: the shared system layer only),
   writes `/etc/shipshape/image.json`, cleans up (cloud-init state, machine id,
   host keys, logs, caches) and compresses the result;
4. records it in `ships.toml` as `[image.<id>]`, with its sha256.

The image lands in `~/.local/share/fleetledger/images/<id>.qcow2` (mode 0600),
with its manifest next to it. Ids look like `26.04-20261102-3f9a1c2e`: Ubuntu
release, bake date, and a hash of what went in.

Needs, on your computer: `qemu-img`, `gpg` and `gpgv`, `curl`, and one of

- `virt-customize` (package `libguestfs-tools`; the default backend, nothing is
  booted), or
- `qemu-system-x86_64` with KVM (`--backend qemu`): the copy boots once in local
  QEMU with a throwaway seed served on loopback, bakes and powers off. Use this
  where libguestfs is not installed or cannot read the host kernel.

Both need internet access for the apt packages. A bake takes 5 to 15 minutes.

`--recipes chrome-devtools-axi` limits which catalogue recipes have their system
parts baked; the default is every recipe marked `bake = true`.

## Publish it

```sh
ship-fleet image publish 26.04-20261102-3f9a1c2e            # driver: defaults.driver
```

- **proxmox**: uploads it to `image_storage`, creates a template VM in the pool
  from it (`shipshape-img-<id>`, a VMID from `vmid_range`) and deletes the
  upload. Needs the phase 4 rights in [proxmox-setup.md](proxmox-setup.md)
  section 4. The local file is checked against its recorded sha256 first, and
  Proxmox checks the upload against the same sum.
- **manual**, or a Proxmox server where you would rather not give those rights:
  `--driver manual` prints the `qm` commands to run on the host; afterwards
  record the template with
  `ship-fleet image publish <id> --driver proxmox --template-vmid <vmid>`.

## Pin it (rollout)

```sh
ship-fleet image use 26.04-20261102-3f9a1c2e   # new Ships start from it
ship-fleet image use stock                     # back to the stock template
ship-fleet image ls                            # * marks the pinned image
```

Only new Ships change. Existing Ships are never re-imaged: they are updated in
place (phase 2's `ship-fleet update`). Each Ship records the image it was made from
(`ship.<name>.image` in `ships.toml`, the `SHIPS` column of `image ls`).
`ship-fleet new --image <id>` picks another image for one Ship.

A sensible rollout: build, publish, create one Ship with `--image <id>`, check it
reaches `needs-sign-in` and signs in, destroy it, then `image use <id>`.

## Rebuild

One command, whenever `versions.toml` or `ship-provision` changes, or to pick up
Ubuntu's latest packages:

```sh
ship-fleet image build && ship-fleet image publish <new id> && ship-fleet image use <new id>
```

## Remove an old image

```sh
ship-fleet image rm 26.04-20261016-0a1b2c3d
```

Refused while the image is pinned. It deletes the template through each driver it
was published to (Proxmox checks the VM is the image's template by name first),
then the local file. Ships made from it keep running: they are full copies.

## Check on real hardware

What the unit tests cannot cover, to run once on a Proxmox server, with what a run
on Proxmox VE 9.2 (phase 4 rights, a QEMU-backend bake) gave:

- `ship-fleet image build` with each backend you use; the image boots.
  (qemu backend: checked; virt-customize: not yet.)
- `image publish` with the phase 4 token rights, and `pveum user token
  permissions` shows nothing beyond section 4. (41 s; rights as expected.)
- `ship-fleet new ship-xx --image <id>` reaches `needs-sign-in` within 5 minutes,
  with no Proxmox UI clicks, and its seed ISO is deleted afterwards. (145 s.)
- `snapshot take`, a change, `snapshot rollback`: the change is gone and the Ship
  comes back on its pinned key. (Checked.)
- `destroy` and `image rm` leave nothing in the pool or either storage.
  (Checked.)
