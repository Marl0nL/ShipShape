# Runbook: update the fleet

How to roll a change out to running Ships: a new herdr, herdr-core, npm tool or uv pin, a
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
   ship-fleet pins set herdr_core 0.1.20  # from the releases.herdr.dev manifest
   ship-fleet pins set gh-axi 0.1.48  # the version must exist in the npm registry
   ```

   herdr-core's manifest is not signed: a download must match it, but the sha256
   written to `versions.toml` is what Ships check, so compare it with the release
   page before committing.

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
- On a herdr-core Ship (ship.toml `runtime = "core"`), a new herdr-core does **not**
  reboot it: the running core keeps serving every agent, and the new one takes over
  at the next reboot (an OS update's, say). Until then the Ship shows
  `core-restart-pending`, a notice, and the result says "herdr-core was updated".
  Reboot when it suits you (`ship-fleet reboot ship-01`), or update with
  `--reboot always`. Its core's own log is `ship-fleet logs ship-01 core`.
- The first update with the disk-backed `/tmp` masks `tmp.mount` (systemd's tmpfs on
  `/tmp`) but unmounts nothing: running agents and their sockets live in `/tmp`. The
  result says "/tmp moves from memory (tmpfs) to the disk at the next reboot", and
  the Ship shows `tmp-reboot-pending` (low, with a Reboot button) until it reboots.
  The update does not reboot for it, as for a new herdr-core: the tmpfs keeps
  working until then. Reboot when it suits you; after that `/tmp` is a folder on
  the root disk, keeps its files across reboots, and anything nothing used for
  10 days is cleaned up daily.
- `FAILED` with `agent X came back in auto, was bypassPermissions` means the Ship
  did not resume it in its recorded mode: look at `ship-fleet logs ship-01 resume`
  and `herdr` (`core` on a herdr-core Ship) before going further (and check that
  the Ship still allows bypass).
  `agent X did not come back`: it is not running yet, or it failed to start 3 times
  (attention `agent-relaunch-failed`); `ship-fleet agent restart ship-01 X` tries
  again in the mode it was in, `--fresh` in a new conversation. An agent stays
  stopped only when it is exited (`/exit`) or its pane is closed: one that is
  killed counts as crashed, and the Ship starts it again. A reboot keeps each agent in the mode it was
  in, never the new default: to change it, see "Change a running agent's mode".
- A failed converge rolls the Ship back to its previous tooling by itself; read
  `ship-fleet logs ship-01 provision`.
- `reboot refused` after `updated to ...`: the update is done, but something on
  the Ship held a shutdown lock, so it did not reboot; see
  [A reboot is refused](#a-reboot-is-refused) below.

## 3. Roll out

```sh
ship-fleet update --tag work     # or: ship-fleet update all
```

It asks before touching several Ships (`--yes` skips the question in scripts),
works on four at a time, and ends with `N of M Ships OK`. Ships already on this
bundle are skipped. Run it again for any Ship that failed once you have fixed the
cause.

**It never rolls a Ship back.** `update` packs `ship/` from this checkout, so an
older checkout (another computer's, or a branch you forgot) would take Ships back
to older tooling. Before converging, it compares the Ship's ShipShape commit with
this checkout's in git: a Ship on this commit or an older one is updated; one on a
newer commit, on another branch, or on a commit this checkout does not have is
left as it is, with `not updated: it runs ShipShape <commit>, newer than this
checkout's ...` and what to do:

