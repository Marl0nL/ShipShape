---
name: ship
description: How the ShipShape Ship you are running on works - its name and tags, who manages it, which sign-ins and tools it has, what agents may change, and where its logs are. Use when you need to know about this machine, its other agents, its sign-ins, or before changing anything outside your work folder.
---

# The Ship you are on

You are running on a **Ship**: an Ubuntu VM made for coding agents and managed from
outside by **FleetLedger** (`ship-fleet`), which a person runs on their own computer.

Start with `ship-axi`. It shows this Ship's name and tags, your own herdr agent name
and permission mode, the other agents and their states, Remote Control, the
sign-ins (account names only) and the installed tools. `ship-axi rules` lists what
agents may and may not change.

## How the Ship is run

- You run as the `agent` user inside **herdr**, a terminal multiplexer that keeps
  every agent in its own pane. herdr starts at boot; after a reboot it restores the
  panes and resumes each Claude conversation, in the permission mode it was in.
- Settings come from `/etc/shipshape/ship.toml` (read-only for you). FleetLedger
  changes them with `ship-ctl`; Claude Code's managed settings are derived from it.
- Sign-ins (Claude Code, gcloud, GitHub) belong to the Ship and are shared by every
  agent on it. A person renews them through FleetLedger. If one is missing or
  expired, say so; do not try to sign in or out yourself.
- Tools come from a catalogue: `gh-axi`, `quota-axi`, `chrome-devtools-axi` and
  `ship-axi` on every Ship, plus whatever this Ship lists. You can install more for
  yourself with npm (they go to `~/.local`) or uv.

## Rules

- Work in your own folder under `~/work/`.
- Do not edit `/etc/shipshape/`, `/etc/claude-code/` or `~/.claude/settings.json`.
- Do not stop or restart the herdr server, and do not replace `~/.local/bin/claude`
  or `herdr`.
- There is no network allow-list: the internet is open. The Ship records which
  programs connect where, for the person to review, and if its LAN guard is on,
  new connections to the local network (other than its gateway, DNS and any
  addresses the person has allowed) are refused. Neither is permission to probe the local network.
- Ship logs are read from FleetLedger (`ship-fleet logs <ship> SOURCE`), not here;
  `ship-axi rules` says the same.

## Not available yet

`ship-axi request` (ask FleetLedger for a sign-in or a tool) and `ship-axi message`
(a note to another Ship) are reserved. Until they exist, tell the person you are
working with what you need.
