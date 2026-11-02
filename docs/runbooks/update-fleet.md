# Runbook: update the fleet

How to roll a change out to running Ships: a new herdr, npm tool or uv pin, a
recipe or profile change, or new Ship tooling. Ships are updated in place,
never re-imaged; agents keep their conversations and modes.

What updates itself, so you never roll it out: Claude Code (its own updater, on
each Ship's channel), gh and gcloud (their apt repos, through unattended-upgrades)
and Ubuntu security fixes.

## 1. Change the checkout

`ship-fleet update` sends exactly what is in the checkout FleetLedger runs from.

1. Pull or make the change, for example a new herdr pin in `versions.toml`
   (version, every asset URL and its sha256).
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

Automatic reboots are clean too, so agents resume in their modes as after any
reboot.
