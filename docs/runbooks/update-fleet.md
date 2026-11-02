# Runbook: update the fleet

How to roll a change out to running Ships: a new herdr, npm tool or uv pin, a
recipe or profile change, or new Ship tooling. Ships are updated in place,
never re-imaged; agents keep their conversations and modes.

What updates itself, so you never roll it out: Claude Code (its own updater, on
each Ship's channel), gh and gcloud (their apt repos, through unattended-upgrades)
and Ubuntu security fixes.

## 1. Change the checkout

`ship-fleet update` sends exactly what is in the checkout FleetLedger runs from.

1. Pull or make the change. To move a pin, let FleetLedger do the checking:

   ```sh
   ship-fleet pins                    # every pin against its newest release
   ship-fleet pins set herdr 0.9.6    # new URLs; each download must match GitHub's SHA-256
   ship-fleet pins set gh-axi 0.1.48  # the version must exist in the npm registry
   ```

   It rewrites `versions.toml` in place (comments kept) and refuses anything it
   cannot verify. Read the herdr release notes before moving herdr: its CLI and
   protocol have changed between releases before.
2. Run `scripts/check`.
3. Commit. A bundle packed from uncommitted changes under `ship/` is marked
   `dirty`, and every update converges it again, so commit before a fleet-wide
   rollout.

## 2. Try one Ship first

```sh
ship-fleet update ship-01
```

It prints each step on stderr (upload, converge, reboot) and a result per Ship:

```
ship-01: updated to 0.2.0-0123456789ab; back after 48 s; 2/2 agents resumed in their modes
```

- If herdr changed, the Ship is rebooted (cleanly, with `systemctl reboot`) so the
  new herdr takes over, and FleetLedger checks every agent came back in the mode
  it had. Add `--reboot never` to postpone that; the Ship then shows
  `herdr-restart-pending` until you run `ship-fleet reboot ship-01`.
- `FAILED` with `agent X came back in auto, was bypassPermissions` means the
  resume shim did not restore bypass: look at `ship-fleet logs ship-01 resume`
  and `herdr` before going further.
- A failed converge rolls the Ship back to its previous tooling by itself; read
  `ship-fleet logs ship-01 provision`.

## 3. Roll out

```sh
ship-fleet update --tag work     # or: ship-fleet update all
```

It asks before touching several Ships (`--yes` skips the question in scripts),
works on four at a time, and ends with `N of M Ships OK`. Ships already on this
bundle are skipped. Run it again for any Ship that failed once you have fixed the
cause.

## 4. Check

```sh
ship-fleet ls
ship-fleet attention
```

`behind-tooling` and `herdr-restart-pending` should be gone.

## OS updates and reboots

Unattended-upgrades installs Ubuntu security fixes daily. When one needs a reboot
the Ship shows `reboot-required`; either reboot when it suits you:

```sh
ship-fleet reboot ship-01        # or --tag work, or all
```

or let Ships do it themselves at a quiet hour:

```sh
ship-fleet set --tag work auto_reboot=true reboot_time=04:00
```

`reboot_time` is the Ship's local time. Ships made with `ship-fleet new` take
this computer's time zone; older Ships run on UTC until you set theirs:

```sh
ship-fleet set all timezone=Europe/Berlin
```

Automatic reboots are clean too, so agents resume in their modes as after any
reboot.

## A new Ubuntu image

```sh
ship-fleet pins set ubuntu 20261101
```

FleetLedger fetches that build's `SHA256SUMS` and its signature, checks the
signature with `gpgv` against the pinned Ubuntu signing key alone (a keyserver
answer carrying any other key is refused, and so is the key itself once it is
expired or revoked), and takes the image hashes from the verified file. Only new Ships use the image: rebuild the
Proxmox template from it (`docs/runbooks/proxmox-setup.md`, the template steps,
checking the download against the new `sha256`). Existing Ships are never
re-imaged; unattended-upgrades keeps them current.
