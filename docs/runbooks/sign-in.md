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
itself. They may start before you have signed in to gcloud or GitHub, and find
those missing. So after each later sign-in that checks out, FleetLedger has the
Ship tell its autostart agents, and only those, with one line:

```
ShipShape: GitHub is now signed in as example-user. Re-check it if you need it.
```

`ship-fleet login` lists who was told, and any agent that was not (not running,
or waiting at a permission prompt, which is never typed into). The Ship records
each notice in its journal: `ship-fleet logs SHIP system`.

## Remote Control

Remote Control is on by default: once Claude is signed in, every agent's session
shows in your Claude app. Turn it off or on per Ship:

```sh
ship-fleet set ship-01 remote_control=off    # or remote_control=on
```

- "off" also stops resumed sessions from reconnecting (`disableRemoteControl`).
  `blocked` is an older name for the same setting.
- A change reaches running sessions only when they restart: a reboot restarts
  them all (`ssh` in and `sudo systemctl reboot`, never a forced reset).
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
  the provider file (`fleetledger/fleetledger/providers/<service>.toml`) can be
  updated.
- **"The sign-in code expired" / "window closed":** run the login again and paste
  sooner.
- **"refused FleetLedger's admin key":** `ssh-add ~/.ssh/shipshape_fleet_ed25519`.
- **Signed in, but the check says not signed in:** look at
  `ship-fleet status ship-01`; for gcloud both `gcloud` and `gcloud_adc` must be
  OK.

Every login and logout is recorded in `~/.local/share/fleetledger/audit.log`:
time, Ship, service, outcome and the account, nothing else.
