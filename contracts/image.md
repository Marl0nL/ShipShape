# Image contract: baked base images

Phase 4. A baked image is Ubuntu's verified cloud image with the slow, identical
part of provisioning already done, so a new Ship reaches `needs-sign-in` in
minutes. It is made by the same script that provisions every Ship
(`ship-provision`), run offline inside the image. The Ship track owns the bake
(`ship/image/bake`); the Fleet track drives it (`ship-fleet image build`) and
publishes the result through a driver.

Version 1. Schema: `image-manifest.schema.json`. Example: `examples/image-manifest/`.

## What is in a baked image, and what is not

In the image:

- the Ubuntu packages every Ship has (`ship-provision`'s base list);
- every `versions.toml` `[apt.*]` repository with its pinned keyring, and the
  packages of the core repositories (gh, google-cloud-cli, Node.js);
- herdr and uv, checked against their pinned sha256;
- `/tmp` on the disk: `tmp.mount` masked and `/etc/tmpfiles.d/tmp.conf`
  (`ship-runtime.md`, Paths), so a Ship from the image never has a tmpfs `/tmp`;
- the apt packages of the catalogue recipes marked `bake = true` that the bake
  was asked for (for example Google Chrome for `chrome-devtools-axi`);
- `/etc/shipshape/image.json`: the manifest, without its own `sha256`.

Never in the image: users (no `fleet`, no `agent`), SSH host keys, keys of any
kind, `ship.toml`, the tooling bundle, Claude Code, npm packages, sign-ins,
`tailscale`, or any seed. Everything per-Ship still comes from the seed at first
boot, exactly as with the stock image, and `ship-provision --first-boot` still
runs every step; on a baked image the baked steps find nothing to do.

The bake ends by removing what a first boot must create fresh: cloud-init's
state and logs (`cloud-init clean --logs`), `/etc/machine-id` (emptied), any SSH
host keys, apt's package cache, and `/var/lib/shipshape/provision.json`.

## `ship/image/bake`

```
bake --base IMG --bundle BUNDLE.tar.gz --manifest MANIFEST.json --out OUT.qcow2
     [--backend virt-customize|qemu] [--grow SIZE]
```

- `--base`: Ubuntu's cloud image, already verified by the caller (signature of
  `SHA256SUMS` against `versions.toml` `[ubuntu].signing_key`, and the pinned
  sha256). The bake checks its sha256 against `manifest.ubuntu.sha256` again and
  never modifies it: it works on a copy.
- `--bundle`: a tooling bundle (`seed.md`), packed from the same checkout.
- `--manifest`: the manifest to bake in (everything but `sha256` and
  `size_bytes`); `recipes` lists the recipes whose system parts are baked, and
  each must have `bake = true`.
- `--out`: the finished, compressed qcow2. Written to a temporary name and
  renamed at the end, so a failed bake leaves nothing that looks finished.
- `--backend virt-customize` (default): libguestfs, offline, no VM booted.
  `--backend qemu`: boots the copy once in local QEMU/KVM with a throwaway
  NoCloud seed that only runs the bake and powers off. Both run the same steps.
- Inside the image it runs `ship-provision --bake --recipes ID,...` from the
  unpacked bundle, writes `/etc/shipshape/image.json`, then cleans up (above).
  The clean-up is the very last write: with `--backend qemu` it runs from a unit
  ordered after cloud-init's final stage, then the copy powers off cleanly.
- Exit 0 and the qcow2 on success; non-zero and no output file otherwise.

## Image ids

`<ubuntu release>-<bake date, UTC, YYYYMMDD>-<first 8 hex of inputs_sha256>`,
for example `26.04-20261102-3f9a1c2e`. `inputs_sha256` is the SHA-256 of the
base image's sha256, `versions.toml`, the baked recipe files, `ship-provision`
and the bake scripts, so a rebuild from the same inputs on the same day has the
same id.

## Pinning and rollout

- `ships.toml` `[image.<id>]` records each built image (its sha256, base build,
  tooling, commit, baked recipes) and where it is published, per driver.
- `defaults.image` pins the image new Ships are made from; absent or `"stock"`
  means the driver's own template (the plain cloud image).
- Each Ship records the image it was made from (`ship.<name>.image`).
- Existing Ships are never re-imaged. They are updated in place (phase 2's
  `ship-fleet update`); a new image only changes how new Ships start.
- An image is verified against its recorded sha256 before every publish, and an
  image still pinned cannot be removed.
