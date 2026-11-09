# Runbook: FleetLedger as a service (always on, no terminal)

Run FleetLedger's web UI and its poller under systemd, so it is up whenever the
computer is, starts at boot, and needs no terminal kept open. This is what an always-on
FleetLedger computer (a small VM, say) runs; it works on a desktop too.

It is off until you install it. Without it, `ship-fleet ui` in a terminal is exactly
what it always was.

```sh
ship-fleet service install          # write, enable and start the systemd user unit
ship-fleet ui link --local          # a one-time sign-in link for this computer
ship-fleet service status           # is it running, and what it is doing
ship-fleet service restart          # after updating FleetLedger (running jobs finish first)
ship-fleet service uninstall        # stop it and remove the unit
journalctl --user -u fleetledger    # what it said
```

## What changes as a service

The service runs `ship-fleet ui --service --port 8765`, which is `ship-fleet ui` with
four differences:

- **No terminal, no browser, no link printed.** Its output goes to the journal, which
  is no place for a sign-in token, so it never prints one. You ask the running service
  for a link instead (below).
- **A fixed port**: 8765 unless you say otherwise, or the remote web UI's port when that
  is on (`ship-fleet ui remote`), since Tailscale Serve and ssh tunnels need to know it.
- **The poller is always on.** `ship-fleet ui` polls the Ships only while a page is
  open; the service polls every Ship every 30 seconds (`--interval`), so the charts,
  Needs attention and certificate renewals are current whenever you look. The history
  it keeps is bounded as ever (7 days of samples).
- **A stop lets running jobs finish.** On `systemctl --user stop fleetledger` (or
  `ship-fleet service restart`) it first stops taking new jobs, lets the running ones
  (an update, a new Ship, an image bake ...) finish, for up to 40 minutes, and only
  then stops. The pages keep answering meanwhile and say "FleetLedger is stopping".
  To stop it at once instead, cutting the jobs short (as a second Ctrl-C does in a
  terminal), send it a second signal: `systemctl --user kill -s TERM fleetledger`
  (a second `systemctl --user stop` does nothing: systemd merges it into the
  first). Each job cut short is saved to Activity as "stopped".
- **A reboot does not wait, unless you let it.** A reboot stops your whole systemd
  user manager, and Ubuntu gives that 5 seconds, so running jobs are killed, and not
  even recorded as stopped. Before a reboot, `ship-fleet service status` shows what is
  running. To let a reboot wait for them too, raise that limit for your user, once,
  as root (`ship-fleet service install` prints this when it is needed):

  ```sh
  sudo mkdir -p /etc/systemd/system/user@$(id -u).service.d
  printf '[Service]\nTimeoutStopSec=45min\n' | sudo tee /etc/systemd/system/user@$(id -u).service.d/zz-fleetledger-drain.conf
  sudo systemctl daemon-reload
  systemctl show user@$(id -u).service -p TimeoutStopUSec     # 45min
  ```

  A reboot then waits as long as the jobs need, up to 45 minutes. The file's name must
  sort after Ubuntu's own `timeout.conf`: systemd applies drop-ins in name order, and
  a plain `fleetledger-drain.conf` would lose to it.

Restarting it (or stopping it) cancels any sign-in still open in the web UI, as
stopping `ship-fleet ui` always has: the Ship is unchanged; start the sign-in again.

Everything else is the same: it binds `127.0.0.1` only, the same gate, sessions, CSRF
checks and Content-Security-Policy hold, and every action in it runs the same CLI
command function as before.

## 1. Install it

As the user FleetLedger runs as, on the computer it runs on:

```sh
ship-fleet service install
```

It writes `~/.config/systemd/user/fleetledger.service` (a systemd **user** unit: it
runs as you, with your FleetLedger folders and your ssh, and needs no root), reloads
systemd, enables it and starts it. Options: `--port N`, `--interval SECONDS`,
`--no-start`, and `--ship-fleet PATH` if the `ship-fleet` it should run is not the one
on your `PATH`.

If your shell sets `FLEETLEDGER_CONFIG_DIR`, `FLEETLEDGER_DATA_DIR`,
`FLEETLEDGER_RUNTIME_DIR`, `XDG_CONFIG_HOME` or `XDG_DATA_HOME`, the unit carries them,
so the service uses the same folders as your commands. Install it again after changing
one.

**Secrets without a desktop keyring.** The service reads the same secrets as your
commands: the passphrases of the keys FleetLedger looks after, the Proxmox token and
the Tailscale OAuth secret. On a desktop they are in your OS keyring, which is
unlocked only while you are logged in, so a service that runs while you are logged
out (lingering, below) cannot read them then: its jobs and renewals fail until you
log in again. On a computer with no desktop session (an always-on VM), turn on
FleetLedger's vault ([../vault.md](../vault.md)): every secret in one encrypted file,
unlocked once after each boot (`ship-fleet unlock`, or the web UI), for the service
and your commands alike, and a copy of the disk opens nothing. Without it, the file
store works too (`0600` files in `~/.config/fleetledger/`), but they sit on the same
disk as the keys, so a copy of that disk holds the whole fleet.

