# Runbook: create a Ship

From nothing to a Ship that waits for you to sign it in. You need a ShipShape
checkout, Python 3.12 or newer, pipx and OpenSSH on your computer. For Proxmox,
`ship-fleet setup` (step 2) checks the host and prints the root commands for
anything missing; [proxmox-setup.md](proxmox-setup.md) explains them.

## 1. Install FleetLedger (once)

```sh
git clone <this repository> ShipShape && cd ShipShape
fleetledger/install.sh            # pipx, editable: `git pull` updates it
ship-fleet --version
```

## 2. Set up your fleet (once)

```sh
ship-fleet setup
```

`setup` opens the setup page in your browser ([web-ui.md](../web-ui.md#first-run-the-setup-page);
`--text` asks in the terminal). It walks through everything below, plus the
certificate authority, Proxmox and the overlay, and offers to make a first Ship;
each step shows done or next, so come back whenever you like. For a new fleet it
makes and looks after every key itself. The two commands below do the same by
hand, with passphrases you choose:

```sh
ship-fleet init                   # asks for a passphrase for the new admin key
ssh-add ~/.ssh/shipshape_fleet_ed25519
```

`init` makes the fleet admin key (FleetLedger's own key; it logs in to Ships as
`fleet`) and an empty inventory, `~/.config/fleetledger/ships.toml`. Your own key
(`~/.ssh/id_ed25519.pub` by default, or `--agent-key`) goes onto each Ship's
`agent` user, so you can attach with herdr or plain SSH. By hand, also run
`ship-fleet ca init`, and after the first Ship `ship-fleet trust --this-computer`,
so `ssh ship-01` and `herdr --remote ship-01` work here.

Either way, put your tags and the accounts each tag must use into the inventory
(setup fills in `[defaults] driver` and `[drivers.proxmox]` itself). A typical one:

```toml
[defaults]
driver = "proxmox"                # or "manual" (prints the steps)
tags = ["work"]                   # tags for new Ships unless you give --tag

[tag.work]                        # the accounts a work Ship must sign in as
claude = "work@example.com"
gcloud = "work@example.com"
github = "example-user"

[tag.personal]
claude = "you@example.com"
gcloud = "you@example.com"
github = "example-user"

[profile_options.orchestrator]    # values a profile needs, if you use one
repo_url = "https://example.com/you/orchestrator.git"

[drivers.proxmox]                 # from proxmox-setup.md
# ...
```

The full format is `contracts/ships.toml.schema.json`. The file stays on your
computer: it names your accounts and machines.

## 3. Create the Ship

```sh
ship-fleet new ship-01 --tag work
```

Useful options (`ship-fleet new --help` lists them all):

| Option | Effect |
|---|---|
| `--mode bypassPermissions` | new agents start in bypass, with no warning dialog (implies `--allow-bypass`); the default is `auto` |
| `--autostart main:~/work/main:auto` | an agent started at boot and resumed after reboots, each in its mode (repeatable); each starts with a first prompt, "Get oriented and ready to work." unless you choose one with `--autostart-prompt`. Add one later with `ship-fleet set ship-01 autostart.add=main:~/work` (started at once, once Claude is signed in), or on the Ship's page; `autostart.remove=main` stops starting it |
| `--repo someone/agent-setup` | the first agent works in this GitHub repo: the Ship clones it into `~/work/agent-setup` first (as the agent; a private repo once the Ship is signed in to GitHub, which its page then asks for), so the repo's own CLAUDE.md or AGENTS.md, skills and settings apply. Later: `ship-fleet set SHIP autostart.add=NAME:github:OWNER/REPO` |
| `--signins claude,github` | the sign-ins it uses (default: all); `ship-fleet signins SHIP --on/--off NAME` later, which signs one out when turned off |
| `--github-scopes workflow` | extra GitHub scopes its GitHub sign-in asks for, beyond gh's minimum (default: none): `workflow`, `project`, `write:packages` ([sign-in.md](sign-in.md#extra-github-scopes)); later `ship-fleet set SHIP github_scopes=...` |
| `--no-overlay` | with `[overlay]` set up, do not join it after enrolling (it joins by default) |
| `--tools tasks-axi` | extra catalogue tools (`ship-fleet tools ls`); the defaults are kept unless `--no-default-tools` |
| `--profile orchestrator` | a profile; its options come from `[profile_options]` |
| `--cores 4 --memory-mb 8192 --disk-gb 40` | size (these are the defaults; for a supervisor and its workers, see [below](#size-it-for-a-supervisor-and-its-workers)); later `ship-fleet resize SHIP --cores 8 --restart` (cores and memory apply at a restart through the hypervisor; a disk only grows, at once, with the Ship's filesystem: ship-ctl grow-root) |
| `--agent-sudo` | give the agent user sudo (off by default) |
| `--on proxmox` | where to create it (the same as `--driver`; default `defaults.driver`) |
| `--image <id>` | the base image (default: the pinned one, see [images.md](images.md)); `stock` for the plain cloud image |

What happens:

1. FleetLedger makes the Ship's SSH host key and pins it in `ships.toml`, so the
   very first connection is already verified. It writes the seed ISO (the
   Ship's settings, your public keys, the tooling from this checkout) to
   `~/.local/share/fleetledger/seeds/`, mode 0600.
2. **proxmox:** it uploads the seed, clones the template, attaches the seed,
   sizes and starts the VM, waits for it to provision (about 10 minutes the
   first time), then ejects the seed and deletes both copies. **manual:** it
   prints the steps; once the VM has booted, run
   `ship-fleet status ship-01 --wait` to finish.
3. The Ship ends in `needs-sign-in`: everything is installed, nobody is signed
   in. Next: [sign-in.md](sign-in.md).

### Names

Ships are reached by name, `ship-01.local` by default (the Ship announces it
over mDNS). If your computer does not resolve `.local` names, either:

- install mDNS name resolution (on Ubuntu: `sudo apt install libnss-mdns`), or
- give the Ship a name your DNS knows: `--host ship-01.example.net`.

With the proxmox driver, if no name resolves when the Ship comes up, FleetLedger
adds the address the hypervisor reports as a fallback, so creation still
finishes. The host key is pinned either way: whatever name or address you dial,
only the Ship's own key is accepted.

## 4. Look at it

```sh
ship-fleet ls                     # every Ship: state, agents, sign-ins, load
ship-fleet status ship-01         # one Ship in detail
ship-fleet ssh ship-01            # a shell as agent (your key)
ship-fleet logs ship-01 provision # what provisioning did
herdr --remote ship-01            # attach herdr, once this computer is trusted
                                  # (setup does it); else agent@ship-01.local
```

`/tmp` is a folder on the Ship's disk, not in memory. A Ship made from an image
baked with this (see [images.md](images.md#rebuild)) has it so from its first boot.
One made with `--image stock`, or from an image baked before it, starts with
systemd's in-memory `/tmp` and shows `tmp-reboot-pending` until its first reboot
(`ship-fleet reboot ship-01`, before you start heavy work on it).

## Size it for a supervisor and its workers

The defaults (4 cores, 8 GB of memory, a 40 GB disk) suit a Ship with one agent;
keep them for those. A Ship where a supervising agent runs several workers at
once, each in its own worktree with its own test runs, needs more. CPU runs out
first.

| | Plan for | Three workers |
|---|---|---|
| CPU | 2 cores (3 for comfort) per worker that runs the full check suite at the same time, plus 2 for the supervisor, the Claude processes and the Ship itself | 8 cores, 11 for comfort |
| Memory | 3 GB per worker, plus 4 GB | 16 GB |
| Disk | 40 GB, plus 5 GB per worker running at once | 60–80 GB |

- **CPU.** A full suite keeps cores busy for its whole run. On 6 cores, run at
  most two full suites at once: with three running, one suite took over half an
  hour. Agents should run the full check in the background, never under a
  10-minute foreground tool limit, and prefer targeted tests while they work.
- **Memory** is rarely the limit: each Claude process takes about 1 GB, and the
  rest is room for test runs.
- **Disk.** A worktree is small, but a task's temporary files can reach a few GB
  and hundreds of thousands of files in a day. They sit on the root disk, which
  is also the Ship's pool of inodes (a smaller disk has fewer). The Ship clears
  only what nothing used for 10 days in `/tmp` and 30 days in `/var/tmp`, so
  delete each task's temporary folder when the task ends.

```sh
ship-fleet new ship-02 --tag personal --cores 8 --memory-mb 16384 --disk-gb 80
ship-fleet resize ship-01 --cores 8 --memory-mb 16384 --restart
ship-fleet resize ship-01 --disk-gb 80
```

`resize` changes cores and memory at the next restart through the hypervisor;
`--restart` does that now, which stops the Ship's agents, so resize while they are
idle. A disk only grows, at once, with no restart.

## A Ship FleetLedger did not create

`ship-fleet scan` lists Ships announcing themselves on your LAN. To take one into
the inventory, compare the fingerprint FleetLedger shows with the one cloud-init
printed on the VM's console, then confirm:

```sh
ship-fleet adopt ship-07 --host ship-07.local
```

The same command with `--replace` re-pins a Ship you rebuilt from a new seed.

## Stop and start it

```sh
ship-fleet stop ship-01    # a clean shutdown
ship-fleet start ship-01   # its agents resume in their modes as it boots
```

Both go through the driver (the proxmox driver checks the VM's name first). With
the manual driver they print the step to run by hand. A reboot from inside the
Ship is `ship-fleet reboot`; stopping and starting through the hypervisor is what
makes a new core count or memory size apply (`ship-fleet resize --restart` does
it for you).

## Snapshots

With the proxmox driver (and the phase 4 rights in
[proxmox-setup.md](proxmox-setup.md) section 4):

```sh
ship-fleet snapshot take ship-01 before-upgrade --note "before the big change"
ship-fleet snapshot ls ship-01
ship-fleet snapshot rollback ship-01 before-upgrade   # shuts down cleanly, rolls back, starts
ship-fleet snapshot rm ship-01 before-upgrade
```

Snapshots are of the disk only, never memory. A snapshot holds the Ship's
sign-ins as they were, so treat it like the Ship itself; rolling back can bring
back a sign-in you have since revoked.

## Destroy it

```sh
ship-fleet destroy ship-01            # asks you to type the name
```

It stops and deletes the VM and its disks through the driver, deletes any seed
left over, and removes the Ship from the inventory. The proxmox driver checks
the VM's name first and refuses to touch a VM that is not this Ship. With the
manual driver it prints the steps; once you have run them,
`ship-fleet destroy ship-01 --forget` removes the inventory entry. Sign the Ship
out of its services first (`ship-fleet logout ship-01 all`) if its sign-ins
should not outlive it in any snapshot or backup. It also drops the Ship's charts
(its rows in the history store).

In the web UI: the Ship's **Settings** tab, **Danger zone**, or **More ▾ ▸ Destroy…**
on any tab. You type the Ship's name to confirm; it runs this command as a job.

## If something goes wrong

### Try a failed Ship again

When `ship-fleet new` fails, FleetLedger keeps what it was asked for (every option,
nothing secret) and prints the exact command to run again, ready to edit:

```sh
ship-fleet: Proxmox POST failed: 500 storage full
  To try again (edit what you need): ship-fleet new ship-03 --tag work --cores 4 --memory-mb 8192
```

In the web UI, the failed job's card (under Activity, also after a restart) has
**Edit and retry**: it opens the New Ship form filled in with everything that
attempt was given. Change what you need and **Make it again**. The New Ship page
lists every saved attempt, each with Edit and retry and **Discard**, and a new
Ship's default name skips their names. An attempt goes once a Ship of that name is
made, or when you discard it (one that still names something it left on the
hypervisor stays until that is removed); FleetLedger keeps the last 20.

Nothing secret is kept: there are no passphrases, tokens or keys in a Ship's
options, and sign-ins happen after the Ship is up, as always. The first agent's
first prompt is the one free-text option; if it holds anything that looks like a
token, an address or a link, it is not kept, and the form asks you to type it again.

What blocks a retry, and what to do:

| The attempt | What happens next |
|---|---|
| failed before the VM was made, or the driver removed what it made | nothing to clear: try again |
| left a VM or seed volume the driver could not remove (it says which) | the next `new` of that name removes it first (after checking its name), so there is never a second VM of that Ship. It asks first: `ship-fleet new` wants the Ship's name typed, as `destroy` does, or `--yes` (without a terminal it refuses without `--yes`); the web forms (New Ship and setup's first Ship) ask in the dialog, whether you came from Edit and retry or typed the name, and remove only what the dialog named. If it still cannot, it says what is left: delete it on the hypervisor and try again, or discard the attempt |
| made the VM, but the Ship did not finish enrolling | the Ship stays in the inventory: finish it (`ship-fleet status ship-03 --wait`, or **Finish enrolling** on its page) or remove it (`ship-fleet destroy ship-03`, or **Destroy…**) before trying again under that name, or give the retry another name |
| a name that is taken now | the form says so, as for any new Ship: choose another name |

| Symptom | Check |
|---|---|
| `new` waits a long time | the VM console in Proxmox, then `ship-fleet logs ship-01 provision` |
| "did not present its pinned host key" | something else answers at that name, or the Ship was rebuilt: re-pin with `adopt --replace` after checking the console |
| "refused FleetLedger's admin key" | was the Ship seeded from this inventory? A key you manage yourself must be loaded (`ssh-add -l`); FleetLedger loads the keys it looks after itself |
| `unreachable` | the name resolves? (see Names) The VM is running? |
| `reboot refused: ... holds a shutdown lock` | usually apt still running in a login session you have open on the Ship: let it finish or close that session, or `ship-fleet reboot ship-01 --force` ([update-fleet.md](update-fleet.md#a-reboot-is-refused)) |
