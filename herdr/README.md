# Herdr layouts

Layouts and mods for Herdr, the terminal app ShipShape's Ships run their agents in.

## Set up or update

```sh
# 1. On FleetLedger's VM:
ship-fleet self-update
# 2. On a computer that runs Herdr (later, to update: shipshape-setup):
ssh fleetledger@fleetledger.example.invalid ship-fleet machine-setup --installer | sh -s -- --herdr-client --fleetledger fleetledger@fleetledger.example.invalid
```

The second (from a checkout: `./shipshape-setup --herdr-client --fleetledger
fleetledger@HOST`) installs this computer's refresher, the Desk and the Fleet panel (from
FleetLedger, the version it runs), fills in the panel's
`settings.json` where you have not, has FleetLedger trust this computer if it does not yet
(shown first, one yes), and opens the Fleet tab. Later, `shipshape-setup` alone updates it
all. See [`../docs/runbooks/trust-machines.md`](../docs/runbooks/trust-machines.md), "Set up
or update", for what it checks and what it does without a key that logs in to the VM.
`install-desk.sh` and `install-fleet.sh` below still install from this checkout by hand.

## What is here

**The Desk** (`layouts/desk.json`): Herdr's default layout plus a panel that follows the focused
agent, which agents on any machine, Ships included, fill in themselves with the `desk` CLI. See
[`DESK.md`](DESK.md) for how it works; it installs with `herdr/install-desk.sh`, which installs
the Fleet panel too (the Desk's bar opens it). A Ship's tags come from `~/.ssh/shipshape/fleet.json`
on this computer, which self-refresh keeps current, else from a local `ship-fleet`; its tests are
in `tests/desk/`.

**The Fleet panel** (`mods/fleet/`): a Herdr tab with your Ships as FleetLedger last saw them,
read over ssh from the FleetLedger computer (`ship-fleet ls --json --offline`). What changes
anything (a new Ship, a Ship's settings, a sign-in, update, reboot, snapshots, destroy, unlock; trusting, untrusting
and pairing on its Machines page, which also shows this computer's own refresh) opens a terminal
tab running the exact `ship-fleet` command, which asks its own questions there. It works in any
layout. On Herdr 0.1.17 and later, a Ctrl/Cmd-click on an https link in one of those tabs can open
it in the browser profile you map to the Ship's tag (its **Browsers** view). See [`mods/fleet/README.md`](mods/fleet/README.md); it installs on its own
with `herdr/install-fleet.sh`, and its model's tests are in `tests/fleet/`.

## Licences

- `mods/desk-agents/` and `mods/desk-spaces/` are modified copies of Herdr's `agents` and `spaces`
  mods from Herdr 0.1.17, and `layouts/desk.json` of its `main` layout from Herdr 0.1.15 (the
  same in 0.1.17) (<https://github.com/herdrdev/herdr-app>, `defaults/`), under the Apache License 2.0
  ([`LICENSE-herdr.txt`](LICENSE-herdr.txt)). The mods' changes are the lines marked `Desk:`;
  the layout's are the Desk's own parts, described in [`DESK.md`](DESK.md).
- `mods/desk/icons/caution.svg` (and `mods/desk/brand/crew-caution.png`, drawn from it) is
  Lucide's `triangle-alert` icon, under the ISC licence, derived from Feather's under MIT
  ([`mods/desk/icons/LICENSE-Lucide.txt`](mods/desk/icons/LICENSE-Lucide.txt), as Herdr ships it).
- `mods/desk/fonts/Urbanist-*.ttf` is Urbanist, under the SIL Open Font License 1.1
  ([`mods/desk/fonts/Urbanist-OFL.txt`](mods/desk/fonts/Urbanist-OFL.txt)).
