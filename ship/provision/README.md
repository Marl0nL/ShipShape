# ship/provision

`ship-provision` (W4): the idempotent bash script that turns a fresh Ubuntu cloud
image into a Ship and makes `contracts/ship-runtime.md` true. Pinned versions,
checksums and apt signing keys come from `versions.toml` at the bundle root.

```sh
ship-provision --first-boot   # seed bootstrap: everything, then apply, tools, start herdr
ship-provision --converge     # ship-ctl converge: everything; ship-ctl then applies and runs tools
ship-provision --converge --dry-run      # on a Ship: what would change, changing nothing
ship-provision --first-boot --root DIR   # anywhere: write the files under DIR, skip the rest
ship-provision --bake --recipes ID,...   # inside an image being baked (ship/image/bake)
```

`--bake` (phase 4, `contracts/image.md`) runs only steps 1 to 3 below plus the apt
packages of the given recipes (each must be marked `bake = true`). It creates no
users' files, keys, settings or units and writes no provisioning record, so a
Ship from a baked image still provisions itself fully at first boot; the baked
steps just find nothing to do.

What it does, in order (each step checks first and only counts real changes):

1. Ubuntu packages (git, jq, tmux, avahi, qemu-guest-agent, ...).
2. Every `[apt.*]` repo in `versions.toml`, as a deb822 source with its own
   keyring. A downloaded key is used only if every primary key in it is pinned.
   Packages of the GitHub, Google Cloud and NodeSource repos are installed; Google
   Chrome comes from the chrome-devtools-axi recipe.
3. herdr and uv, downloaded and checked against their pinned sha256.
4. ShipShape files: the resume shim, the mode-ledger hook, `/usr/local/bin/ship-ctl`,
   `/etc/profile.d/shipshape.sh`.
5. The agent's home folders, its npm prefix (`~/.local`) and lingering.
6. Claude Code with the native installer, as `agent` (`install-claude.sh`: the
   release manifest's signature and the binary's checksum are checked), once;
   then it updates itself. herdr's Claude integration.
7. The sshd, logind and journald drop-ins, then the units: `ship-status.timer`
   (system), `herdr.service` and `ship-resume.service` (in `/etc/systemd/user`,
   root-owned, `ConditionUser=agent`).
8. First boot only: `ship-ctl apply`, `ship-ctl tools run`, start herdr, then
   `ship-resume` (nothing to resume yet, but its record lets autostart run at once).

The result goes to `/var/lib/shipshape/provision.json`, which `ship-ctl status`
reports as `provision.result`. Output goes to `/var/log/shipshape/provision.log`.

Everything in the agent's home (folders, `.npmrc`) is done *as the agent*, so a
symlink the agent planted can only reach its own files; root-owned helpers refuse
paths in the agent's home. `--root /` is refused.

Tested offline in `ship/tests/test_ship_provision.py` (sandbox runs twice, dry runs,
the pinned-key check). Real-VM runs happen through the phase 0 harness and W11.

## Check on a real Ship (W11)

- First boot with socket-activated sshd: the drop-in is picked up
  (`try-reload-or-restart` does nothing while `ssh.service` is idle).
- `InhibitDelayMaxSec=30` takes effect from the second boot (logind is not reloaded).
- `ship-resume`'s detach keys; it falls back to ending tmux.
- Google Chrome's package adds its own apt source; check apt does not warn about a
  repository configured twice.
