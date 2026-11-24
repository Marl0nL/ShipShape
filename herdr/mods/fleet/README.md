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
| `browsers` | Optional: which browser and profile a link in a Ship's job tab opens in, by the Ship's tag (see Links in job tabs). The panel's **Browsers** view edits it. |

Nothing else goes in the file, and nothing secret: no password or passphrase is ever typed into
the panel, and no command line (`browsers` names a browser from the panel's own list). Saving the
file reloads the panel. `shipshape-setup` leaves a file with `browsers` in it as it is, so let it
fill in `ssh` first (or set it by hand).

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
**Destroy**, `yes` before **Roll back**, **Stop**, **Sign out**, a fresh restart, a resize that
restarts the VM, SSH on the overlay only and a sign-in turned off (`contracts/ship-fleet.md`,
"Commands that ask first"), the vault's passphrase for **Unlock**, a sign-in's link and code. The
panel never passes a flag that skips one or overrides a check (`--yes`, `--force`,
`--revoke-my-key`, `--stdin`, `--code`, nor any of them cut short), never resizes record-only,
asks no question of its own, never types into a terminal, and takes no secret: the only things
typed into it are a new Ship's name, tags, repository and size (and under Advanced its first
agent's name and folder, host names, VM id and time zone), in a Ship's Settings its session
prefix, LAN guard addresses, reboot time, time zone, wake delay and message and new tags, in its
Agents and size an autostart agent's name and folder and a new size, a snapshot's name, and on
the Machines page a machine's name, public key and scope and a device's name, each checked
against the pattern `ship-fleet` enforces and refused, not sent, when it does not match. (In
Browsers, a tag and a browser profile's name are typed too: they go to `settings.json` only, never
to `ship-fleet`.)

| Where | Action | Runs on FleetLedger |
|---|---|---|
| **New Ship** | Create | `ship-fleet new NAME [--tag TAG]… [--repo OWNER/NAME] [--cores N] [--memory-mb N] [--disk-gb N] [--signins A,B] [--autostart AGENT:FOLDER[:MODE]] [--github-scopes A,B] [--no-overlay] [--vmid N] [--host HOST]… [--mode MODE] [--allow-bypass] [--agent-sudo] [--timezone ZONE] [--driver DRIVER] [--image IMAGE] [--no-default-tools] [--tools A,B] [--profile P]… && ship-fleet login NAME claude` (the login only when Claude is one of its sign-ins; with the manual driver `new` only prints the steps, so that login fails until you have made the VM: sign in from the Ship's menu then). Tags: buttons for the fleet's, or type some (spaces or commas between). Sign-ins: FleetLedger's list (`signins --accounts --json`), all by default. A first agent, `main` in `~/work`, as the web form's (with a repository it works in its clone). Size: blank is FleetLedger's default, which the hint shows once `defaults --json` is read. |
| | Advanced | Closed until you open it, and what it holds is in the line shown either way: the first agent's name, folder and mode, or none (then no repository); extra GitHub scopes (`workflow`, `project`, `write:packages`; none: gh's minimum); not joining the overlay; host names to reach it by; a Proxmox VM id; the default permission mode, allowing `bypassPermissions` (a first agent in that mode needs it), sudo for the agent user; a time zone (blank: FleetLedger's computer's, named once read; `new` refuses one its tzdata lacks). Never the first prompt: `new` treats it as possibly secret, so it is typed only in a terminal (`--autostart-prompt`). From FleetLedger's lists, as buttons, never typed: the driver (those set up there) and the base image (those the driver can start from; blank: the pinned one, and the form says when that one is not on the driver chosen), the tools (ticked as FleetLedger's defaults are; adding one sends `--tools`, leaving a default out `--no-default-tools --tools` with every one ticked) and the profiles (they replace the default ones). A toolkit's tool or profile is refused, as `new` refuses it, on a Ship with none of its toolkit's tags. When a list cannot be read (an older FleetLedger, say), its option stays at FleetLedger's default and the form says why. |
| A Ship's **⋯** | Settings… | Opens the Ship's Settings (below), only while ship-fleet reaches it over ssh (the states that offer Update) |
| | Agents and size… | Opens the Ship's Agents and size (below), in the states that go through the hypervisor (its agents only while ship-fleet reaches it over ssh) |
| | Sign in: SERVICE | `ship-fleet login NAME SERVICE`, one per sign-in the Ship uses (from `signins NAME --offline --json`), only while it answers |
| | Sign out: SERVICE | `ship-fleet logout NAME SERVICE` (`ship-fleet` asks for `yes`), one per sign-in it is signed in to (the rows the web UI offers Sign out on), only while it answers |
| | Update, Reboot | `ship-fleet update NAME`, `ship-fleet reboot NAME` |
| | Start, Stop | `ship-fleet start NAME`, `ship-fleet stop NAME` (`ship-fleet` asks for `yes` before a stop), through the hypervisor, so also while it does not answer |
| | Snapshots, Take a snapshot | `ship-fleet snapshot ls NAME`, `ship-fleet snapshot take NAME` |
| | Roll back to | `ship-fleet snapshot rollback NAME SNAPSHOT` (type the snapshot's name in the menu; `ship-fleet` asks for `yes`) |
| | Destroy | `ship-fleet destroy NAME` (`ship-fleet` asks for the Ship's name) |
| | Finish enrolling | `ship-fleet status NAME --wait`, for a Ship still provisioning or never enrolled |
| **FleetLedger** | Unlock | `ship-fleet unlock`, only while the vault is locked |
| | Self-update, Doctor | `ship-fleet self-update`, `ship-fleet doctor` |
| | Open the web UI | no ssh: `xdg-open` on the remote web UI's address from `ship-fleet about` (when the remote web UI is on) |

**A Ship's Settings** are what the web UI's Settings tab changes, grouped as it groups them, each
showing the value the Ship last reported to FleetLedger (its status `settings`, in the offline
list the panel already reads; no read of its own). A Ship whose tooling does not report them shows
"(unchanged)" (with what its status says elsewhere, where it says it), and changes only what you
choose. Choices are buttons; a typed value is checked as `ship-fleet set` checks it, and refused,
not sent, when it does not match. Apply opens one terminal tab with only what changed, each part
only when it changes something:

| Part | Runs on FleetLedger |
|---|---|
| Claude Code, Agent, Network, Updates, After a reboot | `ship-fleet set NAME KEY=VALUE…`: `remote_control`, `default_mode`, `allow_bypass`, `update_channel`, `session_prefix`, `sudo`, `lan_guard`, `lan_guard_allow`, `auto_reboot`, `reboot_time`, `timezone`, `wake_after_boot`, `wake_delay_minutes`, `wake_text` (`bypassPermissions` as the default mode with it disallowed is refused) |
| GitHub scopes | `github_scopes=A,B` (or `none`) in the same `set` line: the extra scopes its next GitHub sign-in asks for. A scope its ship.toml chose that this FleetLedger does not offer is said, and changing the ticks drops it. |
| Tags | `&& ship-fleet tag NAME +A -B`: the fleet's tags as buttons, new ones typed |
| SSH listens | `ssh_listen=any` in the same `set` line; `ssh_listen=overlay` in a `set` of its own, last, after every other part has run. On the overlay only, it refuses SSH from the LAN, so this computer reaches it only over the overlay: `ship-fleet` asks for `yes` first, and the Ship refuses it until it has joined the overlay. |
| Sign-ins it uses | `&& ship-fleet signins NAME --on A --off B`: a sign-in that is off can be turned on, and one in use turned off, which signs the Ship out of it (`ship-fleet` asks for `yes` first). From `signins NAME --offline --json` and `signins --accounts --json`, read when the view opens if the menu has not read them. |

When `ship-fleet` is answered anything but `yes`, that part changes nothing and the parts after it
do not run. Its tools are not changed in the panel.

**A Ship's Agents and size** are what the web UI changes about its agents and its VM, from what
the panel already reads (the Ship's last status in the offline list: herdr's agents, its agent
record, its autostart list and the size it sees; no read of its own). Each action is its own
button, with its line shown under it, and opens one terminal tab:

| Part | Runs on FleetLedger |
|---|---|
| Restart an agent | `ship-fleet agent restart NAME AGENT [--mode MODE] [--fresh]`: one of the Claude agents herdr runs or its record keeps, in the mode it starts in (its autostart mode, else the default) or a chosen one, continuing its conversation or a new one (`--fresh`: `ship-fleet` asks for `yes`). A working agent is left alone by `ship-fleet`: the panel never interrupts a turn. `bypassPermissions` while the Ship disallows it is refused. |
| Remote Control per agent | `ship-fleet agent remote-control NAME AGENT [--off]`, for each agent its record keeps and says this of, from its next start by the Ship; while the Ship's own Remote Control is not `on`, it says so |
| Started at every boot | `ship-fleet set NAME autostart.add=AGENT:FOLDER[:MODE]` (a name and a folder typed, the mode a button; a name it has already, or a folder with `:`, is refused) and `ship-fleet set NAME autostart.remove=AGENT` (a running one keeps running) |
| Size | `ship-fleet resize NAME [--cores N] [--memory-mb N] [--disk-gb N] [--restart]`: blank is unchanged; a disk only grows. Without `--restart` vCPUs and memory apply at its next restart through the hypervisor; with it (`ship-fleet` asks for `yes`) its VM restarts now. Never `--record-only`. |

On tooling that does not report its agent record or autostart list, those parts say so and offer
nothing.

What is offered follows `contracts/ship-fleet.md`: a Ship's state decides its menu (a Ship that is
unreachable gets only what goes through the hypervisor; one still provisioning or never enrolled,
Finish enrolling and Destroy; sign-ins need a Ship that answers). While the vault is locked, on a
copy of FleetLedger on standby or retired, or with a FleetLedger older than these contracts, no
Ship action and no New Ship is offered, and the panel says why and what to do instead.

**After a job.** The panel watches Herdr's own list of terminals (no ssh). When a job's terminal
ends, it runs this computer's refresher if there is one (`~/.ssh/shipshape/refresh`, which keeps
`~/.ssh/shipshape` and Herdr's machines current; without it, which is normal, the panel says
nothing), then reads FleetLedger again as ever (offline). A new Ship that is ready gets a toast.

