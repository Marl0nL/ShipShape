# The Fleet panel

A Herdr tab with your Ships as FleetLedger last saw them: one row per Ship (its name, tags, state,
first attention item, and Herdr's own connection to it), and above them where the list came from,
FleetLedger's version, whether its vault is locked, and when the list was read. Click a row to go
to that Ship's space in Herdr.

It only reads. It has no button that changes anything (no New Ship, sign-in, update, destroy,
unlock or trust); those come later, and they will run in terminal tabs, where `ship-fleet` asks
for its own confirmations.

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

## What it runs, and where

On this computer, as you, only while the Fleet tab is the one on show: when it opens, every five
minutes after, and on Refresh.

| Command | For |
|---|---|
| `ssh -o BatchMode=yes -o ConnectTimeout=8 [-i KEY -o IdentitiesOnly=yes] -- DEST 'ship-fleet' 'ls' '--json' '--offline'` | The Ships, as FleetLedger last fetched them (it asks no Ship, and answers while the vault is locked) |
| the same, `'attention' '--offline' '--json'` | The fleet's own items: vault locked, a machine certificate or the Proxmox token about to lapse |
| the same, `'about' '--json'` | FleetLedger's version and its vault's state |
| `hc machine ls --json` | Herdr's connection to each Ship |

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
