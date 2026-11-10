# Runbook: sign a Ship in

Each Ship signs in to Claude Code, gcloud (with Application Default
Credentials) and GitHub as itself. The tokens are created on the Ship and stay
there; FleetLedger only relays the one-time code. The account each Ship must use
comes from its tags (`[tag.<tag>]` in `ships.toml`).

## Sign in

```sh
ship-fleet login ship-01 all        # claude, then gcloud (+ ADC), then github
ship-fleet login ship-01 claude     # or one service
ship-fleet login --tag work claude  # every work Ship, one after the other
```

Or in the browser: `ship-fleet ui`, then **Sign in** on a Ship's page, in
Needs attention, or for a whole tag on the Sign-ins page
([web-ui.md](../web-ui.md#the-concierge-in-the-browser)). The rules below are
the same.

To see what needs signing in across the whole fleet, most urgent first (waiting,
expired, wrong account, missing while something needs it, then expiring or
unknown, then fine):

```sh
ship-fleet signins                  # every sign-in on every Ship, with its fix
ship-fleet signins --group action   # only what needs you
```

The web UI's Sign-ins page shows the same list
([web-ui.md](../web-ui.md#every-sign-in-in-one-list)).

For each service:

| Service | You do |
|---|---|
| Claude Code | open the link, approve with the tag's Claude account, copy the code the page shows, paste it at the prompt |
| gcloud (and ADC) | open the link, approve with the tag's Google account, paste the code; one sign-in fills both |
| gcloud-adc (alone) | the same, for Application Default Credentials only |
| GitHub | open github.com/login/device, type the code FleetLedger shows, approve; FleetLedger waits |

The pasted code is read without echo and typed into the Ship's terminal: it never
appears on a command line, in a log or in shell history. Each link stays valid
for a few minutes (FleetLedger says how long); GitHub's code for 15.

Afterwards FleetLedger checks who is signed in:

```
ship-01 claude: signed in (claude work@example.com).
ship-01 gcloud: signed in (gcloud work@example.com).
ship-01 github: signed in (github example-user).
```

A mismatch with the tag is reported straight away:

```
ship-01 claude: WRONG ACCOUNT: claude is you@example.com, the Ship's tags expect work@example.com.
  Sign it out with: ship-fleet logout ship-01 claude
```

`ship-fleet ls` shows the same verdict on every poll ("Wrong account" in the
sign-in columns).

Within a minute of Claude signing in, the Ship starts its autostart agents by
itself (one that works in a private GitHub repo waits for the GitHub sign-in). They may start before you have signed in to gcloud or GitHub, and find
those missing. So after each later sign-in that checks out, FleetLedger has the
Ship tell its autostart agents, and only those, with one line:

```
ShipShape: GitHub is now signed in as example-user. Re-check it if you need it.
```

`ship-fleet login` lists who was told, and any agent that was not (not running,
or waiting at a permission prompt, which is never typed into). The Ship records
each notice in its journal: `ship-fleet logs SHIP system`.

## Extra GitHub scopes

gh signs in with its own minimum scopes (`repo`, `read:org`, `gist`). Some agent work
needs more, chosen per Ship (none by default):

| Scope | Allows | Why you might want it |
|---|---|---|
| `workflow` | changing GitHub Actions workflows | agents that push commits touching `.github/workflows/` (GitHub refuses those pushes without it). Workflows run with the repository's secrets: tick it only where agents maintain CI |
| `project` | reading and changing GitHub Projects (boards) | `gh project`, agents that move their own cards |
| `write:packages` | publishing and reading GitHub Packages | agents that build and push images to ghcr.io |

Scopes that delete or administer (`delete_repo`, `admin:org`, ...) are not offered: an
agent holding them can do lasting damage. Add one by hand on the Ship if you must;
FleetLedger leaves scopes outside this list alone.

```sh
ship-fleet new ship-02 --github-scopes workflow       # a new Ship
ship-fleet set ship-01 github_scopes=workflow,project # change the choice (empty: none)
ship-fleet login ship-01 github --scopes workflow     # choose, then sign in with them
```

Or tick them on the New Ship form (and setup's first Ship), or in a Ship's
**Settings** under **GitHub scopes**.

Changing the choice never changes the sign-in on the Ship. Until you sign in again,
its status says so (`signin-scopes-differ`), and the Sign-ins page lists the row
under **Needs you**:

- The token **lacks** a chosen scope: **Sign in again with these scopes** adds it.
- The token **has** a catalogue scope that is not chosen, most likely one you added
  by hand before this choice existed (say `gh auth refresh -s workflow` over SSH):
  **Keep workflow: tick it in Settings** comes first (`ship-fleet set SHIP
  github_scopes=workflow`, which the fix column says). Signing in again would drop
  it, and its button says so (**Sign in again and drop workflow**).

A scope the Ship reports as chosen that this FleetLedger does not know (chosen by a
newer FleetLedger, or `ship.toml` edited by hand) is shown on its Settings, never
asked for, and dropped from the choice if you save the tickboxes there. Signing in to GitHub, from anywhere, asks for exactly the
chosen scopes: on a Ship not signed in, `gh auth login --scopes ...`; on one signed
in with other scopes, `gh auth refresh --scopes <added> --remove-scopes <dropped>`,
in the same device flow, for the same account (GitHub shows the scopes to approve).

## Remote Control

Remote Control is on by default: once Claude is signed in, every agent's session
shows in your Claude app. Turn it off or on per Ship:

```sh
ship-fleet set ship-01 remote_control=off    # or remote_control=on
```

- "off" also stops resumed sessions from reconnecting (`disableRemoteControl`).
  `blocked` is an older name for the same setting.
- A change reaches running sessions only when they restart: a reboot restarts
  them all (`ship-fleet reboot ship-01`, never a forced reset).
- The Claude app can take a few minutes to show or drop sessions.
- Sessions are named `<session prefix>-<agent name>` (the prefix defaults to the
  Ship's name), from `claude --name` when the agent starts. Sessions you start
  some other way can be named with `/rename`; the prefix setting is only a
  fallback for unnamed sessions.

## Sign out

```sh
ship-fleet logout ship-01 github
ship-fleet logout ship-01 all
```

## If a sign-in does not go through

- **"No sign-in link appeared" / "The prompt for the code did not appear":** the
  tool changed its wording. Answer `y` to take over the Ship's terminal and
  finish by hand; FleetLedger still checks the account afterwards. Report it so
  the sign-in file (`ship/signins/<service>.toml`) can be
  updated.
- **"The sign-in code expired" / "window closed":** run the login again and paste
  sooner.
- **"refused FleetLedger's admin key":** see
  [create-ship.md](create-ship.md#if-something-goes-wrong).
- **Signed in, but the check says not signed in:** look at
  `ship-fleet status ship-01`; for gcloud both `gcloud` and `gcloud_adc` must be
  OK.

Every login and logout is recorded in `~/.local/share/fleetledger/audit.log`:
time, Ship, service, outcome and the account, nothing else.
