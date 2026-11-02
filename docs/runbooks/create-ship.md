# Runbook: create a Ship

From nothing to a Ship that waits for you to sign it in. You need a ShipShape
checkout, Python 3.12 or newer, pipx and OpenSSH on your computer. For Proxmox,
first do [proxmox-setup.md](proxmox-setup.md) once.

## 1. Install FleetLedger (once)

```sh
git clone https://github.com/Marl0nL/ShipShape.git && cd ShipShape
fleetledger/install.sh            # pipx, editable: `git pull` updates it
ship-fleet --version
```

## 2. Set up your fleet (once)

```sh
ship-fleet init                   # asks for a passphrase for the new admin key
ssh-add ~/.ssh/shipshape_fleet_ed25519
```

`init` makes the fleet admin key (FleetLedger's own key; it logs in to Ships as
`fleet`) and an empty inventory, `~/.config/fleetledger/ships.toml`. Your own key
(`~/.ssh/id_ed25519.pub` by default, or `--agent-key`) goes onto each Ship's
`agent` user, so you can attach with herdr or plain SSH.

Then edit the inventory. A typical one:

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
| `--autostart main:~/work/main:auto` | an agent started at boot and resumed after reboots (repeatable); each gets a first prompt (`--autostart-prompt` to choose it), which also records its mode so bypass survives reboots |
| `--tools tasks-axi` | extra catalogue tools (`ship-fleet tools ls`); the defaults are kept unless `--no-default-tools` |
| `--profile orchestrator` | a profile; its options come from `[profile_options]` |
| `--cores 4 --memory-mb 8192 --disk-gb 40` | size (these are the defaults) |
| `--agent-sudo` | give the agent user sudo (off by default) |

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
herdr --remote agent@ship-01.local   # attach herdr
```

## A Ship FleetLedger did not create

`ship-fleet scan` lists Ships announcing themselves on your LAN. To take one into
the inventory, compare the fingerprint FleetLedger shows with the one cloud-init
printed on the VM's console, then confirm:

```sh
ship-fleet adopt ship-07 --host ship-07.local
```

The same command with `--replace` re-pins a Ship you rebuilt from a new seed.

## If something goes wrong

| Symptom | Check |
|---|---|
| `new` waits a long time | the VM console in Proxmox, then `ship-fleet logs ship-01 provision` |
| "did not present its pinned host key" | something else answers at that name, or the Ship was rebuilt: re-pin with `adopt --replace` after checking the console |
| "refused FleetLedger's admin key" | `ssh-add -l` lists the fleet key? Was the Ship seeded from this inventory? |
| `unreachable` | the name resolves? (see Names) The VM is running? |
