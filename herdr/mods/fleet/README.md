# The Fleet panel

A Herdr tab with your Ships as FleetLedger last saw them: one row per Ship (its name, tags, state,
first attention item, and Herdr's own connection to it), and above them where the list came from,
FleetLedger's version, whether its vault is locked, and when the list was read. Click a row to go
to that Ship's space in Herdr.

**Needs you**, above the Ships, is everything `ship-fleet attention` lists, the Ships' items and
the fleet's own, most urgent first (high, medium, low), each with its fix: sign-ins missing,
expiring or on the wrong account, reboots and restarts pending, tooling behind, failed units, a
locked vault, a machine's certificate lapsing. An item's button opens what deals with it, and
never runs anything itself: **Sign in…**, **Reboot…** and **Update…** open the Ship's menu with
just those actions (each with its exact command), **Actions…** its whole menu, **Go to** its space
(an agent waiting), **Unlock…** the FleetLedger menu and **Machines** the Machines page.

What changes anything runs in a terminal tab, never in the panel: **New Ship**, a Ship's **⋯**
menu and the **FleetLedger** menu each show the exact command, and clicking opens one terminal
tab on this computer that runs it, where `ship-fleet` asks its own questions (see Actions).
**Machines** shows this computer's own refresh and the machines trusted to attach to Ships, and
trusts, untrusts and pairs from there (see Machines).

## Install

`shipshape-setup --herdr-client` installs it with the Desk, from FleetLedger (see
`herdr/README.md`). By hand, from this checkout:

```sh
herdr/install-fleet.sh                                   # into ${HERDR_APP_HOME:-~/.config/herdr-app}/mods/fleet
herdr-app do tab.new '{"name":"Fleet","mod":"fleet"}'    # open it as a tab
```

The script installs this mod only, `mod.json` first (it says `"place": "none"`, so Herdr never docks
it into the layout on screen). Run it again to update; it never writes `settings.json`. To remove
the panel, close its tab and delete the folder.

While a Fleet tab is open, `fleet.open` (from `keys.json` or `herdr-app do fleet.open`) goes to it,
or opens a new one, and `fleet.refresh` reads again.

## Settings

`shipshape-setup --herdr-client` (at the checkout's root) fills these in where you have
not: `ssh` with the destination it reached FleetLedger by, and `identity_file` with the key
file ssh logged in with there when that key was not in an agent. It never changes a value
you set, and leaves a file with anything else in it as it is. By hand, say where
FleetLedger is in `~/.config/herdr-app/mods/fleet/settings.json` (under `$HERDR_APP_HOME`
when that is set; `settings.example.json` is beside it):

```json
{
  "ssh": "fleetledger@fleetledger.example.invalid",
  "identity_file": null
}
```

| Key | What it is |
|---|---|
| `ssh` | How this computer reaches the FleetLedger computer with ssh: `user@host`, or a `Host` from `~/.ssh/config`. Its host key must already be in `known_hosts` (the panel never answers ssh's questions). |
| `identity_file` | Optional: a private key file (`/...` or `~/...`) for ssh, when Herdr does not see your ssh-agent (an app started from a desktop launcher may not). Its path only, never a secret. |

Nothing else goes in the file, and nothing secret: no password or passphrase is ever typed into
the panel. Saving the file reloads the panel.

Without `ssh`, the panel says where to put it. If the refresher has left
`~/.ssh/shipshape/fleet.json` on this computer (this machine's own bundle from `ship-fleet
machine-bundle NAME`, `contracts/machine-bundle.schema.json`), the panel shows its Ships (name and
tags) when it has no list from FleetLedger. Without the refresher (`ship-fleet trust` prints
the line that installs it) the file is not there, which is normal. It never
takes FleetLedger's address from that file: a refresher's key may be allowed only
`ship-fleet machine-bundle`, and every call with it counts as this machine refreshing. If
FleetLedger ever answers the panel with this machine's bundle, the panel says so and does not ask
with that key again until `settings.json` names another.

## Actions

Each one opens a terminal tab in a space on this computer (never a Ship's) running

```sh
ssh -t -o ConnectTimeout=8 [-i KEY -o IdentitiesOnly=yes] -- DEST "'ship-fleet' 'destroy' 'ship-01'"
```

which is exactly the line the panel showed under the action before you clicked it. Whatever
`ship-fleet` asks, it asks in that terminal, and you answer it there: a Ship's name before
**Destroy**, `yes` before **Roll back**, the vault's passphrase for **Unlock**, a sign-in's link and
code. The panel never passes a flag that skips one (`--yes`, `--force`, `--revoke-my-key`,
`--stdin`, `--code`), asks no question of its own, never types into a terminal, and takes no secret: the
only things typed into it are a new Ship's name, tag, repository and size, a snapshot's name, and
on the Machines page a machine's name, public key and scope and a device's name, each checked
against the pattern `ship-fleet` enforces and refused, not sent, when it does not match.

| Where | Action | Runs on FleetLedger |
|---|---|---|
| **New Ship** | Create | `ship-fleet new NAME [--tag TAG] [--repo OWNER/NAME] [--cores N] [--memory-mb N] [--disk-gb N] [--signins A,B] && ship-fleet login NAME claude` (the login only when Claude is one of its sign-ins; with the manual driver `new` only prints the steps, so that login fails until you have made the VM: sign in from the Ship's menu then). Tags: buttons for the fleet's, or type one. Sign-ins: FleetLedger's list (`signins --accounts --json`), all by default. |
| A Ship's **⋯** | Sign in: SERVICE | `ship-fleet login NAME SERVICE`, one per sign-in the Ship uses (from `signins NAME --offline --json`), only while it answers |
| | Update, Reboot | `ship-fleet update NAME`, `ship-fleet reboot NAME` |
| | Snapshots, Take a snapshot | `ship-fleet snapshot ls NAME`, `ship-fleet snapshot take NAME` |
| | Roll back to | `ship-fleet snapshot rollback NAME SNAPSHOT` (type the snapshot's name in the menu; `ship-fleet` asks for `yes`) |
| | Destroy | `ship-fleet destroy NAME` (`ship-fleet` asks for the Ship's name) |
| | Finish enrolling | `ship-fleet status NAME --wait`, for a Ship still provisioning or never enrolled |
| **FleetLedger** | Unlock | `ship-fleet unlock`, only while the vault is locked |
| | Self-update, Doctor | `ship-fleet self-update`, `ship-fleet doctor` |
| | Open the web UI | no ssh: `xdg-open` on the remote web UI's address from `ship-fleet about` (when the remote web UI is on) |

What is offered follows `contracts/ship-fleet.md`: a Ship's state decides its menu (a Ship that is
unreachable gets only what goes through the hypervisor; one still provisioning or never enrolled,
Finish enrolling and Destroy; sign-ins need a Ship that answers). While the vault is locked, on a
copy of FleetLedger on standby or retired, or with a FleetLedger older than these contracts, no
Ship action and no New Ship is offered, and the panel says why and what to do instead.

**After a job.** The panel watches Herdr's own list of terminals (no ssh). When a job's terminal
ends, it runs this computer's refresher if there is one (`~/.ssh/shipshape/refresh`, which keeps
`~/.ssh/shipshape` and Herdr's machines current; without it, which is normal, the panel says
nothing), then reads FleetLedger again as ever (offline). A new Ship that is ready gets a toast.

## Machines

The **Machines** button swaps the Ships for two parts.

**This computer.** What its refresher (`~/.ssh/shipshape/refresh`, which `ship-fleet trust`'s
one-line install leaves) last did, from `~/.ssh/shipshape/state.json`
(`contracts/refresh-state.schema.json`, checked field by field; one it does not match is said,
not shown): when its certificate ends, when it last refreshed and how many Ships it got, and the
last failure and warning. The name it is trusted under comes from `machine.conf`, and its row
below is marked. **Refresh now** runs the refresher, shows the first line of what it said on
stderr if it failed, and reads everything again. Without a refresher the panel says so, and how
to get one: Trust a machine for this computer from the computer that runs `ship-fleet`, then
paste here, in a terminal, the line it prints. The Fleet header shows the certificate's end and
the last refresh too.

**Trusted machines**, from `ship-fleet ca show --json`, read when the page opens, on **Reload**
and after a trust, untrust or allow-again job ends (never on a timer): one row per machine (the
untrusted last) with its scope, logins, certificate (`ok`, `expiring`, `lapsed`, `none`) and
when it ends, whether it may refresh (or was refused for being idle 30 days), whether its key is
`bundle only` (its line on FleetLedger's VM may fetch its own bundle and nothing else) or a
`full key` (a line of yours), and when it last asked for its bundle.

| Where | Action | Runs on FleetLedger |
|---|---|---|
| **Trust a machine** | Trust | `ship-fleet trust NAME --key 'ssh-ed25519 …' --scope SCOPE… --logins agent\|owner\|both`. On FleetLedger's own VM, `ship-fleet` prints the one line to paste on that machine in the terminal (elsewhere it writes the machine's bundle instead). |
| A machine's **⋯** | Allow again | `ship-fleet trust NAME --allow-again`, only while it is refused for being idle |
| | Untrust | `ship-fleet untrust NAME` (`ship-fleet` asks you to type the machine's name; it cannot be undone) |
| **Pair a phone's browser** | Pair | `ship-fleet ui link --name DEVICE --qr`: a one-time link and its QR code, in the terminal. Only while the remote web UI is on. |
| **Pairing by code** | Turn pairing on, off | `ship-fleet pair on`, `ship-fleet pair off` |
| A computer waiting | Approve… | `ship-fleet pair approve ID`: in the terminal, type the code that computer shows, then choose its name, scope and logins (defaults shown); it ends in `ship-fleet trust`. |
| | Refuse | `ship-fleet pair refuse ID` |

**Pairing by code** comes with the same read (`ca show --json`'s `pairing`): whether it is on,
and a row per computer waiting to be trusted, with the name it gives itself (shown as its own
claim), its key's fingerprint, the address it asked from, and when it lapses. Never its code:
that is typed in the approval's terminal, read off the new computer's screen, so a click here
approves nothing by itself. A FleetLedger from before pairing shows no such part.

What Trust takes, before anything is sent: a name that is a machine name and not one in the list
(names are never reused, untrusted ones included); the new machine's public key, exactly one
line of an ed25519 `.pub` file (`ssh-keygen -t ed25519 -f ~/.ssh/shipshape/id_ed25519` on that
machine), sent as its type and key alone, never its comment, and refused when it has a second
line, options before it, another key type, a certificate, a private key or a cut key; a scope
(`all` alone, or tags and Ships: `tag:work`, `ship:ship-03`); and its logins.

On a computer that only fetches its own bundle (no `"ssh"` in `settings.json`, or a key that may
only fetch this machine's bundle), the list cannot be read: the page shows this computer's
refresh alone and says why in one line.

## What it runs, and where

On this computer, as you, only while the Fleet tab is the one on show: when it opens, every five
minutes after, and on Refresh. Once more when a job's terminal ends, and on the Machines page
when it opens and on its buttons.

| Command | For |
|---|---|
| `ssh -o BatchMode=yes -o ConnectTimeout=8 [-i KEY -o IdentitiesOnly=yes] -- DEST 'ship-fleet' 'ls' '--json' '--offline'` | The Ships, as FleetLedger last fetched them (it asks no Ship, and answers while the vault is locked) |
| the same, `'attention' '--offline' '--json'` | Needs you: every Ship's items and the fleet's own (vault locked, a machine certificate or the Proxmox token about to lapse), ranked |
| the same, `'about' '--json'` | FleetLedger's version and its vault's state |
| `hc machine ls --json` | Herdr's connection to each Ship |
| the same ssh, `'signins' NAME '--offline' '--json'` | When a Ship's menu opens: the sign-ins it uses |
| the same ssh, `'signins' '--accounts' '--json'` | When New Ship opens: the sign-ins a Ship may use, and the fleet's tags |
| the same ssh, `'ca' 'show' '--json'` | When Machines opens, on its Reload, and after a job that trusted, untrusted or allowed a machine again, or turned pairing on or off, approved or refused a computer: the trusted machines and the computers waiting |
| `test -x ~/.ssh/shipshape/refresh`, then the refresher | When a job ends, if there is one; on Refresh now (the `test -x` also when Machines opens) |

It also reads `settings.json`, `fleet.json` (above) and the refresher's `state.json` and
`machine.conf` with every read. Every word sent to the FleetLedger computer
is quoted for its shell, a value with a line break is refused, and a Ship name outside
`[a-z0-9-]` is never sent. The documents it reads are `contracts/ship-fleet.md`'s: each one's
`schema` is checked, and one the panel does not know (a newer panel's, or a FleetLedger older than
the contract) is left out with a line saying so. When a command fails the panel shows the first
line of what it said on stderr.

## Code

`model.js` holds all of the logic and imports nothing; `main.js` only draws, and hands `model.js`
the Herdr functions it uses. The tests (`herdr/tests/fleet/`, run by `scripts/check`) load
`model.js` with a stand-in for Herdr. The drawing is not tested: only a running Herdr shows it.