- **This checkout is behind** (another computer's is ahead): bring it up to date
  (`git pull`) and update again.
- **The Ship ran a branch that was since merged or rebased** (a squash merge, say):
  git cannot see that `main` now holds it, so updating from `main` is the move
  forward. Add `--force`.
- **A shallow clone** (`git clone --depth`) cannot see older commits either, and
  says so: `git fetch --unshallow`, then update again.
- **To roll a Ship back on purpose**, add `--force`: `ship-fleet update ship-01
  --force`, or in the web UI **Fleet actions**, **Update** with the **converge even
  ...** box ticked (for a tag or every Ship). A forced rollback says so in its
  progress and its result (`rolled back to ...`, and `rolled_back` in `--json`).

A checkout outside git cannot tell, and updates as before.

## 4. Check

```sh
ship-fleet ls
ship-fleet attention
```

`behind-tooling` and `herdr-restart-pending` should be gone (`core-restart-pending`
stays on herdr-core Ships until their next reboot, `tmp-reboot-pending` on every Ship
whose `/tmp` was moved to the disk; both are expected).

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

### Waking the primary agent after a reboot

Resumed agents continue their conversations, but nothing prompts them, so their
background monitors and processes stay down until someone writes to them. A Ship
can send its primary agent one line a few minutes after each boot, asking it to
start them again (off by default):

```sh
ship-fleet set ship-01 wake_after_boot=true                # 10 minutes after each boot
ship-fleet set ship-01 wake_delay_minutes=15 wake_text="Rebooted {delay} ago: restart your monitors."
```

or on the Ship's Settings tab, **After a reboot**. It takes effect from the next
boot. The primary agent is the first `[[autostart]]` agent unless you mark another,
on the Ship: `sudo ship-ctl agent primary NAME`. It is sent once per boot, never
into an agent waiting at a permission prompt; `ship-ctl logs system` on the Ship
shows what happened (`wake: ...`). Details: `contracts/settings.md`, "Waking the
primary agent after a boot".

### A reboot is refused

A program holding a shutdown lock makes the Ship refuse to reboot. On a Ship that
is APT while it installs or removes packages, for example a `sudo apt upgrade`
still going in an SSH session you have open on the Ship. FleetLedger says what
holds the lock and where:

```
ship-01: FAILED: reboot refused: a login session is blocking it: apt (PID 4242, as root)
  in sam's SSH login session on pts/0 holds a shutdown lock: "APT is installing or
  removing packages".
  Let it finish, or close sam's session on the Ship (log out of it), then reboot again.
  To reboot anyway: ship-fleet reboot ship-01 --force (in the web UI, Force reboot). ...
```

apt holds the lock until it exits, not only while packages unpack: also while it
waits at a question (a changed configuration file, or needrestart asking which
services to restart, where it is set to ask) and while its after-install steps
run. So an apt that looks finished can still hold it: look at that terminal.

Let it finish, or close that session, and reboot again. To reboot anyway:

```sh
ship-fleet reboot ship-01 --force
```

It is still a clean reboot (services stop, herdr saves its layout first, and the
agents resume in their modes), but what holds the lock is cut off. If that was
APT, run `sudo dpkg --configure -a` on the Ship afterwards. In the web UI a
refused reboot's card has **Force reboot…**, and so has the Ship's **More**
menu, each behind its own confirm dialog. Nothing needs updating on the Ships for
this: FleetLedger reads the locks with what every Ship already has.

## Change a running agent's mode

`default_mode` (and an autostart agent's own mode) applies to agents started
afterwards. A running agent keeps its mode, and so does every reboot and update:
the Ship resumes each session itself, in the mode its agent record has for it. To switch
running agents, swap them for new ones in the mode you want:

```sh
ship-fleet set ship-01 default_mode=bypassPermissions   # says running agents keep theirs
ship-fleet agent restart ship-01 --all                  # each in the mode it starts in now
ship-fleet agent restart ship-01 main --mode plan       # or one agent, in a chosen mode
```

Each agent exits and starts again in its own herdr pane, with the same name and
folder, and continues its conversation (`claude --resume`); `--fresh` starts a new
one instead. Remote Control and the session name come back as before, and the
Ship records the new mode, so the next reboot or `ship-fleet update` resumes it in
that mode and `ship-fleet reboot` checks for it.

- An agent in the middle of a turn is left alone and the command says so; run it
  again once the agent is idle, or add `--force` to interrupt the turn. An agent
  waiting at a question has that question dismissed.
- `bypassPermissions` needs `allow_bypass=true` on the Ship; without it the Ship
  refuses and nothing is restarted. (`default_mode=bypassPermissions` turns
  `allow_bypass` on with it.)
- Without `--mode`, an agent with a mode of its own in autostart keeps that mode;
  give `--mode` to override it.
- A Ship whose tooling predates the command says so: `ship-fleet update` it first.

In the web UI: the Ship's Overview lists each agent with **Restart in mode…**,
and after you change the default mode on its Settings tab, **Apply the default
mode to running agents** restarts the agents not in the mode they start in. Both
run as jobs behind the confirm dialog.

By hand, if the Ship cannot run the command: attach (`herdr --remote ship-01` or
`ship-fleet ssh ship-01`, then `herdr`), go to the agent's pane, type `/exit`,
and in the same pane start it again with the mode's flag and `--continue`:

```sh
claude --permission-mode acceptEdits --continue     # or plan, auto, default, dontAsk
claude --dangerously-skip-permissions --continue    # bypassPermissions (allow_bypass)
```

`--continue` picks the folder's most recent conversation; with several agents in
one folder, use `--resume <session id>` instead. The Ship records the mode from
the launch flags, so a reboot keeps it.

## A new Ubuntu image

```sh
ship-fleet pins set ubuntu 20261101
```

FleetLedger fetches that build's `SHA256SUMS` and its signature, checks the
signature with `gpgv` against the pinned Ubuntu signing key alone (a keyserver
answer carrying any other key is refused, and so is the key itself once it is
expired or revoked), and takes the image hashes from the verified file. Only
new Ships use the image: rebuild the Proxmox template from it
([proxmox-setup.md](proxmox-setup.md#2-the-template-vm), checking the download
against the new `sha256`), and if you use a baked image, bake a new one
([images.md](images.md#rebuild)). Existing Ships are never
re-imaged; unattended-upgrades keeps them current.
