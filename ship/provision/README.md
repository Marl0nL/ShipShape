# ship/provision

`ship-provision`: the idempotent bash script that turns a fresh Ubuntu cloud image
into a Ship (W4). Called with `--first-boot` by the seed bootstrap and with
`--converge` by `ship-ctl converge`.

Must make `contracts/ship-runtime.md` true. Pinned versions, checksums and apt
signing keys come from `versions.toml` (copied into the bundle root).
