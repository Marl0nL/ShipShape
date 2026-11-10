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
users' files, keys, settings or units (only the `tmp.mount` mask of step 3) and writes no provisioning record, so a
Ship from a baked image still provisions itself fully at first boot; the baked
steps just find nothing to do.

What it does, in order (each step checks first and only counts real changes):

1. Ubuntu packages (git, jq, tmux, avahi, qemu-guest-agent, ...).
2. Every `[apt.*]` repo in `versions.toml`, as a deb822 source with its own
   keyring. A downloaded key is used only if every primary key in it is pinned.
   Packages of the GitHub, Google Cloud and NodeSource repos are installed; Google
   Chrome comes from the chrome-devtools-axi recipe.
3. herdr and uv, downloaded and checked against their pinned sha256. Then `/tmp`
   on the root disk: `tmp.mount` (systemd's tmpfs, which Ubuntu pulls in from
   `/usr/lib/systemd/system/local-fs.target.wants/`) is masked with a link to
   `/dev/null` in `/etc/systemd/system/`, and `/etc/tmpfiles.d/tmp.conf` keeps
   `/tmp` 1777 at every boot and cleans what nothing used for 10 days (30 for
   `/var/tmp`; the same lines as systemd's own, which it replaces). A tmpfs `/tmp`
   has a fixed number of files (about a million), which parallel test runs used
   up, and it is wiped at each boot. Nothing is unmounted: on a running Ship the
   live tmpfs stays until the next reboot, and `ship-ctl status` reports
   `tmp-reboot-pending` until then. A Ship from a baked image has `/tmp` on the
   disk from its first boot; one from the stock image, from its first reboot.
4. ShipShape files: the mode-ledger hook, `/usr/local/bin/ship-ctl`,
   `/usr/local/bin/ship-axi`, `/etc/profile.d/shipshape.sh`; then ship-axi's Node
   dependencies (`npm ci` from its lockfile). The resume shim of older releases
   (`/opt/shipshape/shim/claude`) is removed.
5. The agent's home folders, its npm prefix (`~/.local`) and lingering.
6. Claude Code with the native installer, as `agent` (`install-claude.sh`: the
   release manifest's signature and the binary's checksum are checked), once;
   then it updates itself. herdr's Claude integration (it reports each agent's
   session and state, which ship-ctl's agent record follows).
7. The sshd, logind and journald drop-ins, then the units: `ship-status.timer`
   and the two `ship-lan-guard` units (system; `ship-ctl apply` turns the LAN
   guard on or off), `herdr.service` (in `/etc/systemd/user`, root-owned,
   `ConditionUser=agent`); older releases' `ship-resume.service` is disabled and
   removed (ship-ctl resumes agents itself, from its agent record). Then
   `ship-netwatch.service`, which records which programs connect where.
8. First boot only: `ship-ctl apply`, `ship-ctl tools run`, start herdr. The first
   status refresh records this boot's (empty) resume and runs autostart.

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
- After a reboot, the first status refresh resumes every recorded agent into the
  pane herdr restored for it (`herdr pane list` shows it under its old id, a shell
  in its folder), with no duplicate workspace; then a crash (kill the claude
  process) brings it back at the next refresh, and `/exit` does not.
- Google Chrome's package adds its own apt source; check apt does not warn about a
  repository configured twice.
- `/tmp` after a converge, then a reboot: before it, `findmnt /tmp` still shows the
  tmpfs and agents keep working; after it, `findmnt /tmp` shows nothing (a folder of
  `/`), `stat -c %a /tmp` is 1777, `systemctl is-enabled tmp.mount` says `masked`,
  and services with `PrivateTmp=yes` still start. `/tmp` now keeps its files across
  reboots: check that sockets left there from before the reboot (tmux's, Claude
  Code's) do not stop agents or herdr from coming back.