## Links in job tabs

On Herdr 0.1.17 and later, a Ctrl-click (Cmd-click on macOS) on an `https://` link in a terminal
tab the panel opened for a Ship, such as the sign-in link `ship-fleet login` shows, opens it in the
browser and profile you chose for one of that Ship's tags: a `work` Ship's sign-ins in your work
profile, a `personal` one's in yours. The panel's `link.js` is the handler (`mod.json`
declares it for `https://` links, and the Desk layout lists it in `link_handlers`, so an update of
the Desk keeps it; Herdr adds it to a layout of your own the first time it sees it). Older Herdr
ignores both, and the rest of the panel works as before.

**Browsers** (beside FleetLedger) shows the mappings and edits them: choose a tag (or type one), a
browser and the profile, Add, and Save, which writes `browsers` in `settings.json` (every other key
as it was). Up moves a mapping before the one above it. By hand:

```json
{
  "ssh": "fleetledger@fleetledger.example.invalid",
  "identity_file": null,
  "browsers": [
    { "tag": "work", "browser": "chrome", "profile": "Profile 1" },
    { "tag": "personal", "browser": "firefox", "profile": "personal" }
  ]
}
```

A Ship with several mapped tags opens links in the first mapping in this list whose tag it has.
`profile` is a Chromium browser's profile folder (`Default`, `Profile 1`, …: the end of Profile Path
on `chrome://version`; a name it does not have starts a new, empty profile) or a Firefox profile's
name (`about:profiles`; one it does not have opens Firefox's Profile Manager): letters, digits, spaces, `.`, `_` and `-`, from a letter or digit. An entry
that is not one of these, has another key, or repeats a tag is left out, and Browsers says so.

| `browser` | Linux (the first on `PATH`) | macOS |
|---|---|---|
| `chrome` | `google-chrome`, `google-chrome-stable` | Google Chrome |
| `chromium` | `chromium`, `chromium-browser` | Chromium |
| `edge` | `microsoft-edge`, `microsoft-edge-stable` | Microsoft Edge |
| `brave` | `brave-browser`, `brave` | Brave Browser |
| `vivaldi` | `vivaldi`, `vivaldi-stable` | Vivaldi |
| `firefox` | `firefox` | Firefox |

What runs, with no shell, the profile and the URL each one argument: on macOS `open -n -a APP
--args --profile-directory=PROFILE URL` (Firefox: `-P PROFILE -new-tab URL`); on Linux `setsid -f
BROWSER …` with the same arguments, after `which setsid NAME…` finds both (setsid lets a browser it
starts keep running on its own). Not covered, so Herdr opens the link as usual: Windows, Safari,
a browser not on `PATH` (a Flatpak, say) or under another name.

The handler takes a click only when all of these hold, and otherwise passes it to Herdr, which
opens it as it always does:

- the link starts with `https://`, has only the characters a URL may have, and a plain host (no
  user name before an `@`; no IPv6 address);
- the terminal is on this computer, still running, and the panel's own job tab for a Ship:
  the panel publishes its running jobs (`fleet.links`, a Herdr shared value) and the terminal must
  run that job's exact command and not have been there before the job started (once the panel has
  found the job's tab, only that one);
- the Ship is in FleetLedger's list (or is the new Ship that tab is creating, with the tags typed
  for it, else FleetLedger's default tags), and one of its tags is mapped;
- the browser is installed: on macOS `open` says when it is not; on Linux `which` must find it.

Saving `settings.json` reloads the panel, which then forgets the job tabs already open: their
links open as usual.

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
| the same ssh, `'signins' '--accounts' '--json'` | When New Ship opens: the sign-ins a Ship may use, and the fleet's tags; when a Ship's Settings open, if it has not been read |
| the same ssh, `'tools' 'ls' '--json'`, `'image' 'ls' '--json'` and `'defaults' '--json'` | When New Ship opens, at once with the one above: the tools and profiles, the drivers and the images each can start from, and what `new` uses for an option left blank (`contracts/ship-fleet.md`) |
| the same ssh, `'ca' 'show' '--json'` | When Machines opens, on its Reload, and after a job that trusted, untrusted or allowed a machine again, or turned pairing on or off, approved or refused a computer: the trusted machines and the computers waiting |
| `test -x ~/.ssh/shipshape/refresh`, then the refresher | When a job ends, if there is one; on Refresh now (the `test -x` also when Machines opens) |

It also reads `settings.json`, `fleet.json` (above) and the refresher's `state.json` and
`machine.conf` with every read. Every word sent to the FleetLedger computer
is quoted for its shell, a value with a line break is refused, and a Ship name outside
`[a-z0-9-]` is never sent. The documents it reads are `contracts/ship-fleet.md`'s: each one's
`schema` is checked, and one the panel does not know (a newer panel's, or a FleetLedger older than
the contract) is left out with a line saying so. When a command fails the panel shows the first
line of what it said on stderr.

After each read it publishes shared `fleet.summary` for the Desk's Dashboard: when it read, how
many items need you, and each Ship's state with the agents its last status document lists
(`ship-status.schema.json`, `herdr.agents`: name, state, and while blocked what it waits for).
The Dashboard only reads that, so it is as fresh as this panel's last read, and the Dashboard
asks FleetLedger nothing.

## Code

`model.js` holds the logic and imports nothing (the Desk links to it); `settings.js`, a Ship's
Settings, and `agents.js`, its Agents and size, import only `model.js`; `main.js` only draws,
and hands `model.js` the Herdr functions it uses. The tests (`herdr/tests/fleet/`, run by
`scripts/check`) load `model.js`, `settings.js` and `agents.js` with a stand-in for Herdr. The drawing is not tested: only a running Herdr shows it.
