# The Fleet panel

A Herdr tab with your Ships as FleetLedger last saw them: one row per Ship (its name, tags, state,
first attention item, and Herdr's own connection to it), and above them where the list came from,
FleetLedger's version, whether its vault is locked, and when the list was read. Click a row to go
to that Ship's space in Herdr.

What changes anything runs in a terminal tab, never in the panel: **New Ship**, a Ship's **⋯**
menu and the **FleetLedger** menu each show the exact command, and clicking opens one terminal
tab on this computer that runs it, where `ship-fleet` asks its own questions (see Actions). Trusting
machines and pairing a phone's browser are not here yet.

## Install

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

Say where FleetLedger is in `~/.config/herdr-app/mods/fleet/settings.json` (under
`$HERDR_APP_HOME` when that is set; `settings.example.json` is beside it):

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
tags) when it has no list from FleetLedger. The file is not there today, which is normal. It never
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
`--stdin`), asks no question of its own, never types into a terminal, and takes no secret: the
only things typed into it are a new Ship's name, tag, repository and size, and a snapshot's name,
each checked against the pattern `ship-fleet` enforces and refused, not sent, when it does not
match.

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
`~/.ssh/shipshape` and Herdr's machines current; it is not there today, which is normal and
silent), then reads FleetLedger again as ever (offline). A new Ship that is ready gets a toast.

## What it runs, and where

On this computer, as you, only while the Fleet tab is the one on show: when it opens, every five
minutes after, and on Refresh. Once more when a job's terminal ends.

| Command | For |
|---|---|
| `ssh -o BatchMode=yes -o ConnectTimeout=8 [-i KEY -o IdentitiesOnly=yes] -- DEST 'ship-fleet' 'ls' '--json' '--offline'` | The Ships, as FleetLedger last fetched them (it asks no Ship, and answers while the vault is locked) |
| the same, `'attention' '--offline' '--json'` | The fleet's own items: vault locked, a machine certificate or the Proxmox token about to lapse |
| the same, `'about' '--json'` | FleetLedger's version and its vault's state |
| `hc machine ls --json` | Herdr's connection to each Ship |
| the same ssh, `'signins' NAME '--offline' '--json'` | When a Ship's menu opens: the sign-ins it uses |
| the same ssh, `'signins' '--accounts' '--json'` | When New Ship opens: the sign-ins a Ship may use, and the fleet's tags |
| `test -x ~/.ssh/shipshape/refresh`, then the refresher | When a job ends, if there is one |

It also reads `settings.json` and `fleet.json` (above). Every word sent to the FleetLedger computer
is quoted for its shell, a value with a line break is refused, and a Ship name outside
`[a-z0-9-]` is never sent. The documents it reads are `contracts/ship-fleet.md`'s: each one's
`schema` is checked, and one the panel does not know (a newer panel's, or a FleetLedger older than
the contract) is left out with a line saying so. When a command fails the panel shows the first
line of what it said on stderr.

## Code

`model.js` holds all of the logic and imports nothing; `main.js` only draws, and hands `model.js`
the Herdr functions it uses. The tests (`herdr/tests/fleet/`, run by `scripts/check`) load
`model.js` with a stand-in for Herdr. The drawing is not tested: only a running Herdr shows it.