**At boot, and while you are logged out.** A user unit runs while you are logged in.
For it to start at boot and keep running after you log out, turn on lingering for your
user, once, as root (install says so when it is off):

```sh
sudo loginctl enable-linger "$USER"
```

## 2. Sign in

**From a browser on this computer:**

```sh
ship-fleet ui link --local
```

prints a one-time link, `http://127.0.0.1:8765/login?token=...`. It works once, within
15 minutes, like the links `ship-fleet ui` prints in its terminal. The command asks the
running service for it over its control socket (below); nothing is written down.

**From another computer, through ssh:** tunnel the port, then open the link there:

```sh
ssh -L 8765:127.0.0.1:8765 fleetledger            # leave it open
ssh fleetledger ship-fleet ui link --local         # in another terminal; open the link
```

**From your laptop and phone, with no tunnel:** turn on the remote web UI through
Tailscale Serve ([remote-ui.md](remote-ui.md)), then pair each device once with
`ship-fleet ui link --name laptop` (over ssh is fine) or Settings ▸ Devices. Turn it on
before installing the service, or reinstall it afterwards, so the unit uses the remote
UI's port. Each start of the service checks Serve as `ship-fleet ui` does and refuses
to start if it is off; just after boot Tailscale may not be up yet, so systemd tries
again (after 15 seconds, then longer, up to every 15 minutes).

## 3. Day to day

```sh
ship-fleet service status
```

says whether systemd runs it and whether it is enabled, the address it answers on, and
the jobs it is running (or that it is stopping, waiting for them). Its log:
`journalctl --user -u fleetledger` (add `-f` to follow it).

After updating FleetLedger (`git pull` in the checkout, and `fleetledger/install.sh` if
its dependencies changed):

```sh
ship-fleet service restart
```

It returns at once; the service lets its running jobs finish, then starts again with
the new code (`systemctl --user kill -s TERM fleetledger` makes it stop at once). Sessions on this computer's address end with the restart (get a new link);
paired remote devices stay signed in.

To stop it for a while: `systemctl --user stop fleetledger` (running jobs finish
first), and `systemctl --user start fleetledger` again. Moving FleetLedger to another
computer (`ship-fleet migrate`) needs it stopped: migrate refuses while anything else
runs here, the service included.

## The control socket

`ship-fleet ui` and the service listen on a Unix socket,
`$XDG_RUNTIME_DIR/fleetledger/ui.sock`, so commands on the same computer can ask the
running UI what no file can answer: a sign-in link for this computer's address (its
token lives only in the running UI's memory) and what it is doing (`ship-fleet service
status`). Only you can use it: it is `0600` in a `0700` folder, and a caller whose user
id is not yours gets no answer. A request is one line; anything else is refused. Over
ssh, log in as the user the service runs as (so `XDG_RUNTIME_DIR` is the same).

## Uninstall

```sh
ship-fleet service uninstall
```

stops it (running jobs finish first), disables it and removes the unit. Run `ship-fleet
ui` yourself from then on. On a copy that handed its fleet on (`ship-fleet migrate`),
`ship-fleet service` still works, so you can remove it; the UI it runs refuses to start
there, as `ship-fleet ui` does.

## When it refuses

| It says | Do |
|---|---|
| No systemd user manager answers here | log in as this user (a desktop session, or ssh, so `XDG_RUNTIME_DIR` is set), then try again; or run `ship-fleet ui` in `tmux` |
| … exists and ship-fleet did not write it | move your own `fleetledger.service` away first |
| … has a character a systemd unit would need quoted | a folder setting or the `ship-fleet` path has a space or the like; use a plainer one |
| The remote web UI is set up on port … | leave out `--port`, or move the remote UI to that port first |
| (`ship-fleet ui link --local`) No ship-fleet ui is running here | `ship-fleet service status`; `journalctl --user -u fleetledger` says why it stopped |
| (`ship-fleet ui --service`) A service needs a fixed port | `--port 8765`; `ship-fleet service install` sets it |
| (in the journal) Tailscale is not running here / Serve is not serving … | as in [remote-ui.md](remote-ui.md); systemd tries again by itself (after 15 seconds, then longer, up to every 15 minutes), or `ship-fleet service restart` once it is fixed |
| (in the journal) This FleetLedger is on standby / was retired, or a setting it cannot read | it exits with status 2, which systemd does not retry: only you can fix these. A handed-over copy ([move-fleetledger.md](move-fleetledger.md)): `ship-fleet service uninstall` here if it should no longer run; else fix what it says, then `systemctl --user start fleetledger` |
